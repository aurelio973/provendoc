"""Evidence-only extraction of DOCX table-depth facts (gap register §5).

Covers: multi-level headers (merged leading rows as candidates), vMerge /
gridSpan merge structure, repeated header rows (``w:tblHeader``), hidden rows
(``w:vanish``), nested tables, rich cell content (drawings, OMML formulas,
fields, numbered list paragraphs), empty and multiline cells, table captions
and table footnotes, and declared layout mode (``w:tblLayout`` fixed vs
autofit).

Everything is evidence: merge geometry is recorded, never re-flowed; the
python-docx ``row.cells`` duplication that vertical merges produce stays in
the narrative rows, and this summary lets consumers interpret it; empty-cell
semantics are counted, never guessed.
"""

from __future__ import annotations

import re
from typing import Any

from docx.oxml.ns import qn

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_TABLE_CAPTION_RE = re.compile(r"^(?:table|表)\s*[\d]+", re.IGNORECASE)
_TABLE_FOOTNOTE_RE = re.compile(r"^(?:注|备注|note)\s*[:：.]", re.IGNORECASE)

_MAX_TABLES = 50
_MAX_TEXT_CHARS = 120


_NS_BY_PREFIX = {"w": _W, "m": _M}


def _tag(element, prefix: str, local: str) -> bool:
    return element.tag == f"{{{_NS_BY_PREFIX[prefix]}}}{local}"


def _cell_text(tc) -> str:
    """Text of a cell's own paragraphs, excluding nested-table content."""
    parts: list[str] = []
    for paragraph in tc.findall(qn("w:p")):
        parts.append(
            "".join(node.text or "" for node in paragraph.iter(qn("w:t")))
        )
    return "\n".join(part for part in parts if part).strip()


def extract_table_summary(doc) -> dict[str, Any]:
    """Collect table-depth evidence from one docx.

    ``doc`` is an opened python-docx Document.  The summary lands under the
    ``docx_tables`` metadata key.
    """
    body = doc.element.body
    children = list(body)

    tables: list[dict[str, Any]] = []
    for position, element in enumerate(children):
        if element.tag != qn("w:tbl"):
            continue
        tables.append(_table_facts(element, children, position))
        if len(tables) >= _MAX_TABLES:
            break

    return {
        "table_count": len(tables),
        "merged_cell_table_count": sum(
            1 for fact in tables if fact["grid_span_cells"] or fact["vmerge_cells"]
        ),
        "multi_level_header_candidate_count": sum(
            1 for fact in tables if fact["merged_leading_rows"]
        ),
        "repeat_header_table_count": sum(
            1 for fact in tables if fact["tbl_header_rows"]
        ),
        "hidden_row_count": sum(fact["hidden_rows"] for fact in tables),
        "nested_table_count": sum(fact["nested_tables"] for fact in tables),
        "rich_cell_table_count": sum(
            1
            for fact in tables
            if fact["cell_drawings"]
            or fact["cell_formulas"]
            or fact["cell_fields"]
            or fact["cell_list_paragraphs"]
        ),
        "empty_cell_count": sum(fact["empty_cells"] for fact in tables),
        "multiline_cell_count": sum(fact["multiline_cells"] for fact in tables),
        "layout_counts": _layout_counts(tables),
        "captioned_table_count": sum(1 for fact in tables if fact["caption_before"]),
        "table_footnote_count": sum(1 for fact in tables if fact["footnote_after"]),
        "samples": tables[:_MAX_TABLES],
        "note": (
            "merge structure (vMerge/gridSpan), header repetition flags, "
            "hidden rows, nested tables and rich cell content are recorded "
            "as evidence; cells are never re-flowed and duplicated merge "
            "text in extracted rows is left untouched"
        ),
    }


def _table_facts(tbl, children: list, position: int) -> dict[str, Any]:
    rows = tbl.findall(qn("w:tr"))
    grid_cols = len(tbl.findall(f"{qn('w:tblGrid')}/{qn('w:gridCol')}"))

    grid_span_cells = 0
    vmerge_restart = 0
    vmerge_continue = 0
    tbl_header_rows = 0
    hidden_rows = 0
    empty_cells = 0
    multiline_cells = 0
    cell_drawings = 0
    cell_formulas = 0
    cell_fields = 0
    cell_list_paragraphs = 0
    nested_tables = 0
    merged_rows: list[bool] = []

    for row in rows:
        row_has_merge = False
        row_pr = row.find(qn("w:trPr"))
        if row_pr is not None:
            if row_pr.find(qn("w:tblHeader")) is not None:
                tbl_header_rows += 1
            if row_pr.find(qn("w:vanish")) is not None:
                hidden_rows += 1
        for tc in row.findall(qn("w:tc")):
            tc_pr = tc.find(qn("w:tcPr"))
            if tc_pr is not None:
                if tc_pr.find(qn("w:gridSpan")) is not None:
                    grid_span_cells += 1
                    row_has_merge = True
                vmerge = tc_pr.find(qn("w:vMerge"))
                if vmerge is not None:
                    row_has_merge = True
                    if vmerge.get(qn("w:val")) == "restart":
                        vmerge_restart += 1
                    else:
                        vmerge_continue += 1
            text = _cell_text(tc)
            if not text:
                empty_cells += 1
            elif "\n" in text:
                multiline_cells += 1
            nested_tables += len(tc.findall(qn("w:tbl")))
            for paragraph in tc.findall(qn("w:p")):
                cell_drawings += len(paragraph.findall(f".//{qn('w:drawing')}"))
                cell_formulas += len(
                    [
                        node
                        for node in paragraph.iter(f"{{{_M}}}oMath")
                    ]
                )
                cell_fields += len(paragraph.findall(f".//{qn('w:fldSimple')}"))
                cell_fields += len(
                    [
                        node
                        for node in paragraph.iter(qn("w:instrText"))
                    ]
                )
                if paragraph.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None:
                    cell_list_paragraphs += 1
        merged_rows.append(row_has_merge)

    return {
        "table_index": len(
            [c for c in children[:position] if _tag(c, "w", "tbl")]
        ),
        "row_count": len(rows),
        "grid_col_count": grid_cols,
        "grid_span_cells": grid_span_cells,
        "vmerge_restart_cells": vmerge_restart,
        "vmerge_continue_cells": vmerge_continue,
        "vmerge_cells": vmerge_restart + vmerge_continue,
        "tbl_header_rows": tbl_header_rows,
        "hidden_rows": hidden_rows,
        "empty_cells": empty_cells,
        "multiline_cells": multiline_cells,
        "cell_drawings": cell_drawings,
        "cell_formulas": cell_formulas,
        "cell_fields": cell_fields,
        "cell_list_paragraphs": cell_list_paragraphs,
        "nested_tables": nested_tables,
        # Candidate evidence: consecutive leading rows that carry merges are
        # the classic multi-level header shape; whether they truly are extra
        # header levels stays a judgement the parser never makes.
        "merged_leading_rows": _leading_true_count(merged_rows),
        "layout": _layout_type(tbl),
        "caption_before": _caption_before(children, position),
        "footnote_after": _footnote_after(children, position),
    }


def _leading_true_count(flags: list[bool]) -> int:
    count = 0
    for flag in flags:
        if not flag:
            break
        count += 1
    return count


def _layout_type(tbl) -> str | None:
    layout = tbl.find(f"{qn('w:tblPr')}/{qn('w:tblLayout')}")
    if layout is None:
        return None
    return layout.get(qn("w:type"))


def _caption_before(children: list, position: int) -> str | None:
    for offset in range(1, 4):
        index = position - offset
        if index < 0:
            break
        sibling = children[index]
        if _tag(sibling, "w", "tbl"):
            break
        if not _tag(sibling, "w", "p"):
            continue
        text = " ".join(
            "".join(node.text or "" for node in sibling.iter(qn("w:t"))).split()
        )
        if not text:
            continue
        if _TABLE_CAPTION_RE.match(text):
            return text[:_MAX_TEXT_CHARS]
        # First non-empty paragraph that is not a caption ends the search.
        return None
    return None


def _footnote_after(children: list, position: int) -> str | None:
    for offset in range(1, 4):
        index = position + offset
        if index >= len(children):
            break
        sibling = children[index]
        if _tag(sibling, "w", "tbl"):
            break
        if not _tag(sibling, "w", "p"):
            continue
        text = " ".join(
            "".join(node.text or "" for node in sibling.iter(qn("w:t"))).split()
        )
        if not text:
            continue
        if _TABLE_FOOTNOTE_RE.match(text):
            return text[:_MAX_TEXT_CHARS]
        return None
    return None


def _layout_counts(tables: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fact in tables:
        layout = fact["layout"] or "unspecified"
        counts[layout] = counts.get(layout, 0) + 1
    return counts


__all__ = ["extract_table_summary"]
