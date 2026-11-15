# ============================================================
# DOCX Main Structure & Numbering (evidence-only)
# ============================================================
"""Sections, headers/footers, numbering.xml semantics and computed list
markers, explicit page/column breaks, core properties, and style
inheritance chains.

python-docx's ``paragraph.text`` never contains the automatic numbers
Word renders from ``numbering.xml``; this module parses the numbering
part (``w:abstractNum``/``w:num`` mapping, multi-level formats, restart
overrides) and simulates Word's counter semantics in document order to
recover each list item's marker text.

Everything here is evidence: computed markers attach to list blocks as
facts, section semantics and style chains are reported — nothing in the
source document is rewritten, and broken or unusual numbering is
reported, never repaired.
"""

import re
from dataclasses import dataclass
from typing import Any

from docx.oxml.ns import qn

from provendoc.core.parsers.narrative.docx_style_memo import paragraph_style

# Payload caps so pathological documents cannot flood metadata.
_MAX_TEXT_CHARS = 400
_MAX_LISTED_ITEMS = 200
_MAX_LEVELS = 9
_MAX_DEFINITIONS = 50
_MAX_STYLE_CHAINS = 50

_NUMBERING_RELTYPE_SUFFIX = "/numbering"

# Word's convention: numId "0" explicitly removes numbering.
_NO_NUMBERING_NUMID = "0"

# Private-use-area characters (Symbol/Wingdings bullet glyphs such as
# U+F0B7) are meaningless outside their font; render them as a plain
# bullet so the marker stays readable in narrative text.
_PUA_RANGE = range(0xF000, 0xF900)
_BULLET_FALLBACK = "\u2022"


# ------------------------------------------------------------
# numbering part model (w:abstractNum / w:num)
# ------------------------------------------------------------

@dataclass
class LevelDef:
    """One ``w:lvl`` of an ``w:abstractNum``."""

    ilvl: int
    num_fmt: str          # decimal | lowerRoman | bullet | none | ...
    lvl_text: str         # literal text with %1..%9 placeholders
    start: int            # first counter value (default 1)
    restart: int | None   # w:lvlRestart val; 0 = never restart


class NumberingModel:
    """Parsed numbering.xml: abstract levels, num→abstract map, overrides."""

    def __init__(self) -> None:
        """Initialize an empty numbering model."""
        self.abstracts: dict[str, dict[int, LevelDef]] = {}
        self.num_to_abstract: dict[str, str] = {}
        self.start_overrides: dict[tuple[str, int], int] = {}
        self.notes: list[str] = []

    # ----------------------------------------------------
    # construction
    # ----------------------------------------------------
    @classmethod
    def from_document(cls, doc) -> "NumberingModel":
        """Build the model from the python-docx document's numbering part."""
        model = cls()
        root = _load_numbering_root(doc)
        if root is None:
            model.notes.append("no numbering part in package")
            return model

        for abstract in root.findall(qn("w:abstractNum")):
            abstract_id = abstract.get(qn("w:abstractNumId")) or ""
            levels: dict[int, LevelDef] = {}
            for lvl in abstract.findall(qn("w:lvl")):
                try:
                    ilvl = int(lvl.get(qn("w:ilvl")))
                except (TypeError, ValueError):
                    continue
                if not 0 <= ilvl < _MAX_LEVELS:
                    continue
                start = _child_int(lvl, "w:start", default=1)
                levels[ilvl] = LevelDef(
                    ilvl=ilvl,
                    num_fmt=_child_val(lvl, "w:numFmt") or "decimal",
                    lvl_text=_child_val(lvl, "w:lvlText") or "",
                    start=start if start is not None else 1,
                    restart=_child_int(lvl, "w:lvlRestart", default=None),
                )
            if levels:
                model.abstracts[abstract_id] = levels

        for num in root.findall(qn("w:num")):
            num_id = num.get(qn("w:numId")) or ""
            abstract_ref = num.find(qn("w:abstractNumId"))
            if abstract_ref is not None:
                model.num_to_abstract[num_id] = (
                    abstract_ref.get(qn("w:val")) or ""
                )
            for override in num.findall(qn("w:lvlOverride")):
                try:
                    ilvl = int(override.get(qn("w:ilvl")))
                except (TypeError, ValueError):
                    continue
                start_el = override.find(qn("w:startOverride"))
                if start_el is None:
                    continue
                try:
                    model.start_overrides[(num_id, ilvl)] = int(
                        start_el.get(qn("w:val"))
                    )
                except (TypeError, ValueError):
                    continue

        if not model.abstracts and not model.num_to_abstract:
            model.notes.append("numbering part present but empty")
        return model

    # ----------------------------------------------------
    # resolution
    # ----------------------------------------------------
    def resolve(self, num_id: str | None, ilvl: int) -> LevelDef | None:
        """Resolve the level definition by (num_id, ilvl); None when unnumbered."""
        abstract_id = self.num_to_abstract.get(num_id or "")
        if abstract_id is None:
            return None
        return self.abstracts.get(abstract_id, {}).get(ilvl)

    def start_for(self, num_id: str, ilvl: int) -> int:
        """Start count for this numbering level (override first, default 1)."""
        override = self.start_overrides.get((num_id, ilvl))
        if override is not None:
            return override
        level = self.resolve(num_id, ilvl)
        return level.start if level is not None else 1

    def definition_count(self) -> int:
        """Number of num→abstract mappings (numbering definitions)."""
        return len(self.num_to_abstract)


def _load_numbering_root(doc):
    """Locate the numbering part through the package relationships."""
    try:
        for rel in doc.part.rels.values():
            if rel.reltype.endswith(_NUMBERING_RELTYPE_SUFFIX):
                return rel.target_part.element
    except Exception:  # pragma: no cover - defensive
        return None
    return None


# ------------------------------------------------------------
# marker rendering
# ------------------------------------------------------------

def _to_roman(value: int) -> str:
    if value <= 0:
        return str(value)
    table = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    parts = []
    remaining = value
    for amount, glyph in table:
        while remaining >= amount:
            parts.append(glyph)
            remaining -= amount
    return "".join(parts)


def _to_letters(value: int) -> str:
    if value <= 0:
        return str(value)
    letters = []
    remaining = value
    while remaining > 0:
        remaining, offset = divmod(remaining - 1, 26)
        letters.append(chr(ord("a") + offset))
    return "".join(reversed(letters))


def _format_counter(value: int, num_fmt: str) -> str:
    if num_fmt == "none":
        return ""
    if num_fmt == "lowerRoman":
        return _to_roman(value).lower()
    if num_fmt == "upperRoman":
        return _to_roman(value)
    if num_fmt == "lowerLetter":
        return _to_letters(value)
    if num_fmt == "upperLetter":
        return _to_letters(value).upper()
    # decimal and every other scheme (chineseCounting, russianLower, …)
    # render as the plain counter value — the count stays factual even
    # when the glyph style is not reproduced.
    return str(value)


def _normalize_bullet(text: str) -> str:
    if any(ord(char) in _PUA_RANGE for char in text):
        return _BULLET_FALLBACK
    return text or _BULLET_FALLBACK


class ListNumberingTracker:
    """Simulate Word's list counters in document order.

    ``feed`` is called once per numbered paragraph (body order) and
    returns the marker text Word would render: counters advance on the
    same level, a deeper entry restarts at that level's ``w:start``
    (unless ``w:lvlRestart`` says otherwise), and returning to a
    shallower level keeps its counter running.
    """

    def __init__(self, model: NumberingModel) -> None:
        """Initialize counters from the numbering model."""
        self._model = model
        self._counters: dict[str, dict[int, int]] = {}
        self._last_ilvl: dict[str, int] = {}

    def feed(self, num_id: str | None, ilvl: int) -> str | None:
        """Consume one numbered paragraph and return the numbering text Word would render; None when unnumbered."""
        if not num_id or num_id == _NO_NUMBERING_NUMID:
            return None
        level = self._model.resolve(num_id, ilvl)
        if level is None:
            return None

        counters = self._counters.setdefault(num_id, {})
        last = self._last_ilvl.get(num_id)
        start = self._model.start_for(num_id, ilvl)
        if ilvl not in counters:
            counters[ilvl] = start
        elif last is not None and ilvl > last and level.restart != 0:
            # descending into a fresh deeper level restarts at w:start,
            # unless w:lvlRestart=0 marks this level as never-restarting
            counters[ilvl] = start
        else:
            counters[ilvl] = counters[ilvl] + 1

        # Levels deeper than the current one go stale: drop them so a
        # later re-entry restarts at w:start.  A w:lvlRestart of 0 means
        # the level never restarts — keep those counters alive.
        for deeper in [k for k in counters if k > ilvl]:
            deeper_level = self._model.resolve(num_id, deeper)
            if deeper_level is not None and deeper_level.restart == 0:
                continue
            del counters[deeper]

        self._last_ilvl[num_id] = ilvl
        return self._render(num_id, counters, ilvl, level)

    def _render(
        self,
        num_id: str,
        counters: dict[int, int],
        ilvl: int,
        level: LevelDef,
    ) -> str:
        if level.num_fmt == "bullet":
            return _normalize_bullet(level.lvl_text)

        def replace(match: re.Match[str]) -> str:
            """Replace %N placeholders in lvlText with the rendered counter values."""
            target = int(match.group(1)) - 1
            if target in counters:
                value = counters[target]
            else:
                value = self._model.start_for(num_id, target)
            target_level = self._model.resolve(num_id, target)
            fmt = target_level.num_fmt if target_level is not None else "decimal"
            if fmt == "bullet" and target_level is not None:
                return _normalize_bullet(target_level.lvl_text)
            return _format_counter(value, fmt)

        return re.sub(r"%([1-9])", replace, level.lvl_text)


# ------------------------------------------------------------
# paragraph numbering reference (direct + style chain)
# ------------------------------------------------------------

def _numref_from_ppr(pPr) -> tuple[str | None, int] | None:
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return None
    num_id: str | None = None
    num_el = numPr.find(qn("w:numId"))
    if num_el is not None:
        num_id = num_el.get(qn("w:val"))
    ilvl = 0
    ilvl_el = numPr.find(qn("w:ilvl"))
    if ilvl_el is not None:
        try:
            ilvl = int(ilvl_el.get(qn("w:val")))
        except (TypeError, ValueError):
            ilvl = 0
    return num_id, ilvl


def paragraph_numref(paragraph) -> tuple[str, int] | None:
    """``(num_id, ilvl)`` declared on the paragraph or its style chain.

    A direct ``w:numPr`` wins; ``numId`` "0" is Word's explicit
    "numbering removed" and clears the reference entirely.  When the
    paragraph carries no direct numbering the style chain is walked
    (ordered-list styles usually declare their ``w:numPr`` there).
    """
    direct = _numref_from_ppr(paragraph._p.pPr)
    if direct is not None:
        num_id, ilvl = direct
        if num_id and num_id != _NO_NUMBERING_NUMID:
            return num_id, ilvl
        return None

    # paragraph.style linearly scans styles.xml on every access
    # (bug65649: 2672 styles); it goes through the shared memo — see
    # docx_style_memo.
    style = paragraph_style(paragraph)
    visited: set[int] = set()
    while style is not None and id(style) not in visited:
        visited.add(id(style))
        inherited = _numref_from_ppr(getattr(style.element, "pPr", None))
        if inherited is not None:
            num_id, ilvl = inherited
            if num_id and num_id != _NO_NUMBERING_NUMID:
                return num_id, ilvl
            return None
        style = style.base_style
    return None


# ------------------------------------------------------------
# sections (headers/footers, orientation, page numbering, geometry)
# ------------------------------------------------------------

_SECTION_PARTS = (
    "header",
    "footer",
    "first_page_header",
    "first_page_footer",
    "even_page_header",
    "even_page_footer",
)


def _child_val(node, tag: str) -> str | None:
    child = node.find(qn(tag))
    if child is None:
        return None
    return child.get(qn("w:val"))


def _child_int(node, tag: str, default: int | None = None) -> int | None:
    value = _child_val(node, tag)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _attr_int(node, name: str) -> int | None:
    """Read an integer XML attribute (``w:pgSz w:w=…`` style)."""
    value = node.get(qn(name))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sect_start_type(sectPr) -> str:
    value = _child_val(sectPr, "w:type")
    if value:
        return value
    return "nextPage"


def _sect_geometry(sectPr) -> dict[str, Any]:
    pgSz = sectPr.find(qn("w:pgSz"))
    orientation = "portrait"
    page: dict[str, Any] = {}
    if pgSz is not None:
        orientation = pgSz.get(qn("w:orient")) or "portrait"
        page = {
            "width_twips": _attr_int(pgSz, "w:w"),
            "height_twips": _attr_int(pgSz, "w:h"),
        }
    pgMar = sectPr.find(qn("w:pgMar"))
    margins: dict[str, Any] = {}
    if pgMar is not None:
        margins = {
            key: _attr_int(pgMar, tag)
            for key, tag in (
                ("top", "w:top"),
                ("bottom", "w:bottom"),
                ("left", "w:left"),
                ("right", "w:right"),
                ("gutter", "w:gutter"),
            )
        }
    return {"orientation": orientation, "page": page, "margins": margins}


def _sect_page_numbering(sectPr) -> dict[str, Any]:
    pgNumType = sectPr.find(qn("w:pgNumType"))
    if pgNumType is None:
        return {}
    return {
        "start": _attr_int(pgNumType, "w:start"),
        "format": pgNumType.get(qn("w:fmt")),
    }


def _section_part_evidence(part) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    try:
        entry["linked_to_previous"] = bool(part.is_linked_to_previous)
    except Exception:  # pragma: no cover - defensive
        entry["linked_to_previous"] = False
    if entry["linked_to_previous"]:
        return entry
    texts = []
    try:
        for paragraph in part.paragraphs:
            text = paragraph.text.strip()
            if text:
                texts.append(text)
    except Exception as error:  # pragma: no cover - defensive
        entry["note"] = f"paragraph read failed: {error}"
        return entry
    entry["paragraph_count"] = len(texts)
    entry["text"] = "\n".join(texts)[:_MAX_TEXT_CHARS]
    return entry


def extract_sections(doc) -> dict[str, Any]:
    """Per-section evidence: break type, geometry, page numbering and the
    text of every header/footer part (linked parts are marked, not
    duplicated)."""
    try:
        odd_and_even = bool(doc.settings.odd_and_even_pages_header_footer)
    except Exception:
        odd_and_even = False

    sections = []
    for index, section in enumerate(doc.sections):
        sectPr = section._sectPr
        info: dict[str, Any] = {
            "index": index,
            "start_type": _sect_start_type(sectPr),
            **_sect_geometry(sectPr),
            "page_numbering": _sect_page_numbering(sectPr),
            "different_first_page": sectPr.find(qn("w:titlePg")) is not None,
            "parts": {},
        }
        for part_name in _SECTION_PARTS:
            part = getattr(section, part_name, None)
            if part is None:
                continue
            info["parts"][part_name] = _section_part_evidence(part)
        sections.append(info)

    return {
        "section_count": len(sections),
        "odd_and_even_headers": odd_and_even,
        "sections": sections,
    }


# ------------------------------------------------------------
# explicit breaks (w:br page/column, w:pageBreakBefore)
# ------------------------------------------------------------

def count_breaks(doc) -> dict[str, Any]:
    """Count explicit page/column breaks and page-break-before marks with locations."""
    page_breaks = 0
    column_breaks = 0
    page_break_before = 0
    locations: list[dict[str, Any]] = []

    for para_index, para in enumerate(doc.paragraphs):
        for br in para._p.iter(qn("w:br")):
            break_type = br.get(qn("w:type"))
            if break_type == "page":
                page_breaks += 1
                locations.append({"para_index": para_index, "kind": "page"})
            elif break_type == "column":
                column_breaks += 1
                locations.append({"para_index": para_index, "kind": "column"})
        pPr = para._p.pPr
        if pPr is not None and pPr.find(qn("w:pageBreakBefore")) is not None:
            page_break_before += 1

    return {
        "page_break_count": page_breaks,
        "column_break_count": column_breaks,
        "page_break_before_count": page_break_before,
        "break_locations": locations[:_MAX_LISTED_ITEMS],
    }


# ------------------------------------------------------------
# core properties
# ------------------------------------------------------------

def extract_core_properties(doc) -> dict[str, Any]:
    """Extract the document core properties (title, author, timestamps, etc.)."""
    def iso(value) -> str | None:
        """Convert a datetime value to an ISO string; None passes through."""
        if value is None:
            return None
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    try:
        cp = doc.core_properties
    except Exception:  # pragma: no cover - defensive
        return {}
    return {
        "title": cp.title or None,
        "subject": cp.subject or None,
        "author": cp.author or None,
        "last_modified_by": cp.last_modified_by or None,
        "created": iso(cp.created),
        "modified": iso(cp.modified),
        "revision": cp.revision,
        "category": cp.category or None,
        "keywords": cp.keywords or None,
    }


# ------------------------------------------------------------
# style inheritance chains (basedOn evidence)
# ------------------------------------------------------------

def extract_style_chains(doc) -> dict[str, Any]:
    """For every style actually used by a paragraph: its basedOn ancestor
    chain plus whether the chain declares numbering or an outline level.

    The parser already resolves outlineLvl through basedOn; this report
    makes the rest of the inheritance visible (evidence, not a resolver).
    """
    used: dict[str, dict[str, Any]] = {}

    def walk(paragraphs) -> None:
        """Walk paragraphs, registering style names and their basedOn chain facts."""
        for paragraph in paragraphs:
            # paragraph.style linearly scans styles.xml on every access
            # (bug65649: 2672 styles); it goes through the shared memo —
            # see docx_style_memo.
            try:
                style = paragraph_style(paragraph)
            except Exception:
                continue
            if style is None:
                continue
            name = style.name or ""
            if not name or name in used:
                continue
            chain: list[str] = []
            has_numbering = False
            outline_level: int | None = None
            node = style
            visited: set[int] = set()
            while node is not None and id(node) not in visited:
                visited.add(id(node))
                chain.append(node.name or "")
                pPr = getattr(node.element, "pPr", None)
                if pPr is not None:
                    if pPr.find(qn("w:numPr")) is not None:
                        has_numbering = True
                    if outline_level is None:
                        outline_level = _child_int(pPr, "w:outlineLvl")
                        if outline_level is not None:
                            outline_level += 1
                node = node.base_style
            used[name] = {
                "name": name,
                "chain": chain,
                "has_numbering": has_numbering,
                "outline_level": outline_level,
            }

    walk(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                walk(cell.paragraphs)

    return {
        "style_count": len(used),
        "styles": list(used.values())[:_MAX_STYLE_CHAINS],
    }


# ------------------------------------------------------------
# list / table interplay
# ------------------------------------------------------------

def _list_table_interplay(doc) -> dict[str, Any]:
    numbered_in_tables = 0
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if paragraph_numref(paragraph) is not None:
                        numbered_in_tables += 1

    # A table directly following a numbered paragraph renders inside the
    # list item in Word — count top-level body occurrences.
    tables_inside_lists = 0
    last_paragraph_numbered = False
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            pPr = child.find(qn("w:pPr"))
            numPr = pPr.find(qn("w:numPr")) if pPr is not None else None
            last_paragraph_numbered = numPr is not None
        elif child.tag == qn("w:tbl"):
            if last_paragraph_numbered:
                tables_inside_lists += 1
            last_paragraph_numbered = False

    return {
        "numbered_paragraphs_in_tables": numbered_in_tables,
        "tables_inside_lists": tables_inside_lists,
    }


# ------------------------------------------------------------
# summaries (metadata payloads)
# ------------------------------------------------------------

def extract_structure_summary(doc) -> dict[str, Any]:
    """Assemble section/break/property/style evidence; never fatal."""
    summary: dict[str, Any] = {}
    for key, extractor in (
        ("sections", extract_sections),
        ("breaks", count_breaks),
        ("core_properties", extract_core_properties),
        ("style_chains", extract_style_chains),
        ("list_table_interplay", _list_table_interplay),
    ):
        try:
            summary[key] = extractor(doc)
        except Exception as error:
            summary[key] = {"notes": [f"{key} extraction failed: {error}"]}
    return summary


def numbering_summary(
    model: NumberingModel,
    list_item_count: int,
) -> dict[str, Any]:
    """The ``metadata["docx_numbering"]`` payload."""
    if not model.abstracts and not model.num_to_abstract:
        return {
            "available": False,
            "list_item_count": list_item_count,
            "notes": model.notes or ["numbering definitions unavailable"],
        }

    definitions = []
    for num_id, abstract_id in model.num_to_abstract.items():
        levels = model.abstracts.get(abstract_id, {})
        definitions.append(
            {
                "num_id": num_id,
                "abstract_num_id": abstract_id,
                "levels": [
                    {
                        "ilvl": level.ilvl,
                        "num_fmt": level.num_fmt,
                        "lvl_text": level.lvl_text,
                        "start": level.start,
                    }
                    for _, level in sorted(levels.items())
                ],
                "start_overrides": {
                    str(ilvl): start
                    for (nid, ilvl), start in model.start_overrides.items()
                    if nid == num_id
                },
            }
        )

    referenced = set(model.num_to_abstract.values())
    return {
        "available": True,
        "list_item_count": list_item_count,
        "abstract_num_count": len(model.abstracts),
        "num_count": len(model.num_to_abstract),
        "unreferenced_abstract_ids": sorted(
            abstract_id
            for abstract_id in model.abstracts
            if abstract_id not in referenced
        ),
        "definition_count": len(definitions),
        "definitions": definitions[:_MAX_DEFINITIONS],
        "notes": model.notes,
    }
