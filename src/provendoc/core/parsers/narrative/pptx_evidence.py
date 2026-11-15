"""Evidence-only extraction of PPTX package/security/character/watermark
facts (mirrors the PDF evidence layer's security, text-character and
watermark channels at deck scope).

Everything is evidence: macro parts are never executed, external targets
are never fetched, encrypted containers are never decrypted, hidden
slides stay in the narrative (extraction is not rendering), and
watermark glyphs are never removed from the extracted text.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from provendoc.core.parsers.narrative.shared import CFB_MAGIC

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    # r25 (6): embeddedFont slot r:id attribute (officeDocument relationship family)
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_ALTERNATE_TAG = f"{{{_MC_NS}}}AlternateContent"
_CHOICE_TAG = f"{{{_MC_NS}}}Choice"
_FALLBACK_TAG = f"{{{_MC_NS}}}Fallback"

_DC_NS = "http://purl.org/dc/elements/1.1/"
_CP_NS = (
    "http://schemas.openxmlformats.org/package/2006/"
    "metadata/core-properties"
)
_DCTERMS_NS = "http://purl.org/dc/terms/"
_EXT_PROPS_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/"
    "extended-properties"
)

# docProps/core.xml direct-child fields (verbatim facts, not normalized)
_CORE_FIELDS = {
    "title": f"{{{_DC_NS}}}title",
    "creator": f"{{{_DC_NS}}}creator",
    "last_modified_by": f"{{{_CP_NS}}}lastModifiedBy",
    "created": f"{{{_DCTERMS_NS}}}created",
    "modified": f"{{{_DCTERMS_NS}}}modified",
}

# docProps/app.xml direct-child fields
_APP_FIELDS = {
    "application": f"{{{_EXT_PROPS_NS}}}Application",
    "company": f"{{{_EXT_PROPS_NS}}}Company",
    # r25 (8): TotalTime total editing minutes (verbatim string; same key-name
    # precedent as docx total_editing_minutes)
    "total_editing_minutes": f"{{{_EXT_PROPS_NS}}}TotalTime",
    # r26: app.xml declaration-chain completion (probes evidenced the whole chain below as unregistered);
    # verbatim strings copied as-is (no casting, no interpretation); written only when non-empty.
    "slides": f"{{{_EXT_PROPS_NS}}}Slides",
    "notes": f"{{{_EXT_PROPS_NS}}}Notes",
    "hidden_slides": f"{{{_EXT_PROPS_NS}}}HiddenSlides",
    "words": f"{{{_EXT_PROPS_NS}}}Words",
    "mmclips": f"{{{_EXT_PROPS_NS}}}MMClips",
    "hyperlinks_changed": f"{{{_EXT_PROPS_NS}}}HyperlinksChanged",
}

# rels of any part: <base>/_rels/<part>.rels (root-level _rels/.rels also matches)
_RELS_PART_RE = re.compile(r"^(?:(?P<base>.*/))?_rels/[^/]*\.rels$")

# content type → package variant (check order: specific first, prevents prefix mismatches)
_CONTENT_TYPE_VARIANTS = (
    ("slideshow.macroEnabled.main+xml", "ppsm"),
    ("slideshow.main+xml", "ppsx"),
    ("presentation.macroEnabled.main+xml", "pptm"),
    ("presentation.main+xml", "pptx"),
)

_SLIDE_NAME_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_NOTES_NAME_RE = re.compile(r"^ppt/notesSlides/notesSlide(\d+)\.xml$")
_SLIDE_RELS_RE = re.compile(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$")
_NOTES_RELS_RE = re.compile(
    r"^ppt/notesSlides/_rels/notesSlide(\d+)\.xml\.rels$"
)
_PRESENTATION_PART = "ppt/presentation.xml"
_PRESENTATION_RELS_PART = "ppt/_rels/presentation.xml.rels"

    # r25 (6): embeddedFont slot labels + pathological-file sample cap
_FONT_SLOT_TAGS = ("regular", "bold", "italic", "boldItalic")
_MAX_EMBEDDED_FONT_SAMPLES = 20

# Pathological-deck caps (same spirit as the PDF evidence layer).
_MAX_SLIDES_SCANNED = 200
_MAX_CANDIDATES = 10
_MAX_SAMPLE_SLIDES = 20
_MAX_WATERMARK_TEXT_CHARS = 80
_MAX_EXTERNAL_SAMPLES = 50
_MAX_DANGLING_SAMPLES = 50
_MAX_COMMENT_SAMPLES = 200

# Watermark features (identical semantics to the PDF channel):
_BODY_SIZE_RATIO = 1.6
_PAGE_COVERAGE_RATIO = 0.5

# DrawingML angle units: 60000ths of a degree.  A rotation that is a
# multiple of 90° (within this tolerance) is axis-aligned; anything
# else is diagonal rendering — the classic watermark shape.
_ANGLE_UNIT = 60000
_AXIS_TOLERANCE = 2 * _ANGLE_UNIT


def slide_rels(
    zf: zipfile.ZipFile, names: list[str], slide_number: int
) -> dict[str, str]:
    """slide → {rId: target-part} map (package rels contract).

    target is a path relative to the slide part (e.g. ../charts/chart1.xml),
    normalized to the absolute in-zip path; parse failure returns an empty dict (tolerant).
    Shared by pptx_parser and pptx_image_assets (single source of truth).
    """
    return _rels_targets(
        zf, f"ppt/slides/_rels/slide{slide_number}.xml.rels", "ppt/slides",
    )


def _resolve_part(target: str, base: str) -> str:
    """package-relative target → absolute in-zip part path."""
    if target.startswith("/"):
        return target.lstrip("/")
    parts = base.split("/") + target.split("/")
    resolved: list[str] = []
    for seg in parts:
        if seg == "." or seg == "":
            continue
        if seg == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(seg)
    return "/".join(resolved)


def _rels_targets(
    zf: zipfile.ZipFile, rels_name: str, base: str
) -> dict[str, str]:
    """rels part → {rId: target-part} (parse failure returns an empty dict)."""
    if rels_name not in zf.namelist():
        return {}
    try:
        root = ElementTree.fromstring(zf.read(rels_name))
    except (KeyError, ElementTree.ParseError):
        return {}
    mapping = {}
    for rel in root.iter(f"{{{_NS['rel']}}}Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target")
        if not rid or not target:
            continue
        mapping[rid] = _resolve_part(target, base)
    return mapping


def iter_preferred(element: ElementTree.Element, tag: str):
    """yield descendants matching the tag, entering only the preferred mc:AlternateContent branch.

    The same object may appear in both mc:Choice and mc:Fallback (common in WPS/
    LibreOffice/newer Office): an unfiltered whole-tree iter would extract the text
    twice. Preferred branch = the first mc:Choice; fall back to mc:Fallback only when there is no Choice
    (pptx_parsing_standard.md §4).
    """
    for child in element:
        if child.tag == _ALTERNATE_TAG:
            preferred = child.find(_CHOICE_TAG)
            if preferred is None:
                preferred = child.find(_FALLBACK_TAG)
            if preferred is not None:
                yield from iter_preferred(preferred, tag)
            continue
        if child.tag == tag:
            yield child
        yield from iter_preferred(child, tag)


def presentation_slide_order(
    zf: zipfile.ZipFile, names: list[str]
) -> tuple[list[tuple[int, str]], str, list[int]]:
    """Declaration order (sldIdLst → presentation rels → slide part).

    Returns (ordered_slides, order_method, orphan_numbers):
        - ordered: [(file_number, part_name)] in presentation order
        - order_method: "declared" | "filename_fallback"
        - orphans: slide file numbers present in the package but not declared by sldIdLst

    When presentation.xml / rels is missing or damaged, or any sldId's rId cannot be resolved,
    the whole listing degrades to filename order (conservative: no half-declared order). Orphan slides are appended at
    the end of the declared order (filename order) — they are in-package facts, not silently dropped.
    """
    filename_order = sorted(
        (int(m.group(1)), name)
        for name in names
        if (m := _SLIDE_NAME_RE.match(name))
    )
    if _PRESENTATION_PART not in names:
        return filename_order, "filename_fallback", []

    try:
        root = ElementTree.fromstring(zf.read(_PRESENTATION_PART))
    except (KeyError, ElementTree.ParseError):
        return filename_order, "filename_fallback", []

    sld_ids = []
    for sld_id_lst in root.iter(f"{{{_NS['p']}}}sldIdLst"):
        for sld_id in sld_id_lst.findall(f"{{{_NS['p']}}}sldId"):
            rid = sld_id.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            if rid:
                sld_ids.append(rid)
    if not sld_ids:
        return filename_order, "filename_fallback", []

    rid_map = _rels_targets(zf, _PRESENTATION_RELS_PART, "ppt")
    if not rid_map:
        return filename_order, "filename_fallback", []

    by_part = {name: (number, name) for number, name in filename_order}
    ordered: list[tuple[int, str]] = []
    resolved_parts: set[str] = set()
    for rid in sld_ids:
        part = rid_map.get(rid)
        if part is None or part not in by_part:
            # any rId unresolvable → whole-listing degradation, no half-declared order
            return filename_order, "filename_fallback", []
        if part not in resolved_parts:
            resolved_parts.add(part)
            ordered.append(by_part[part])

    orphans = [
        number for number, name in filename_order
        if name not in resolved_parts
    ]
    ordered.extend(
        (number, name) for number, name in filename_order
        if name not in resolved_parts
    )
    return ordered, "declared", orphans


def presentation_modify_verifier(
    zf: zipfile.ZipFile, names: list[str]
) -> dict[str, Any] | None:
    """r23: presentation.xml root-level p:modifyVerifier write-protection declaration.

    OOXML write-protection carrier (editing password hash): spins/saltData/hashData/
    algName verbatim attributes — purely declared facts; verification and enforcement always false; a missing or
    damaged presentation part returns None (the degradation path fabricates nothing).
    """
    if _PRESENTATION_PART not in names:
        return None
    try:
        root = ElementTree.fromstring(
            zf.read(_PRESENTATION_PART)
        )
    except (KeyError, ElementTree.ParseError):
        return None
    verifier = root.find(f"{{{_NS['p']}}}modifyVerifier")
    if verifier is None:
        return None
    return dict(verifier.attrib)


def _related_part(
    zf: zipfile.ZipFile,
    rels_name: str,
    base: str,
    type_suffix: str,
) -> str | None:
    """rels part → the first internal target part matching a type suffix (None if absent).

    The rels file, when present, is authoritative — all relationship types of the same part go through rels
    resolution (§5); External targets and empty targets don't count. Shared by the notes/comments/
    commentAuthors channels (single source of truth).
    """
    if rels_name not in zf.namelist():
        return None
    try:
        root = ElementTree.fromstring(zf.read(rels_name))
    except (KeyError, ElementTree.ParseError):
        return None
    for rel in root.iter(f"{{{_NS['rel']}}}Relationship"):
        rel_type = rel.get("Type") or ""
        if not rel_type.endswith(type_suffix):
            continue
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target")
        if target:
            return _resolve_part(target, base)
    return None


def notes_slide_part(
    zf: zipfile.ZipFile, names: list[str], slide_number: int
) -> str | None:
    """slide → notes part (via the .../notesSlide relationship; None if absent).

    Callers must distinguish two None semantics: the slide has no .rels file (degraded filename
    numbering convention) vs rels exists but no notesSlide relationship (truly no notes —
    pptx_parsing_standard.md §5, rels is the authoritative source of relationship facts).
    """
    rels_name = f"ppt/slides/_rels/slide{slide_number}.xml.rels"
    if rels_name not in names:
        return None
    return _related_part(zf, rels_name, "ppt/slides", "/notesSlide")


def classify_pptx_container(raw_head: bytes) -> dict:
    """Factual container classification from the leading raw bytes."""
    ole_container = raw_head == CFB_MAGIC
    return {
        "ole_container": ole_container,
        "note": (
            "OLE/CFB binary container where a pptx zip was expected "
            "(encrypted OOXML package or legacy .ppt); never opened or "
            "decrypted here"
            if ole_container
            else "zip container (Open XML package)"
        ),
    }


def extract_pptx_evidence(file_path: str | Path) -> dict:
    """Collect all PPTX evidence namespaces from one package.

    Never raises: a broken namespace degrades to an honest error record
    while the remaining namespaces stay available.
    """
    facts: dict = {}
    try:
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()
            slides = sorted(
                (int(m.group(1)), name)
                for name in names
                if (m := _SLIDE_NAME_RE.match(name))
            )
            facts["pptx_package"] = _package_facts(zf, names, slides, file_path)
            facts["pptx_presentation"] = _presentation_facts(zf, names)
            facts["pptx_integrity"] = _integrity_facts(zf, names)
            facts["pptx_comments"] = _comment_facts(zf, names)
            facts["pptx_security"] = _security_facts(zf, names, slides)
            facts["pptx_text_characters"] = _text_character_facts(
                zf, names, slides
            )
            facts["pptx_watermarks"] = _watermark_facts(zf, slides)
    except Exception as error:  # Evidence gathering is never fatal.
        facts["pptx_evidence_error"] = f"{type(error).__name__}: {error}"
    return facts


# ------------------------------------------------------------
# package facts
# ------------------------------------------------------------

def _safe_int(value):
    """str → int, None when invalid/missing (no assertion, no guessing)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _package_facts(
    zf: zipfile.ZipFile,
    names: list[str],
    slides: list[tuple[int, str]],
    file_path: str | Path,
) -> dict:
    media = [name for name in names if name.startswith("ppt/media/")]
    variant = _format_variant(zf)
    return {
        "part_count": len(names),
        "slide_count": len(slides),
        "notes_slide_count": sum(
            1 for name in names if _NOTES_NAME_RE.match(name)
        ),
        "slide_master_count": sum(
            1 for name in names
            if re.match(r"^ppt/slideMasters/slideMaster\d+\.xml$", name)
        ),
        "slide_layout_count": sum(
            1 for name in names
            if re.match(r"^ppt/slideLayouts/slideLayout\d+\.xml$", name)
        ),
        "media_count": len(media),
        # §9 #21: package variant declared by the content type + extension consistency (masquerade/rename detection)
        "format_variant": variant,
        "format_mismatch": _format_mismatch(variant, file_path),
        "note": "package part inventory recorded as facts only",
    }


def _format_variant(zf: zipfile.ZipFile) -> str | None:
    """[Content_Types].xml main-document content type → pptx/pptm/ppsx/ppsm.

    The content type is an in-package fact, independent of the file extension (§9 #21).
    """
    try:
        content_types = zf.read("[Content_Types].xml").decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return None
    for marker, variant in _CONTENT_TYPE_VARIANTS:
        if marker in content_types:
            return variant
    return None


def _format_mismatch(
    variant: str | None, file_path: str | Path
) -> bool | None:
    """Extension vs content-type variant consistency (None = undecidable)."""
    if variant is None:
        return None
    suffix = Path(file_path).suffix.lower().lstrip(".")
    return suffix != variant


# ------------------------------------------------------------
# presentation-level metadata (sldSz / docProps) — §9 #9
# ------------------------------------------------------------

def _presentation_facts(zf: zipfile.ZipFile, names: list[str]) -> dict:
    """presentation-level metadata: sldSz + docProps verbatim facts.

    Dates/strings keep raw values, not normalized (spec §principles: normalization never overwrites raw values);
    missing parts/fields are silently omitted — absent means absent.
    """
    facts: dict = {
        "note": (
            "presentation-level metadata recorded as raw facts; values "
            "are never normalized or rewritten"
        )
    }
    if _PRESENTATION_PART in names:
        try:
            root = ElementTree.fromstring(zf.read(_PRESENTATION_PART))
        except (KeyError, ElementTree.ParseError):
            root = None
        if root is not None:
            sld_sz = root.find(f"{{{_NS['p']}}}sldSz")
            if sld_sz is not None:
                size = {
                    "cx": _safe_int(sld_sz.get("cx")),
                    "cy": _safe_int(sld_sz.get("cy")),
                }
                if sld_sz.get("type"):
                    size["type"] = sld_sz.get("type")
                facts["slide_size"] = size
            # r25 (6): p:embeddedFontLst embedded-font registration (verbatim +
            # rels-resolved fntdata part path); written only when non-empty.
            embedded_fonts, truncated = _embedded_font_facts(zf, root)
            if embedded_fonts:
                facts["embedded_fonts"] = embedded_fonts
                if truncated:
                    facts["embedded_fonts_truncated"] = True
    core = _docprops_fields(zf, names, "docProps/core.xml", _CORE_FIELDS)
    if core:
        facts["core_properties"] = core
    app = _docprops_fields(zf, names, "docProps/app.xml", _APP_FIELDS)
    if app:
        facts["app_properties"] = app
    return facts


def _embedded_font_facts(
    zf: zipfile.ZipFile,
    pres_root: ElementTree.Element,
) -> tuple[list[dict], bool]:
    """p:embeddedFontLst → embedded-font registration (r25 (6), verbatim).

    typeface verbatim + each slot's (regular/bold/italic/boldItalic) r:id
    resolved through presentation rels into the fntdata part path; a missing rid or
    an absent rels mapping → None placeholder (no fabricated values). Returns (records, truncated);
    pathological files cap at _MAX_EMBEDDED_FONT_SAMPLES. No embeddedFontLst
    → ([], False); clean files gain zero new keys.
    """
    lst = pres_root.find(f"{{{_NS['p']}}}embeddedFontLst")
    if lst is None:
        return [], False
    rels = _rels_targets(zf, _PRESENTATION_RELS_PART, "ppt")
    records: list[dict] = []
    truncated = False
    for emb in lst.findall(f"{{{_NS['p']}}}embeddedFont"):
        if len(records) >= _MAX_EMBEDDED_FONT_SAMPLES:
            truncated = True
            break
        font = emb.find(f"{{{_NS['p']}}}font")
        record: dict[str, Any] = {
            "typeface": font.get("typeface") if font is not None else None,
        }
        slots = {}
        for slot in _FONT_SLOT_TAGS:
            node = emb.find(f"{{{_NS['p']}}}{slot}")
            if node is None:
                continue
            rid = node.get(f"{{{_NS['r']}}}id")
            slots[slot] = rels.get(rid) if rid else None
        if slots:
            record["slots"] = slots
        records.append(record)
    return records, truncated


def _docprops_fields(
    zf: zipfile.ZipFile,
    names: list[str],
    part: str,
    fields: dict[str, str],
) -> dict:
    """docProps part direct-child fields → {key: verbatim} (empty text/missing fields omitted)."""
    if part not in names:
        return {}
    try:
        root = ElementTree.fromstring(zf.read(part))
    except (KeyError, ElementTree.ParseError):
        return {}
    values = {}
    for key, tag in fields.items():
        node = root.find(tag)
        if node is not None and node.text and node.text.strip():
            values[key] = node.text.strip()
    return values


# ------------------------------------------------------------
# integrity facts (dangling relationships / missing media) — §9 #10
# ------------------------------------------------------------

def _integrity_facts(zf: zipfile.ZipFile, names: list[str]) -> dict:
    """Structural integrity: internal relationships pointing at parts missing from the package (dangling rels).

    External targets don't count as dangling (external links are legitimate facts, recorded by the security channel);
    dangling is registered but never repaired — failures are locatable, not silent.
    """
    part_set = set(names)
    dangling: list[dict] = []
    media_missing = 0
    for rel_name in names:
        match = _RELS_PART_RE.match(rel_name)
        if match is None:
            continue
        base = (match.group("base") or "").rstrip("/")
        try:
            root = ElementTree.fromstring(zf.read(rel_name))
        except (KeyError, ElementTree.ParseError):
            continue
        for rel in root.iter(f"{{{_NS['rel']}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target")
            if not target:
                continue
            resolved = _resolve_part(target, base)
            if resolved not in part_set:
                if resolved.startswith("ppt/media/"):
                    media_missing += 1
                dangling.append(
                    {
                        "rels": rel_name,
                        "rel_type": (rel.get("Type") or "").rsplit(
                            "/", 1
                        )[-1]
                        or None,
                        "target": resolved,
                    }
                )
    return {
        "dangling_relationship_count": len(dangling),
        "missing_media_count": media_missing,
        "samples": dangling[:_MAX_DANGLING_SAMPLES],
        "note": (
            "internal relationship targets pointing outside the package "
            "are recorded as evidence, never fetched or repaired"
        ),
    }


# ------------------------------------------------------------
# comment / comment-author facts — §9 #20 (UNSUPPORTED-BUT-RECORDED)
# ------------------------------------------------------------

def _comment_facts(zf: zipfile.ZipFile, names: list[str]) -> dict:
    """Comments and authors registration (UNSUPPORTED-BUT-RECORDED, §9 #20).

    ECMA-376 §13.3.1/§13.3.2: comments part root element p:cmLst (p:cm entries:
    authorId/dt/idx attributes + p:text child), corresponding via slide rels
    (.../comments); authors part root element p:cmAuthorLst
    (p:cmAuthor: id/name/initials), corresponding via presentation rels
    (.../commentAuthors). Comment content never enters narrative — only registered as
    facts; author names/timestamps stay verbatim; privacy redaction (retain/redact/hash/
    drop) is a downstream policy decision — the parser never rewrites.
    """
    authors: list[dict] = []
    authors_by_id: dict[str, str] = {}
    author_part = _related_part(
        zf, _PRESENTATION_RELS_PART, "ppt", "/commentAuthors",
    )
    if author_part and author_part in names:
        try:
            root = ElementTree.fromstring(zf.read(author_part))
        except (KeyError, ElementTree.ParseError):
            root = None
        if root is not None:
            for author in root.iter(f"{{{_NS['p']}}}cmAuthor"):
                author_id = author.get("id")
                name = author.get("name")
                if author_id is None or name is None:
                    continue
                entry = {"id": author_id, "name": name}
                initials = author.get("initials")
                if initials:
                    entry["initials"] = initials
                authors.append(entry)
                authors_by_id[author_id] = name

    comments: list[dict] = []
    slides_with_comments: list[int] = []
    parts_without_entries: list[str] = []
    for rel_name in names:
        match = _SLIDE_RELS_RE.match(rel_name)
        if match is None:
            continue
        slide_number = int(match.group(1))
        comments_part = _related_part(
            zf, rel_name, "ppt/slides", "/comments",
        )
        if not comments_part or comments_part not in names:
            continue
        try:
            root = ElementTree.fromstring(zf.read(comments_part))
        except (KeyError, ElementTree.ParseError):
            parts_without_entries.append(comments_part)
            continue
        found = 0
        for cm in root.iter(f"{{{_NS['p']}}}cm"):
            text_node = cm.find(f"{{{_NS['p']}}}text")
            text = (
                (text_node.text or "").strip()
                if text_node is not None else ""
            )
            author_id = cm.get("authorId")
            comment_entry: dict[str, Any] = {
                "slide_number": slide_number,
                "part": comments_part,
                "idx": cm.get("idx"),
                "author_id": author_id,
                "author_name": (
                    authors_by_id.get(author_id)
                    if author_id is not None else None
                ),
                "text": text,
            }
            created = cm.get("dt")
            if created:
                comment_entry["created"] = created
            comments.append(comment_entry)
            found += 1
        if found:
            slides_with_comments.append(slide_number)
        else:
            # The referenced part has no p:cm entries — empty part or non-ECMA-376
            # legacy shape (e.g. newer modern comments); register, don't guess.
            parts_without_entries.append(comments_part)

    return {
        "comment_count": len(comments),
        "slides_with_comments": slides_with_comments,
        "authors": authors,
        "samples": comments[:_MAX_COMMENT_SAMPLES],
        "parts_without_entries": parts_without_entries[:_MAX_DANGLING_SAMPLES],
        "note": (
            "legacy comments (cmLst) recorded verbatim as evidence; author "
            "names and timestamps are raw facts, privacy redaction "
            "(retain/redact/hash/drop) is a downstream policy decision, "
            "never a parser rewrite"
        ),
    }


# ------------------------------------------------------------
# security facts (macros, hidden slides, external targets)
# ------------------------------------------------------------

def _security_facts(
    zf: zipfile.ZipFile, names: list[str], slides: list[tuple[int, str]]
) -> dict:
    return {
        "macros": _macro_facts(zf, names),
        "hidden_slides": _hidden_slide_facts(zf, slides),
        "external_targets": _external_target_facts(zf, names),
    }


def _macro_facts(zf: zipfile.ZipFile, names: list[str]) -> dict:
    vba_parts = [name for name in names if "vbaProject" in name]
    macro_content_type = False
    try:
        content_types = zf.read("[Content_Types].xml").decode(
            "utf-8", errors="replace"
        )
        macro_content_type = (
            "macroEnabled" in content_types or "vbaProject" in content_types
        )
    except Exception:
        pass
    # Adversarial round 4 (PP4): part names and content types can both be faked (o.bin +
    # octet-stream); a rel Type pointing at vbaProject is the structural fact —
    # present when any one of the three signal sources hits.
    vba_rel_types = _vba_rel_type_parts(zf, names)
    return {
        "present": (
            bool(vba_parts) or macro_content_type or bool(vba_rel_types)
        ),
        "vba_parts": vba_parts,
        "macro_content_type": macro_content_type,
        "vba_rel_type_parts": vba_rel_types,
        "note": "macro parts are recorded as evidence and never executed",
    }


def _vba_rel_type_parts(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """Rels parts whose Relationship/@Type ends in vbaProject."""
    hits: list[str] = []
    for rel_name in names:
        if not _RELS_PART_RE.match(rel_name):
            continue
        try:
            root = ElementTree.fromstring(zf.read(rel_name))
        except Exception:
            continue
        for rel in root.iter(f"{{{_NS['rel']}}}Relationship"):
            if (rel.get("Type") or "").endswith("/vbaProject"):
                hits.append(rel_name)
                break
    return hits


def _hidden_slide_facts(
    zf: zipfile.ZipFile, slides: list[tuple[int, str]]
) -> dict:
    """Slides whose ``p:sld/@show`` is ``0``.

    Hidden slides are a presentation fact, not an extraction instruction:
    their text stays in the narrative exactly like visible slides.
    """
    hidden: list[int] = []
    scanned = slides[:_MAX_SLIDES_SCANNED]
    for number, name in scanned:
        try:
            root = ElementTree.fromstring(zf.read(name))
        except Exception:
            continue
        if root.get("show") == "0":
            hidden.append(number)
    # Adversarial round 4 (PP5): slides past the _MAX_SLIDES_SCANNED cap go unscanned;
    # hidden slides beyond the cap must not silently slip through — the truncation fact is
    # explicitly registered (same discipline as the embedded_files/fields truncated keys).
    truncated = len(slides) > len(scanned)
    return {
        "count": len(hidden),
        "slide_numbers": hidden[:_MAX_SAMPLE_SLIDES],
        "truncated": truncated,
        "note": (
            "hidden slides (show=\"0\") are recorded as evidence; their "
            "text stays in the narrative because extraction is not "
            "rendering"
            + (
                f"; only the first {_MAX_SLIDES_SCANNED} of "
                f"{len(slides)} slides were scanned"
                if truncated
                else ""
            )
        ),
    }


def _external_target_facts(zf: zipfile.ZipFile, names: list[str]) -> dict:
    samples: list[dict] = []
    count = 0
    # Adversarial round 4 (PP1/PP2): external links can hide in any part's rels (presentation,
    # slideMaster, slideLayout…); scanning only slides/notes would let phishing links mounted
    # at the master layer go invisible as a whole layer — scan all rels (bounded: package member count is already capped by the container guard).
    rel_names = [
        name for name in names if _RELS_PART_RE.match(name)
    ]
    for rel_name in rel_names:
        try:
            root = ElementTree.fromstring(zf.read(rel_name))
        except Exception:
            continue
        for rel in root.iter(f"{{{_NS['rel']}}}Relationship"):
            if rel.get("TargetMode") != "External":
                continue
            count += 1
            if len(samples) >= _MAX_EXTERNAL_SAMPLES:
                continue
            target = rel.get("Target") or ""
            rel_type = rel.get("Type") or ""
            samples.append(
                {
                    "part": rel_name,
                    "rel_type": rel_type.rsplit("/", 1)[-1] or None,
                    "target": target[:200] or None,
                }
            )
    return {
        "count": count,
        "samples": samples,
        "truncated": count > len(samples),
        "note": (
            "external relationship targets are recorded as evidence; "
            "no external resource is ever fetched"
        ),
    }


# ------------------------------------------------------------
# text character facts (fonts, RTL, vertical text, replacements)
# ------------------------------------------------------------

def _text_character_facts(
    zf: zipfile.ZipFile, names: list[str], slides: list[tuple[int, str]]
) -> dict:
    fonts: dict[str, int] = {}
    rtl_paragraph_count = 0
    vertical_text_slide_numbers: list[int] = []
    text_chars = 0
    replacement_chars = 0
    has_any_text = False

    for number, name in slides[:_MAX_SLIDES_SCANNED]:
        try:
            root = ElementTree.fromstring(zf.read(name))
        except Exception:
            continue

        for body_pr in iter_preferred(root, f"{{{_NS['a']}}}bodyPr"):
            vert = body_pr.get("vert")
            if vert and vert != "horz":
                if number not in vertical_text_slide_numbers:
                    vertical_text_slide_numbers.append(number)
                break

        for paragraph in iter_preferred(root, f"{{{_NS['a']}}}p"):
            p_pr = paragraph.find(f"{{{_NS['a']}}}pPr")
            if p_pr is not None and p_pr.get("rtl") == "1":
                rtl_paragraph_count += 1

        for tag in ("latin", "ea", "cs"):
            for font_node in iter_preferred(root, f"{{{_NS['a']}}}{tag}"):
                typeface = font_node.get("typeface")
                if typeface:
                    fonts[typeface] = fonts.get(typeface, 0) + 1

        for text_node in iter_preferred(root, f"{{{_NS['a']}}}t"):
            value = text_node.text or ""
            if value:
                has_any_text = True
                text_chars += len(value)
                replacement_chars += value.count("\ufffd")

    fonts_referenced = [
        {"typeface": typeface, "count": count}
        for typeface, count in sorted(
            fonts.items(), key=lambda item: (-item[1], item[0])
        )[:10]
    ]
    return {
        "font_count": len(fonts),
        "fonts_referenced": fonts_referenced,
        "rtl_paragraph_count": rtl_paragraph_count,
        "vertical_text_slide_numbers": vertical_text_slide_numbers,
        "replacement_char_count": replacement_chars,
        "replacement_char_ratio": round(
            replacement_chars / text_chars, 4
        ) if text_chars else 0.0,
        "has_text": has_any_text,
        "note": (
            "font/RTL/vertical/replacement evidence over slide text "
            "runs; text is never rewritten"
        ),
    }


# ------------------------------------------------------------
# watermark facts (repeated diagonal/large text across slides)
# ------------------------------------------------------------

def _axis_aligned(rot_attr: str | None) -> bool:
    """True when a body/shape rotation is a multiple of 90°."""
    if not rot_attr:
        return True
    try:
        rot = int(rot_attr)
    except ValueError:
        return True
    remainder = abs(rot) % (90 * _ANGLE_UNIT)
    remainder = min(remainder, 90 * _ANGLE_UNIT - remainder)
    return remainder <= _AXIS_TOLERANCE


def _watermark_facts(zf: zipfile.ZipFile, slides: list[tuple[int, str]]) -> dict:
    scanned = slides[:_MAX_SLIDES_SCANNED]
    occurrences: dict[str, dict] = {}
    slide_body_sizes: dict[int, float] = {}

    for number, name in scanned:
        try:
            root = ElementTree.fromstring(zf.read(name))
        except Exception:
            continue

        run_sizes: list[float] = []
        for shape in iter_preferred(root, f"{{{_NS['p']}}}sp"):
            # Shape rotation (spPr/xfrm/@rot) and body rotation
            # (bodyPr/@rot) are both 60000ths of a degree.
            rot_attrs: list[str | None] = []
            for xfrm in iter_preferred(shape, f"{{{_NS['a']}}}xfrm"):
                rot_attrs.append(xfrm.get("rot"))
            for body_pr in iter_preferred(shape, f"{{{_NS['a']}}}bodyPr"):
                rot_attrs.append(body_pr.get("rot"))
            rotated = any(not _axis_aligned(attr) for attr in rot_attrs)

            shape_sizes: list[float] = []
            shape_text_parts: list[str] = []
            for run_props in iter_preferred(shape, f"{{{_NS['a']}}}rPr"):
                sz = run_props.get("sz")
                if sz:
                    try:
                        shape_sizes.append(int(sz) / 100.0)
                    except ValueError:
                        pass
            for text_node in iter_preferred(shape, f"{{{_NS['a']}}}t"):
                if text_node.text:
                    shape_text_parts.append(text_node.text)

            text = "".join(shape_text_parts).strip()
            if text:
                run_sizes.extend(shape_sizes or [])
                if len(text) <= _MAX_WATERMARK_TEXT_CHARS:
                    entry = occurrences.setdefault(
                        text,
                        {"slides": [], "sizes": [], "rotated": False},
                    )
                    if number not in entry["slides"]:
                        entry["slides"].append(number)
                    entry["sizes"].extend(shape_sizes)
                    entry["rotated"] = entry["rotated"] or rotated

        if run_sizes:
            run_sizes.sort()
            slide_body_sizes[number] = run_sizes[len(run_sizes) // 2]

    candidates: list[dict] = []
    if len(scanned) >= 2:
        for text, entry in occurrences.items():
            slide_count = len(entry["slides"])
            coverage = slide_count / len(scanned)
            if coverage < _PAGE_COVERAGE_RATIO:
                continue

            rotated = bool(entry["rotated"])
            avg_size = (
                sum(entry["sizes"]) / len(entry["sizes"])
                if entry["sizes"] else 0.0
            )
            body_sizes = [
                slide_body_sizes[number]
                for number in entry["slides"]
                if number in slide_body_sizes and slide_body_sizes[number] > 0
            ]
            body_reference = (
                sum(body_sizes) / len(body_sizes) if body_sizes else 0.0
            )
            size_ratio = (
                round(avg_size / body_reference, 2) if body_reference else None
            )
            large_type = size_ratio is not None and size_ratio >= _BODY_SIZE_RATIO

            if not (rotated or large_type):
                continue

            candidates.append(
                {
                    "text": text,
                    "slide_numbers": entry["slides"][:_MAX_SAMPLE_SLIDES],
                    "slide_count": slide_count,
                    "slide_coverage": round(coverage, 3),
                    "rotated": rotated,
                    "avg_font_size": round(avg_size, 2),
                    "size_vs_body_ratio": size_ratio,
                    "features": (
                        ["repeated"]
                        + (["rotated"] if rotated else [])
                        + (["large_type"] if large_type else [])
                    ),
                }
            )
            if len(candidates) >= _MAX_CANDIDATES:
                break

    candidates.sort(
        key=lambda candidate: (-candidate["slide_count"], candidate["text"])
    )
    return {
        "scanned_slides": len(scanned),
        "present": bool(candidates),
        "candidates": candidates,
        "note": (
            "watermark evidence only: the watermark glyphs remain in the "
            "extracted narrative text; removal is a downstream policy "
            "decision, never a parser rewrite"
        ),
    }
