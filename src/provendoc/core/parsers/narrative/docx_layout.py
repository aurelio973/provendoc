"""Evidence-only extraction of DOCX floating-object & layout-geometry facts.

B5 scope (gap register): anchored drawings (``wp:anchor`` floating above the
text), text-wrapping modes (``wrapNone`` / ``wrapSquare`` / ``wrapTight`` /
``wrapThrough`` / ``wrapTopAndBottom``), z-order (``relativeHeight`` /
``behindDoc``), drawing anchors vs body position (anchoring paragraph index),
text boxes (``w:txbxContent``), and table width vs page bounds
(``tblW`` / ``tblGrid`` vs ``sectPr`` page width).

Everything is evidence: floating geometry is recorded, wrap modes and
z-order are never simulated, and text-box text is preserved verbatim under
``docx_layout`` metadata without being injected into the narrative body —
where a floating box belongs in reading order is a rendering decision the
parser does not make.
"""

from __future__ import annotations

from typing import Any

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = (
    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
)
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WPS_NS = (
    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
)
_VML_NS = "urn:schemas-microsoft-com:vml"

_WRAP_TYPES = (
    "wrapNone",
    "wrapSquare",
    "wrapTight",
    "wrapThrough",
    "wrapTopAndBottom",
)

# graphicData uri suffixes worth classifying beyond a raw uri string.
_GRAPHIC_URI_LABELS = (
    ("/picture", "picture"),
    ("/chart", "chart"),
    ("/diagram", "diagram"),
    ("/wordprocessingShape", "shape"),
    ("/group", "group"),
    ("/3dscenario", "3d"),
)

# Sample caps are metadata-size guards, not evidence filters: counts
# stay factual (full totals), the cap is stated as a *_truncated flag,
# and per-box text beyond the char cap keeps its full char_count with
# a text_truncated marker.
_MAX_TEXT_CHARS = 2000
_MAX_TEXTBOX_SAMPLES = 200
_MAX_DRAWING_SAMPLES = 20
_MAX_TABLE_SAMPLES = 20

# 1 twip = 1/20 pt; 914400 EMU per inch, 1440 twips per inch.
_EMU_PER_TWIP = 914400.0 / 1440.0


def _emu_to_cm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round(int(value) / 360000.0, 2)
    except ValueError:
        return None


def _twips(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _graphic_kind(uri: str | None) -> str:
    if not uri:
        return "unknown"
    for suffix, label in _GRAPHIC_URI_LABELS:
        if uri.endswith(suffix):
            return label
    return "other"


def extract_layout_summary(doc) -> dict[str, Any]:
    """Collect floating-object and layout-geometry evidence from one docx.

    ``doc`` is an opened python-docx Document.  The summary lands under the
    ``docx_layout`` metadata key.
    """
    body = doc.element.body
    return {
        "drawings": _drawing_facts(body),
        "textboxes": _textbox_facts(body),
        "table_geometry": _table_geometry_facts(doc, body),
    }


# ------------------------------------------------------------
# drawings (wp:inline / wp:anchor)
# ------------------------------------------------------------

def _drawing_facts(body) -> dict[str, Any]:
    inline_count = 0
    anchored: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    samples_truncated = False
    z_order: list[dict[str, Any]] = []
    z_order_truncated = False

    paragraph_index = -1
    for element in body.iter():
        if element.tag == f"{{{_W_NS}}}p":
            paragraph_index += 1
        tag = element.tag
        if tag == f"{{{_WP_NS}}}inline":
            inline_count += 1
            if len(samples) >= _MAX_DRAWING_SAMPLES:
                samples_truncated = True
            else:
                samples.append(_inline_sample(element, paragraph_index))
            continue
        if tag == f"{{{_WP_NS}}}anchor":
            anchor_facts = _anchor_sample(element, paragraph_index)
            anchored.append(anchor_facts)
            if len(samples) >= _MAX_DRAWING_SAMPLES:
                samples_truncated = True
            else:
                samples.append(anchor_facts)
            if len(z_order) >= _MAX_DRAWING_SAMPLES:
                z_order_truncated = True
            else:
                z_order.append(
                    {
                        "paragraph_index": paragraph_index,
                        "relative_height": anchor_facts["relative_height"],
                        "behind_doc": anchor_facts["behind_doc"],
                    }
                )

    # z-order evidence: the document's declared stacking of floating
    # objects (relativeHeight ascending = back to front), never simulated.
    z_order.sort(
        key=lambda item: (
            item["relative_height"] is None,
            item["relative_height"] or 0,
        )
    )
    facts: dict[str, Any] = {
        "inline_count": inline_count,
        "anchored_count": len(anchored),
        "anchored_behind_doc_count": sum(
            1 for item in anchored if item["behind_doc"]
        ),
        "wrap_mode_counts": _wrap_mode_counts(anchored),
        "z_order": z_order,
        "samples": samples,
    }
    if z_order_truncated:
        facts["z_order_truncated"] = True
        facts["z_order_total"] = len(anchored)
    if samples_truncated:
        facts["samples_truncated"] = True
        facts["sample_cap"] = _MAX_DRAWING_SAMPLES
    facts["note"] = (
        "floating-object geometry (anchor, wrap, z-order) is recorded "
        "as evidence; wrapping is never simulated and body text stays "
        "in document order"
    )
    return facts


def _wrap_mode_counts(anchored: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in anchored:
        mode = item.get("wrap") or "unspecified"
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _inline_sample(element, paragraph_index: int) -> dict[str, Any]:
    doc_pr = element.find(f"{{{_WP_NS}}}docPr")
    extent = element.find(f"{{{_WP_NS}}}extent")
    return {
        "placement": "inline",
        "paragraph_index": paragraph_index,
        "name": (doc_pr.get("name") if doc_pr is not None else None),
        "description": (doc_pr.get("descr") if doc_pr is not None else None),
        "width_cm": _emu_to_cm(extent.get("cx") if extent is not None else None),
        "height_cm": _emu_to_cm(extent.get("cy") if extent is not None else None),
        "graphic_kind": _graphic_kind_for(element),
    }


def _anchor_sample(element, paragraph_index: int) -> dict[str, Any]:
    facts = _inline_sample(element, paragraph_index)
    facts["placement"] = "anchored"
    facts["behind_doc"] = element.get("behindDoc") == "1"
    try:
        facts["relative_height"] = int(element.get("relativeHeight"))
    except (TypeError, ValueError):
        facts["relative_height"] = None
    facts["wrap"] = _wrap_mode(element)
    facts["allow_overlap"] = element.get("allowOverlap") == "1"
    return facts


def _wrap_mode(anchor_element) -> str | None:
    for child in anchor_element:
        if child.tag.startswith(f"{{{_WP_NS}}}"):
            local = child.tag.split("}", 1)[-1]
            if local in _WRAP_TYPES:
                return local
    return None


def _graphic_kind_for(element) -> str:
    for graphic_data in element.iter(f"{{{_A_NS}}}graphicData"):
        return _graphic_kind(graphic_data.get("uri"))
    return "unknown"


# ------------------------------------------------------------
# text boxes (w:txbxContent)
# ------------------------------------------------------------

def _textbox_facts(body) -> dict[str, Any]:
    boxes: list[dict[str, Any]] = []
    total = 0
    samples_truncated = False
    paragraph_index = -1
    for element in body.iter():
        if element.tag == f"{{{_W_NS}}}p":
            paragraph_index += 1
        if element.tag != f"{{{_W_NS}}}txbxContent":
            continue
        total += 1
        if len(boxes) >= _MAX_TEXTBOX_SAMPLES:
            samples_truncated = True
            continue
        text = "".join(
            (node.text or "")
            for node in element.iter(f"{{{_W_NS}}}t")
        ).strip()
        entry: dict[str, Any] = {
            "paragraph_index": paragraph_index,
            "text": text[:_MAX_TEXT_CHARS] or None,
            "char_count": len(text),
            # A VML (legacy) or DrawingML (modern) host says which
            # technology carries the box — a declaration fact, not a
            # rendering claim.
            "host": _textbox_host(element),
        }
        if len(text) > _MAX_TEXT_CHARS:
            # char_count stays the full fact; the cut is stated, never
            # silent.
            entry["text_truncated"] = True
            entry["text_cap"] = _MAX_TEXT_CHARS
        boxes.append(entry)

    facts: dict[str, Any] = {
        "count": total,
        "sample_count": len(boxes),
        "samples": boxes,
    }
    if samples_truncated:
        facts["samples_truncated"] = True
        facts["sample_cap"] = _MAX_TEXTBOX_SAMPLES
    facts["note"] = (
        "text-box text is preserved verbatim as evidence (full count "
        "kept; per-box text beyond the cap keeps its full char_count "
        "with a text_truncated marker); it is not injected into the "
        "narrative body because the parser does not decide where a "
        "floating box belongs in reading order"
    )
    return facts


def _textbox_host(element) -> str:
    parent = element.getparent()
    while parent is not None:
        tag = parent.tag
        if tag.startswith(f"{{{_WPS_NS}}}"):
            return "drawingml"
        if tag.startswith(f"{{{_VML_NS}}}"):
            return "vml"
        parent = parent.getparent()
    return "unknown"


# ------------------------------------------------------------
# table width vs page bounds
# ------------------------------------------------------------

def _table_geometry_facts(doc, body) -> dict[str, Any]:
    page_content_width = _page_content_width(body)
    all_tables = body.findall(f"{{{_W_NS}}}tbl")
    table_total = len(all_tables)
    tables: list[dict[str, Any]] = []
    samples_truncated = False

    for index, tbl in enumerate(all_tables):
        if len(tables) >= _MAX_TABLE_SAMPLES:
            samples_truncated = True
            break
        declared = _declared_table_width(tbl)
        grid_width = _grid_width(tbl)
        entry: dict[str, Any] = {
            "table_index": index,
            "declared_width": declared,
            "grid_width_twips": grid_width,
        }
        if page_content_width is not None:
            entry["page_content_width_twips"] = page_content_width
            reference = (
                grid_width
                if grid_width is not None
                else declared.get("twips")
                if isinstance(declared, dict)
                else None
            )
            if reference is not None:
                entry["exceeds_page_bounds"] = reference > page_content_width
                entry["overflow_twips"] = max(
                    0, reference - page_content_width
                )
        tables.append(entry)

    summary: dict[str, Any] = {
        "table_count": table_total,
        "table_count_reported": len(tables),
        "tables": tables,
    }
    if samples_truncated:
        summary["samples_truncated"] = True
        summary["sample_cap"] = _MAX_TABLE_SAMPLES
    if page_content_width is not None:
        summary["page_content_width_twips"] = page_content_width
        summary["exceeding_table_count"] = sum(
            1 for entry in tables if entry.get("exceeds_page_bounds")
        )
    return summary


def _page_content_width(body) -> int | None:
    sect_pr = body.find(f"{{{_W_NS}}}sectPr")
    if sect_pr is None:
        return None
    page_size = sect_pr.find(f"{{{_W_NS}}}pgSz")
    margins = sect_pr.find(f"{{{_W_NS}}}pgMar")
    page_width = (
        _twips(page_size.get(f"{{{_W_NS}}}w")) if page_size is not None else None
    )
    if page_width is None:
        return None
    left = (
        _twips(margins.get(f"{{{_W_NS}}}left")) if margins is not None else None
    )
    right = (
        _twips(margins.get(f"{{{_W_NS}}}right")) if margins is not None else None
    )
    return page_width - (left or 0) - (right or 0)


def _declared_table_width(tbl) -> dict[str, Any] | None:
    tbl_pr = tbl.find(f"{{{_W_NS}}}tblPr")
    if tbl_pr is None:
        return None
    tbl_w = tbl_pr.find(f"{{{_W_NS}}}tblW")
    if tbl_w is None:
        return None
    width_type = tbl_w.get(f"{{{_W_NS}}}type")
    value = _twips(tbl_w.get(f"{{{_W_NS}}}w"))
    declared: dict[str, Any] = {"type": width_type}
    if value is not None:
        declared["twips"] = value
    if width_type == "pct" and value is not None:
        # pct units are fiftieths of a percent (e.g. 5000 = 100%).
        declared["percent"] = round(value / 50.0, 1)
    return declared


def _grid_width(tbl) -> int | None:
    grid = tbl.find(f"{{{_W_NS}}}tblGrid")
    if grid is None:
        return None
    total = 0
    seen = False
    for column in grid.findall(f"{{{_W_NS}}}gridCol"):
        value = _twips(column.get(f"{{{_W_NS}}}w"))
        if value is not None:
            total += value
            seen = True
    return total if seen else None


__all__ = ["extract_layout_summary"]
