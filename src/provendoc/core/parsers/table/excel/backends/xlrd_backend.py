"""Legacy XLS backend used only by ExcelParser's grid fallback."""

from typing import Any, Dict, List, Optional

try:
    import xlrd
except ImportError:
    xlrd = None

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import (
    BackendGuardMixin,
    column_letters,
    merged_range_entry,
)

# Characters that force quoting in a sheet-name reference (Excel convention: non-simple identifiers get wrapped in quotes,
# embedded single quotes are doubled). Renders declared facts deterministically, inventing nothing.
_SHEET_QUOTE_CHARS = frozenset(" '\"!$#%&()*+,/:;<=>?@[]^`{}~")

_XL_VISIBILITY = {0: "visible", 1: "hidden", 2: "veryHidden"}


class XlrdBackend(BackendGuardMixin):
    """xlrd backend (.xls, table_backend_parity prereg v1 D4 deepening).

    Outputs a ``legacy_sheets`` wrapping dict (D2), converted into the shared spreadsheet contract
    by ExcelParser._contract_from_legacy_sheets:

    - merge ranges: under ``formatting_info=True``, ``sheet.merged_cells``'s
      0-based half-open tuples → contract merged_cells entry dicts (five keys, same
      shape as openpyxl); for BIFF<8 files xlrd does not support
      formatting_info, and after the degraded reopen the merge declarations are unreachable (empty list, honesty boundary).
    - defined_names: ``workbook.name_obj_list``, with Ref3D coordinates converted into
      ``Sheet!$A$1:$B$2`` declaration text; when a formula/constant result is not convertible
      attr_text stays None, never faked. Name info is not extracted for BIFF<5 (xlrd
      behaviour), yielding an empty list then — the declaration is unreachable, not absent.
    - visibility: ``sheet.visibility`` 0/1/2 → visible/hidden/veryHidden
      (aligned with the openpyxl sheet_state vocabulary).
    - formulas: **unreachable** — xlrd exposes only cached values (prereg §4 honesty boundary),
      ``formulas`` is always an empty list.

    Resource guards (guards, not filters, prereg
    table_backends_limits_prereg_v1 §3): xlrd's ``nrows`` / ``ncols``
    are pre-parse declared values, the four dimensions are isomorphic to openpyxl — an over-cap sheet gets only a skeleton
    (declared shape carried honestly), rows/columns truncate at the cap, a declared-shape product over
    the cells cap skips extraction for the whole sheet; facts go into ``guard_reasons`` /
    ``guard_facts``, persisted by ExcelParser into ``metadata["resource_guard"]``
    and derived into the object-level unresolved registrations.
    """

    def __init__(self, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with optional resource guards (None means historical behaviour)."""
        self._init_guards(limits)

    def load(self, file_path) -> Dict[str, Any]:
        """Read .xls into a legacy_sheets wrapping dict; raises ImportError when xlrd is missing."""
        if xlrd is None:
            raise ImportError("未安装xlrd，请先pip install xlrd")
        self._reset_guards()

        workbook = self._open_workbook(file_path)
        limits = self.limits
        max_sheets = limits.max_sheets if limits is not None else None
        total_sheets = len(workbook.sheets())
        sheets: List[Dict[str, Any]] = []
        sheet_order: List[str] = []
        for sheet_index, sheet in enumerate(workbook.sheets()):
            sheet_name = sheet.name
            sheet_order.append(sheet_name)
            visibility = self._visibility(sheet)
            if max_sheets is not None and sheet_index >= max_sheets:
                # Sheet cap: the declared shape is foreknown, the skeleton carries nrows/ncols honestly,
                # never entering the per-row row_values extraction.
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
                    "visibility": visibility,
                    "raw_rows": [],
                    "merged_cells": [],
                    "formulas": [],
                    "shape": {
                        "rows": int(sheet.nrows),
                        "columns": int(sheet.ncols),
                    },
                })
                continue
            sheets.append({
                "sheet_name": sheet_name,
                "visibility": visibility,
                "raw_rows": self._rows_with_guards(sheet, sheet_name, sheet_index),
                "merged_cells": self._merged_cells(sheet),
                "formulas": [],
                "shape": {
                    "rows": int(sheet.nrows),
                    "columns": int(sheet.ncols),
                },
            })
        return {
            "legacy_sheets": sheets,
            "defined_names": self._defined_names(workbook),
            "sheet_order": sheet_order,
            "relations": [],
            "extra": {"engine": "xlrd"},
        }

    @staticmethod
    def _open_workbook(file_path):
        """Prefer formatting_info=True (merge declarations); on BIFF<8, degrade and reopen.

        On the degraded path the value grid and defined_names evidence survive while merge declarations are unreachable
        (honest empty list); if the file itself is corrupt, the degraded reopen re-raises the underlying error
        as-is (honest failure), never swallowed by this layer.
        """
        try:
            return xlrd.open_workbook(file_path, formatting_info=True)
        except Exception:
            return xlrd.open_workbook(file_path)

    @staticmethod
    def _visibility(sheet) -> str:
        """xlrd sheet.visibility integer → the openpyxl sheet_state vocabulary."""
        try:
            state = int(getattr(sheet, "visibility", 0))
        except (TypeError, ValueError):
            return "visible"
        return _XL_VISIBILITY.get(state, "visible")

    @staticmethod
    def _merged_cells(sheet) -> List[Dict[str, Any]]:
        """sheet.merged_cells half-open tuples (0-based) → contract merged_cells entries.

        Entries are five-key dicts (range/start_row/start_column/end_row/
        end_column), the same shape as OpenpyxlBackend._merged_cells — downstream
        (representation/quality layers) consumes dicts; a str alien shape crashes.
        """
        merged: List[Dict[str, Any]] = []
        for crange in getattr(sheet, "merged_cells", None) or []:
            if len(crange) != 4:
                continue
            row_lo, row_hi, col_lo, col_hi = crange
            merged.append(merged_range_entry(
                row_lo + 1, col_lo + 1, row_hi, col_hi,
            ))
        return merged

    @staticmethod
    def _defined_names(workbook) -> List[Dict[str, Any]]:
        """workbook.name_obj_list → contract defined_names (D4).

        The shape aligns with the openpyxl backend (the four keys
        name/attr_text/local_sheet_id/hidden). attr_text is convertible only when result is a Ref3D list
        (``Sheet!$A$1:$B$2``); for formula expressions/constants/macro names attr_text
        stays None, never faked. scope=-1 is workbook level (local_sheet_id
        None), otherwise the declared sheet index is carried honestly.
        """
        if not getattr(workbook, "name_obj_list", None):
            return []
        sheet_names = list(getattr(workbook, "sheet_names", lambda: [])() or [])
        defined_names: List[Dict[str, Any]] = []
        for name in getattr(workbook, "name_obj_list", None) or []:
            name_text = getattr(name, "name", None)
            if not name_text:
                continue
            scope = getattr(name, "scope", -1)
            defined_names.append({
                "name": name_text,
                "attr_text": XlrdBackend._name_attr_text(name, sheet_names),
                "local_sheet_id": None if scope is None or scope < 0 else int(scope),
                "hidden": bool(getattr(name, "hidden", 0)),
            })
        return defined_names

    @staticmethod
    def _name_attr_text(name, sheet_names: List[str]) -> Optional[str]:
        """Convert the declared-range text when Name.result is a Ref3D list, otherwise None.

        Multiple Ref3Ds are a union declaration, rendered in Excel union syntax (comma-joined);
        if any member's coordinates are non-convertible (out of range/malformed) the whole thing is None — never render
        a partial union that misleads downstream.
        """
        result = getattr(name, "result", None)
        refs = getattr(result, "value", None)
        if not isinstance(refs, list) or not refs:
            return None
        texts: List[str] = []
        for ref in refs:
            text = XlrdBackend._ref3d_text(ref, sheet_names)
            if text is None:
                return None
            texts.append(text)
        return ",".join(texts)

    @staticmethod
    def _ref3d_text(ref, sheet_names: List[str]) -> Optional[str]:
        """Ref3D.coords (0-based half-open six-tuple) → ``Sheet!$A$1:$B$2``.

        Out-of-range coordinates (sheet index beyond range) return None, never faked; 3D references across
        multiple sheets render in ``First:Last!$A$1`` syntax.
        """
        coords = getattr(ref, "coords", None)
        if not coords or len(coords) != 6:
            return None
        sheet_lo, sheet_hi, row_lo, row_hi, col_lo, col_hi = coords
        if not (0 <= sheet_lo < sheet_hi <= len(sheet_names)):
            return None
        start = f"${column_letters(col_lo + 1)}${row_lo + 1}"
        end = f"${column_letters(col_hi)}${row_hi}"
        range_text = start if (row_lo + 1, col_lo + 1) == (row_hi, col_hi) else f"{start}:{end}"
        first = XlrdBackend._quoted_sheet_name(sheet_names[sheet_lo])
        if sheet_hi - sheet_lo == 1:
            return f"{first}!{range_text}"
        last = XlrdBackend._quoted_sheet_name(sheet_names[sheet_hi - 1])
        return f"{first}:{last}!{range_text}"

    @staticmethod
    def _quoted_sheet_name(sheet_name: str) -> str:
        """Quote a sheet name per the Excel convention (simple identifiers stay unquoted)."""
        if sheet_name and not any(
            char in _SHEET_QUOTE_CHARS or ord(char) < 0x20
            for char in sheet_name
        ):
            return sheet_name
        return f"'{sheet_name.replace(chr(39), chr(39) * 2)}'"

    def _rows_with_guards(
        self, sheet, sheet_name: str, sheet_index: int,
    ) -> List[List[Any]]:
        """Extract row values under all four guard dimensions; with no guards, byte-identical to the historical path."""
        limits = self.limits
        nrows = int(sheet.nrows)
        ncols = int(sheet.ncols)
        if limits is None:
            return [sheet.row_values(row_index) for row_index in range(nrows)]
        cell_budget = limits.max_cells_per_sheet
        if cell_budget is not None and nrows * ncols > cell_budget:
            # Product cap first: the declared R×C alone exceeds the budget — the whole sheet skips extraction
            # (skeleton raw_rows empty, the declared shape carried by the sheet dict).
            self._guard(
                f"sheet '{sheet_name}': cell extraction skipped, declared shape "
                f"{nrows}x{ncols} exceeds max_cells_per_sheet={cell_budget}"
            )
            self._guard_fact({
                "kind": "cells_skipped",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "declared_rows": nrows,
                "declared_columns": ncols,
                "max_cells_per_sheet": cell_budget,
            })
            return []
        effective_rows = nrows
        if limits.max_rows_per_sheet is not None and nrows > limits.max_rows_per_sheet:
            effective_rows = limits.max_rows_per_sheet
            self._guard(
                f"sheet '{sheet_name}': rows "
                f"{limits.max_rows_per_sheet + 1}-{nrows} "
                f"not extracted (max_rows_per_sheet={limits.max_rows_per_sheet})"
            )
            self._guard_fact({
                "kind": "rows_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_row": limits.max_rows_per_sheet + 1,
                "last_declared_row": nrows,
                "max_rows_per_sheet": limits.max_rows_per_sheet,
            })
        effective_columns = ncols
        if (
            limits.max_columns_per_sheet is not None
            and ncols > limits.max_columns_per_sheet
        ):
            effective_columns = limits.max_columns_per_sheet
            self._guard(
                f"sheet '{sheet_name}': columns "
                f"{limits.max_columns_per_sheet + 1}-{ncols} "
                f"not extracted (max_columns_per_sheet={limits.max_columns_per_sheet})"
            )
            self._guard_fact({
                "kind": "columns_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_column": limits.max_columns_per_sheet + 1,
                "last_declared_column": ncols,
                "max_columns_per_sheet": limits.max_columns_per_sheet,
            })
        raw_rows: List[List[Any]] = []
        for row_index in range(effective_rows):
            values = sheet.row_values(row_index)
            if len(values) > effective_columns:
                values = values[:effective_columns]
            raw_rows.append(values)
        return raw_rows
