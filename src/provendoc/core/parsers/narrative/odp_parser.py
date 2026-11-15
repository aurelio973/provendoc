# ============================================================
# Narrative Domain
# ODP (.odp/.fodp/.otp/.sti) Parser — OpenDocument Presentation
# ============================================================
# Facts:
#   - .odp/.otp/.sti are zip packages (otp/sti flagged is_template);
#     .fodp is a single flat XML file. Both share one XML walk.
#   - The mimetype part is recorded, not enforced; extension/content
#     mismatch is logged as format_mismatch, never rejected.
#   - All pages live in one content.xml part:
#     office:presentation/draw:page; page order = document order.
#   - Titles come only from presentation:class (title/subtitle,
#     first in document order); frames without class are not
#     promoted to titles.
#   - Every text:p inside a frame enters the text output
#     regardless of wrapper layer (box/object/table).
#   - No-slide packages / non-ODF flat files / missing or broken
#     content.xml -> honest failure (no text, no units).
#   - HeadingCandidate: one trusted title fact per page (same
#     contract as tex/pptx/epub).
#   - Observation keys are additive-only; caps guard pathological
#     files and every truncation is itself recorded.
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts as _build_carrier_facts,
)
from provendoc.core.diagnostics import (
    detect_security_carriers as _detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.parsers.container_guard import (
    check_zip_package_quietly as _check_zip_package_safe,
)
from provendoc.core.parsers.container_guard import (
    zip_guard_error,
)
from provendoc.core.parsers.narrative.odp_presentation_nodes import (
    derive_odp_nodes as _derive_odp_nodes,
)
from provendoc.core.parsers.narrative.shared import (
    localname as _localname,
)
from provendoc.core.structure.heading_patterns import HeadingCandidate
from provendoc.core.text.normalization import (
    sanitize_invisible_chars as _sanitize_invisible_chars,
)

# Zip container guard (zip-bomb protection), same as epub/pptx.

# ODF 1.2 namespaces needed for text extraction
_NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_NS_PRES = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
# Namespaces for object-level evidence
_NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_XLINK = "http://www.w3.org/1999/xlink"
# Annotation author / form control / RDF namespaces
_NS_DC = "urn:oasis:names:tc:opendocument:xmlns:dc:1.0"
_NS_FORM = "urn:oasis:names:tc:opendocument:xmlns:form:1.0"
_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
# meta.xml document metadata + svg:font-face-uri
# (ODF svg-compatible, distinct from the W3C SVG image namespace below)
_NS_META = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
_NS_SVG_COMPAT = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"

_CONTENT_PART = "content.xml"
_MIMETYPE_PART = "mimetype"
_RDF_PART = "META-INF/manifest.rdf"
# Manifest/styles parts (media-type mapping + font declarations)
_MANIFEST_PART = "META-INF/manifest.xml"
_STYLES_PART = "styles.xml"
# Document metadata part
_META_PART = "meta.xml"
_EXPECTED_MIMETYPE = "application/vnd.oasis.opendocument.presentation"

# Flat ODP suffix: single XML file, not a zip container.
_FLAT_SUFFIX = ".fodp"
# Template suffixes: same zip chain, is_template recorded.
_TEMPLATE_SUFFIXES = frozenset((".otp", ".sti"))

# Title evidence: presentation:class title/subtitle placeholders.
_TITLE_CLASSES = {"title", "subtitle"}

# draw:layer sample cap; over-limit lists are truncated and
# the truncation itself recorded (*_truncated).
_MAX_LAYER_SAMPLES = 50

# Standard ODF form: control localnames.
_CONTROL_LOCALNAMES = frozenset(
    (
        "text",
        "textarea",
        "checkbox",
        "radio",
        "option",
        "button",
        "listbox",
        "combobox",
        "image",
        "frame",
        "grid",
        "value-range",
        "file",
        "fixed-text",
        "hidden",
        "generic-control",
        "formatted-text",
    )
)

# chart / MathML / animation / SMIL / script / manifest / style
_NS_CHART = "urn:oasis:names:tc:opendocument:xmlns:chart:1.0"
_NS_MATHML = "http://www.w3.org/1998/Math/MathML"
_NS_ANIM = "urn:oasis:names:tc:opendocument:xmlns:animation:1.0"
_NS_SMIL = "urn:oasis:names:tc:opendocument:xmlns:smil-compatible:1.0"
_NS_SCRIPT = "urn:oasis:names:tc:opendocument:xmlns:script:1.0"
_NS_MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
_NS_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"

# Object subdocument part patterns (chart/formula/embedded input)
_OBJECT_CONTENT_RE = re.compile(r"^Object [^/]+/content\.xml$")
_OBJECT_DIR_RE = re.compile(r"^(Object [^/]+)/$")
# Chart cell cap; over-limit cells truncated and flagged.
_CHART_CELL_CAP = 10000
# Formula source caps (MathML serialization / OpenFormula text).
_FORMULA_XML_CAP = 4096
_FORMULA_TEXT_CAP = 512

# Manifest media type / root sniff -> embedded format short name
_EMBEDDED_FORMAT_BY_MEDIA_TYPE = {
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.graphics": "odg",
    "application/vnd.oasis.opendocument.chart": "odc",
    "application/vnd.oasis.opendocument.formula": "odf",
    # OLE binary embed: typed only, never parsed.
    "application/x-ole-object": "ole",
}
_EMBEDDED_FORMAT_BY_BODY = {
    "spreadsheet": "ods",
    "text": "odt",
    "presentation": "odp",
    "graphics": "odg",
}

# OLE compound-file magic (first 8 bytes).
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OBJECT_BIN_RE = re.compile(r"^Object [^/]+/[^/]+\.bin$")
# Basic macro module parts (openoffice.org/2000/script namespace,
# distinct from the ODF script: namespace).
_NS_BASIC_SCRIPT = "http://openoffice.org/2000/script"
_BASIC_MODULE_RE = re.compile(r"^Basic/[^/]+/[^/]+\.xml$")
# Animation timeline: anim:* localnames + smil: attribute names.
_ANIMATION_LOCALNAMES = frozenset((
    "par",
    "seq",
    "iterate",
    "animate",
    "animateColor",
    "animateMotion",
    "animateTransform",
    "set",
    "transitionFilter",
    "command",
    "audio",
    "video",
))
_SMIL_ATTR_NAMES = (
    "begin",
    "end",
    "dur",
    "repeatCount",
    "targetElement",
    "type",
)
# Resource caps: animation elements per page, recursion preview, macro modules.
_ANIMATION_ELEMENT_CAP = 200
_RECURSION_PREVIEW_CAP = 5
_RECURSION_TABLE_CAP = 10
_MACRO_MODULE_CAP = 50
# Page-boundary recovery via draw:page open-tag byte scan.
_RECOVERED_PAGE_OPEN_RE = re.compile(rb"<draw:page[\s>]")

# Image format sniffing / SVG text facts. Head sniff window: 512 bytes.
_NS_SVG = "http://www.w3.org/2000/svg"
_IMAGE_HEAD_WINDOW = 512
_SVG_OPEN_HEAD_RE = re.compile(rb"<svg[\s:>]", re.IGNORECASE)
# SVG text preview cap.
_SVG_TEXT_PREVIEW_CAP = 5

# parse_error free text -> reason-code mapping for the parallel
# parse_error_code key (first match wins; zip guard violations use
# the extended code ZIP_PACKAGE_GUARD_VIOLATION).
_ERROR_CODE_RULES = (
    ("package contains no content.xml", "MISSING_CONTENT_XML"),
    ("content.xml parse error", "BROKEN_ODF_XML"),
    ("flat XML parse error", "BROKEN_ODF_XML"),
    ("not an OpenDocument flat XML document", "NOT_ODF_PACKAGE"),
    ("not an OpenDocument zip package", "NOT_ODF_PACKAGE"),
    ("document has no office:presentation", "NOT_PRESENTATION"),
    ("presentation contains no pages", "EMPTY_PRESENTATION"),
)


def _error_code(message):
    """Map a parse_error message to a reason code; None if no match."""
    for fragment, code in _ERROR_CODE_RULES:
        if fragment in message:
            return code
    return None


def _sniff_image_format(head):
    """Sniff an image part header; return the format short name or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    # WMF placeable header (0x9AC6CDD7 little-endian).
    if head.startswith(b"\xd7\xcd\xc6\x9a"):
        return "wmf"
    # WMF standard header: mtType 1-3 + mtHeaderSize 9 words.
    if (
        head[:2] in (b"\x01\x00", b"\x02\x00", b"\x03\x00")
        and head[2:4] == b"\x09\x00"
    ):
        return "wmf"
    # EMR_HEADER: record type 1 + " EMF" signature at offset 40.
    if (
        len(head) >= 44
        and head[:4] == b"\x01\x00\x00\x00"
        and head[40:44] == b" EMF"
    ):
        return "emf"
    if _SVG_OPEN_HEAD_RE.search(head):
        return "svg"
    return None


def _svg_text_facts(svg_root):
    """SVG root -> text facts: <text> count + capped preview, plus
    root <title>/<desc> verbatim; no keys when empty."""
    texts = []
    for el in svg_root.iter(f"{{{_NS_SVG}}}text"):
        value = "".join(el.itertext()).strip()
        if value:
            texts.append(value)
    facts = {}
    if texts:
        facts["svg_text_paragraph_count"] = len(texts)
        facts["svg_text_preview"] = texts[:_SVG_TEXT_PREVIEW_CAP]
    title_el = svg_root.find(f"{{{_NS_SVG}}}title")
    if title_el is not None and (title_el.text or "").strip():
        facts["svg_title"] = title_el.text.strip()
    desc_el = svg_root.find(f"{{{_NS_SVG}}}desc")
    if desc_el is not None and (desc_el.text or "").strip():
        facts["svg_desc"] = desc_el.text.strip()
    return facts


class OdpParser(BaseParser):
    """ODP (.odp) parser: zipfile + ElementTree conservative extraction.

    Presentation-family parser converging with PPT/PPTX at the
    unified representation layer. Emits markdown-ish text plus one
    trusted HeadingCandidate per page; metadata carries odp_slides
    span and odp_profile identity facts. Zip-container format:
    source_text is None (same contract as pptx).
    """

    def __init__(self, encoding="utf-8", tolerant=True, include_notes=True):
        """Accept parser options and store them."""
        self.encoding = encoding
        self.tolerant = tolerant
        self.include_notes = include_notes

    # ========================================================
    # inline text extraction
    # ========================================================

    @staticmethod
    def _para_text(p):
        """text:p -> normalized text (None for an empty paragraph).

        text:span/text:a inline text flows through; text:s / tab /
        line-break expand to equivalent whitespace; annotations,
        bookmarks and change marks never enter the text.
        """
        parts = []

        def _walk(el):
            if el.text:
                parts.append(el.text)
            for child in el:
                name = _localname(child.tag)
                if name == "s":
                    try:
                        count = max(
                            1,
                            int(child.get(f"{{{_NS_TEXT}}}c", "1")),
                        )
                    except ValueError:
                        count = 1
                    parts.append(" " * count)
                elif name == "tab":
                    parts.append(" ")
                elif name == "line-break":
                    parts.append(" ")
                elif name in (
                    "annotation",
                    "annotation-end",
                    "bookmark",
                    "bookmark-start",
                    "bookmark-end",
                    "change",
                    "change-start",
                    "change-end",
                    "soft-page-break",
                ):
                    pass
                else:
                    _walk(child)
                if child.tail:
                    parts.append(child.tail)

        _walk(p)
        text = "".join(parts)
        return " ".join(text.split()) or None

    # ========================================================
    # frame content walk (document order)
    # ========================================================

    @classmethod
    def _walk_blocks(cls, el, lines, depth=0):
        """Block-level recursion: text:p -> paragraph line; text:list
        -> "- " bullet lines with 2-space nesting. Other wrapper
        layers are descended so no paragraph text is lost.
        """
        for child in el:
            tag = child.tag
            if tag == f"{{{_NS_TEXT}}}p":
                text = cls._para_text(child)
                if text:
                    lines.append(text)
            elif tag == f"{{{_NS_TEXT}}}list":
                for item in child:
                    if _localname(item.tag) not in (
                        "list-item",
                        "list-header",
                    ):
                        continue
                    cls._walk_list_item(item, lines, depth)
            else:
                cls._walk_blocks(child, lines, depth)

    @classmethod
    def _walk_list_item(cls, item, lines, depth):
        """One list item: "- " prefixed paragraphs, nested lists recursed."""
        for child in item:
            tag = child.tag
            if tag == f"{{{_NS_TEXT}}}p":
                text = cls._para_text(child)
                if text:
                    lines.append("  " * depth + "- " + text)
            elif tag == f"{{{_NS_TEXT}}}list":
                for sub in child:
                    if _localname(sub.tag) not in (
                        "list-item",
                        "list-header",
                    ):
                        continue
                    cls._walk_list_item(sub, lines, depth + 1)
            else:
                cls._walk_blocks(child, lines, depth)

    # ========================================================
    # object-level evidence
    # ========================================================

    @staticmethod
    def _href_kind(href):
        """xlink:href -> internal/external by URL scheme only
        (relative path = internal part)."""
        if not href:
            return None
        try:
            return "external" if urlparse(href).scheme else "internal"
        except ValueError:
            return "internal"

    @classmethod
    def _body_frames(cls, page):
        """draw:frame enumeration within a page, excluding the
        presentation:notes subtree (notes-channel objects are not
        double-counted as body evidence)."""
        for child in page:
            name = _localname(child.tag)
            if name == "notes":
                continue
            if name == "frame":
                yield child
            else:
                yield from cls._body_frames(child)

    @staticmethod
    def _table_shape(tbl):
        """table:table -> (row_count, cell_count, repeats).

        number-rows/columns-repeated attributes expand the counts;
        [(axis, count), ...] repeats are recorded by the caller.
        Nested tables count as their own record; covered (merged)
        cells are not counted; invalid repeat values (< 1,
        non-int) are treated as 1.
        """
        rows = 0
        cells = 0
        repeats = []

        def _repeat_of(el, attr):
            raw = el.get(f"{{{_NS_TABLE}}}{attr}")
            if raw is None:
                return 1
            try:
                value = int(raw)
            except ValueError:
                return 1
            return value if value >= 1 else 1

        def _walk(el):
            nonlocal rows, cells
            for child in el:
                name = _localname(child.tag)
                if name == "table":
                    continue
                if name == "table-row":
                    row_repeat = _repeat_of(
                        child,
                        "number-rows-repeated",
                    )
                    if row_repeat > 1:
                        repeats.append(("row", row_repeat))
                    rows += row_repeat
                    row_cells = 0
                    for cell in child:
                        if _localname(cell.tag) == "table-cell":
                            col_repeat = _repeat_of(
                                cell,
                                "number-columns-repeated",
                            )
                            if col_repeat > 1:
                                repeats.append(("column", col_repeat))
                            row_cells += col_repeat
                    cells += row_repeat * row_cells
                _walk(child)

        _walk(tbl)
        return rows, cells, repeats

    @classmethod
    def _slide_object_facts(
        cls,
        page,
        position,
        records,
        repeats=None,
        pkg=None,
        covered_parts=None,
    ):
        """Per-page object-level evidence records (image/hyperlink/
        table/notes/form_control/chart/equation/media/embedded_object)
        appended flat to `records`. Structure mirrors pptx
        _slide_object_facts with page_name in place of slide_number
        (ODF has a single part). xml_order is the document-order
        frame position; z_order is the raw draw:z-index (None when
        absent or invalid); Flat-XML office:binary-data images are
        recorded with inline_binary=True. No I/O: reuses the XML
        tree already loaded by parse().
        """
        obj_seq = 0
        page_name = page.get(f"{{{_NS_DRAW}}}name")

        def _record(kind, **fields):
            nonlocal obj_seq
            obj_seq += 1
            records.append(
                {
                    "kind": kind,
                    "slide_index": position,
                    "page_name": page_name,
                    "object_seq": obj_seq,
                    **fields,
                }
            )

        for frame_order, frame in enumerate(
            cls._body_frames(page),
            start=1,
        ):
            z_attr = frame.get(f"{{{_NS_DRAW}}}z-index")
            try:
                z_order = int(z_attr) if z_attr is not None else None
            except ValueError:
                z_order = None
            frame_facts = {
                "xml_order": frame_order,
                "z_order": z_order,
            }
            # image: draw:image with an internal part path or
            # external href, or inline office:binary-data (no href)
            for image in frame.iter(f"{{{_NS_DRAW}}}image"):
                href = image.get(f"{{{_NS_XLINK}}}href")
                inline_binary = image.find(f"{{{_NS_OFFICE}}}binary-data") is not None
                if href is None and not inline_binary:
                    continue
                # image_format from the part-header sniff; absent
                # when the header is unavailable (Flat XML inline
                # binary included). SVG parts additionally carry
                # text facts (count/preview/title/desc).
                image_facts = {}
                if href and pkg is not None:
                    image_format = (pkg.get("image_formats") or {}).get(href)
                    if image_format is not None:
                        image_facts["image_format"] = image_format
                        if image_format == "svg":
                            image_facts.update(
                                (pkg.get("svg_text_facts") or {}).get(href, {}),
                            )
                _record(
                    "image",
                    href=href,
                    href_kind=cls._href_kind(href),
                    inline_binary=inline_binary,
                    **frame_facts,
                    **image_facts,
                )
            # hyperlink: text:a carries the raw href
            for anchor in frame.iter(f"{{{_NS_TEXT}}}a"):
                href = anchor.get(f"{{{_NS_XLINK}}}href")
                if href is None:
                    continue
                _record(
                    "hyperlink",
                    href=href,
                    href_kind=cls._href_kind(href),
                    text=cls._para_text(anchor) or None,
                    **frame_facts,
                )
            # table: covers both direct-frame and draw:object-wrapped forms
            for tbl in frame.iter(f"{{{_NS_TABLE}}}table"):
                rows, cells, table_repeats = cls._table_shape(tbl)
                table_name = tbl.get(f"{{{_NS_TABLE}}}name")
                _record(
                    "table",
                    table_name=table_name,
                    row_count=rows,
                    cell_count=cells,
                    **frame_facts,
                )
                if repeats is not None:
                    for axis, repeat_count in table_repeats:
                        repeats.append(
                            {
                                "slide_index": position,
                                "page_name": page_name,
                                "table_name": table_name,
                                "axis": axis,
                                "repeat_count": repeat_count,
                                "expanded": True,
                                "source_compact": True,
                            }
                        )

            # media: draw:movie/sound/plugin, registered and
            # checked only (execution is never observed).
            for el in frame.iter(f"{{{_NS_DRAW}}}movie"):
                _record(
                    "media",
                    **cls._media_facts(el, "video", pkg),
                    **frame_facts,
                )
            for el in frame.iter(f"{{{_NS_DRAW}}}sound"):
                _record(
                    "media",
                    **cls._media_facts(el, "audio", pkg),
                    **frame_facts,
                )
            for el in frame.iter(f"{{{_NS_DRAW}}}plugin"):
                _record(
                    "media",
                    **cls._media_facts(el, "plugin", pkg),
                    **frame_facts,
                )

            # draw:object: zip Object part reference or Flat XML
            # inline subtree
            for obj in frame.iter(f"{{{_NS_DRAW}}}object"):
                (
                    obj_root,
                    source_part,
                    inline,
                    parse_error,
                ) = cls._resolve_object(obj, pkg)
                if obj_root is not None and obj_root.tag == f"{{{_NS_TABLE}}}table":
                    # table wrapper: recorded by the table branch
                    # above, not duplicated as embedded object.
                    continue
                if source_part and covered_parts is not None:
                    covered_parts.add(source_part)
                media_type = pkg["manifest"].get(f"{source_part}/") if pkg and source_part else None
                frame_has_image = frame.find(f"{{{_NS_DRAW}}}image") is not None
                if obj_root is not None and obj_root.tag == f"{{{_NS_CHART}}}chart":
                    facts = cls._chart_facts(obj_root)
                    _record(
                        "chart",
                        source_part=source_part,
                        inline=inline,
                        preview_asset=cls._preview_asset(source_part),
                        preview_present=cls._preview_present(
                            source_part,
                            pkg,
                        ),
                        data_status=("complete" if facts.get("local_data_rows") else "partial"),
                        resolution_status="complete",
                        **facts,
                        **frame_facts,
                    )
                elif obj_root is not None and (
                    obj_root.tag.startswith(f"{{{_NS_MATHML}}}") or cls._has_formula_attr(obj_root)
                ):
                    _record(
                        "equation",
                        source_part=source_part,
                        inline=inline,
                        resolution_status="complete",
                        **cls._equation_facts(obj_root),
                        **frame_facts,
                    )
                elif obj_root is not None:
                    # ODF subdocuments recurse one level (text/
                    # table summary, depth-capped); other roots
                    # stay not_attempted.
                    rec_status, recursion = cls._recursion_facts(obj_root)
                    embedded_fields = {
                        "source_part": source_part,
                        "inline": inline,
                        "embedded_format": cls._embedded_format(
                            obj_root,
                            media_type,
                        ),
                        "preview_asset": cls._preview_asset(source_part),
                        "preview_present": cls._preview_present(
                            source_part,
                            pkg,
                        ),
                        "recursive_parse_status": rec_status,
                        "resolution_status": "complete",
                        **frame_facts,
                    }
                    if recursion is not None:
                        embedded_fields["recursion"] = recursion
                    _record("embedded_object", **embedded_fields)
                elif media_type and media_type.endswith(".chart"):
                    # data unreadable but manifest types it:
                    # preview kept, partial.
                    _record(
                        "chart",
                        source_part=source_part,
                        data_status="partial",
                        resolution_status=("unresolved" if parse_error else "partial"),
                        preview_asset=cls._preview_asset(source_part),
                        preview_present=cls._preview_present(
                            source_part,
                            pkg,
                        ),
                        **frame_facts,
                    )
                elif media_type and media_type.endswith(".formula"):
                    _record(
                        "equation",
                        source_part=source_part,
                        formula_kind=("image" if frame_has_image else "unresolved"),
                        resolution_status="partial",
                        **frame_facts,
                    )
                else:
                    _record(
                        "embedded_object",
                        source_part=source_part,
                        embedded_format=(
                            _EMBEDDED_FORMAT_BY_MEDIA_TYPE.get(
                                media_type or "",
                            )
                        ),
                        resolution_status=("unresolved" if parse_error else "partial"),
                        recursive_parse_status="not_attempted",
                        # OLE binary sniffed from the CFB header;
                        # recursion stays not_attempted.
                        **cls._ole_facts(source_part, pkg),
                        **frame_facts,
                    )

        # form controls: event bindings registered, execution
        # always False; control text stays out of the page body.
        for forms_el in page.iter(f"{{{_NS_OFFICE}}}forms"):
            for control in forms_el.iter():
                if not control.tag.startswith(f"{{{_NS_FORM}}}"):
                    continue
                control_type = _localname(control.tag)
                if control_type not in _CONTROL_LOCALNAMES:
                    continue
                value = control.get(f"{{{_NS_FORM}}}value")
                if value is None:
                    value = control.get(f"{{{_NS_FORM}}}current-value")
                _record(
                    "form_control",
                    control_type=control_type,
                    name=control.get(f"{{{_NS_FORM}}}name"),
                    value=value,
                    event_binding_present=(
                        control.find(f"{{{_NS_OFFICE}}}event-listeners") is not None
                    ),
                    execution=False,
                )

        # notes presence (independent of the include_notes switch)
        if page.find(f"{{{_NS_PRES}}}notes") is not None:
            _record("notes", channel="inline_content_xml")

    # ========================================================
    # object kinds walk (chart/equation/media/embedded_object)
    # ========================================================

    @classmethod
    def _resolve_object(cls, obj, pkg):
        """draw:object -> (root|None, source_part|None, inline, parse_error).

        Zip reference (xlink:href "./Object N" -> preloaded
        subdocument root) or Flat inline child. Missing target ->
        root None; broken subdocument -> parse_error=True;
        external-scheme href -> treated as missing (no part
        evidence).
        """
        href = obj.get(f"{{{_NS_XLINK}}}href")
        if href and not urlparse(href).scheme:
            part = href[2:] if href.startswith("./") else href
            source_part = part or None
            if pkg is None:
                # Flat XML has no parts (inline is the norm)
                return None, source_part, False, False
            content_part = f"{part}/content.xml"
            if content_part not in pkg["object_roots"]:
                return None, source_part, False, False
            root = pkg["object_roots"][content_part]
            return root, source_part, False, root is None
        for child in obj:
            if isinstance(child.tag, str):
                return child, None, True, False
        return None, None, False, False

    @classmethod
    def _media_facts(cls, el, media_kind, pkg):
        """Media element -> record fields (registered, never executed).

        Raw href + kind; mime from the manifest; part_present
        checked against the zip (None without a package, False
        for external hrefs).
        """
        href = el.get(f"{{{_NS_XLINK}}}href")
        mime = pkg["manifest"].get(href) if pkg and href else None
        if pkg is None:
            part_present = None
        elif href:
            part_present = href in pkg["names"]
        else:
            part_present = False
        return {
            "media_kind": media_kind,
            "href": href,
            "href_kind": cls._href_kind(href),
            "mime": mime,
            "part_present": part_present,
            "execution": False,
        }

    @classmethod
    def _chart_facts(cls, chart_root):
        """chart:chart -> data-plane fields.

        chart:class type; aggregated title; series/categories
        cell-range verbatim; local table:table values with the
        cell cap (truncation flagged). External data references
        are kept verbatim, never expanded.
        """
        chart_class = chart_root.get(f"{{{_NS_CHART}}}class")
        chart_type = chart_class.rsplit(":", 1)[-1] if chart_class else None
        title = None
        title_el = chart_root.find(f"{{{_NS_CHART}}}title")
        if title_el is not None:
            paras = [cls._para_text(p) for p in title_el.iter(f"{{{_NS_TEXT}}}p")]
            title = " ".join(t for t in paras if t).strip() or None
        series = []
        for s_el in chart_root.iter(f"{{{_NS_CHART}}}series"):
            series.append(
                {
                    "values_cell_range_address": s_el.get(
                        f"{{{_NS_CHART}}}values-cell-range-address"
                    ),
                    "label_cell_range_address": s_el.get(
                        f"{{{_NS_CHART}}}label-cell-range-address"
                    ),
                }
            )
        categories_cell_range = None
        for c_el in chart_root.iter(f"{{{_NS_CHART}}}categories"):
            # ODF canonical table:cell-range-address; chart:
            # prefix variant as fallback.
            categories_cell_range = c_el.get(f"{{{_NS_TABLE}}}cell-range-address") or c_el.get(
                f"{{{_NS_CHART}}}values-cell-range-address"
            )
            break
        local_rows = []
        cells_seen = 0
        truncated = False
        table_el = chart_root.find(f"{{{_NS_TABLE}}}table")
        if table_el is not None:
            for row in table_el.iter(f"{{{_NS_TABLE}}}table-row"):
                row_values = []
                for cell in row:
                    if _localname(cell.tag) != "table-cell":
                        continue
                    if cells_seen >= _CHART_CELL_CAP:
                        truncated = True
                        break
                    paras = [cls._para_text(p) for p in cell.iter(f"{{{_NS_TEXT}}}p")]
                    row_values.append(" ".join(t for t in paras if t))
                    cells_seen += 1
                if truncated:
                    break
                local_rows.append(row_values)
        facts = {
            "chart_type": chart_type,
            "title": title,
            "series": series,
            "categories_cell_range": categories_cell_range,
            "local_data_truncated": truncated,
        }
        # No local data -> key absent (data_status=partial
        # already conveys it).
        if local_rows:
            facts["local_data_rows"] = local_rows
        return facts

    @classmethod
    def _equation_facts(cls, root):
        """Formula root (MathML | OpenFormula document) -> fields.

        MathML serialization and OpenFormula text are capped
        with truncated flags; formula_kind is normalized here
        (mathml / native_openformula).
        """
        if root.tag.startswith(f"{{{_NS_MATHML}}}"):
            xml_text = ElementTree.tostring(
                root,
                encoding="unicode",
            )
            return {
                "formula_kind": "mathml",
                "formula_xml": xml_text[:_FORMULA_XML_CAP],
                "formula_xml_truncated": (len(xml_text) > _FORMULA_XML_CAP),
            }
        raw = "\n".join(t for t in (cls._para_text(p) for p in root.iter(f"{{{_NS_TEXT}}}p")) if t)
        return {
            "formula_kind": "native_openformula",
            "formula_text": raw[:_FORMULA_TEXT_CAP],
            "formula_text_truncated": (len(raw) > _FORMULA_TEXT_CAP),
        }

    @classmethod
    def _has_formula_attr(cls, root):
        """OpenFormula detection: any text:formula attribute in the tree."""
        for el in root.iter(f"{{{_NS_TEXT}}}p"):
            if el.get(f"{{{_NS_TEXT}}}formula") is not None:
                return True
        return False

    @classmethod
    def _embedded_format(cls, root, media_type):
        """Embedded format short name: manifest media type first,
        root sniff fallback."""
        fmt = _EMBEDDED_FORMAT_BY_MEDIA_TYPE.get(media_type or "")
        if fmt:
            return fmt
        if root is None:
            return None
        if _localname(root.tag) in ("document-content", "document"):
            body = root.find(f"{{{_NS_OFFICE}}}body")
            if body is not None:
                for child in body:
                    body_local = _localname(child.tag)
                    if body_local in _EMBEDDED_FORMAT_BY_BODY:
                        return _EMBEDDED_FORMAT_BY_BODY[body_local]
        return None

    @staticmethod
    def _preview_asset(source_part):
        """Object part -> ObjectReplacements preview path (None
        without a part)."""
        if not source_part:
            return None
        return f"ObjectReplacements/{source_part}"

    @staticmethod
    def _preview_present(source_part, pkg):
        """Preview part presence check (None without a package)."""
        if pkg is None:
            return None
        if not source_part:
            return False
        return f"ObjectReplacements/{source_part}" in pkg["names"]

    @classmethod
    def _manifest_object_facts(cls, pkg, records, covered_parts):
        """Register Object parts not covered by frame references.

        Orphan or unreadable Object parts must still be recorded:
        chart/formula media types get partial status with preview
        kept; recursion stays not_attempted; page-level fields
        (slide_index/page_name/object_seq) are None. Zip packages
        only (Flat XML has no manifest).
        """
        if not pkg:
            return
        for full_path in sorted(pkg["manifest"]):
            match = _OBJECT_DIR_RE.match(full_path)
            if not match:
                continue
            part = match.group(1)
            if part in covered_parts:
                continue
            media_type = pkg["manifest"].get(full_path)
            content_part = f"{part}/content.xml"
            root = pkg["object_roots"].get(content_part)
            part_broken = content_part in pkg["object_roots"] and root is None
            preview_present = f"ObjectReplacements/{part}" in pkg["names"]
            resolution = (
                "complete" if root is not None else ("unresolved" if part_broken else "partial")
            )
            common = {
                "source_part": part,
                "preview_asset": f"ObjectReplacements/{part}",
                "preview_present": preview_present,
                "recursive_parse_status": "not_attempted",
            }
            is_chart = (media_type or "").endswith(".chart") or (
                root is not None and root.tag == f"{{{_NS_CHART}}}chart"
            )
            is_formula = (
                (media_type or "").endswith(".formula")
                or (root is not None and root.tag.startswith(f"{{{_NS_MATHML}}}"))
                or (root is not None and cls._has_formula_attr(root))
            )
            if is_chart:
                facts = (
                    cls._chart_facts(root)
                    if root is not None and root.tag == f"{{{_NS_CHART}}}chart"
                    else {}
                )
                records.append(
                    {
                        "kind": "chart",
                        "slide_index": None,
                        "page_name": None,
                        "object_seq": None,
                        **common,
                        "data_status": ("complete" if facts.get("local_data_rows") else "partial"),
                        "resolution_status": resolution,
                        **facts,
                    }
                )
            elif is_formula:
                facts = (
                    cls._equation_facts(root)
                    if root is not None
                    and (root.tag.startswith(f"{{{_NS_MATHML}}}") or cls._has_formula_attr(root))
                    else {}
                )
                records.append(
                    {
                        "kind": "equation",
                        "slide_index": None,
                        "page_name": None,
                        "object_seq": None,
                        **common,
                        "resolution_status": resolution,
                        **facts,
                    }
                )
            else:
                records.append(
                    {
                        "kind": "embedded_object",
                        "slide_index": None,
                        "page_name": None,
                        "object_seq": None,
                        **common,
                        "embedded_format": cls._embedded_format(
                            root,
                            media_type,
                        ),
                        "resolution_status": resolution,
                    }
                )

    # ========================================================
    # advanced recovery
    # ========================================================

    @classmethod
    def _recover_pages(cls, content_bytes):
        r"""Recover pages from broken content.xml via byte scanning.

        `<draw:page[\s>]` open tags bound the fragments; one
        containing a closing `</draw:page>` is a complete page.
        Root xmlns declarations are injected into the fragment
        root tag before re-parsing. Returns ({page position:
        element}, [lost positions]).
        """
        opens = [m.start() for m in _RECOVERED_PAGE_OPEN_RE.finditer(content_bytes)]
        if not opens:
            return {}, []
        root_m = re.search(rb"<[\w:.-]+[ >]", content_bytes)
        ns_decls = b""
        if root_m is not None:
            root_tag = content_bytes[: content_bytes.find(b">", root_m.start()) + 1]
            ns_decls = b" ".join(
                m.group(0)
                for m in re.finditer(rb'xmlns:[\w.-]+="[^"]*"', root_tag)
            )
        recovered = {}
        lost = []
        for idx, start in enumerate(opens, start=1):
            end = opens[idx] if idx < len(opens) else len(content_bytes)
            chunk = content_bytes[start:end]
            close = chunk.rfind(b"</draw:page>")
            if close == -1:
                lost.append(idx)
                continue
            frag = chunk[: close + len(b"</draw:page>")]
            gt = frag.find(b">")
            if ns_decls and gt != -1:
                frag = frag[:gt] + b" " + ns_decls + frag[gt:]
            try:
                page = ElementTree.fromstring(frag)
            except ElementTree.ParseError:
                lost.append(idx)
                continue
            if page.tag != f"{{{_NS_DRAW}}}page":
                lost.append(idx)
                continue
            recovered[idx] = page
        return recovered, lost

    @staticmethod
    def _rebuild_recovered_root(recovered, lost):
        """Rebuild a synthetic office:document-content root from
        recovered pages, with empty draw:page placeholders at
        lost positions."""
        presentation = ElementTree.Element(f"{{{_NS_OFFICE}}}presentation")
        for position in range(1, len(recovered) + len(lost) + 1):
            element = recovered.get(position)
            if element is None:
                element = ElementTree.Element(f"{{{_NS_DRAW}}}page")
            presentation.append(element)
        body = ElementTree.Element(f"{{{_NS_OFFICE}}}body")
        body.append(presentation)
        root = ElementTree.Element(f"{{{_NS_OFFICE}}}document-content")
        root.append(body)
        return root

    @staticmethod
    def _ole_facts(source_part, pkg):
        """OLE binary part CFB sniff: typed only, never parsed."""
        if not pkg or not source_part:
            return {}
        prefix = f"{source_part}/"
        for name in sorted(pkg.get("object_binaries", {})):
            if name.startswith(prefix) and pkg["object_binaries"][name] == _CFB_MAGIC:
                return {"ole_container": "cfb", "ole_binary_part": name}
        return {}

    @classmethod
    def _recursion_facts(cls, root):
        """One-level recursion summary for an ODF subdocument root.

        Text/table facets are summarized into the recursion
        object (depth capped at one level); non-ODF roots return
        (not_attempted, None). Returns (recursive_parse_status,
        recursion | None).
        """
        if _localname(root.tag) not in ("document-content", "document"):
            return "not_attempted", None
        recursion = {"depth": 1}
        body = root.find(f"{{{_NS_OFFICE}}}body")
        if body is not None:
            for child in body:
                body_local = _localname(child.tag)
                if body_local:
                    recursion["child_document_type"] = body_local
                    break
        paras = [t for t in (cls._para_text(p) for p in root.iter(f"{{{_NS_TEXT}}}p")) if t]
        recursion["text_paragraph_count"] = len(paras)
        preview = paras[:_RECURSION_PREVIEW_CAP]
        if preview:
            recursion["text_preview"] = preview
        tables = []
        table_count = 0
        summaries_truncated = False
        for tbl in root.iter(f"{{{_NS_TABLE}}}table"):
            table_count += 1
            if len(tables) < _RECURSION_TABLE_CAP:
                rows, cells, _ = cls._table_shape(tbl)
                tables.append(
                    {
                        "table_name": tbl.get(f"{{{_NS_TABLE}}}name"),
                        "row_count": rows,
                        "cell_count": cells,
                    },
                )
            else:
                summaries_truncated = True
        if table_count:
            recursion["table_count"] = table_count
            recursion["table_summaries"] = tables
            if summaries_truncated:
                recursion["table_summaries_truncated"] = True
        return "parsed", recursion

    @classmethod
    def _animation_facts(cls, page):
        """Static walk of anim:* timeline elements.

        Element localnames + raw smil: attribute values;
        playback is never simulated. Page-level cap with
        truncated flag. Returns (elements, truncated).
        """
        elements = []
        truncated = False
        for el in page.iter():
            if not el.tag.startswith(f"{{{_NS_ANIM}}}"):
                continue
            local = _localname(el.tag)
            if local not in _ANIMATION_LOCALNAMES:
                continue
            if len(elements) >= _ANIMATION_ELEMENT_CAP:
                truncated = True
                break
            entry = {"element": local}
            for attr in _SMIL_ATTR_NAMES:
                value = el.get(f"{{{_NS_SMIL}}}{attr}")
                if value is not None:
                    entry[attr] = value
            elements.append(entry)
        return elements, truncated

    _MACRO_PROCEDURE_RE = re.compile(
        r"^(?:Public\s+|Private\s+)?(?:Sub|Function|Property)\s+(\w+)",
        re.MULTILINE,
    )

    @classmethod
    def _macro_module_facts(cls, module):
        """basic:module -> static registration facts.

        Module name/language/procedures/line counts; the source
        text itself is never recorded.
        """
        source = module.get("source") or ""
        facts = {
            "module_name": module.get("module_name"),
            "procedures": cls._MACRO_PROCEDURE_RE.findall(source),
            "source_line_count": len(source.strip().splitlines()),
        }
        if module.get("language"):
            facts["language"] = module["language"]
        if module.get("part"):
            facts["source_part"] = module["part"]
        return facts

    # ========================================================
    # page rendering (pptx text contract)
    # ========================================================

    @classmethod
    def _render(cls, pages, include_notes=True, pkg=None, lost_pages=frozenset()):
        """Pages -> (text, headings, slides_meta, stats).

        Each page renders as "## Slide N: {title}" plus
        document-order paragraph/bullet lines with blank
        separators; notes append a "Notes:" paragraph (pptx
        shape). N is the XML document-order position. Lost pages
        render only the UNRESOLVED:SLIDE:N marker — no heading,
        no text evidence, no HeadingCandidate.
        """
        lines = []  # [(text, is_heading)]
        headings = []
        slide_starts = []  # [(position, page_name, title, start)]
        stats = {"slides": 0, "titles": 0, "notes": 0}

        for position, page in enumerate(pages, start=1):
            if position in lost_pages:
                # lost page marker: the odp_slides entry is
                # still created, without title/page_name evidence.
                marker_line = f'[["UNRESOLVED:SLIDE:{position}"]]'
                start = sum(len(t) + 1 for t, _ in lines)
                slide_starts.append((position, None, None, start))
                lines.append((marker_line, False))
                lines.append(("", False))
                stats["slides"] += 1
                continue
            page_name = page.get(f"{{{_NS_DRAW}}}name")
            # title: first title/subtitle-class frame in
            # document order; its paragraphs are consumed as the
            # title, not duplicated into the body.
            title = None
            title_frame = None
            for frame in page.findall(f"{{{_NS_DRAW}}}frame"):
                pclass = frame.get(f"{{{_NS_PRES}}}class")
                if pclass in _TITLE_CLASSES:
                    title_frame = frame
                    paras = [cls._para_text(p) for p in frame.iter(f"{{{_NS_TEXT}}}p")]
                    title = " ".join(p for p in paras if p).strip() or None
                    break

            body_lines = []
            for frame in page.findall(f"{{{_NS_DRAW}}}frame"):
                if frame is title_frame:
                    continue
                # chart/equation frame markers enter the text;
                # media and embedded objects stay metadata-only.
                marker = cls._frame_marker(frame, pkg)
                if marker:
                    body_lines.append(marker)
                cls._walk_blocks(frame, body_lines)

            # speaker notes (include_notes switch)
            notes_text = None
            if include_notes:
                notes_el = page.find(f"{{{_NS_PRES}}}notes")
                if notes_el is not None:
                    paras = [cls._para_text(p) for p in notes_el.iter(f"{{{_NS_TEXT}}}p")]
                    notes_text = " ".join(p for p in paras if p).strip() or None

            heading_line = f"## Slide {position}: {title}" if title else f"## Slide {position}"
            start = sum(len(t) + 1 for t, _ in lines)
            slide_starts.append((position, page_name, title, start))
            headings.append(
                HeadingCandidate(
                    raw=title or f"Slide {position}",
                    pattern="markdown",
                    depth_hint=2,
                    start=start,
                    end=start + len(heading_line),
                )
            )
            lines.append((heading_line, True))
            for line in body_lines:
                lines.append((line, False))
            lines.append(("", False))
            if notes_text:
                stats["notes"] += 1
                lines.append((f"Notes: {notes_text}", False))
                lines.append(("", False))

            stats["slides"] += 1
            if title:
                stats["titles"] += 1

        text = "\n".join(t for t, _ in lines)

        # spans tile the whole text: page i ends where page
        # i+1 starts, so every offset belongs to exactly one page.
        slides_meta = []
        for i, (position, page_name, title, start) in enumerate(
            slide_starts,
        ):
            end = slide_starts[i + 1][3] if i + 1 < len(slide_starts) else len(text)
            slides_meta.append(
                {
                    "slide_index": position,
                    "page_name": page_name,
                    "title": title,
                    "start": start,
                    "end": end,
                }
            )
        return text, headings, slides_meta, stats

    @classmethod
    def _frame_marker(cls, frame, pkg):
        """chart/equation frame -> text marker.

        "[Chart: {title}]" / "[Formula: kind]"; a missing data
        face adds "(data_status=partial)". Media and embedded
        objects produce no marker; table-wrapped objects neither
        (table content renders as text).
        """
        obj = frame.find(f"{{{_NS_DRAW}}}object")
        if obj is None:
            return None
        root, source_part, _inline, _err = cls._resolve_object(obj, pkg)
        if root is not None and root.tag == f"{{{_NS_TABLE}}}table":
            return None
        if root is not None and root.tag == f"{{{_NS_CHART}}}chart":
            facts = cls._chart_facts(root)
            marker = f"[Chart: {facts['title']}]" if facts["title"] else "[Chart]"
            if not facts.get("local_data_rows"):
                marker += " (data_status=partial)"
            return marker
        if root is not None and root.tag.startswith(f"{{{_NS_MATHML}}}"):
            return "[Formula: mathml]"
        if root is not None and cls._has_formula_attr(root):
            return "[Formula: native_openformula]"
        if root is not None:
            return None
        media_type = pkg["manifest"].get(f"{source_part}/") if pkg and source_part else None
        if media_type and media_type.endswith(".chart"):
            return "[Chart] (data_status=partial)"
        if media_type and media_type.endswith(".formula"):
            frame_has_image = frame.find(f"{{{_NS_DRAW}}}image") is not None
            return f"[Formula: {'image' if frame_has_image else 'unresolved'}]"
        return None

    # ========================================================
    # container loading (zip / Flat XML)
    # ========================================================

    @staticmethod
    def _load_zip_document(file_path):
        """zip package -> (root, mimetype, rdf_bytes | None, pkg | None).

        pkg preloads the manifest media-type map, the name set,
        and Object N/content.xml subdocument roots (broken ->
        None placeholder), shared by the object/security/font
        facets with no second open of the archive.
        """
        try:
            zf = zipfile.ZipFile(file_path)
        except zipfile.BadZipFile as exc:
            raise ValueError(
                "not an OpenDocument zip package (flat XML or corrupt file): " + str(exc)
            ) from exc
        with zf:
            names = set(zf.namelist())
            if _CONTENT_PART not in names:
                raise ValueError("package contains no content.xml")
            mimetype = None
            if _MIMETYPE_PART in names:
                raw = zf.read(_MIMETYPE_PART)[:256]
                mimetype = raw.decode("ascii", errors="replace").strip()
            rdf_bytes = zf.read(_RDF_PART) if _RDF_PART in names else None
            content_bytes = zf.read(_CONTENT_PART)
            # manifest media types; a broken manifest is
            # skipped (a missing facet must not fail the parse).
            manifest = {}
            if _MANIFEST_PART in names:
                try:
                    manifest_root = ElementTree.fromstring(zf.read(_MANIFEST_PART))
                except ElementTree.ParseError:
                    manifest_root = None
                if manifest_root is not None:
                    for entry in manifest_root.iter(
                        f"{{{_NS_MANIFEST}}}file-entry",
                    ):
                        full_path = entry.get(f"{{{_NS_MANIFEST}}}full-path")
                        if full_path:
                            manifest[full_path] = entry.get(f"{{{_NS_MANIFEST}}}media-type")
            # Object subdocument roots preloaded; broken ones
            # stored as None to distinguish from missing parts.
            object_roots = {}
            for name in names:
                if _OBJECT_CONTENT_RE.match(name):
                    try:
                        object_roots[name] = ElementTree.fromstring(zf.read(name))
                    except ElementTree.ParseError:
                        object_roots[name] = None
            # OLE binary part heads: only the 8-byte magic
            # is read, never the full payload.
            object_binaries = {}
            for name in sorted(names):
                if not _OBJECT_BIN_RE.match(name):
                    continue
                try:
                    with zf.open(name) as fp:
                        object_binaries[name] = fp.read(8)
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
            # Basic macro library modules: basic:module roots
            # carry source text; descriptor parts have different
            # tags and are naturally excluded.
            basic_modules = []
            for name in sorted(names):
                if not _BASIC_MODULE_RE.match(name):
                    continue
                try:
                    mod_root = ElementTree.fromstring(zf.read(name))
                except ElementTree.ParseError:
                    continue
                if mod_root.tag == f"{{{_NS_BASIC_SCRIPT}}}module":
                    basic_modules.append(
                        {
                            "part": name,
                            "module_name": mod_root.get(
                                f"{{{_NS_BASIC_SCRIPT}}}name",
                            ),
                            "language": mod_root.get(
                                f"{{{_NS_BASIC_SCRIPT}}}language",
                            ),
                            "source": mod_root.text or "",
                        },
                    )
            # image part heads: manifest image/* or Pictures/
            # entries sniffed (512 B); SVG parts then fully
            # parsed for text facts. Broken parts keep their
            # sniffed format with text facts absent; unknown
            # format -> no key.
            image_formats = {}
            svg_text_facts = {}
            for name in sorted(names):
                media_type = manifest.get(name)
                if not (
                    name.startswith("Pictures/")
                    or (media_type or "").startswith("image/")
                ):
                    continue
                try:
                    with zf.open(name) as fp:
                        head = fp.read(_IMAGE_HEAD_WINDOW)
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
                image_format = _sniff_image_format(head)
                if image_format is None:
                    continue
                image_formats[name] = image_format
                if image_format == "svg":
                    try:
                        svg_root = ElementTree.fromstring(zf.read(name))
                    except ElementTree.ParseError:
                        continue
                    part_facts = _svg_text_facts(svg_root)
                    if part_facts:
                        svg_text_facts[name] = part_facts
            # styles.xml root for font declarations; broken -> skipped.
            styles_root = None
            if _STYLES_PART in names:
                try:
                    styles_root = ElementTree.fromstring(zf.read(_STYLES_PART))
                except ElementTree.ParseError:
                    styles_root = None
            # meta.xml root, same silent degradation.
            meta_root = None
            if _META_PART in names:
                try:
                    meta_root = ElementTree.fromstring(zf.read(_META_PART))
                except ElementTree.ParseError:
                    meta_root = None
            pkg = {
                "names": names,
                "manifest": manifest,
                "object_roots": object_roots,
                "styles_root": styles_root,
                "meta_root": meta_root,
                "object_binaries": object_binaries,
                "basic_modules": basic_modules,
                "image_formats": image_formats,
                "svg_text_facts": svg_text_facts,
            }

        try:
            root = ElementTree.fromstring(content_bytes)
        except ElementTree.ParseError as exc:
            # broken content.xml: root=None + raw bytes/error
            # carried in pkg for the page-scan recovery chain;
            # with no recoverable page the same honest failure
            # is restored.
            pkg["content_bytes"] = content_bytes
            pkg["content_parse_error"] = f"content.xml parse error: {exc}"
            return None, mimetype, rdf_bytes, pkg
        return root, mimetype, rdf_bytes, pkg

    @staticmethod
    def _load_flat_document(file_path):
        """Flat file -> (root, mimetype | None) for Flat ODP (.fodp).

        Root element office:document (office:mimetype identity
        attribute) or office:document-content (no mimetype
        evidence -> None); anything else -> honest failure.
        """
        raw = Path(file_path).read_bytes()
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise ValueError(
                f"not an OpenDocument zip package (flat XML parse error): {exc}"
            ) from exc
        if not root.tag.startswith(f"{{{_NS_OFFICE}}}") or _localname(root.tag) not in (
            "document",
            "document-content",
        ):
            raise ValueError(f"not an OpenDocument flat XML document (root element is {root.tag})")
        mimetype = (
            root.get(f"{{{_NS_OFFICE}}}mimetype")
            if root.tag == f"{{{_NS_OFFICE}}}document"
            else None
        )
        return root, mimetype

    # ========================================================
    # annotations / tracked changes / RDF
    # ========================================================

    @staticmethod
    def _annotation_facts(pages):
        """office:annotation -> odp_annotations records.

        author (dc:creator) / date (dc:date) / aggregated text;
        never mixed into the page body.
        """
        records = []
        for position, page in enumerate(pages, start=1):
            page_name = page.get(f"{{{_NS_DRAW}}}name")
            for ann in page.iter(f"{{{_NS_OFFICE}}}annotation"):
                creator = ann.find(f"{{{_NS_DC}}}creator")
                date = ann.find(f"{{{_NS_DC}}}date")
                texts = [
                    t for t in (OdpParser._para_text(p) for p in ann.iter(f"{{{_NS_TEXT}}}p")) if t
                ]
                records.append(
                    {
                        "slide_index": position,
                        "page_name": page_name,
                        "author": (creator.text if creator is not None else None),
                        "date": date.text if date is not None else None,
                        "text": " ".join(texts) or None,
                    }
                )
        return records

    @staticmethod
    def _change_facts(root):
        """text:tracked-changes -> odp_changes records.

        insertion/deletion kind + change-info author/date; the
        affected paragraph text is recorded verbatim — deleted
        content never returns to the body, but must be
        registered.
        """
        records = []
        for region in root.iter(f"{{{_NS_TEXT}}}changed-region"):
            region_id = region.get(f"{{{_NS_TEXT}}}id")
            for child in region:
                kind = _localname(child.tag)
                if kind not in ("insertion", "deletion"):
                    continue
                info = child.find(f"{{{_NS_OFFICE}}}change-info")
                creator = info.find(f"{{{_NS_DC}}}creator") if info is not None else None
                date = info.find(f"{{{_NS_DC}}}date") if info is not None else None
                # affected paragraphs recorded verbatim: deleted
                # content stays out of the body but must not
                # vanish unregistered.
                texts = [
                    t
                    for t in (
                        OdpParser._para_text(p)
                        for p in child.iter(f"{{{_NS_TEXT}}}p")
                    )
                    if t
                ]
                records.append(
                    {
                        "change_id": region_id,
                        "kind": kind,
                        "author": (creator.text if creator is not None else None),
                        "date": date.text if date is not None else None,
                        "text": " ".join(texts) or None,
                    }
                )
        return records

    @staticmethod
    def _layer_facts(root, pages):
        """draw:layer declarations and references -> odp_layers.

        Declared names (with draw:protected when present) and
        reference counts, recorded verbatim; visibility
        semantics are never inferred. Returns None when empty.
        """
        declared = []
        for layer in root.iter(f"{{{_NS_DRAW}}}layer"):
            name = layer.get(f"{{{_NS_DRAW}}}name")
            if not name:
                continue
            record = {"name": name}
            protected = layer.get(f"{{{_NS_DRAW}}}protected")
            if protected is not None:
                record["protected"] = protected
            declared.append(record)
        references: dict[str, int] = {}
        for page in pages:
            for el in page.iter():
                ref = el.get(f"{{{_NS_DRAW}}}layer")
                if ref:
                    references[ref] = references.get(ref, 0) + 1
        if not declared and not references:
            return None
        facts = {"note": (
            "layer declarations and references recorded as raw "
            "facts; visibility semantics are never inferred"
        )}
        if declared:
            facts["declared"] = declared[:_MAX_LAYER_SAMPLES]
            if len(declared) > _MAX_LAYER_SAMPLES:
                facts["declared_truncated"] = True
        if references:
            facts["referenced_names"] = dict(
                sorted(references.items())[:_MAX_LAYER_SAMPLES]
            )
            if len(references) > _MAX_LAYER_SAMPLES:
                facts["references_truncated"] = True
        return facts

    @staticmethod
    def _rdf_facts(rdf_bytes):
        """manifest.rdf bytes -> odp_rdf facts (minimal structure).

        Triples never enter the page body; only presence +
        description count; broken -> parse_error flag, no
        invented values.
        """
        facts = {"part": _RDF_PART, "present": True}
        try:
            rdf_root = ElementTree.fromstring(rdf_bytes)
        except ElementTree.ParseError:
            facts["parse_error"] = True
            return facts
        facts["description_count"] = sum(1 for _ in rdf_root.iter(f"{{{_NS_RDF}}}Description"))
        return facts

    @staticmethod
    def _meta_facts(meta_el):
        """office:meta element -> odp_meta facts (verbatim).

        Same contract for zip (meta.xml office:meta subtree) and
        Flat (single-XML office:meta child). All fields stay
        verbatim strings (editing_cycles is not int-coerced;
        document_statistic attributes keyed by localname).
        Returns None when empty or absent.
        """
        if meta_el is None:
            return None
        facts = {}

        def _text(tag):
            el = meta_el.find(tag)
            return el.text if el is not None else None

        generator = _text(f"{{{_NS_META}}}generator")
        if generator:
            facts["generator"] = generator
        title = _text(f"{{{_NS_DC}}}title")
        if title:
            facts["title"] = title
        creator = _text(f"{{{_NS_DC}}}creator")
        if creator:
            facts["creator"] = creator
        creation_date = _text(f"{{{_NS_META}}}creation-date")
        if creation_date:
            facts["creation_date"] = creation_date
        modification_date = _text(f"{{{_NS_DC}}}date")
        if modification_date:
            facts["modification_date"] = modification_date
        editing_cycles = _text(f"{{{_NS_META}}}editing-cycles")
        if editing_cycles:
            facts["editing_cycles"] = editing_cycles
        editing_duration = _text(f"{{{_NS_META}}}editing-duration")
        if editing_duration:
            facts["editing_duration"] = editing_duration
        stat = meta_el.find(f"{{{_NS_META}}}document-statistic")
        if stat is not None and stat.attrib:
            facts["document_statistic"] = {
                _localname(k): v for k, v in stat.attrib.items()
            }
        return facts or None

    # ========================================================
    # presence layer: page animation/transition + fonts + security
    # (additive, non-empty only)
    # ========================================================

    @classmethod
    def _page_presence_facts(cls, root, pages, slides_meta, lost_pages=frozenset()):
        """Page-level presence fields: animation/transition/visibility.

        animation_present: anim:par/anim:seq presence;
        visibility_observed: raw presentation:visibility
        attribute (observed only, no hidden semantics);
        transition_present: page style -> drawing-page-
        properties transition/smil attributes from
        automatic-styles. Lost pages get no presence keys.
        """
        page_styles = {}
        auto_styles = root.find(f"{{{_NS_OFFICE}}}automatic-styles")
        if auto_styles is not None:
            for style_el in auto_styles.findall(
                f"{{{_NS_STYLE}}}style",
            ):
                if style_el.get(f"{{{_NS_STYLE}}}family") != "drawing-page":
                    continue
                props = style_el.find(f"{{{_NS_STYLE}}}drawing-page-properties")
                has_transition = props is not None and any(
                    name.startswith(f"{{{_NS_PRES}}}transition")
                    or name.startswith(f"{{{_NS_SMIL}}}")
                    for name in props.attrib
                )
                page_styles[style_el.get(f"{{{_NS_STYLE}}}name")] = has_transition
        for page, entry in zip(pages, slides_meta):
            if entry["slide_index"] in lost_pages:
                # lost page: no presence evidence -> keys absent.
                continue
            entry["animation_present"] = (
                page.find(f"{{{_NS_ANIM}}}par") is not None
                or page.find(f"{{{_NS_ANIM}}}seq") is not None
            )
            entry["visibility_observed"] = page.get(f"{{{_NS_PRES}}}visibility")
            entry["transition_present"] = page_styles.get(
                page.get(f"{{{_NS_DRAW}}}style-name"),
                False,
            )
            anim_elements, anim_truncated = cls._animation_facts(page)
            if anim_elements:
                entry["animation_elements"] = anim_elements
            if anim_truncated:
                entry["animation_elements_truncated"] = True

    @classmethod
    def _font_facts(cls, root, styles_root, pkg):
        """Font declarations: font_declared + embedded presence.

        style:font-face names from content/styles; embedded
        fonts from manifest media types or Font parts;
        font_face_uris from svg:font-face-uri xlink:href.
        availability/risk slots are never filled. Returns None
        when empty.
        """
        declared = []
        face_uris = []
        for source in (root, styles_root):
            if source is None:
                continue
            for decl in source.iter(
                f"{{{_NS_OFFICE}}}font-face-decls",
            ):
                for face in decl.findall(
                    f"{{{_NS_STYLE}}}font-face",
                ):
                    name = face.get(f"{{{_NS_STYLE}}}name")
                    if name and name not in declared:
                        declared.append(name)
                    # svg:font-face-uri xlink:href recorded
                    # verbatim (an external font is a
                    # render-environment fetch surface).
                    for uri in face.iter(f"{{{_NS_SVG_COMPAT}}}font-face-uri"):
                        href = uri.get(f"{{{_NS_XLINK}}}href")
                        if href and href not in face_uris:
                            face_uris.append(href)
        embedded = False
        if pkg is not None:
            for full_path, media_type in pkg["manifest"].items():
                if (media_type or "").startswith(
                    ("font/", "application/x-font")
                ) or full_path.startswith("Font"):
                    embedded = True
                    break
            if not embedded:
                embedded = any(name.startswith("Font") for name in pkg["names"])
        facts = {}
        if declared:
            facts["font_declared"] = declared
        if face_uris:
            facts["font_face_uris"] = face_uris
        if embedded:
            facts["embedded_font_present"] = True
        return facts or None

    @classmethod
    def _security_facts(cls, root, pkg):
        """Macro/event-binding/signature presence: registered,
        never executed.

        Macros = Basic//Scripts/ parts (zip only); event
        bindings = document-level listeners; signatures =
        documentsignatures.xml presence (validation out of
        scope). Encryption candidates are flagged only on the
        failure path (indistinguishable from ordinary
        corruption). Returns None when no presence holds.
        """
        facts = {}
        if pkg is not None:
            names = pkg["names"]
            if any(name.startswith(("Basic/", "Scripts/")) for name in names):
                facts["macro_present"] = True
                facts["macro_executed"] = False
                # basic:module static registration (capped).
                modules = pkg.get("basic_modules") or []
                if modules:
                    facts["macro_modules"] = [
                        cls._macro_module_facts(m)
                        for m in modules[:_MACRO_MODULE_CAP]
                    ]
            if "META-INF/documentsignatures.xml" in names:
                facts["signature_present"] = True
        if any(True for _ in root.iter(f"{{{_NS_OFFICE}}}event-listeners")) or any(
            True for _ in root.iter(f"{{{_NS_SCRIPT}}}event-listener")
        ):
            facts["event_bindings_present"] = True
        # root-level office:script blocks: presence + language
        # values + child counts, capped; script bodies are never
        # read or executed.
        script_blocks = list(root.findall(f"{{{_NS_OFFICE}}}script"))
        if script_blocks:
            facts["document_script_present"] = True
            languages: list[str] = []
            script_count = 0
            for block in script_blocks:
                for script_el in block.findall(f"{{{_NS_SCRIPT}}}script"):
                    script_count += 1
                    lang = script_el.get(f"{{{_NS_SCRIPT}}}language")
                    if lang and lang not in languages:
                        languages.append(lang)
            if script_count:
                facts["document_script_count"] = script_count
            if languages:
                facts["document_script_languages"] = languages[:_MACRO_MODULE_CAP]
        return facts or None

    # ========================================================
    # parse
    # ========================================================

    def parse(self, file_path):
        """Parse OpenDocument Presentation (.odp/.fodp/.otp/.sti).

        is_zipfile routing: zip packages and Flat XML share one
        XML walk; extension/container mismatch is logged as
        format_mismatch (content identification wins). Failure
        cases (non-ODF, missing content.xml, no
        office:presentation, broken XML, zero pages) -> honest
        failure with a precise parse_error.
        """
        metadata = _build_metadata(file_path)
        suffix = Path(file_path).suffix.lower()
        # file_type derived from the suffix.
        metadata["file_type"] = suffix.lstrip(".")

        # zip container guard (same as epub/pptx): central
        # directory only; flat files return None and pass through.
        _zip_guard = _check_zip_package_safe(file_path)
        if _zip_guard is not None:
            metadata["zip_package_guard"] = _zip_guard
            if _zip_guard.get("verdict") == "violation":
                metadata["parse_failed"] = True
                metadata["parse_error"] = zip_guard_error(_zip_guard)
                # parallel reason code (extended code).
                metadata["parse_error_code"] = "ZIP_PACKAGE_GUARD_VIOLATION"
                return Document(text="", metadata=metadata)

        start_time = time.perf_counter()
        flat_source = False
        pkg = None
        recovery_facts = None
        lost_pages = frozenset()
        try:
            if zipfile.is_zipfile(file_path):
                (
                    root,
                    mimetype,
                    rdf_bytes,
                    pkg,
                ) = self._load_zip_document(file_path)
                if root is None:
                    # broken content.xml -> page-scan recovery;
                    # no recoverable page -> the same honest
                    # failure.
                    recovered, lost = self._recover_pages(
                        pkg["content_bytes"],
                    )
                    if not recovered:
                        raise ValueError(pkg["content_parse_error"])
                    recovery_facts = {
                        "reason_code": "BROKEN_ODF_XML",
                        "method": "page_boundary_scan",
                        "recovered_pages": sorted(recovered),
                        "lost_pages": lost,
                    }
                    lost_pages = frozenset(lost)
                    root = self._rebuild_recovered_root(recovered, lost)
                if suffix == _FLAT_SUFFIX:
                    # extension mismatch: .fodp is actually a
                    # zip package — logged, not rejected.
                    metadata["format_mismatch"] = {
                        "extension": suffix,
                        "detected_format": "odp",
                        "format_mismatch": True,
                    }
            else:
                root, mimetype = self._load_flat_document(file_path)
                rdf_bytes = None
                pkg = None
                flat_source = True
                if suffix != _FLAT_SUFFIX:
                    # extension mismatch: zip-family suffix is
                    # actually Flat XML.
                    metadata["format_mismatch"] = {
                        "extension": suffix,
                        "detected_format": "fodp",
                        "format_mismatch": True,
                    }

            body = root.find(f"{{{_NS_OFFICE}}}body")
            presentation = body.find(f"{{{_NS_OFFICE}}}presentation") if body is not None else None
            if presentation is None:
                raise ValueError(
                    "document has no office:presentation "
                    "(not an OpenDocument presentation; "
                    f"mimetype={mimetype})"
                )
            pages = presentation.findall(f"{{{_NS_DRAW}}}page")
            if not pages:
                # empty narrative: a zero-page package fails,
                # never an empty document.
                raise ValueError("presentation contains no pages")

            text, headings, slides_meta, stats = self._render(
                pages,
                include_notes=self.include_notes,
                pkg=pkg,
                lost_pages=lost_pages,
            )

            # object-level evidence: flat records + frame order
            # + repeat expansions; the manifest pass closes
            # orphan parts.
            object_records = []
            repeat_records = []
            covered_parts = set()
            for position, page in enumerate(pages, start=1):
                self._slide_object_facts(
                    page,
                    position,
                    object_records,
                    repeat_records,
                    pkg=pkg,
                    covered_parts=covered_parts,
                )
            self._manifest_object_facts(
                pkg,
                object_records,
                covered_parts,
            )
            if object_records:
                metadata["odp_slide_objects"] = object_records
            if repeat_records:
                metadata["odp_repeat_expansions"] = repeat_records

            # annotations/changes/RDF: separate keys, never in
            # the body.
            annotations = self._annotation_facts(pages)
            if annotations:
                metadata["odp_annotations"] = annotations
            changes = self._change_facts(root)
            if changes:
                metadata["odp_changes"] = changes
            # draw:layer facts; non-empty only.
            layers = self._layer_facts(root, pages)
            if layers:
                metadata["odp_layers"] = layers
            if rdf_bytes is not None:
                metadata["odp_rdf"] = self._rdf_facts(rdf_bytes)
            # document metadata; non-empty only.
            if pkg is not None:
                meta_root = pkg.get("meta_root")
                meta_el = (
                    meta_root.find(f"{{{_NS_OFFICE}}}meta")
                    if meta_root is not None
                    else None
                )
            else:
                meta_el = root.find(f"{{{_NS_OFFICE}}}meta")
            meta_facts = self._meta_facts(meta_el)
            if meta_facts:
                metadata["odp_meta"] = meta_facts

            # page presence + fonts + macro/signature facts;
            # non-empty only.
            self._page_presence_facts(root, pages, slides_meta, lost_pages)
            styles_root = pkg["styles_root"] if pkg else None
            fonts = self._font_facts(root, styles_root, pkg)
            if fonts:
                metadata["odp_fonts"] = fonts
            security = self._security_facts(root, pkg)
            if security:
                metadata["odp_security"] = security
        except ValueError as exc:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(exc)
            # parallel reason code (no key without a match).
            _code = _error_code(str(exc))
            if _code is not None:
                metadata["parse_error_code"] = _code
            # encrypted candidate: mimetype readable but
            # content.xml missing — indistinguishable from
            # ordinary corruption, so only a candidate flag
            # (never decrypted or bypassed).
            if not flat_source and zipfile.is_zipfile(file_path):
                try:
                    with zipfile.ZipFile(file_path) as zf:
                        names = set(zf.namelist())
                except (zipfile.BadZipFile, OSError):
                    names = None
                if names and (_CONTENT_PART not in names and _MIMETYPE_PART in names):
                    metadata["odp_security"] = {
                        "encrypted_candidate": True,
                    }
            return Document(text="", metadata=metadata)

        metadata["heading_candidates"] = headings
        # recovery facts (additive on success).
        if recovery_facts is not None:
            metadata["odp_recovery"] = recovery_facts
        # per-page [start, end) spans + titles (odp_slides).
        metadata["odp_slides"] = slides_meta
        # identity facts: suffix + container + template variant;
        # written on success only.
        profile = {
            "extension": suffix,
            "document_type": "presentation",
            "mimetype": mimetype,
            "container": "flat_xml" if flat_source else "zip",
            "note": (
                "OpenDocument Presentation (ODF); independent "
                "Presentation-family parser (boundary blueprint "
                "§1)"
            ),
        }
        if suffix in _TEMPLATE_SUFFIXES:
            profile["is_template"] = True
        if suffix == ".sti":
            # legacy StarOffice template suffix, kept as
            # identity evidence.
            profile["legacy_suffix"] = True
        metadata["odp_profile"] = profile
        metadata["parse_success"] = True
        # security carrier census on the extracted text view
        # (odp bypasses base _read_and_diagnose); fact codes
        # appended to warnings, structured counts written only
        # when non-zero. Carriers may hide in body text, table
        # cells, or notes — observed only.
        _carrier_census = _detect_security_carriers(text)
        if _carrier_census:
            metadata["security_carriers"] = _carrier_census
            metadata.setdefault("warnings", []).extend(
                _build_carrier_facts(_carrier_census)
            )
        # unified node tree -> presentation_nodes
        # (source_format="odp"), same key as pptx; non-empty only.
        _nodes = _derive_odp_nodes(metadata)
        if _nodes:
            metadata["presentation_nodes"] = _nodes
        metadata["parse_duration_ms"] = round(
            (time.perf_counter() - start_time) * 1000,
            2,
        )
        # sanitize Document.text only (odp is a binary container,
        # source_text is None by contract); page/title/node facts
        # above are based on the raw view; the sanitization is
        # disclosed via metadata["sanitization"] + warning.
        sanitized_text, _sanitization = _sanitize_invisible_chars(text)
        if _sanitization:
            metadata["sanitization"] = _sanitization
            _normalized = sum(
                _sanitization.get(key, 0)
                for key in (
                    "zero_width_space", "bidi_resolved",
                    "bidi_unresolved", "zero_width", "mid_file_bom",
                    "ansi_escape", "control_char", "form_feed",
                )
            )
            if _normalized:
                metadata.setdefault("warnings", []).append(
                    f"{_normalized} invisible characters normalized "
                    "(see metadata.sanitization)"
                )
        return Document(text=sanitized_text, metadata=metadata)
