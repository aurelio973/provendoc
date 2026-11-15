# pyxlsb mode
from typing import Any, Dict, List, Optional

try:
    from pyxlsb import open_workbook
except ImportError:
    open_workbook = None

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import BackendGuardMixin


class PyxlsbBackend(BackendGuardMixin):
    """Read the Excel 2007+ binary format (.xlsb).

    pyxlsb provides only a value grid (no formulas, no merged-cell info),
    returning legacy sheet structures that ExcelParser._contract_from_legacy_sheets
    converts into the shared spreadsheet contract.

    Resource guards (guards, not filters, prereg
    table_backends_limits_prereg_v1 §3): a row-streaming backend cannot foreknow the declared shape —
    an over-cap sheet never enters the extraction loop (the skeleton keeps sheet_name+index);
    row/cell caps early-stop during row iteration (a generator break truly saves memory); the column cap truncates
    per row; facts go through the streaming new kinds (sheet_rows_truncated /
    sheet_columns_truncated / sheet_cells_truncated),
    the unknowable declared last value is never fabricated (prefer missing over fabricating).
    """

    def __init__(self, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with optional resource guards (None means historical behaviour)."""
        self._init_guards(limits)

    def load(self, file_path) -> List[Dict[str, Any]]:
        """Read a .xlsb file into a legacy sheet list; raises ImportError when pyxlsb is missing."""
        if open_workbook is None:
            raise ImportError("未安装pyxlsb，请先pip install pyxlsb")
        self._reset_guards()

        sheets: List[Dict[str, Any]] = []
        with open_workbook(file_path) as wb:
            # wb.sheets is the workbook's sheet-name list (old code mistakenly iterated a local empty list)
            sheet_names = list(wb.sheets)
            limits = self.limits
            max_sheets = limits.max_sheets if limits is not None else None
            total_sheets = len(sheet_names)
            for sheet_index, sheet_name in enumerate(sheet_names):
                if max_sheets is not None and sheet_index >= max_sheets:
                    # Sheet cap: no get_sheet (saving the streaming open itself); the declared
                    # shape is not foreknowable in a streaming backend, the skeleton honestly stays 0×0.
                    self._guard(
                        f"sheet '{sheet_name}' (index {sheet_index}) kept as skeleton: "
                        f"workbook has {total_sheets} sheets, max_sheets={max_sheets}"
                    )
                    self._guard_fact({
                        "kind": "sheet_skeleton",
                        "sheet_name": sheet_name,
                        "sheet_index": sheet_index,
                        "total_sheets": total_sheets,
                        "max_sheets": max_sheets,
                    })
                    sheets.append({
                        "sheet_name": sheet_name,
                        "raw_rows": [],
                        "merged_cells": [],
                        "shape": {"rows": 0, "columns": 0},
                    })
                    continue
                sheet = wb.get_sheet(sheet_name)
                raw_rows = self._rows_streamed(sheet, sheet_name, sheet_index)
                sheets.append({
                    "sheet_name": sheet_name,
                    "raw_rows": raw_rows,
                    "merged_cells": [],
                    "shape": {
                        "rows": len(raw_rows),
                        "columns": max((len(r) for r in raw_rows), default=0),  # pyxlsb没有max_column
                    },
                })
        return sheets

    def _rows_streamed(
        self, sheet, sheet_name: str, sheet_index: int,
    ) -> List[List[Any]]:
        """Stream-extract row values; row/cell caps early-stop, the column cap truncates per row (one fact per sheet)."""
        limits = self.limits
        if limits is None:
            return [[cell.v for cell in row] for row in sheet.rows()]
        row_cap = limits.max_rows_per_sheet
        column_cap = limits.max_columns_per_sheet
        cell_budget = limits.max_cells_per_sheet
        raw_rows: List[List[Any]] = []
        cells_seen = 0
        column_fact_done = False
        row_stop = cell_stop = False
        for row in sheet.rows():
            values = [cell.v for cell in row]  # cell.v 获取cell.value
            if column_cap is not None and len(values) > column_cap:
                if not column_fact_done:
                    self._guard(
                        f"sheet '{sheet_name}': columns beyond "
                        f"{column_cap} not extracted "
                        f"(max_columns_per_sheet={column_cap}); declared "
                        "total unknown (streaming)"
                    )
                    self._guard_fact({
                        "kind": "sheet_columns_truncated",
                        "sheet_name": sheet_name,
                        "sheet_index": sheet_index,
                        "first_not_extracted_column": column_cap + 1,
                        "max_columns_per_sheet": column_cap,
                    })
                    column_fact_done = True
                values = values[:column_cap]
            if row_cap is not None and len(raw_rows) >= row_cap:
                row_stop = True
                break
            if cell_budget is not None and cells_seen + len(values) > cell_budget:
                cell_stop = True
                break
            raw_rows.append(values)
            cells_seen += len(values)
        if row_stop:
            self._guard(
                f"sheet '{sheet_name}': row extraction stopped at "
                f"{len(raw_rows)} rows (max_rows_per_sheet={row_cap}); "
                "declared total unknown (streaming)"
            )
            self._guard_fact({
                "kind": "sheet_rows_truncated",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_row": len(raw_rows) + 1,
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
                "sheet_index": sheet_index,
                "first_not_extracted_row": len(raw_rows) + 1,
                "cells_extracted": cells_seen,
                "max_cells_per_sheet": cell_budget,
            })
        return raw_rows
