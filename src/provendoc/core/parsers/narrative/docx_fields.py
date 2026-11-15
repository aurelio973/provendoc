# ============================================================
# DOCX Fields & Links Extraction (evidence-only)
# ============================================================
"""Hyperlink relationships, bookmarks, and field instructions from DOCX.

Complex fields (``w:fldChar`` begin/separate/end runs) may span paragraphs —
a TOC field opens in the first TOC paragraph and closes after the last entry —
and nest (each TOC entry hides a PAGEREF/HYPERLINK field inside the outer
TOC field's cached result).  The extractor therefore walks paragraphs in
document order with a depth-counting state machine that survives both.

Everything here is evidence: broken references and stale cached results are
reported, never repaired.
"""

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Iterable

from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from provendoc.core.parsers.narrative.docx_style_memo import paragraph_style

# Cap recorded payload sizes so a real 200-entry TOC cannot flood metadata.
_MAX_INSTRUCTION_CHARS = 200
_MAX_RESULT_CHARS = 200
_MAX_LISTED_ITEMS = 200

_HYPERLINK_RELTYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)

# Field instruction keyword → canonical kind (first whitespace-separated
# token of the instruction, uppercased).
_FIELD_KINDS = {
    "TOC": "toc",
    "PAGE": "page",
    "NUMPAGES": "numpages",
    "REF": "ref",
    "NOTEREF": "noteref",
    "PAGEREF": "pageref",
    "SEQ": "seq",
    "DATE": "date",
    "TIME": "time",
    "PRINTDATE": "printdate",
    "SAVEDATE": "savedate",
    "CREATEDATE": "createdate",
    "INCLUDETEXT": "includetext",
    "INCLUDEPICTURE": "includepicture",
    "HYPERLINK": "hyperlink",
    "IF": "if",
    "MERGEFIELD": "mergefield",
    # Adversarial round 6 additions: DDE/DDEAUTO (DDE object injection —
    # the first instruction argument is the service program path, a classic
    # APT attachment TTP; unknown keywords would fall into "other" and lose
    # the dedicated kind count) and INCLUDEPICTURE (remote content fetch —
    # first argument is an external URL).
    "DDE": "dde",
    "DDEAUTO": "ddeauto",
}

# Heading style heuristic for "bookmark anchors a heading" reporting
# (mirrors the parser's style-name signals; report-only, so a local copy is
# acceptable instead of importing the parser module).
_HEADING_STYLE_RE = re.compile(r"(?:heading|标题)\s*([0-9])", re.IGNORECASE)
_HEADING_STYLE_PLAIN_RE = re.compile(r"^(?:heading|标题)$", re.IGNORECASE)
_TITLE_STYLE_RE = re.compile(r"^(?:title|document\s*title|标题|文档标题)$", re.IGNORECASE)

# Word-internal bookmark names (TOC anchors, footnote refs, ...).
_HIDDEN_BOOKMARK_PREFIXES = ("_Toc", "_Ref", "_Hlt", "_GoBack", "_Hlk")


@dataclass
class HyperlinkFact:
    """Hyperlink fact (external URI or internal anchor)."""

    location: str
    para_index: int
    text: str
    kind: str  # external_uri | internal_anchor
    target: str
    rel_id: str | None = None


@dataclass
class BookmarkFact:
    """Bookmark fact (TOC anchor / hidden / user bookmark)."""

    name: str
    bookmark_id: str
    location: str
    para_index: int
    kind: str  # toc_anchor | hidden | user
    wraps_text: str = ""
    cross_paragraph: bool = False
    para_style: str = ""


@dataclass
class FieldFact:
    """Field-code fact (with nesting depth and cached result)."""

    location: str
    para_index: int
    depth: int
    kind: str
    instruction: str
    cached_result: str
    target: str | None = None


@dataclass
class DocxLinkEvidence:
    """DOCX link evidence summary (hyperlinks, bookmarks, fields, notes)."""

    hyperlinks: list[HyperlinkFact] = dc_field(default_factory=list)
    bookmarks: list[BookmarkFact] = dc_field(default_factory=list)
    fields: list[FieldFact] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)


# ------------------------------------------------------------
# paragraph iteration (body + headers/footers)
# ------------------------------------------------------------

def _ordered_paragraph_elements(container_el: Any) -> list[tuple[Any, int]]:
    """(w:p element, para_index) pairs under one block container, in
    document order.

    Block-level nested containers (w:tbl table cells / w:sdt content
    controls) hide their paragraphs from python-docx's paragraphs enumeration
    (direct-child w:p only) — a DDEAUTO field or javascript: link inside a
    table/content control would be invisible to the field state machine and
    hyperlink extraction, while the safety channels (vanish/webHidden) ride
    body.iter/root.iter and include everything. Adversarial rounds 13-14:
    direct-child and nested-block paragraphs are emitted interleaved in a
    single document-order pass (concatenating direct+nested runs broke the
    document order of cross-container fields and lost them as never-closed
    notes). Direct children count sequentially (additive: their indices are
    unchanged); nested-block paragraphs inherit the index of the nearest
    preceding direct child (0 if the container comes first). Run-level
    containers (w:txbxContent text boxes) are outside the recursion — their
    content enters the state machine via the host paragraph's _p.iter()
    descendant scan; enumerating them would double-count.
    """
    ordered: list[tuple[Any, int]] = []
    direct_count = 0

    def _walk(el: Any, is_root: bool) -> None:
        nonlocal direct_count
        for child in el:
            if child.tag == qn("w:p"):
                if is_root:
                    ordered.append((child, direct_count))
                    direct_count += 1
                else:
                    ordered.append((child, max(direct_count - 1, 0)))
            elif child.tag == qn("w:tbl"):
                for row in child.findall(qn("w:tr")):
                    for cell in row.findall(qn("w:tc")):
                        _walk(cell, False)
            elif child.tag == qn("w:sdt"):
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    _walk(content, False)

    _walk(container_el, True)
    return ordered


def _paragraph_locations(doc) -> Iterable[tuple[str, int, Any]]:
    """Yield ``(location, para_index, paragraph)`` in stable document order.

    Body paragraphs carry plain indices; header/footer paragraphs are
    labelled with their section and part so PAGE/NUMPAGES/DATE fields —
    which almost always live there — stay locatable without colliding with
    body indices.  Adversarial rounds 13-14: paragraph enumeration for body
    and headers/footers extends from python-docx direct children to
    block-level nested containers (table cells / content controls); direct
    child indices unchanged, direct-child and nested-block paragraphs
    interleave in document order (cross-container fields depend on
    document-order begin/end).
    """
    for p_element, index in _ordered_paragraph_elements(doc.element.body):
        yield "body", index, Paragraph(p_element, doc._body)
    for section_index, section in enumerate(doc.sections):
        parts = (
            ("header", section.header),
            ("footer", section.footer),
            ("first_page_header", section.first_page_header),
            ("first_page_footer", section.first_page_footer),
            ("even_page_header", section.even_page_header),
            ("even_page_footer", section.even_page_footer),
        )
        seen_parts: set[int] = set()
        for part_name, part in parts:
            if part is None or id(part) in seen_parts:
                continue
            try:
                if part.is_linked_to_previous:
                    continue
            # python-docx may raise on property access to a broken part;
            # on probe failure treat it as an independent part.
            except Exception:
                pass
            seen_parts.add(id(part))
            location = f"{part_name}:sec{section_index}"
            for p_element, para_index in _ordered_paragraph_elements(
                part._element
            ):
                yield location, para_index, Paragraph(p_element, part)


# ------------------------------------------------------------
# footnote/endnote story parts (adversarial round 7 T8)
# ------------------------------------------------------------

class _StoryParagraphProxy:
    """Minimal paragraph facade for footnote/endnote story parts.

    python-docx exposes no footnote/endnote API; the w:p elements of these
    story parts need a minimal proxy to enter the existing extractors
    (hyperlinks / field state machine).
    """

    def __init__(self, p_element, part):
        self._p = p_element
        self.part = part


_STORY_PART_NAMES = {
    "word/footnotes.xml": "footnote",
    "word/endnotes.xml": "endnote",
    # Adversarial round 11: comments are a fourth whole story layer
    # (beyond r7 footnotes/endnotes and r10 headers/footers/glossary) —
    # they never join the narrative but can carry fields and hyperlinks;
    # python-docx has no comment API, only package-level enumeration.
    "word/comments.xml": "comment",
}


def _story_paragraph_locations(doc) -> Iterable[tuple[str, int, Any]]:
    """Yield ``(location, para_index, proxy)`` for story parts.

    Story parts (footnotes/endnotes; comments since adversarial round 11)
    are enumerated at the OPC package level (exact partname match); since
    adversarial round 12 glossary parts are enumerated by the
    ``word/glossary/`` prefix (glossaries can carry DDEAUTO fields and
    javascript: links). Missing/corrupt parts are skipped silently
    (observe, never block). Bookmark extraction needs a real paragraph's
    style chain and stays out (fields and hyperlinks are the adversarial
    surface inside stories).
    """
    try:
        package_parts = list(doc.part.package.iter_parts())
    except Exception:
        return
    for part in package_parts:
        # PackURI looks like '/word/footnotes.xml' (leading slash); match after normalization.
        name = str(part.partname).lstrip("/")
        label = _STORY_PART_NAMES.get(name)
        # Adversarial round 12: glossary (glossary main document and any
        # orphan parts, prefix enumeration) — no python-docx glossary API,
        # package-level only.
        if label is None and name.startswith("word/glossary/"):
            label = "glossary"
        if label is None:
            continue
        try:
            root = parse_xml(part.blob)
        except Exception:
            continue
        for para_index, p_element in enumerate(root.iter(qn("w:p"))):
            yield label, para_index, _StoryParagraphProxy(p_element, part)


# ------------------------------------------------------------
# hyperlink relationships
# ------------------------------------------------------------

def _extract_hyperlinks(paragraph, location: str, para_index: int, facts: DocxLinkEvidence) -> None:
    for hyperlink in paragraph._p.findall(qn("w:hyperlink")):
        text = "".join(
            node.text or ""
            for node in hyperlink.iter(qn("w:t"))
        ).strip()
        rel_id = hyperlink.get(qn("r:id"))
        anchor = hyperlink.get(qn("w:anchor"))

        kind = "internal_anchor"
        target = f"#{anchor}" if anchor else ""
        if rel_id:
            rels = paragraph.part.rels
            rel = rels.get(rel_id)
            if rel is not None and rel.reltype == _HYPERLINK_RELTYPE:
                if rel.is_external:
                    kind = "external_uri"
                    target = str(rel.target_ref or "")
                elif not anchor:
                    # Internal-relationship hyperlink without an anchor:
                    # keep the relationship target for auditing.
                    kind = "relationship"
                    target = str(rel.target_ref or "")
                if kind == "external_uri" and anchor:
                    # Word writes "http://…#anchor" as a single external URL;
                    # keep it as-is rather than guessing a split.
                    pass
            elif rel is None:
                facts.notes.append(
                    f"hyperlink r:id '{rel_id}' has no matching relationship"
                )
        if not target and not text:
            continue
        facts.hyperlinks.append(
            HyperlinkFact(
                location=location,
                para_index=para_index,
                text=text,
                kind=kind,
                target=target,
                rel_id=rel_id,
            )
        )


# ------------------------------------------------------------
# bookmarks
# ------------------------------------------------------------

def _bookmark_kind(name: str) -> str:
    if name.startswith("_Toc"):
        return "toc_anchor"
    for prefix in _HIDDEN_BOOKMARK_PREFIXES:
        if name.startswith(prefix):
            return "hidden"
    return "user"


def _extract_bookmarks(paragraph, location: str, para_index: int, facts: DocxLinkEvidence) -> None:
    # paragraph.style linearly rescans styles.xml on every access
    # (bug65649: 2672 styles); use the shared memo, see docx_style_memo.
    style = paragraph_style(paragraph)
    style_name = style.name if style is not None else ""
    open_bookmarks: dict[str, list[str]] = {}

    for node in paragraph._p.iter():
        tag = node.tag
        if tag == qn("w:bookmarkStart"):
            name = node.get(qn("w:name")) or ""
            bookmark_id = node.get(qn("w:id")) or ""
            if not name:
                continue
            open_bookmarks[bookmark_id] = [name]
        elif tag == qn("w:bookmarkEnd"):
            bookmark_id = node.get(qn("w:id")) or ""
            parts = open_bookmarks.pop(bookmark_id, None)
            if parts is None:
                continue
            name = parts[0]
            wraps_text = "".join(parts[1:]).strip()
            facts.bookmarks.append(
                BookmarkFact(
                    name=name,
                    bookmark_id=bookmark_id,
                    location=location,
                    para_index=para_index,
                    kind=_bookmark_kind(name),
                    wraps_text=wraps_text,
                    cross_paragraph=False,
                    para_style=style_name,
                )
            )
        elif tag == qn("w:t"):
            text = node.text or ""
            for parts in open_bookmarks.values():
                parts.append(text)

    # bookmarkStart without an end in the same paragraph: the bookmark spans
    # paragraphs; record it with whatever text precedes the paragraph end.
    for bookmark_id, parts in open_bookmarks.items():
        name = parts[0]
        wraps_text = "".join(parts[1:]).strip()
        facts.bookmarks.append(
            BookmarkFact(
                name=name,
                bookmark_id=bookmark_id,
                location=location,
                para_index=para_index,
                kind=_bookmark_kind(name),
                wraps_text=wraps_text,
                cross_paragraph=True,
                para_style=style_name,
            )
        )


# ------------------------------------------------------------
# fields (complex fldChar runs + fldSimple)
# ------------------------------------------------------------

def _classify_instruction(instruction: str) -> tuple[str, str | None]:
    """Return ``(kind, target)`` for a field instruction string."""
    tokens = instruction.strip().split()
    if not tokens:
        return "unknown", None
    keyword = tokens[0].strip('"').upper()
    kind = _FIELD_KINDS.get(keyword, "other")
    target = None
    if kind in {"ref", "pageref", "noteref"} and len(tokens) > 1:
        target = tokens[1].strip('"')
    elif kind == "seq" and len(tokens) > 1:
        target = tokens[1].strip('"')
    elif kind in {"dde", "ddeauto", "includepicture"} and len(tokens) > 1:
        # First instruction argument: for DDE/DDEAUTO it is the service
        # program path (cmd.exe carrier), for INCLUDEPICTURE a remote URL —
        # registered verbatim; no verdict, no fetching.
        target = tokens[1].strip('"')
    elif kind == "hyperlink":
        # Only local anchors derived from the \l switch enter target (they
        # participate in bookmark cross-checks); without \l, tokens[1] is an
        # external URL — treating it as target would be systematically
        # mis-reported as broken_reference in link_evidence_summary. The
        # external URL evidence is already registered via the external_uri
        # channel and the instruction text is kept verbatim — zero loss.
        match = re.search(r'\\l\s+"?([^"\s]+)"?', instruction)
        if match:
            target = match.group(1)
    return kind, target


class _FieldStateMachine:
    """Track fldChar begin/separate/end across paragraphs and nesting."""

    def __init__(self) -> None:
        """Initialize the field state machine (depth 0, no active fields)."""
        self.depth = 0
        # Active fields per nesting level; level 1 = outermost.
        self.active: dict[int, dict[str, Any]] = {}

    def feed(self, paragraph, location: str, para_index: int, facts: DocxLinkEvidence) -> None:
        """Consume one paragraph's fldSimple/fldChar nodes and register completed field facts."""
        for node in paragraph._p.iter():
            tag = node.tag
            if tag == qn("w:fldSimple"):
                self._emit_simple(node, location, para_index, facts)
            elif tag == qn("w:fldChar"):
                char_type = node.get(qn("w:fldCharType"))
                if char_type == "begin":
                    self.depth += 1
                    self.active[self.depth] = {
                        "location": location,
                        "para_index": para_index,
                        "depth": self.depth,
                        "instruction": [],
                        "result": [],
                        "in_result": False,
                    }
                elif char_type == "separate":
                    state = self.active.get(self.depth)
                    if state is not None:
                        state["in_result"] = True
                elif char_type == "end":
                    self._close(facts)
            elif tag == qn("w:instrText"):
                state = self.active.get(self.depth)
                if state is not None and not state["in_result"]:
                    state["instruction"].append(node.text or "")
            elif tag == qn("w:t"):
                # Result text contributes to every active field at or below
                # the current depth whose "separate" has been seen — that is
                # how Word caches nested results.
                for level in list(self.active):
                    state = self.active[level]
                    if state["in_result"] and level <= self.depth:
                        if len(state["result"]) < _MAX_RESULT_CHARS:
                            state["result"].append(node.text or "")

    def finish(self, facts: DocxLinkEvidence) -> None:
        """Emit unterminated fields as a note (never silently dropped)."""
        if self.active:
            facts.notes.append(
                f"{len(self.active)} field(s) never closed by fldChar end"
            )
        self.active.clear()
        self.depth = 0

    def _close(self, facts: DocxLinkEvidence) -> None:
        state = self.active.pop(self.depth, None)
        self.depth = max(0, self.depth - 1)
        if state is None:
            return
        instruction = "".join(state["instruction"]).strip()
        cached_result = "".join(state["result"]).strip()
        if not instruction and not cached_result:
            return
        kind, target = _classify_instruction(instruction)
        facts.fields.append(
            FieldFact(
                location=state["location"],
                para_index=state["para_index"],
                depth=state["depth"],
                kind=kind,
                instruction=instruction[:_MAX_INSTRUCTION_CHARS],
                cached_result=cached_result[:_MAX_RESULT_CHARS],
                target=target,
            )
        )

    def _emit_simple(self, node, location: str, para_index: int, facts: DocxLinkEvidence) -> None:
        instruction = (node.get(qn("w:instr")) or "").strip()
        cached_result = "".join(
            text.text or "" for text in node.iter(qn("w:t"))
        ).strip()
        if not instruction and not cached_result:
            return
        kind, target = _classify_instruction(instruction)
        facts.fields.append(
            FieldFact(
                location=location,
                para_index=para_index,
                depth=1,
                kind=kind,
                instruction=instruction[:_MAX_INSTRUCTION_CHARS],
                cached_result=cached_result[:_MAX_RESULT_CHARS],
                target=target,
            )
        )


# ------------------------------------------------------------
# extraction entry point
# ------------------------------------------------------------

def extract_link_evidence(doc) -> DocxLinkEvidence:
    """Extract hyperlink/bookmark/field facts from a python-docx Document."""
    facts = DocxLinkEvidence()
    state = _FieldStateMachine()
    for location, para_index, paragraph in _paragraph_locations(doc):
        try:
            _extract_hyperlinks(paragraph, location, para_index, facts)
        except Exception as error:
            facts.notes.append(f"hyperlink extraction failed ({location}#{para_index}): {error}")
        try:
            _extract_bookmarks(paragraph, location, para_index, facts)
        except Exception as error:
            facts.notes.append(f"bookmark extraction failed ({location}#{para_index}): {error}")
        try:
            state.feed(paragraph, location, para_index, facts)
        except Exception as error:
            facts.notes.append(f"field extraction failed ({location}#{para_index}): {error}")
    # Adversarial round 7 T8: footnote/endnote story parts — fed to the
    # field state machine and hyperlink extraction by the same rules
    # (bookmark extraction needs the real style chain; story proxies skip it).
    for location, para_index, paragraph in _story_paragraph_locations(doc):
        try:
            _extract_hyperlinks(paragraph, location, para_index, facts)
        except Exception as error:
            facts.notes.append(f"hyperlink extraction failed ({location}#{para_index}): {error}")
        try:
            state.feed(paragraph, location, para_index, facts)
        except Exception as error:
            facts.notes.append(f"field extraction failed ({location}#{para_index}): {error}")
    state.finish(facts)
    return facts


# ------------------------------------------------------------
# cross-checks + summary (report, never repair)
# ------------------------------------------------------------

def _is_heading_style(style_name: str) -> bool:
    if not style_name:
        return False
    return bool(
        _HEADING_STYLE_RE.search(style_name)
        or _HEADING_STYLE_PLAIN_RE.match(style_name)
        or _TITLE_STYLE_RE.match(style_name)
    )


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _result_freshness(item) -> str:
    """Derive the frozen freshness label for one field record.

    docx_field_freshness_prereg_v1 §2: only REF fields pointing at a
    bookmark with a non-empty cached result are marked
    ``mirror_compared`` — the comparison fact itself is carried by the
    existing ``field_result_mismatches`` list and is never duplicated
    here. Everything else (page-number fields, date/time fields, all
    remaining kinds) is ``unknown``, because the parse layer never
    updates fields (F5: a cached page/date result cannot be verified
    without rendering, so its freshness is simply not a fact we can
    register).
    """
    if (
        item.kind == "ref"
        and item.target
        and item.cached_result
    ):
        return "mirror_compared"
    return "unknown"


def link_evidence_summary(facts: DocxLinkEvidence) -> dict[str, Any]:
    """Build the report dict stored under ``metadata["docx_links"]``."""
    bookmark_by_name: dict[str, BookmarkFact] = {}
    for bookmark in facts.bookmarks:
        bookmark_by_name.setdefault(bookmark.name, bookmark)

    broken_references: list[dict[str, Any]] = []
    for link in facts.hyperlinks:
        if link.kind == "internal_anchor":
            anchor = link.target.lstrip("#")
            if anchor and anchor not in bookmark_by_name:
                broken_references.append(
                    {
                        "source": "hyperlink",
                        "kind": link.kind,
                        "target": link.target,
                        "text": link.text[:_MAX_RESULT_CHARS],
                        "location": link.location,
                        "para_index": link.para_index,
                    }
                )
    for item in facts.fields:
        if item.kind in {"ref", "pageref", "noteref", "hyperlink"} and item.target:
            target = item.target.lstrip("#")
            if target and target not in bookmark_by_name:
                broken_references.append(
                    {
                        "source": "field",
                        "kind": item.kind,
                        "target": target,
                        "instruction": item.instruction,
                        "location": item.location,
                        "para_index": item.para_index,
                    }
                )

    # Field cached result vs the bookmark text it claims to mirror (REF
    # only: PAGEREF caches a page number, unverifiable without pagination).
    field_result_mismatches: list[dict[str, Any]] = []
    for item in facts.fields:
        if item.kind != "ref" or not item.target:
            continue
        target_bookmark = bookmark_by_name.get(item.target)
        if (
            target_bookmark is None
            or not target_bookmark.wraps_text
            or not item.cached_result
        ):
            continue
        expected = _normalize_for_compare(target_bookmark.wraps_text)
        actual = _normalize_for_compare(item.cached_result)
        if expected != actual:
            field_result_mismatches.append(
                {
                    "target": item.target,
                    "expected": expected[:_MAX_RESULT_CHARS],
                    "cached": actual[:_MAX_RESULT_CHARS],
                    "location": item.location,
                    "para_index": item.para_index,
                }
            )

    field_kind_counts: dict[str, int] = {}
    for item in facts.fields:
        field_kind_counts[item.kind] = field_kind_counts.get(item.kind, 0) + 1

    heading_anchors = [
        {
            "name": bookmark.name,
            "kind": bookmark.kind,
            "heading_text": bookmark.wraps_text,
            "style": bookmark.para_style,
            "para_index": bookmark.para_index,
        }
        for bookmark in facts.bookmarks
        if bookmark.location == "body" and _is_heading_style(bookmark.para_style)
    ]

    external_uris = sorted(
        {link.target for link in facts.hyperlinks if link.kind == "external_uri"}
    )

    return {
        "hyperlink_count": len(facts.hyperlinks),
        "external_uri_count": sum(
            1 for link in facts.hyperlinks if link.kind == "external_uri"
        ),
        "internal_anchor_count": sum(
            1 for link in facts.hyperlinks if link.kind == "internal_anchor"
        ),
        "hyperlinks": [
            {
                "location": link.location,
                "para_index": link.para_index,
                "text": link.text,
                "kind": link.kind,
                "target": link.target,
            }
            for link in facts.hyperlinks[:_MAX_LISTED_ITEMS]
        ],
        "external_uris": external_uris[:_MAX_LISTED_ITEMS],
        "bookmark_count": len(facts.bookmarks),
        "toc_anchor_count": sum(
            1 for bookmark in facts.bookmarks if bookmark.kind == "toc_anchor"
        ),
        "heading_anchor_count": len(heading_anchors),
        "heading_anchors": heading_anchors[:_MAX_LISTED_ITEMS],
        "bookmarks": [
            {
                "name": bookmark.name,
                "kind": bookmark.kind,
                "location": bookmark.location,
                "para_index": bookmark.para_index,
                "wraps_text": bookmark.wraps_text[:_MAX_RESULT_CHARS],
                "cross_paragraph": bookmark.cross_paragraph,
            }
            for bookmark in facts.bookmarks[:_MAX_LISTED_ITEMS]
        ],
        "field_count": len(facts.fields),
        "field_kind_counts": field_kind_counts,
        "field_freshness_note": (
            "result_freshness is evidence-only: REF fields that mirror a "
            "bookmark text are marked mirror_compared (mismatches are "
            "reported in field_result_mismatches); all other cached "
            "results (page numbers, dates, …) are marked unknown "
            "because the parse layer never updates fields (F5)"
        ),
        "fields": [
            {
                "location": item.location,
                "para_index": item.para_index,
                "depth": item.depth,
                "kind": item.kind,
                "instruction": item.instruction,
                "cached_result": item.cached_result,
                "target": item.target,
                "result_freshness": _result_freshness(item),
            }
            for item in facts.fields[:_MAX_LISTED_ITEMS]
        ],
        "broken_reference_count": len(broken_references),
        "broken_references": broken_references[:_MAX_LISTED_ITEMS],
        "field_result_mismatch_count": len(field_result_mismatches),
        "field_result_mismatches": field_result_mismatches[:_MAX_LISTED_ITEMS],
        "notes": list(facts.notes),
    }


def extract_links_summary(doc) -> dict[str, Any]:
    """One-shot: extract link/field evidence and summarize for metadata."""
    try:
        facts = extract_link_evidence(doc)
    except Exception as error:  # Link evidence is never fatal to parsing.
        return {
            "hyperlink_count": 0,
            "bookmark_count": 0,
            "field_count": 0,
            "broken_reference_count": 0,
            "field_result_mismatch_count": 0,
            "notes": [f"link/field evidence unavailable: {error}"],
        }
    return link_evidence_summary(facts)
