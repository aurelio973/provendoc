"""Versioned parsing-facts contract for the Spreadsheet Domain.

CSV/TSV and Excel share the same serializable canvas model:
``workbook -> sheet -> canvas -> row/cell/region/chart/relation``. The parsing layer only
records evidence and deterministic inferences; retrieval rendering and multi-granularity grouping are the representation layer's job.
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

SPREADSHEET_CONTRACT_VERSION = "spreadsheet_v1"

_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


def column_index_to_label(index: int) -> str:
    """Convert a one-based column index to an A1-style column label."""
    if index < 1:
        raise ValueError("column index must be positive")
    parts: List[str] = []
    while index:
        index, remainder = divmod(index - 1, 26)
        parts.append(chr(65 + remainder))
    return "".join(reversed(parts))


def coordinate_for(row: int, column: int) -> str:
    """Return the stable A1 coordinate for a one-based grid position."""
    return f"{column_index_to_label(column)}{row}"


def json_value(value: Any) -> Any:
    """Make parser values safe to store in metadata without losing meaning."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return str(value)
    except Exception:
        return repr(value)


def infer_scalar(value: Any) -> Tuple[str, Any]:
    """Return a conservative, JSON-safe scalar type inference result.

    CSV literals retain their raw string verbatim.  Inference intentionally does
    not coerce identifiers with leading zeroes or ambiguous date strings.
    """
    if value is None:
        return "empty", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer", value
    if isinstance(value, float):
        return "number", json_value(value)
    if isinstance(value, (datetime, date)):
        return "datetime" if isinstance(value, datetime) else "date", value.isoformat()

    text = str(value)
    stripped = text.strip()
    if not stripped:
        return "empty", None
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return "boolean", lowered == "true"
    if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", stripped):
        unsigned = stripped.lstrip("+-")
        if len(unsigned) == 1 or not unsigned.startswith("0"):
            try:
                return "integer", int(stripped)
            except ValueError:
                pass
    if re.fullmatch(
        r"[+-]?(?:(?:[0-9]+\.[0-9]*)|(?:[0-9]*\.[0-9]+)|(?:[0-9]+(?:[eE][+-]?[0-9]+)))",
        stripped,
    ):
        try:
            return "number", float(stripped)
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
        try:
            date.fromisoformat(stripped)
            return "date", stripped
        except ValueError:
            pass
    if "T" in stripped or " " in stripped:
        try:
            datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return "datetime", stripped
        except ValueError:
            pass
    return "text", text


def make_cell(
    *,
    sheet_name: str,
    row: int,
    column: int,
    raw_value: Any,
    display_value: Any = None,
    formula: Optional[str] = None,
    cached_value: Any = None,
    physical_line_start: Optional[int] = None,
    physical_line_end: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a source-addressable cell fact with raw and inferred values split."""
    raw = json_value(raw_value)
    display = json_value(raw_value if display_value is None else display_value)
    inferred_type, inferred_value = infer_scalar(raw_value)
    coordinate = coordinate_for(row, column)
    cell = {
        "id": f"{sheet_name}!{coordinate}",
        "coordinate": coordinate,
        "row": row,
        "column": column,
        "raw_value": raw,
        "display_value": display,
        "formula": formula,
        "cached_value": json_value(cached_value),
        "inferred_type": inferred_type,
        "inferred_value": inferred_value,
        "is_blank": inferred_type == "empty",
        "physical_line_start": physical_line_start,
        "physical_line_end": physical_line_end,
    }
    if extra:
        cell.update(extra)
    return cell


def build_canvas(
    *,
    canvas_id: str,
    shape_rows: int,
    shape_columns: int,
    cells: Sequence[Dict[str, Any]],
    rows: Sequence[Dict[str, Any]],
    merged_cells: Optional[Sequence[Dict[str, Any]]] = None,
    hidden_rows: Optional[Sequence[int]] = None,
    hidden_columns: Optional[Sequence[str]] = None,
    charts: Optional[Sequence[Dict[str, Any]]] = None,
    freeze_panes: Optional[Dict[str, Any]] = None,
    row_groups: Optional[Sequence[Dict[str, Any]]] = None,
    data_validations: Optional[Sequence[Dict[str, Any]]] = None,
    conditional_formats: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a normalized canvas object; all child facts remain JSON-compatible."""
    return {
        "id": canvas_id,
        "shape": {"rows": int(shape_rows), "columns": int(shape_columns)},
        "cells": list(cells),
        "rows": list(rows),
        "regions": [],
        "merged_cells": list(merged_cells or []),
        "hidden_rows": list(hidden_rows or []),
        "hidden_columns": list(hidden_columns or []),
        "charts": list(charts or []),
        "freeze_panes": dict(freeze_panes) if freeze_panes else None,
        "row_groups": list(row_groups or []),
        "data_validations": list(data_validations or []),
        "conditional_formats": list(conditional_formats or []),
    }


def build_workbook_contract(
    *,
    source_kind: str,
    file_name: str,
    sheets: Sequence[Dict[str, Any]],
    defined_names: Optional[Sequence[Dict[str, Any]]] = None,
    relations: Optional[Sequence[Dict[str, Any]]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the public ``metadata['spreadsheet']`` contract."""
    contract = {
        "contract_version": SPREADSHEET_CONTRACT_VERSION,
        "domain": "spreadsheet",
        "source_kind": source_kind,
        "workbook": {
            "file_name": file_name,
            "sheet_count": len(sheets),
            "sheets": list(sheets),
            "defined_names": list(defined_names or []),
            "relations": list(relations or []),
        },
    }
    if extra:
        contract.update(extra)
    return contract


def get_contract(document: Any) -> Optional[Dict[str, Any]]:
    """Read a valid Spreadsheet contract from a Document-like object."""
    metadata = getattr(document, "metadata", {}) or {}
    contract = metadata.get("spreadsheet")
    if not isinstance(contract, dict):
        return None
    if contract.get("contract_version") != SPREADSHEET_CONTRACT_VERSION:
        return None
    workbook = contract.get("workbook")
    if not isinstance(workbook, dict) or not isinstance(workbook.get("sheets"), list):
        return None
    return contract


def nonempty_cells(cells: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return cells that contain a value or formula and participate in regions."""
    return [
        cell
        for cell in cells
        if not cell.get("is_blank") or bool(cell.get("formula"))
    ]


def _bounds(cells: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "start_row": min(cell["row"] for cell in cells),
        "end_row": max(cell["row"] for cell in cells),
        "start_column": min(cell["column"] for cell in cells),
        "end_column": max(cell["column"] for cell in cells),
    }


def _range_label(bounds: Dict[str, int]) -> str:
    return (
        f"{coordinate_for(bounds['start_row'], bounds['start_column'])}:"
        f"{coordinate_for(bounds['end_row'], bounds['end_column'])}"
    )


def _header_candidate(component: Sequence[Dict[str, Any]]) -> Tuple[Optional[int], float]:
    """Conservatively identify a grid component's first row as a table header."""
    if not component:
        return None, 0.0
    bounds = _bounds(component)
    height = bounds["end_row"] - bounds["start_row"] + 1
    width = bounds["end_column"] - bounds["start_column"] + 1
    if height < 2 or width < 2:
        return None, 0.0
    first_row = [cell for cell in component if cell["row"] == bounds["start_row"]]
    nonempty = [cell for cell in first_row if not cell.get("is_blank")]
    if len(nonempty) < 2:
        return None, 0.0
    values = [str(cell.get("display_value", "")).strip() for cell in nonempty]
    if len(set(values)) != len(values):
        return None, 0.0
    textual = sum(cell.get("inferred_type") == "text" for cell in nonempty)
    fill_ratio = len(nonempty) / width
    if textual >= max(1, len(nonempty) - 1) and fill_ratio >= 0.5:
        return bounds["start_row"], 0.7
    return None, 0.0


def _overlap_count(
    component: Sequence[Dict[str, Any]], bounds: Dict[str, int],
) -> int:
    return sum(
        bounds["start_row"] <= cell["row"] <= bounds["end_row"]
        and bounds["start_column"] <= cell["column"] <= bounds["end_column"]
        for cell in component
    )


def detect_regions(
    cells: Sequence[Dict[str, Any]],
    *,
    sheet_name: str,
    known_regions: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Detect non-empty 4-connected regions on a two-dimensional canvas.

    Known source-native regions (for example Excel Table objects) are retained
    with full confidence.  Generic connected components are intentionally
    labelled ``unknown`` unless a conservative header signal provides enough
    evidence for a logical table.
    """
    known = [dict(region) for region in (known_regions or [])]
    occupied = {
        (cell["row"], cell["column"]): cell
        for cell in nonempty_cells(cells)
    }
    visited = set()
    regions: List[Dict[str, Any]] = []

    for position, seed in occupied.items():
        if position in visited:
            continue
        queue = deque([position])
        visited.add(position)
        component: List[Dict[str, Any]] = []
        while queue:
            row, column = queue.popleft()
            component.append(occupied[(row, column)])
            for neighbor in (
                (row - 1, column), (row + 1, column),
                (row, column - 1), (row, column + 1),
            ):
                if neighbor in occupied and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        # A component almost fully covered by a native table is not emitted a
        # second time as an inferred region.  The native table remains the
        # source-of-truth object for representation.
        covered = any(
            _overlap_count(component, region["bounds"]) / len(component) >= 0.8
            for region in known
            if isinstance(region.get("bounds"), dict)
        )
        if covered:
            continue

        bounds = _bounds(component)
        header_row, header_confidence = _header_candidate(component)
        region_type = "table" if header_row is not None else "unknown"
        regions.append({
            "id": f"{sheet_name}!region:{len(known) + len(regions) + 1}",
            "region_type": region_type,
            "confidence": header_confidence if header_row is not None else 0.35,
            "detection_method": "nonempty_4_connected_component",
            "bounds": bounds,
            "range": _range_label(bounds),
            "cell_ids": [cell["id"] for cell in sorted(
                component, key=lambda c: (c["row"], c["column"]),
            )],
            "header_row_index": header_row,
            "header_confidence": header_confidence,
        })

    return known + regions


def apply_explicit_header(
    regions: Sequence[Dict[str, Any]],
    *,
    header_row_index: Optional[int],
    header_confidence: float = 1.0,
) -> List[Dict[str, Any]]:
    """Apply parser-proven header evidence to the region containing that row.

    Regions that already carry an equal-or-stronger header declaration (for
    example a native Excel Table object) keep their original evidence instead
    of being re-labelled by a weaker source.
    """
    if header_row_index is None:
        return [dict(region) for region in regions]
    result = []
    for region in regions:
        updated = dict(region)
        bounds = updated.get("bounds", {})
        if bounds.get("start_row", 0) <= header_row_index <= bounds.get("end_row", -1):
            existing_row = updated.get("header_row_index")
            existing_confidence = float(updated.get("header_confidence") or 0.0)
            if existing_row is not None and header_confidence <= existing_confidence:
                result.append(updated)
                continue
            updated["region_type"] = "table"
            updated["header_row_index"] = header_row_index
            updated["header_confidence"] = header_confidence
            updated["confidence"] = max(float(updated.get("confidence", 0.0)), header_confidence)
            updated["detection_method"] = f"{updated.get('detection_method', 'component')}+header_evidence"
        result.append(updated)
    return result


def region_headers(
    region: Dict[str, Any], cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Extract ordered header cell facts from a table region."""
    header_row = region.get("header_row_index")
    bounds = region.get("bounds") or {}
    if header_row is None:
        return []
    headers = []
    for column in range(bounds.get("start_column", 1), bounds.get("end_column", 0) + 1):
        cell = cells_by_position.get((header_row, column))
        value = "" if cell is None or cell.get("display_value") is None else str(cell["display_value"])
        headers.append({
            "column": column,
            "coordinate": coordinate_for(header_row, column),
            "name": value.strip() or f"column_{column}",
            "cell_id": cell.get("id") if cell else None,
        })
    return headers


@dataclass(frozen=True)
class TableRuntimeLimits:
    """Spreadsheet-domain runtime resource guards (guards, not filters).

    A precedent replica isomorphic to ``PDFRuntimeLimits`` (narrative/pdf/contracts.py):
    - all four dimensions default to ``None`` (no caps, behaviour fully identical to the historical version);
    - no dimension's firing ever drops the document; it only skips expensive paths and records facts;
    - firing facts are written by the parser into ``metadata["resource_guard"]``,
      for ingestion_status PARTIAL verdicts and traceability.

    Applicability (prereg oversize_structure_limits_prereg_v1 frozen; batch 8 wires table_backends_limits_prereg_v1 by extension):
    - openpyxl backend (.xlsx/.xlsm): all four dimensions apply;
    - CSV/TSV/PSV: max_sheets does not apply, the other three dimensions do;
    - legacy excel backends (xlrd/.xls, pandas engine, pyxlsb/.xlsb, odf/.ods): wired in batch 8,
      streaming early-stop goes through the three new kinds sheet_rows/sheet_columns/sheet_cells_truncated;
    - parquet: the column cap applies, row/cells caps apply streaming, max_sheets does not apply.
    """

    TABLE_LIMITS_VERSION: ClassVar[str] = "table_limits_v1"

    max_sheets: Optional[int] = None
    max_rows_per_sheet: Optional[int] = None
    max_columns_per_sheet: Optional[int] = None
    max_cells_per_sheet: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialize into versioned guard facts (written to resource_guard.limits)."""
        return {
            "version": self.TABLE_LIMITS_VERSION,
            "max_sheets": self.max_sheets,
            "max_rows_per_sheet": self.max_rows_per_sheet,
            "max_columns_per_sheet": self.max_columns_per_sheet,
            "max_cells_per_sheet": self.max_cells_per_sheet,
        }
