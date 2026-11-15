# ============================================================
# Spreadsheet Domain
# Parquet parser
# ============================================================
"""Columnar Apache Parquet parsing into the shared Spreadsheet canvas contract."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import BackendGuardMixin
from provendoc.core.parsers.table.excel.excel_parser import ExcelParser

try:
    import pyarrow.parquet as pq
except ImportError:
    pq = None


def _cell_value(value: Any) -> Any:
    """Reduce an arrow-decoded value to a cell fact primitive."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    # datetime / date / list / struct / map — rendered, never guessed
    return str(value)


class _ParquetBackend(BackendGuardMixin):
    """pyarrow-based columnar backend emitting the legacy sheet protocol.

    Resource guards (guards, not filters, prereg
    table_backends_limits_prereg_v1 §3): parquet is a single logical table with column foreknowledge and streaming rows —
    the column cap goes through the existing columns_not_extracted kind (schema column count
    is foreknown, free derivation); row/cell caps early-stop during iter_batches iteration, going through the streaming
    new kind; max_sheets does not apply (same as the csv precedent).
    """

    def __init__(self, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with optional resource guards (None means historical behaviour)."""
        self._init_guards(limits)

    def load(self, file_path) -> List[Dict[str, Any]]:
        """Stream-read parquet into a single-sheet legacy structure; raises ImportError when pyarrow is missing."""
        if pq is None:
            raise ImportError("未安装 pyarrow，请先 pip install pyarrow")
        self._reset_guards()
        parquet_file = pq.ParquetFile(file_path)
        schema = parquet_file.schema_arrow
        columns = list(schema.names)
        sheet_name = Path(file_path).stem
        limits = self.limits
        column_cap = limits.max_columns_per_sheet if limits is not None else None
        row_cap = limits.max_rows_per_sheet if limits is not None else None
        cell_budget = limits.max_cells_per_sheet if limits is not None else None
        selected = columns
        column_indices: Optional[List[int]] = None
        if column_cap is not None and len(columns) > column_cap:
            # Column cap: the schema column count is foreknown; the declared last column is honestly registered (free derivation).
            self._guard(
                f"sheet '{sheet_name}': columns "
                f"{column_cap + 1}-{len(columns)} "
                f"not extracted (max_columns_per_sheet={column_cap})"
            )
            self._guard_fact({
                "kind": "columns_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": 0,
                "first_not_extracted_column": column_cap + 1,
                "last_declared_column": len(columns),
                "max_columns_per_sheet": column_cap,
            })
            selected = columns[:column_cap]
            column_indices = list(range(column_cap))

        data_rows: List[List[Any]] = []
        cells_seen = 0
        row_stop = cell_stop = False
        for batch in parquet_file.iter_batches():
            batch_columns = (
                batch.columns
                if column_indices is None
                else [batch.columns[index] for index in column_indices]
            )
            column_values = [column.to_pylist() for column in batch_columns]
            for values in zip(*column_values):
                if row_cap is not None and len(data_rows) >= row_cap:
                    row_stop = True
                    break
                if cell_budget is not None and cells_seen + len(values) > cell_budget:
                    cell_stop = True
                    break
                data_rows.append([_cell_value(value) for value in values])
                cells_seen += len(values)
            if row_stop or cell_stop:
                break
        if row_stop:
            self._guard(
                f"sheet '{sheet_name}': row extraction stopped at "
                f"{len(data_rows)} rows (max_rows_per_sheet={row_cap}); "
                "declared total unknown (streaming)"
            )
            self._guard_fact({
                "kind": "sheet_rows_truncated",
                "sheet_name": sheet_name,
                "sheet_index": 0,
                "first_not_extracted_row": len(data_rows) + 1,
                "max_rows_per_sheet": row_cap,
            })
        elif cell_stop:
            self._guard(
                f"sheet '{sheet_name}': extraction stopped at {cells_seen} "
                f"cells (max_cells_per_sheet={cell_budget}); declared "
                "total unknown (streaming)"
            )
            self._guard_fact({
                "kind": "sheet_cells_truncated",
                "sheet_name": sheet_name,
                "sheet_index": 0,
                "first_not_extracted_row": len(data_rows) + 1,
                "cells_extracted": cells_seen,
                "max_cells_per_sheet": cell_budget,
            })

        # columnar source: schema column names become the explicit header row
        raw_rows = [list(selected)] + data_rows
        return [
            {
                "sheet_name": sheet_name,
                "raw_rows": raw_rows,
                "merged_cells": [],
                "shape": {
                    "rows": len(data_rows),
                    "columns": len(columns),
                },
                "arrow_schema": {
                    field.name: str(field.type) for field in schema
                },
                "num_row_groups": int(parquet_file.num_row_groups),
            },
        ]


class ParquetParser(ExcelParser):
    """Parse an Apache Parquet file as a single-sheet Spreadsheet workbook.

    A parquet file is one logical table: the arrow schema column names
    become the explicit header row, row groups stream into data rows,
    and the output flows through the same legacy-sheet →
    ``metadata['spreadsheet']`` contract pipeline as every other
    spreadsheet backend.  ``metadata['parquet']`` carries the
    columnar-specific facts (row-group count, arrow schema).  The
    legacy ``table_data`` field is retained for compatibility callers.
    """

    file_type = "parquet"

    def __init__(
        self,
        has_header=None,
        chunk_size: int = 200,
        skip_empty_rows: bool = True,
        limits: Optional[TableRuntimeLimits] = None,
    ):
        """Initialize with header strategy, block size, blank-row switch and optional guards (no Excel engine whitelist)."""
        # not calling super().__init__: the excel engine whitelist does
        # not (and should not) know about the parquet backend
        self.engine = "parquet"
        self.has_header = has_header
        self.chunk_size = chunk_size
        self.skip_empty_rows = skip_empty_rows
        self.read_only = True
        # limits guards (prereg table_backends_limits_prereg_v1):
        # max_sheets does not apply (single logical table, csv precedent); the other three dimensions are
        # adjudicated streaming by _ParquetBackend; None = no cap (historical behaviour),
        # the attributes must exist, otherwise the parent parse's resource_guard check
        # AttributeError.
        self.limits = limits
        self.backend = _ParquetBackend(limits=limits)

    LEGACY_DATA_KEY = "table_data"

    SOURCE_KIND = "parquet_table"

    def _collect_backend_metadata(self, metadata, loaded):
        """Record the columnar-specific facts (row groups, arrow schema)."""
        raw_sheet = loaded[0] if loaded else {}
        arrow_schema = raw_sheet.get("arrow_schema") or {}
        metadata["parquet"] = {
            "num_row_groups": raw_sheet.get("num_row_groups"),
            "arrow_schema": arrow_schema,
            "column_count": len(arrow_schema),
        }

    @staticmethod
    def _spreadsheet_contract(file_path, loaded: Any) -> Dict[str, Any]:
        # legacy-sheet fallback, but the contract must not claim to be
        # an excel workbook — a parquet file is its own source kind
        return ExcelParser._contract_from_legacy_sheets(
            file_path, loaded or [], source_kind="parquet_table",
        )
