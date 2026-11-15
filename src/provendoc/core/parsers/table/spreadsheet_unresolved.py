# -*- coding: utf-8 -*-
"""Spreadsheet-domain object-level unresolved-registration derivation (spreadsheet_unresolved_prereg_v1 frozen).

Derivation-view discipline:

- no new detection logic whatsoever — all records are derived from the structured
  companion facts ``metadata["resource_guard"]["facts"]`` produced synchronously at the guard firing point
  (7d07278 already froze the guard behaviour; this module only does 1:1 derivation);
- **never parse the ``reasons`` strings** (human-readable synthesized strings, their format is not part of the contract;
  facts are derived only from the structured facts produced at the same firing point, never drifting);
- existing keys are kept verbatim, never rewritten or migrated (``resource_guard``'s
  ``scheme`` / ``limits`` / ``reasons`` stay byte-identical);
- records are built via ``core.foundation.unresolved_registry.build_unresolved_record``
  (contract validation fails one record, not the batch); written via ``stamp_unresolved_registry``
  (non-empty write only, honest absence);
- ``placeholder_text`` is always ``None`` (guard-skipped content has no body placeholder line,
  PDF precedent);
- single-tier note: this domain's facts produce only the ``partial`` tier (prereg §2, guards, not
  filters — data before the over-limit surface is already extracted, no whole-dropped object, no out-of-capability
  facts), not an omission;
- silent degradation on failure: this module only defensively skips unexpected shapes (unknown kinds,
  bad entries), never blocking the main parse.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)

# prereg §2 frozen: coordinate-axis value domain of the csv-family facts
_AXES = frozenset({"logical_record", "physical_line"})


def _append(records: List[Dict[str, Any]],
            record: Optional[Dict[str, Any]]) -> None:
    """append-if-valid (silently skips an entry when the build contract validation fails)."""
    if record is not None:
        records.append(record)


def _is_int(value: Any) -> bool:
    """int validation (bool is an int subclass, explicitly excluded — coordinates are never booleans)."""
    return isinstance(value, int) and not isinstance(value, bool)


# ============================================================
# excel-family facts (openpyxl backend; source is always sheet-positioned)
# ============================================================

def _sheet_sheet_base(fact: Dict[str, Any]):
    """Shared by sheet_skeleton / cells_skipped: sheet-position field validation."""
    sheet_name = fact.get("sheet_name")
    sheet_index = fact.get("sheet_index")
    if not isinstance(sheet_name, str) or not sheet_name:
        return None
    if not _is_int(sheet_index):
        return None
    return sheet_name, sheet_index


def _build_sheet_skeleton(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, sheet_index = base
    total_sheets = fact.get("total_sheets")
    max_sheets = fact.get("max_sheets")
    if not _is_int(total_sheets) or not _is_int(max_sheets):
        return None
    return build_unresolved_record(
        object_type="sheet",
        object_id=f"sheet:{sheet_index + 1}:{sheet_name}",
        resolution="partial",
        reason_code="SHEET_KEPT_AS_SKELETON",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="sheet_skeleton",
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        total_sheets=total_sheets,
        max_sheets=max_sheets,
    )


def _build_cells_skipped(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, sheet_index = base
    declared_rows = fact.get("declared_rows")
    declared_columns = fact.get("declared_columns")
    max_cells = fact.get("max_cells_per_sheet")
    if not (
        _is_int(declared_rows) and _is_int(declared_columns)
        and _is_int(max_cells)
    ):
        return None
    return build_unresolved_record(
        object_type="sheet",
        object_id=f"sheet:{sheet_index + 1}:{sheet_name}",
        resolution="partial",
        reason_code="CELL_EXTRACTION_SKIPPED_SHAPE_BUDGET",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="cells_skipped",
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        declared_rows=declared_rows,
        declared_columns=declared_columns,
        max_cells_per_sheet=max_cells,
    )


def _build_rows_not_extracted(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, _ = base
    first = fact.get("first_not_extracted_row")
    last = fact.get("last_declared_row")
    cap = fact.get("max_rows_per_sheet")
    if not (_is_int(first) and _is_int(last) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="rows",
        object_id=f"{sheet_name}!rows:{first}-{last}",
        resolution="partial",
        reason_code="ROWS_NOT_EXTRACTED",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="rows_not_extracted",
        sheet_name=sheet_name,
        first_not_extracted_row=first,
        last_declared_row=last,
        max_rows_per_sheet=cap,
    )


def _build_columns_not_extracted(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, _ = base
    first = fact.get("first_not_extracted_column")
    last = fact.get("last_declared_column")
    cap = fact.get("max_columns_per_sheet")
    if not (_is_int(first) and _is_int(last) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="columns",
        object_id=f"{sheet_name}!columns:{first}-{last}",
        resolution="partial",
        reason_code="COLUMNS_NOT_EXTRACTED",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="columns_not_extracted",
        sheet_name=sheet_name,
        first_not_extracted_column=first,
        last_declared_column=last,
        max_columns_per_sheet=cap,
    )


# ============================================================
# csv-family facts (main-loop logical_record / salvage physical_line)
# ============================================================

def _csv_axis(fact: Dict[str, Any]) -> Optional[str]:
    axis = fact.get("axis")
    if axis not in _AXES:
        return None
    return axis


def _build_records_beyond_row_cap(fact: Dict[str, Any]):
    axis = _csv_axis(fact)
    if axis is None:
        return None
    first = fact.get("first_unextracted")
    cap = fact.get("max_rows_per_sheet")
    if not (_is_int(first) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="records",
        object_id=f"records-after-{first}",
        resolution="partial",
        reason_code="RECORDS_BEYOND_ROW_CAP",
        source="document",
        placeholder_text=None,
        kind="records_beyond_row_cap",
        axis=axis,
        first_unextracted=first,
        max_rows_per_sheet=cap,
    )


def _build_record_columns_truncated(fact: Dict[str, Any]):
    axis = _csv_axis(fact)
    if axis is None:
        return None
    at = fact.get("at")
    record_columns = fact.get("record_columns")
    cap = fact.get("max_columns_per_sheet")
    if not (_is_int(at) and _is_int(record_columns) and _is_int(cap)):
        return None
    object_id = f"record-{at}" if axis == "logical_record" else f"line-{at}"
    return build_unresolved_record(
        object_type="record",
        object_id=object_id,
        resolution="partial",
        reason_code="RECORD_COLUMNS_TRUNCATED",
        source="document",
        placeholder_text=None,
        kind="record_columns_truncated",
        axis=axis,
        at=at,
        record_columns=record_columns,
        max_columns_per_sheet=cap,
    )


def _build_records_beyond_cell_budget(fact: Dict[str, Any]):
    axis = _csv_axis(fact)
    if axis is None:
        return None
    first = fact.get("first_unextracted")
    cells_extracted = fact.get("cells_extracted")
    cap = fact.get("max_cells_per_sheet")
    if not (
        _is_int(first) and _is_int(cells_extracted) and _is_int(cap)
    ):
        return None
    return build_unresolved_record(
        object_type="records",
        object_id=f"records-after-{first}",
        resolution="partial",
        reason_code="RECORDS_BEYOND_CELL_BUDGET",
        source="document",
        placeholder_text=None,
        kind="records_beyond_cell_budget",
        axis=axis,
        first_unextracted=first,
        cells_extracted=cells_extracted,
        max_cells_per_sheet=cap,
    )


# ============================================================
# streaming-backend facts (pyxlsb/odf/parquet; prereg
# table_backends_limits_prereg_v1 §4 adds new kinds only: declared totals are unknowable,
# never fabricated; the last field is omitted and object_id is positioned by rows-after/columns-after)
# ============================================================

def _build_sheet_rows_truncated(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, _ = base
    first = fact.get("first_not_extracted_row")
    cap = fact.get("max_rows_per_sheet")
    if not (_is_int(first) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="rows",
        object_id=f"{sheet_name}!rows-after-{first}",
        resolution="partial",
        reason_code="ROWS_NOT_EXTRACTED",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="sheet_rows_truncated",
        sheet_name=sheet_name,
        first_not_extracted_row=first,
        max_rows_per_sheet=cap,
    )


def _build_sheet_columns_truncated(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, _ = base
    first = fact.get("first_not_extracted_column")
    cap = fact.get("max_columns_per_sheet")
    if not (_is_int(first) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="columns",
        object_id=f"{sheet_name}!columns-after-{first}",
        resolution="partial",
        reason_code="COLUMNS_NOT_EXTRACTED",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="sheet_columns_truncated",
        sheet_name=sheet_name,
        first_not_extracted_column=first,
        max_columns_per_sheet=cap,
    )


def _build_sheet_cells_truncated(fact: Dict[str, Any]):
    base = _sheet_sheet_base(fact)
    if base is None:
        return None
    sheet_name, _ = base
    first = fact.get("first_not_extracted_row")
    cells_extracted = fact.get("cells_extracted")
    cap = fact.get("max_cells_per_sheet")
    if not (_is_int(first) and _is_int(cells_extracted) and _is_int(cap)):
        return None
    return build_unresolved_record(
        object_type="rows",
        object_id=f"{sheet_name}!rows-after-{first}",
        resolution="partial",
        reason_code="CELL_BUDGET_STOPPED",
        source=f"sheet:{sheet_name}",
        placeholder_text=None,
        kind="sheet_cells_truncated",
        sheet_name=sheet_name,
        first_not_extracted_row=first,
        cells_extracted=cells_extracted,
        max_cells_per_sheet=cap,
    )


# prereg §2/§4 derivation mapping table frozen: exactly these ten mappings (the three streaming kinds
# are added by table_backends_limits_prereg_v1 §4, add-only, meaning unchanged); unknown kinds are skipped
# (a new kind must first be added to the prereg (add-only, meaning unchanged) before extending this table — no guessing in the derivation layer).
_KIND_BUILDERS = {
    "sheet_skeleton": _build_sheet_skeleton,
    "cells_skipped": _build_cells_skipped,
    "rows_not_extracted": _build_rows_not_extracted,
    "columns_not_extracted": _build_columns_not_extracted,
    "records_beyond_row_cap": _build_records_beyond_row_cap,
    "record_columns_truncated": _build_record_columns_truncated,
    "records_beyond_cell_budget": _build_records_beyond_cell_budget,
    "sheet_rows_truncated": _build_sheet_rows_truncated,
    "sheet_columns_truncated": _build_sheet_columns_truncated,
    "sheet_cells_truncated": _build_sheet_cells_truncated,
}


def derive_spreadsheet_unresolved(
        metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Derive spreadsheet-domain unresolved registrations from ``resource_guard["facts"]``.

    Args:
        metadata: the Document.metadata already carrying ``resource_guard`` (with structured
            ``facts``); read-only — with no guard firing the key is absent, and an empty list returns.

    Returns:
        A contract-compliant list of records (possibly empty — the caller
        writes via ``stamp_unresolved_registry`` only when non-empty).
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(metadata, dict):
        return records
    guard = metadata.get("resource_guard")
    if not isinstance(guard, dict):
        return records
    facts = guard.get("facts")
    if not isinstance(facts, list):
        return records
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        kind = fact.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        builder = _KIND_BUILDERS.get(kind)
        if builder is None:
            # Unknown kind: skipped (a new kind must first be added to the prereg — prefer missing over fabricating).
            continue
        _append(records, builder(fact))
    return records
