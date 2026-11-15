"""Evidence-only extraction of DOCX protection/attachment/security facts.

B7 scope (gap register): hidden text (``w:vanish``), document protection
(``w:documentProtection`` / ``w:writeProtection``), settings switches
(``w:trackChanges`` / ``w:updateFields``, Adversarial round 11), macros
(``vbaProject.bin`` / macro-enabled content types), embedded objects
(``word/embeddings`` + ``o:OLEObject`` ProgIDs), external data
connections, VML / SmartArt declarations, and encrypted OOXML containers
(MS-OFFCRYPTO OLE packages).  Hidden carriers and story scopes: body +
footnotes/endnotes (r7/r8) + header/footer/glossary (r10) + comments
(r11); webHidden cells additionally cover footnote/endnote story tables (r12). Glossary
part field and hyperlink extraction scopes are covered in docx_fields (r12).

Everything is evidence: macros are never executed, protection is never
lifted, encrypted packages are never decrypted, and hidden runs stay in
the narrative text (the parser extracts, it does not render).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path
from typing import Any

from docx.oxml import parse_xml
from docx.oxml.ns import qn

from provendoc.core.parsers.narrative.docx_fields import (
    _STORY_PART_NAMES,
    _story_paragraph_locations,
)
from provendoc.core.parsers.narrative.shared import CFB_MAGIC

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_VML_NS = "urn:schemas-microsoft-com:vml"
_OFFICE_NS = "urn:schemas-microsoft-com:office:office"
_DRAWINGML_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DIAGRAM_URI_SUFFIX = "/diagram"
_PKG_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)

# rels of any part: <base>/_rels/<part>.rels (the root-level _rels/.rels is matched too)
_RELS_PART_RE = re.compile(r"^(?:(?P<base>.*/))?_rels/[^/]*\.rels$")

# MS-OFFCRYPTO stream names that distinguish an encrypted OOXML package
# from a legacy binary document inside an OLE container.
_ENCRYPTION_MARKERS = (b"EncryptionInfo", b"EncryptedPackage")

# Note: CFB directory-entry names are, per the spec, stored as UTF-16LE (CFBF §2.6);
# classify_container searches both the ASCII and UTF-16LE encodings of the same marker,
# and any hit counts as that stream existing; the CFB structure itself is not parsed (the evidence layer only does
# byte fingerprints).

_MAX_HIDDEN_SAMPLES = 20
_MAX_TEXT_CHARS = 200
_MAX_EXTERNAL_TARGETS = 50
_MAX_FALLBACK_SAMPLES = 20
# Adversarial round 15: docVars samples disclosed with capping/truncation (count keeps the true value).
_MAX_DOC_VAR_SAMPLES = 20
_MAX_DOC_VAR_VALUE_CHARS = 200
# r25 face-2 (4): rsid samples capped (8-hex-digit identifiers need no truncation;
# capping the sample count suffices; count keeps the true value).
_MAX_RSID_SAMPLES = 20

# mc:AlternateContent family: consumers render only one of Choice/Fallback,
# the other branch becomes a dead branch never displayed in the document — a classic hiding place for smuggled text.
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

NS = {
    "w": _W_NS,
    "v": _VML_NS,
    "o": _OFFICE_NS,
    "a": _DRAWINGML_A,
}


# ------------------------------------------------------------
# container classification (runs before python-docx can open anything)
# ------------------------------------------------------------

def classify_container(raw_bytes: bytes) -> dict[str, Any]:
    """Factual container classification from the raw bytes.

    A ``.docx`` that starts with the OLE/CFB magic is either an encrypted
    OOXML package (MS-OFFCRYPTO, carries EncryptionInfo/EncryptedPackage
    streams) or a legacy binary document; neither can be parsed as a zip
    and neither is ever decrypted here.
    """
    facts: dict[str, Any] = {
        "ole_container": raw_bytes.startswith(CFB_MAGIC),
        "encrypted_package": False,
    }
    if facts["ole_container"]:
        head = raw_bytes[:8192]
        facts["encrypted_package"] = all(
            any(
                variant in head
                for variant in (
                    marker,
                    marker.decode("ascii").encode("utf-16-le"),
                )
            )
            for marker in _ENCRYPTION_MARKERS
        )
        if facts["encrypted_package"]:
            facts["note"] = (
                "encrypted OOXML package (MS-OFFCRYPTO OLE container with "
                "EncryptionInfo/EncryptedPackage streams); content is "
                "inaccessible without decryption, which never happens here"
            )
        else:
            facts["note"] = (
                "legacy OLE/CFB container (e.g. .doc binary); not an "
                "Open XML zip package — re-save the document as .docx to "
                "make its text extractable"
            )
    return facts


# ------------------------------------------------------------
# full security summary for an opened document
# ------------------------------------------------------------

def extract_security_summary(doc, raw_bytes: bytes, file_path: str | Path) -> dict[str, Any]:
    """Collect protection/attachment/security evidence from one docx.

    ``doc`` is an opened python-docx Document; ``raw_bytes``/``file_path``
    feed the container classification and zip-level part inspection.
    """
    return {
        "encryption": classify_container(raw_bytes),
        "hidden_text": _hidden_text_facts(doc),
        "protection": _protection_facts(doc),
        "settings": _settings_facts(doc),
        "macros": _macro_facts(file_path),
        "digital_signature": _digital_signature_facts(file_path),
        "embedded_objects": _embedded_object_facts(doc, file_path),
        "external_connections": _external_connection_facts(doc, file_path),
        "legacy_visual": _legacy_visual_facts(doc),
        "shading_obscuration": _shading_obscuration_facts(doc),
        "alternate_content": _alternate_content_facts(doc),
    }


def _alternate_content_facts(doc) -> dict[str, Any]:
    """mc:AlternateContent block counts and Fallback dead-branch content samples.

    Adversarial round 9: an AlternateContent consumer renders only one branch (most renderers
    pick Choice, falling back to Fallback when required features are missing); the unrendered branch's content
    never appears in the "normal view" — a visual hiding surface of the same family as w:vanish,
    previously disclosed nowhere. Scans the main document body and footnote/endnote story parts (isomorphic to the
    story scopes of Adversarial rounds 7/8); choice_requires records the dead branch's other-side
    compatibility condition. Registration only, never rendered.
    Adversarial round 10: the scan domain extended to header/footer/glossary parts (same as
    _extra_story_roots), samples carry location; Adversarial round 11 added the comments
    part (same prefix enumeration, guarding against double counting with the _STORY_PART_NAMES exact match).
    """
    count = 0
    fallback_samples: list[dict[str, Any]] = []
    roots: list[tuple[Any, str | None]] = [(doc.element.body, "body")]
    try:
        for part in doc.part.package.iter_parts():
            name = str(part.partname).lstrip("/")
            # The comments part (since Adversarial round 11) is covered by the _extra_story_roots prefix enumeration
            # and carries location — skipped here to prevent the same part being counted twice.
            if name in _STORY_PART_NAMES and not name.startswith(
                "word/comments"
            ):
                try:
                    roots.append((parse_xml(part.blob), None))
                except Exception:
                    continue
    except Exception:
        pass
    # Adversarial round 10: AlternateContent in header/footer/glossary parts scanned alike;
    # Adversarial round 11: the comments part scanned alike.
    roots.extend(_extra_story_roots(doc))
    for root, location in roots:
        for ac in root.iter(f"{{{_MC_NS}}}AlternateContent"):
            count += 1
            fallback = ac.find(f"{{{_MC_NS}}}Fallback")
            if fallback is None:
                continue
            text = "".join(
                node.text or ""
                for node in fallback.iter(qn("w:t"))
            ).strip()
            if len(fallback_samples) < _MAX_FALLBACK_SAMPLES and text:
                sample: dict[str, Any] = {"text": text[:_MAX_TEXT_CHARS]}
                if location:
                    sample["location"] = location
                fallback_samples.append(sample)
    return {
        "count": count,
        "fallback_samples": fallback_samples,
        "note": (
            "mc:AlternateContent consumers render only one branch; the "
            "other branch never displays. Fallback text samples listed "
            "when present (Choice text stays visible)."
        ),
    }


# ------------------------------------------------------------
# digital signatures (docx_digital_signature_prereg_v1)
# ------------------------------------------------------------

# OOXML XML digital-signature parts live in a package folder whose name
# is fixed and lowercase in the ECMA-376 spec, so a plain prefix match
# on the zip part list is the whole detection story.
_SIGNATURE_PART_PREFIX = "_xmlsignatures/"

# Presence list is capped; the count keeps the full extent of heavily
# signed packages visible even when the sample list is truncated.
_MAX_SIGNATURE_PARTS = 10


def _digital_signature_facts(file_path: str | Path) -> dict[str, Any]:
    """Register the existence of XML digital-signature parts, nothing more.

    docx_digital_signature_prereg_v1 §2: this sub-key is always written
    (``signature_present: false`` is a meaningful fact), detection is a
    zip name-list prefix scan for ``_xmlsignatures/`` (fixed lowercase
    per ECMA-376), signatures are never validated and signature parts
    are never read, and VBA-internal signatures (inside vbaProject.bin)
    are invisible at the package level and therefore not registered.
    """
    signature_parts: list[str] = []
    try:
        with zipfile.ZipFile(file_path) as archive:
            signature_parts = sorted(
                name
                for name in archive.namelist()
                if name.startswith(_SIGNATURE_PART_PREFIX)
            )
    except Exception:
        # Silent degradation: an unreadable package simply registers as
        # unsigned at this layer (container-level encryption notes it).
        signature_parts = []

    facts: dict[str, Any] = {
        "signature_present": bool(signature_parts),
        "signature_part_count": len(signature_parts),
        "signature_parts": signature_parts[:_MAX_SIGNATURE_PARTS],
    }
    if len(signature_parts) > _MAX_SIGNATURE_PARTS:
        facts["truncated_count"] = len(signature_parts) - _MAX_SIGNATURE_PARTS
    facts["note"] = (
        "digital-signature parts are registered as existence evidence "
        "only; signatures are never validated and signature part "
        "contents are never read (VBA-internal signatures are not "
        "visible at the package level and are not registered)"
    )
    return facts


# ------------------------------------------------------------
# hidden text (w:vanish)
# ------------------------------------------------------------

def _runs_owned_by_paragraph(paragraph) -> list[Any]:
    """Runs whose nearest ``w:p`` ancestor is THIS paragraph.

    Adversarial round 5 (DD3/DD4): runs can nest inside ``w:hyperlink`` / ``w:smartTag`` /
    ``w:ins`` and similar containers; ``findall(qn("w:r"))`` sees only direct children and misses whole spans,
    while ``iter`` crosses nested-paragraph boundaries and double-attributes. The nearest-ancestor check
    covers both: full-subtree collection attributed only to the nearest paragraph.
    """
    owned = []
    for run in paragraph.iter(qn("w:r")):
        node = run.getparent()
        while node is not None and node.tag != qn("w:p"):
            node = node.getparent()
        if node is paragraph:
            owned.append(run)
    return owned


def _collect_hidden_runs(p_element, vanish_styles: set[str]) -> list[tuple[Any, str]]:
    """(run, via) pairs hidden by direct w:vanish or a vanish style.

    Adversarial round 9: a run need not carry w:vanish itself — hiding can take effect indirectly via a character style
    (w:rStyle pointing to a style whose rPr has vanish) or a paragraph style (w:pStyle);
    checking only run rPr misses the whole style-level layer of hiding.
    """
    hidden: list[tuple[Any, str]] = []
    for run in _runs_owned_by_paragraph(p_element):
        rPr = run.find(qn("w:rPr"))
        if rPr is not None and rPr.find(qn("w:vanish")) is not None:
            hidden.append((run, "direct"))
            continue
        if rPr is not None:
            rstyle = rPr.find(qn("w:rStyle"))
            if rstyle is not None and (
                rstyle.get(qn("w:val")) in vanish_styles
            ):
                hidden.append((run, "char_style"))
    return hidden


def _vanish_style_ids(doc) -> set[str]:
    """styleIds whose rPr chain (direct or via basedOn) carries vanish.

    Styles inherit via basedOn (safe against cycles in the chain); a missing/corrupt style sheet returns the empty set
    (observation never blocks).
    """
    try:
        styles_root = doc.styles.element
    except Exception:
        return set()
    direct: dict[str, bool] = {}
    parent: dict[str, str] = {}
    for style in styles_root.findall(qn("w:style")):
        sid = style.get(qn("w:styleId"))
        if not sid:
            continue
        rPr = style.find(qn("w:rPr"))
        direct[sid] = (
            rPr is not None and rPr.find(qn("w:vanish")) is not None
        )
        based = style.find(qn("w:basedOn"))
        if based is not None:
            val = based.get(qn("w:val"))
            if val:
                parent[sid] = val

    def _resolves(sid: str | None, seen: set[str]) -> bool:
        while sid and sid not in seen:
            seen.add(sid)
            if direct.get(sid):
                return True
            sid = parent.get(sid)
        return False

    return {sid for sid in direct if _resolves(sid, set())}


# Adversarial round 10: package-level enumeration of header/footer/glossary parts (partname prefix
# matching — dynamic names like header1.xml/header2.xml); Adversarial round 11 added the
# comments-part prefixes (word/comments.xml plus the commentsExtended and other sibling
# parts — the latter two carry no w:p/w:tc hosts, so enumerating them yields nothing to scan).
def _extra_story_roots(doc) -> list[tuple[Any, str]]:
    """(root_element, location) pairs for non-body story parts.

    Field extraction has had a header/footer scope from day one (docx_fields enumerates sections via python-docx),
    but the hidden-carrier scans of the security facts (hidden_text /
    webHidden / alternate_content) covered only footnotes/endnotes before —
    the three whole layers header/footer/glossary never entered any hidden disclosure (Adversarial round 10);
    the comments part is likewise the fourth whole layer (Adversarial round 11: docx_review registers only comment
    counts and author sets, the hidden-carrier channel was fully blind). Enumerated by package-level partname prefix
    (covers orphan parts; glossary/comments have no python-docx API, so package level is the
    only option). Missing/corrupt parts are skipped silently (observation never blocks).
    """
    roots: list[tuple[Any, str]] = []
    seen: set[int] = set()
    try:
        parts = list(doc.part.package.iter_parts())
    except Exception:
        return roots
    for part in parts:
        name = str(part.partname).lstrip("/")
        if name.startswith("word/header"):
            label = "header"
        elif name.startswith("word/footer"):
            label = "footer"
        elif name.startswith("word/glossary/"):
            label = "glossary"
        elif name.startswith("word/comments"):
            label = "comment"
        else:
            continue
        try:
            root = parse_xml(part.blob)
        except Exception:
            continue
        if id(root) in seen:
            continue
        seen.add(id(root))
        roots.append((root, label))
    return roots


def _fn_en_roots(doc) -> list[tuple[Any, str]]:
    """(root_element, location) pairs for footnotes/endnotes parts.

    Adversarial round 12: the webHidden cell channel previously fed only body + extra_roots
    (header/footer/glossary/comment); footnote/endnote stories were fed in only on the vanish
    channel via the proxy (r8) — story-table w:webHidden cells were missing as a whole layer
    (body disclosed, footnotes not: a false absence). Fed by _STORY_PART_NAMES
    exact match to fetch the root element; the vanish and AlternateContent channels keep their own paths,
    no same-paragraph double counting. Missing/corrupt parts are skipped silently.
    """
    roots: list[tuple[Any, str]] = []
    try:
        parts = list(doc.part.package.iter_parts())
    except Exception:
        return roots
    for part in parts:
        label = _STORY_PART_NAMES.get(str(part.partname).lstrip("/"))
        if label not in ("footnote", "endnote"):
            continue
        try:
            roots.append((parse_xml(part.blob), label))
        except Exception:
            continue
    return roots


def _hidden_text_facts(doc) -> dict[str, Any]:
    """Hidden-text evidence: w:vanish runs + w:webHidden cells.

    Adversarial round 8 added two scope extensions:
    - the vanish scan extended from the main document body to footnote/endnote story parts (isomorphic to the
      field-extraction story scope of Adversarial round 7; story-part vanish hidden text was previously missing as a whole layer
      — body disclosed, footnotes not: a false absence);
    - a new w:webHidden cell-level hidden channel (table cells hidden in the web view
      are a classic carrier of smuggled text; docx_tables' hidden_row_count
      counts only rows, the cell level had no disclosure). Samples carry a location tag.
    Adversarial round 9 added style-level hiding: character styles (w:rStyle) and paragraph styles
    (w:pStyle) resolve the vanish style set through the basedOn chain; runs/paragraphs referencing those styles
    are disclosed under the same rules as direct ones, samples carry a via tag.
    Adversarial round 10 added header/footer/glossary parts: those three whole layers previously never entered
    any hidden-carrier scan (field extraction had a header scope; the security facts did not);
    the vanish/webHidden channels were both extended.
    Adversarial round 11 added the comments part: the fourth whole layer (docx_review registers only comment
    counts and author sets, the content channel was fully blind) — vanish/webHidden/
    AlternateContent, three channels extended in step (same _extra_story_roots).
    Adversarial round 12 added webHidden cells of footnote/endnote stories: the vanish channel
    has covered footnotes/endnotes via the story proxy since r8, but the webHidden loop fed only
    body + extra_roots — cell-level hiding inside story tables was previously absent as a whole layer.
    """
    paragraphs: list[dict[str, Any]] = []
    run_count = 0
    char_count = 0
    paragraph_total = 0
    vanish_styles = _vanish_style_ids(doc)

    def _feed(p_element, location: str) -> None:
        nonlocal run_count, char_count, paragraph_total
        hidden_runs = _collect_hidden_runs(p_element, vanish_styles)
        if not hidden_runs:
            # Paragraph-style-level hiding: when pStyle references a vanish style, every run of the paragraph
            # renders hidden (the runs themselves need no marker at all).
            pPr = p_element.find(qn("w:pPr"))
            pstyle = (
                pPr.find(qn("w:pStyle")).get(qn("w:val"))
                if pPr is not None and pPr.find(qn("w:pStyle")) is not None
                else None
            )
            if pstyle is None or pstyle not in vanish_styles:
                return
            hidden_runs = [
                (run, "para_style")
                for run in _runs_owned_by_paragraph(p_element)
            ]
        if not hidden_runs:
            return
        paragraph_total += 1
        run_count += len(hidden_runs)
        text = "".join(
            (node.text or "")
            for run, _via in hidden_runs
            for node in run.findall(qn("w:t"))
        )
        char_count += len(text)
        if len(paragraphs) < _MAX_HIDDEN_SAMPLES:
            vias = sorted({via for _run, via in hidden_runs})
            paragraphs.append(
                {
                    "text": text[:_MAX_TEXT_CHARS] or None,
                    "run_count": len(hidden_runs),
                    "location": location,
                    "via": vias,
                }
            )

    body = doc.element.body
    for paragraph in body.iter(qn("w:p")):
        _feed(paragraph, "body")

    # Adversarial round 8: story parts (footnotes/endnotes) scanned under the same rules;
    # Adversarial round 10: header/footer/glossary parts scanned under the same rules;
    # Adversarial round 11: once the comments part entered _STORY_PART_NAMES, _extra_story_roots feeds it
    # via prefix enumeration uniformly (with location); skipped here to prevent same-paragraph double counting;
    # Adversarial round 12: the glossary part entering _STORY_PART_NAMES (prefix enumeration) is skipped for the same reason —
    # dual-path feeding means same-paragraph double counting; single-path discipline, consistent with the comments part.
    for location, _para_index, proxy in _story_paragraph_locations(doc):
        if location in ("comment", "glossary"):
            continue
        _feed(proxy._p, location)
    extra_roots = _extra_story_roots(doc)
    for root, location in extra_roots:
        for p_element in root.iter(qn("w:p")):
            _feed(p_element, location)

    # Adversarial round 8: w:webHidden cell-level hiding (main-body table scope);
    # Adversarial round 10: extended to header/footer/glossary part tables;
    # Adversarial round 12: extended to footnote/endnote story tables (_fn_en_roots feeds independently,
    # the vanish channel goes through the proxy path — no mutual double counting).
    webhidden_cells = 0
    webhidden_chars = 0
    webhidden_samples: list[dict[str, Any]] = []
    for root, location in [
        (body, "body")
    ] + extra_roots + _fn_en_roots(doc):
        for cell in root.iter(qn("w:tc")):
            tcPr = cell.find(qn("w:tcPr"))
            if tcPr is None or tcPr.find(qn("w:webHidden")) is None:
                continue
            webhidden_cells += 1
            text = "".join(
                (node.text or "")
                for node in cell.iter(qn("w:t"))
            )
            webhidden_chars += len(text)
            if len(webhidden_samples) < _MAX_HIDDEN_SAMPLES:
                webhidden_samples.append(
                    {
                        "text": text[:_MAX_TEXT_CHARS] or None,
                        "location": location,
                    }
                )

    return {
        "paragraph_count": paragraph_total,
        "run_count": run_count,
        "char_count": char_count,
        "samples": paragraphs,
        "webhidden_cell_count": webhidden_cells,
        "webhidden_cell_char_count": webhidden_chars,
        "webhidden_cell_samples": webhidden_samples,
        "note": (
            "hidden runs (w:vanish, body + footnote/endnote stories + "
            "header/footer/glossary/comment parts) and web-hidden cells "
            "(w:webHidden, body/header/footer/glossary/comment tables "
            "+ footnote/endnote story tables) "
            "are reported as evidence; the narrative text keeps them "
            "verbatim because extraction is not rendering"
        ),
    }


# ------------------------------------------------------------
# settings switches (Adversarial round 11)
# ------------------------------------------------------------

def _settings_facts(doc) -> dict[str, Any]:
    """settings.xml switch-class facts: revision tracking, on-open field recalculation, document variables, and
    revision session identifiers.

    ``w:trackChanges`` declares the document is under revision tracking (forensically important:
    deleted text stays in the revision record instead of the body narrative); ``w:updateFields`` declares
    a forced recalculation of all fields on open — the combination-punch switch for field-class TTPs (DDEAUTO/INCLUDEPICTURE).
    Adversarial round 15: ``w:docVars`` is a slot for arbitrary string key-value pairs
    (the classic dropper-payload location: base64/paths/configs hide here,
    never entering any narrative text stream; previously zero disclosure) — names and values are copied verbatim (values
    truncated under the cap), count keeps the true value, never evaluated. r25 face-2 (4): ``w:rsids``
    revision session identifier census (rsidRoot + rsid value samples, count keeps the true value) —
    a forensic signal of editing-session reassembly, written only when non-empty. Registration only, never executed.
    """
    facts: dict[str, Any] = {
        "track_changes": False,
        "update_fields": False,
        "doc_var_count": 0,
        "doc_var_samples": [],
    }
    try:
        settings = doc.settings.element
    except Exception:
        settings = None
    if settings is not None:
        facts["track_changes"] = (
            settings.find(qn("w:trackChanges")) is not None
        )
        update_fields = settings.find(qn("w:updateFields"))
        if update_fields is not None:
            facts["update_fields"] = True
            # w:val copied verbatim (when absent the spec default is true — no substitution, no guessing).
            facts["update_fields_val"] = update_fields.get(qn("w:val"))
        # Adversarial round 15: w:docVars key-value registration (census-style minimization:
        # no docVars means count 0 and empty samples, no false reporting).
        doc_vars = settings.find(qn("w:docVars"))
        if doc_vars is not None:
            entries = doc_vars.findall(qn("w:docVar"))
            facts["doc_var_count"] = len(entries)
            facts["doc_var_samples"] = [
                {
                    "name": entry.get(qn("w:name")) or "",
                    "value": (entry.get(qn("w:val")) or "")[
                        :_MAX_DOC_VAR_VALUE_CHARS
                    ],
                }
                for entry in entries[:_MAX_DOC_VAR_SAMPLES]
            ]
        # r25 face-2 (4): w:rsids revision session identifier registration (census-style minimization).
        # rsids are 8-hex-digit identifiers, one per save session (rsidRoot is the first
        # session identifier) — a forensic signal of edit counts/session reassembly, previously zero disclosure.
        # A new key is written only when the w:rsids element exists (a clean document without rsids gets zero
        # new keys, additive-only); values are copied verbatim, never interpreted.
        rsids = settings.find(qn("w:rsids"))
        if rsids is not None:
            entries = rsids.findall(qn("w:rsid"))
            facts["rsid_count"] = len(entries)
            facts["rsid_samples"] = [
                entry.get(qn("w:val")) or ""
                for entry in entries[:_MAX_RSID_SAMPLES]
            ]
            rsid_root = rsids.find(qn("w:rsidRoot"))
            if rsid_root is not None:
                facts["rsid_root"] = rsid_root.get(qn("w:val"))
    facts["note"] = (
        "settings switches, doc variables and revision session ids "
        "(rsids) are recorded as evidence verbatim; variables are "
        "never evaluated, fields are never recalculated and revisions "
        "are never accepted or rejected"
    )
    return facts


# ------------------------------------------------------------
# document protection
# ------------------------------------------------------------

def _protection_facts(doc) -> dict[str, Any]:
    facts: dict[str, Any] = {}

    try:
        settings = doc.settings.element
    except Exception:
        settings = None

    if settings is not None:
        protection = settings.find(qn("w:documentProtection"))
        if protection is not None:
            facts["document_protection"] = {
                "edit": protection.get(qn("w:edit")),
                "enforced": protection.get(qn("w:enforcement")) == "1",
                "hash_protected": (
                    protection.get(qn("w:hash")) is not None
                    or protection.get(qn("w:cryptProviderType")) is not None
                ),
            }
        write_protection = settings.find(qn("w:writeProtection"))
        if write_protection is not None:
            facts["write_protection"] = {
                "recommended": write_protection.get(qn("w:recommended")) == "1",
            }

    if "document_protection" not in facts and "write_protection" not in facts:
        facts["present"] = False
    else:
        facts["present"] = True
        facts["note"] = (
            "protection declarations are recorded as evidence; "
            "restrictions are never lifted"
        )
    return facts


# ------------------------------------------------------------
# macros
# ------------------------------------------------------------

def _macro_facts(file_path: str | Path) -> dict[str, Any]:
    parts: list[str] = []
    macro_content_type = False
    vba_rel_type_parts: list[str] = []
    try:
        with zipfile.ZipFile(file_path) as archive:
            parts = [
                name
                for name in archive.namelist()
                if name.endswith("vbaProject.bin")
            ]
            try:
                content_types = archive.read("[Content_Types].xml").decode(
                    "utf-8", errors="replace"
                )
                macro_content_type = (
                    "vbaProject" in content_types
                    or "macroEnabled" in content_types
                )
            # The content-type list can throw arbitrary exceptions on a corrupt package; macro-detection failure is treated as no-macro (observation never blocks).
            except Exception:
                pass
            # Adversarial round 5 (DD1): part names can be disguised (o.bin + clean Content_Types),
            # a rel Type pointing at vbaProject is a structural declaration — any one of the three signal sources hitting counts as present.
            vba_rel_type_parts = _vba_rel_type_parts(archive)
    # Package-level manifest read failure is likewise treated as no-macro; macro absence != a no-risk claim, merely absence of evidence.
    except Exception:
        pass

    return {
        "present": (
            bool(parts) or macro_content_type or bool(vba_rel_type_parts)
        ),
        "vba_parts": parts,
        "macro_content_type": macro_content_type,
        "vba_rel_type_parts": vba_rel_type_parts,
        "note": "macro parts are recorded as evidence and never executed",
    }


def _vba_rel_type_parts(archive: zipfile.ZipFile) -> list[str]:
    """Rels parts whose Relationship/@Type ends in vbaProject."""
    hits: list[str] = []
    for rel_name in archive.namelist():
        if not _RELS_PART_RE.match(rel_name):
            continue
        try:
            root = ElementTree.fromstring(archive.read(rel_name))
        except Exception:
            continue
        for rel in root.iter(f"{{{_PKG_REL_NS}}}Relationship"):
            if (rel.get("Type") or "").endswith("/vbaProject"):
                hits.append(rel_name)
                break
    return hits


# ------------------------------------------------------------
# embedded objects (word/embeddings + o:OLEObject)
# ------------------------------------------------------------

def _embedded_object_facts(doc, file_path: str | Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    ole_objects: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(file_path) as archive:
            for info in archive.infolist():
                if "/embeddings/" not in info.filename:
                    continue
                files.append(
                    {
                        "name": info.filename,
                        "size": int(info.file_size),
                    }
                )
            # Adversarial round 5 (DD5): o:OLEObject declarations can appear in any XML part
            # (footnotes/endnotes/header/footer...); scanning only the main document body
            # would make embedded objects inside footnotes invisible — the zip layer scans all XML parts
            # (rels excepted, bounded: part counts are capped by the container guard).
            for name in archive.namelist():
                if not name.endswith(".xml") or name.endswith(".rels"):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except Exception:
                    continue
                for node in root.iter(f"{{{_OFFICE_NS}}}OLEObject"):
                    ole_objects.append(
                        {
                            "prog_id": node.get("ProgID"),
                            "type": node.get("Type"),
                        }
                    )
    # Embedded-part enumeration failures are skipped (under-report rather than mis-report).
    except Exception:
        pass

    return {
        "embedded_file_count": len(files),
        "embedded_files": files,
        "ole_object_count": len(ole_objects),
        "ole_objects": ole_objects[:_MAX_EXTERNAL_TARGETS],
        "truncated": len(ole_objects) > _MAX_EXTERNAL_TARGETS,
    }


# ------------------------------------------------------------
# external data connections
# ------------------------------------------------------------

def _external_connection_facts(doc, file_path: str | Path) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    # Adversarial round 5 (DD2): external non-hyperlink relations can hang off any part's rels
    # (footnotes/header/footer/glossary...); enumerating only the main document part's
    # doc.part.rels would leave a whole layer of external links invisible — the zip layer scans all rels (container opening
    # is already guarded upstream, part counts are bounded).
    try:
        with zipfile.ZipFile(file_path) as archive:
            for rel_name in archive.namelist():
                if not _RELS_PART_RE.match(rel_name):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(rel_name))
                except Exception:
                    continue
                for rel in root.iter(
                    f"{{{_PKG_REL_NS}}}Relationship"
                ):
                    if rel.get("TargetMode") != "External":
                        continue
                    reltype = rel.get("Type") or ""
                    if reltype.endswith("/hyperlink"):
                        # Hyperlink targets are already covered by the
                        # B2 docx_links evidence; only non-hyperlink
                        # external connections belong here.
                        continue
                    targets.append(
                        {
                            "rel_type": (
                                reltype.rsplit("/", 1)[-1]
                                if reltype
                                else None
                            ),
                            "target": (rel.get("Target") or "")[:200]
                            or None,
                            "rels_part": rel_name,
                        }
                    )
    # Relation enumeration failures are skipped (observation never blocks).
    except Exception:
        pass

    return {
        "count": len(targets),
        "targets": targets[:_MAX_EXTERNAL_TARGETS],
        "truncated": len(targets) > _MAX_EXTERNAL_TARGETS,
        "note": (
            "external connection targets are recorded as evidence; "
            "no external resource is ever fetched"
        ),
    }


# ------------------------------------------------------------
# legacy & diagram visuals (VML, SmartArt declarations)
# ------------------------------------------------------------

def _legacy_visual_facts(doc) -> dict[str, Any]:
    body = doc.element.body
    pict_count = len(body.findall(f".//{{{_W_NS}}}pict"))
    vml_shape_count = len(body.findall(f".//{{{_VML_NS}}}shape"))

    smart_art_count = 0
    for graphic_data in body.findall(
        f".//{{{_DRAWINGML_A}}}graphicData"
    ):
        uri = graphic_data.get("uri") or ""
        if uri.endswith(_DIAGRAM_URI_SUFFIX):
            smart_art_count += 1

    return {
        "pict_count": pict_count,
        "vml_shape_count": vml_shape_count,
        "smart_art_count": smart_art_count,
        "note": (
            "legacy VML pictures and SmartArt diagrams are counted as "
            "declarations; their internals are not interpreted (B5/B11 "
            "scope)"
        ),
    }


# ------------------------------------------------------------
# shading obscuration (near-black run shading over text)
# ------------------------------------------------------------

# Each RGB channel must be at or below this value (0-255) for the
# fill to count as near-black.  0x1A ≈ 10% — the same near-black
# tolerance the PDF visual-obscuration channel applies to drawn
# rectangle fills.
_SHADING_CHANNEL_MAX = 0x1A


def _is_near_black_fill(fill_value: str) -> bool:
    """True when a w:shd/@w:fill hex color renders as near-black.

    ``auto`` means "no shading" and never qualifies.  Values are 6-digit
    RRGGBB (8-digit AARRGGBB tolerates an ignored alpha prefix).
    """
    value = str(fill_value or "").strip()
    if not value or value.lower() == "auto":
        return False
    if len(value) not in (6, 8):
        return False
    try:
        channels = [
            int(value[i : i + 2], 16) for i in range(0, len(value), 2)
        ]
    except ValueError:
        return False
    if len(channels) == 4:
        channels = channels[1:]  # alpha ignored — opacity is a rendering fact
    return all(channel <= _SHADING_CHANNEL_MAX for channel in channels)


def _shading_obscuration_facts(doc) -> dict[str, Any]:
    """Runs carrying near-black ``w:shd`` shading over visible text.

    This is the DOCX twin of the PDF *visual obscuration* channel: a
    black highlight/shading over a run hides the text visually, but the
    run text is fully recoverable by extraction — which is exactly why
    it is dangerous to treat as a real redaction.  Everything is
    evidence: the shaded runs stay in the narrative text verbatim.
    """
    run_count = 0
    char_count = 0
    samples: list[dict[str, Any]] = []

    try:
        body_paragraphs = list(doc.element.body.iter(qn("w:p")))
    except Exception:
        body_paragraphs = []

    for paragraph_index, paragraph in enumerate(body_paragraphs):
        for run in _runs_owned_by_paragraph(paragraph):
            r_pr = run.find(qn("w:rPr"))
            if r_pr is None:
                continue
            shd = r_pr.find(qn("w:shd"))
            if shd is None:
                continue
            fill = str(shd.get(qn("w:fill")) or "")
            if not _is_near_black_fill(fill):
                continue
            run_text = "".join(
                node.text or "" for node in run.iter(qn("w:t"))
            )
            if not run_text.strip():
                # Shading without text underneath is decoration.
                continue
            run_count += 1
            char_count += len(run_text)
            if len(samples) < _MAX_HIDDEN_SAMPLES:
                samples.append(
                    {
                        "paragraph_index": paragraph_index,
                        "fill": fill,
                        "text": run_text[:_MAX_TEXT_CHARS] or None,
                        # Shading that covers text is not true deletion: the extraction layer recovers it fully.
                        "text_recoverable": True,
                    }
                )

    return {
        "run_count": run_count,
        "char_count": char_count,
        "samples": samples,
        "note": (
            "near-black run shading (w:shd) hides text visually but the "
            "runs remain fully extractable; recorded as compliance "
            "evidence, this is not a true redaction and the narrative "
            "text is never rewritten"
        ),
    }
