# pandas mode
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
except ImportError:
    pd = None

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import BackendGuardMixin


class PandasBackend(BackendGuardMixin):
    """pandas engine backend (value view only, header stitched back into the first row).

    Resource guards (guards, not filters, prereg
    table_backends_limits_prereg_v1 §3）：``ExcelFile.sheet_names``
    known at open time — an over-cap sheet is not parsed (saving the parse itself); ``df.shape`` is
    known after parse, so row/column truncation and whole-sheet skipping go through the openpyxl-isomorphic kinds (declared counts
    = data row count, header excluded, as stated in the reasons); post-load truncation saves no
    memory but is semantically consistent — pandas is a compatibility backend and gets no nrows parameter injected
    (uneven engine support surface, behaviour-drift risk, prereg §6).
    """

    def __init__(self, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with optional resource guards (None means historical behaviour)."""
        self._init_guards(limits)

    def load(self, file_path) -> List[Dict[str, Any]]:
        """Read all sheets of the workbook into a legacy sheet list; raises ImportError when pandas is missing."""
        if pd is None:
            raise ImportError("未安装pandas，请先pip install pandas")
        self._reset_guards()

        excel_file = pd.ExcelFile(file_path) # 获取所有sheet信息

        limits = self.limits
        max_sheets = limits.max_sheets if limits is not None else None
        sheet_names = list(excel_file.sheet_names)
        total_sheets = len(sheet_names)
        sheets: List[Dict[str, Any]] = []
        for sheet_index, sheet_name in enumerate(sheet_names):
            if max_sheets is not None and sheet_index >= max_sheets:
                # Sheet cap: not parsed (saving the parse itself); the declared shape is
                # not knowable before parse and is not fabricated; the skeleton honestly stays 0×0.
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
            df = excel_file.parse(sheet_name)
            sheets.append({
                "sheet_name": sheet_name,
                "raw_rows": self._rows_with_guards(df, sheet_name, sheet_index),
                "merged_cells": [],
                "shape": {
                    "rows": int(df.shape[0]),
                    "columns": int(df.shape[1])
                },
                "pandas_dtypes": {
                    str(col): str(dtype) for col, dtype in df.dtypes.items()
                }
            })
        return sheets

    def _rows_with_guards(
        self, df, sheet_name: str, sheet_index: int,
    ) -> List[List[Any]]:
        """Extract row values under all four guard dimensions; with no guards, byte-identical to the historical path."""
        limits = self.limits
        if limits is None:
            raw_rows = df.values.tolist()

            # stitch the header back into the first row
            raw_rows.insert(0, list(df.columns))
            return raw_rows
        rows_count = int(df.shape[0])
        columns_count = int(df.shape[1])
        cell_budget = limits.max_cells_per_sheet
        if cell_budget is not None and rows_count * columns_count > cell_budget:
            # product cap takes precedence: the declared R×C alone exceeds the budget — the whole sheet skips extraction
            # (skeleton raw_rows empty, the declared shape carried by the sheet dict).
            self._guard(
                f"sheet '{sheet_name}': cell extraction skipped, declared shape "
                f"{rows_count}x{columns_count} exceeds "
                f"max_cells_per_sheet={cell_budget} (declared rows exclude header)"
            )
            self._guard_fact({
                "kind": "cells_skipped",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "declared_rows": rows_count,
                "declared_columns": columns_count,
                "max_cells_per_sheet": cell_budget,
            })
            return []
        effective_rows = rows_count
        if (
            limits.max_rows_per_sheet is not None
            and rows_count > limits.max_rows_per_sheet
        ):
            effective_rows = limits.max_rows_per_sheet
            self._guard(
                f"sheet '{sheet_name}': data rows "
                f"{limits.max_rows_per_sheet + 1}-{rows_count} not extracted "
                f"(max_rows_per_sheet={limits.max_rows_per_sheet}); declared "
                "counts exclude header"
            )
            self._guard_fact({
                "kind": "rows_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_row": limits.max_rows_per_sheet + 1,
                "last_declared_row": rows_count,
                "max_rows_per_sheet": limits.max_rows_per_sheet,
            })
        effective_columns = columns_count
        if (
            limits.max_columns_per_sheet is not None
            and columns_count > limits.max_columns_per_sheet
        ):
            effective_columns = limits.max_columns_per_sheet
            self._guard(
                f"sheet '{sheet_name}': columns "
                f"{limits.max_columns_per_sheet + 1}-{columns_count} "
                f"not extracted (max_columns_per_sheet={limits.max_columns_per_sheet})"
            )
            self._guard_fact({
                "kind": "columns_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_column": limits.max_columns_per_sheet + 1,
                "last_declared_column": columns_count,
                "max_columns_per_sheet": limits.max_columns_per_sheet,
            })
        frame = df.iloc[:effective_rows, :effective_columns]
        raw_rows = frame.values.tolist()

        # stitch the header back into the first row
        raw_rows.insert(0, list(frame.columns))
        return raw_rows
