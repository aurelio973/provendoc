"""Native table evidence extraction using PyMuPDF's ruled-table detector.

A detected table remains a single evidence block.  Its extracted headers and
rows are preserved in ``raw_payload`` so reconstruction can recognise an
explicit continuation on the following page without losing the source cells.

B6 adds table-depth evidence (gap register §5): row/col spans from cell
geometry, blank header cells, multi-level header candidates, totals rows,
multiline cells, ragged rows, dashed/light rules, image-in-table overlaps,
nested-table containment and per-table column/row edge maps.  Everything is
recorded as evidence — the parser never re-flows, re-computes, or re-orders
cells.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFPage,
)

# Totals / subtotals row labels (first cell).  Recorded as evidence only —
# sums are never recomputed or validated by the parser.
_TOTAL_ROW_RE = re.compile(
    r"^(?:合计|小计|总计|累计|结转|total|subtotal|grand\s*total|sum)\b",
    re.IGNORECASE,
)

# A header-like cell: short, no digits, not a lone number.
_NUMERIC_CELL_RE = re.compile(r"[\d]")

# Registry #125 L3: vertical-writing label cell — every non-empty line of the
# cell is a single CJK ideograph (a Chinese e-invoice vertical 「购买方信息」
# label, one ideograph per cell).
# find_tables returns the stacked glyphs as one multi-line cell
# ("购\n买\n方\n信\n息"), which renders as one-char-per-line markdown. The merge is
# evidence-shape-driven only (all lines single CJK char, >= 2 lines) — no
# column-position constraint, cells outside the shape keep byte-identical.
_SINGLE_CJK_CHAR_RE = re.compile(r"^[\u4e00-\u9fff]$")

_EDGE_TOLERANCE = 0.5  # pt
_MAX_SPAN_SAMPLES = 12
_MAX_TABLE_SAMPLES = 20


def _round1(value: float) -> float:
    return round(float(value), 1)


def _bbox4(values: Any) -> tuple[float, float, float, float]:
    """Any 4-element bbox (pymupdf Rect/sequence) → an explicit 4-float tuple."""
    return (
        float(values[0]), float(values[1]), float(values[2]), float(values[3]),
    )


class SharedTableFinder:
    """Run ``page.find_tables()`` once per page and share the result.

    Table detection is needed by the native table extractor, the page
    geometry evidence and the figure evidence alike; without sharing, a
    document pays the detector cost once per consumer per page. This finder
    owns one fitz document and must be closed by the caller. Detection is
    lazy: only pages actually requested are ever analysed.
    """

    def __init__(self, file_path: str | Path):
        """Open the document once; pages load lazily in ``finder_for``."""
        self._document = fitz.open(file_path)
        self._finders: dict[int, Any] = {}

    def finder_for(self, page_index: int) -> Any:
        """Return the (cached) table finder for a zero-based page index."""
        if page_index not in self._finders:
            self._finders[page_index] = self._document.load_page(
                page_index
            ).find_tables()
        return self._finders[page_index]

    def tables_for(self, page_index: int) -> list[Any]:
        """Return the detected tables for a zero-based page index."""
        return list(self.finder_for(page_index).tables)

    def close(self) -> None:
        """Close the underlying fitz document and release cached finders."""
        self._document.close()


class SharedTableFinderConsumer(Protocol):
    """Types the pipeline's opt-in shared table-finder injection site.

    ``accepts_shared_table_finder`` is the runtime opt-in flag the pipeline
    checks via ``getattr``; extractors that set it (native tables, page
    geometry, figures) accept an injected ``table_finder`` keyword so table
    detection runs once per page across all evidence consumers.
    """

    accepts_shared_table_finder: bool

    def extract(
        self,
        file_path: str | Path,
        *,
        table_finder: SharedTableFinder | None = None,
    ) -> PDFExtractionResult:
        """Extract evidence, optionally reusing the shared table finder."""
        ...


def _merge_vertical_cjk_cell(value: str) -> tuple[str, bool]:
    """Collapse one vertical-writing label cell; report whether it merged."""
    lines = [part.strip() for part in value.split("\n")]
    if len(lines) >= 2 and all(_SINGLE_CJK_CHAR_RE.match(part) for part in lines):
        return "".join(lines), True
    return value, False


def _merge_vertical_cjk_cells(
    rows: list[list[str]],
) -> tuple[list[list[str]], int]:
    """Registry #125 L3: apply the vertical-cell merge across a row grid."""
    merged_count = 0
    output: list[list[str]] = []
    for row in rows:
        new_row: list[str] = []
        for cell in row:
            merged, did = _merge_vertical_cjk_cell(cell)
            new_row.append(merged)
            merged_count += int(did)
        output.append(new_row)
    return output, merged_count


class NativeTableExtractor:
    """Extract ruled / vector tables available in a native PDF text layer."""

    # Capability flag: the pipeline may inject a SharedTableFinder so table
    # detection runs once per page across all evidence consumers.
    accepts_shared_table_finder = True

    def extract(
        self,
        file_path: str | Path,
        *,
        table_finder: SharedTableFinder | None = None,
    ) -> PDFExtractionResult:
        """Extract ruled-line / vector tables from the native text layer."""
        document = fitz.open(file_path)
        try:
            pages = [
                self._extract_page(document[page_index], page_index + 1, table_finder)
                for page_index in range(int(document.page_count))
            ]
            return PDFExtractionResult(
                pages=pages,
                extraction_method="native_tables",
                metadata={
                    "page_count": len(pages),
                    "source_kind": "native_table",
                    "table_block_count": sum(len(page.blocks) for page in pages),
                    "pdf_tables": _summarize_structure(pages),
                },
            )
        finally:
            document.close()

    def _extract_page(
        self,
        page: fitz.Page,
        page_number: int,
        table_finder: SharedTableFinder | None = None,
    ) -> PDFPage:
        blocks: list[PDFBlock] = []
        try:
            if table_finder is not None:
                finder = table_finder.finder_for(page_number - 1)
            else:
                finder = page.find_tables()
            tables = list(finder.tables)
        except Exception:
            # Table evidence is additive. A detector failure must not make the
            # text extractor or the complete PDF parser fail.
            tables = []

        page_facts = _page_context_facts(page, tables)
        for table_index, table in enumerate(tables):
            headers, rows, merged_vertical_cells = self._extract_rows(table)
            if not headers and not rows:
                continue
            bbox = _bbox4(table.bbox)
            raw_payload = {
                "table_index": table_index,
                "headers": headers,
                "rows": rows,
                "header_external": bool(getattr(table.header, "external", False)),
                **_table_depth_facts(table, headers, rows, page_facts, table_index),
            }
            if merged_vertical_cells:
                # Registry #125 L3: merge count recorded only when a merge
                # happened — zero-difference discipline for additive keys.
                raw_payload["merged_vertical_cells"] = merged_vertical_cells
            blocks.append(
                PDFBlock(
                    block_type="table",
                    text=self._render_searchable_table(headers, rows),
                    page_number=page_number,
                    bbox=bbox,
                    source_kind="native_table",
                    confidence=0.9,
                    raw_payload_ref=f"page:{page_number}:table:{table_index}",
                    raw_payload=raw_payload,
                    evidence_id=f"table:p{page_number}:t{table_index}",
                )
            )

        return PDFPage(
            page_number=page_number,
            width=float(page.rect.width),
            height=float(page.rect.height),
            rotation=int(page.rotation),
            blocks=blocks,
        )

    @staticmethod
    def _extract_rows(table) -> tuple[list[str], list[list[str]], int]:
        raw_rows = table.extract() or []
        rows = [
            ["" if value is None else str(value).strip() for value in row]
            for row in raw_rows
        ]
        raw_headers = list(getattr(table.header, "names", []) or [])
        headers = ["" if value is None else str(value).strip() for value in raw_headers]

        # Registry #125 L3: collapse vertical-writing label cells before any
        # consumer sees them (render + raw_payload + continuation re-render).
        # The header is a flat cell list — wrapped as a single grid row so
        # one helper serves both shapes.
        merged_count = 0
        merged, count = _merge_vertical_cjk_cells([headers])
        headers, merged_count = merged[0], merged_count + count
        rows, count = _merge_vertical_cjk_cells(rows)
        merged_count += count

        # Some producers encode the header as the first grid row rather than
        # exposing it through ``table.header``. Keep a factual fallback.
        if not any(headers) and rows:
            headers = rows.pop(0)
        elif headers and rows and rows[0] == headers:
            rows.pop(0)
        return headers, rows, merged_count

    @staticmethod
    def _render_searchable_table(headers: list[str], rows: list[list[str]]) -> str:
        columns = max(len(headers), *(len(row) for row in rows), 1)
        normalized_headers = headers + [""] * (columns - len(headers))
        lines = ["[Table]"]
        if any(normalized_headers):
            lines.extend(
                [
                    "| " + " | ".join(normalized_headers) + " |",
                    "| " + " | ".join("---" for _ in range(columns)) + " |",
                ]
            )
        for row in rows:
            normalized_row = row + [""] * (columns - len(row))
            lines.append("| " + " | ".join(normalized_row) + " |")
        return "\n".join(lines)

    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        table_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Merge table blocks into their text pages (page order untouched)."""
        tables_by_page = {page.page_number: page.blocks for page in table_pages}
        merged: list[PDFPage] = []
        for page in text_pages:
            merged.append(
                PDFPage(
                    page_number=page.page_number,
                    width=page.width,
                    height=page.height,
                    rotation=page.rotation,
                    blocks=list(page.blocks) + list(tables_by_page.get(page.page_number, [])),
                )
            )
        return merged


# ------------------------------------------------------------
# B6: table-depth facts (evidence only)
# ------------------------------------------------------------


def _page_context_facts(page: fitz.Page, tables: list[Any]) -> dict[str, Any]:
    """Collect page-level drawing / image facts tables are compared against."""
    drawings: list[dict[str, Any]] = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            dashes = str(drawing.get("dashes") or "")
            if rect is None:
                continue
            drawings.append(
                {
                    "rect": (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                    "dashed": bool(dashes) and not dashes.startswith("[]"),
                }
            )
    except Exception:
        drawings = []

    images: list[tuple[float, float, float, float]] = []
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                images.append(_bbox4(bbox))
    except Exception:
        images = []

    return {"drawings": drawings, "images": images, "table_bboxes": [tuple(t.bbox) for t in tables]}


def _table_depth_facts(
    table: Any,
    headers: list[str],
    rows: list[list[str]],
    page_facts: dict[str, Any],
    table_index: int,
) -> dict[str, Any]:
    cells = [_bbox4(cell) for cell in (getattr(table, "cells", None) or [])]
    column_edges = _axis_edges(cells, axis=0)
    row_edges = _axis_edges(cells, axis=1)
    spanning_cells = _spanning_cells(cells, column_edges, row_edges)
    col_count = int(getattr(table, "col_count", 0) or 0)
    row_count = int(getattr(table, "row_count", 0) or 0)

    facts: dict[str, Any] = {
        "row_count": row_count,
        "col_count": col_count,
        "column_edges": column_edges,
        "row_edges": row_edges,
        "spanning_cells": spanning_cells,
        "blank_header_cells": sum(1 for header in headers if not header),
        "multiline_cell_count": sum(
            1
            for row in rows
            for cell in row
            if "\n" in cell
        ),
        "ragged_row_count": sum(
            1 for row in rows if col_count and len(row) != col_count
        ),
        "total_rows": [
            {"row_index": index, "label": row[0]}
            for index, row in enumerate(rows)
            if row and _TOTAL_ROW_RE.match(str(row[0]).strip())
        ],
        "multi_level_header_candidate_rows": _header_like_leading_rows(rows),
    }
    facts.update(_rule_style_facts(page_facts, _bbox4(table.bbox)))
    facts.update(_image_overlap_facts(page_facts, _bbox4(table.bbox)))
    facts.update(_nested_table_facts(page_facts, table_index))
    return facts


def _axis_edges(cells: list[tuple[float, float, float, float]], axis: int) -> list[float]:
    """Sorted unique boundary coordinates of the cell grid on one axis."""
    values: set[float] = set()
    for cell in cells:
        values.add(_round1(cell[axis]))
        values.add(_round1(cell[axis + 2]))
    return sorted(values)


def _spanning_cells(
    cells: list[tuple[float, float, float, float]],
    column_edges: list[float],
    row_edges: list[float],
) -> list[dict[str, Any]]:
    """Cells whose geometry covers more than one grid interval.

    A merged (spanning) cell is a detected cell that crosses a column or row
    boundary shared by other cells — a geometric fact, not an inference about
    what the author intended.
    """
    spanning: list[dict[str, Any]] = []
    for cell in cells:
        col_span = _interval_coverage(cell[0], cell[2], column_edges)
        row_span = _interval_coverage(cell[1], cell[3], row_edges)
        if col_span > 1 or row_span > 1:
            spanning.append(
                {
                    "bbox": [_round1(value) for value in cell],
                    "col_span": col_span,
                    "row_span": row_span,
                }
            )
            if len(spanning) >= _MAX_SPAN_SAMPLES:
                break
    return spanning


def _interval_coverage(start: float, end: float, edges: list[float]) -> int:
    count = 0
    for index in range(len(edges) - 1):
        if (
            start <= edges[index] + _EDGE_TOLERANCE
            and end >= edges[index + 1] - _EDGE_TOLERANCE
        ):
            count += 1
    return max(count, 1)


def _header_like_leading_rows(rows: list[list[str]]) -> int:
    """Consecutive rows after the primary header that look like extra header levels.

    Candidate evidence only: a row qualifies when every non-empty cell is
    short and digit-free.  Whether it truly is a second header level stays a
    judgement the parser never makes.
    """
    count = 0
    for row in rows:
        non_empty = [str(cell) for cell in row if str(cell).strip()]
        if not non_empty:
            break
        if any(
            "\n" in cell or _NUMERIC_CELL_RE.search(cell) or len(cell) > 40
            for cell in non_empty
        ):
            break
        count += 1
    return count


def _rule_style_facts(
    page_facts: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    dashed = 0
    solid = 0
    for drawing in page_facts.get("drawings", []):
        if not _rects_intersect(drawing["rect"], bbox):
            continue
        if drawing["dashed"]:
            dashed += 1
        else:
            solid += 1
    return {"dashed_rule_count": dashed, "solid_rule_count": solid}


def _image_overlap_facts(
    page_facts: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    overlaps = [
        list(image)
        for image in page_facts.get("images", [])
        if _rects_intersect(image, bbox)
    ]
    return {
        "image_overlap_count": len(overlaps),
        "image_overlap_samples": overlaps[:4],
    }


def _nested_table_facts(
    page_facts: dict[str, Any],
    table_index: int,
) -> dict[str, Any]:
    """Detected tables whose bbox lies inside another detected table's bbox."""
    bboxes = page_facts.get("table_bboxes", [])
    contained_in: list[int] = []
    contains: list[int] = []
    for other_index, other in enumerate(bboxes):
        if other_index == table_index:
            continue
        if _rect_contains(other, bboxes[table_index]):
            contained_in.append(other_index)
        elif _rect_contains(bboxes[table_index], other):
            contains.append(other_index)
    return {
        "nested_inside_table_index": contained_in or None,
        "contains_table_indices": contains or None,
    }


def _rects_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _rect_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    padding: float = 2.0,
) -> bool:
    return (
        inner[0] >= outer[0] - padding
        and inner[1] >= outer[1] - padding
        and inner[2] <= outer[2] + padding
        and inner[3] <= outer[3] + padding
    )


def _summarize_structure(pages: list[PDFPage]) -> dict[str, Any]:
    """Aggregate per-table depth facts into the ``pdf_tables`` metadata key."""
    table_facts: list[dict[str, Any]] = []
    for page in pages:
        for block in page.blocks:
            payload = block.raw_payload or {}
            if block.block_type != "table":
                continue
            table_facts.append(
                {
                    "page": page.page_number,
                    "evidence_id": block.evidence_id,
                    "row_count": payload.get("row_count"),
                    "col_count": payload.get("col_count"),
                    "spanning_cell_count": len(payload.get("spanning_cells") or []),
                    "blank_header_cells": payload.get("blank_header_cells", 0),
                    "multi_level_header_candidate_rows": payload.get(
                        "multi_level_header_candidate_rows", 0
                    ),
                    "total_row_count": len(payload.get("total_rows") or []),
                    "multiline_cell_count": payload.get("multiline_cell_count", 0),
                    "ragged_row_count": payload.get("ragged_row_count", 0),
                    "dashed_rule_count": payload.get("dashed_rule_count", 0),
                    "image_overlap_count": payload.get("image_overlap_count", 0),
                    "nested_inside_table_index": payload.get("nested_inside_table_index"),
                    "contains_table_indices": payload.get("contains_table_indices"),
                }
            )
    return {
        "table_count": len(table_facts),
        "tables_with_spanning_cells": sum(
            1 for fact in table_facts if fact["spanning_cell_count"]
        ),
        "tables_with_blank_headers": sum(
            1 for fact in table_facts if fact["blank_header_cells"]
        ),
        "multi_level_header_candidate_count": sum(
            1 for fact in table_facts if fact["multi_level_header_candidate_rows"]
        ),
        "tables_with_total_rows": sum(
            1 for fact in table_facts if fact["total_row_count"]
        ),
        "tables_with_multiline_cells": sum(
            1 for fact in table_facts if fact["multiline_cell_count"]
        ),
        "ragged_table_count": sum(
            1 for fact in table_facts if fact["ragged_row_count"]
        ),
        "dashed_rule_table_count": sum(
            1 for fact in table_facts if fact["dashed_rule_count"]
        ),
        "tables_with_image_overlap": sum(
            1 for fact in table_facts if fact["image_overlap_count"]
        ),
        "nested_table_count": sum(
            1
            for fact in table_facts
            if fact["nested_inside_table_index"] or fact["contains_table_indices"]
        ),
        "samples": table_facts[:_MAX_TABLE_SAMPLES],
        "note": (
            "table-depth facts (spans, header candidates, totals rows, rule "
            "styles, image overlaps, nesting) are recorded as evidence; cells "
            "are never re-flowed and totals are never recomputed"
        ),
    }
