# ODS (ODF Spreadsheet) mode — odfpy reads the value grid + declared structural facts
from typing import Any, Dict, List, Optional

try:
    from odf.namespaces import TABLENS as _TABLENS
    from odf.office import Spreadsheet as _Spreadsheet
    from odf.opendocument import load as _odf_load
    from odf.table import CoveredTableCell, NamedRange, Table, TableCell, TableRow
    from odf.teletype import extractText as extractString
except ImportError:  # pragma: no cover - 依赖未安装时的降级
    _odf_load = None
    _Spreadsheet = None
    _TABLENS = None
    Table = TableCell = TableRow = None
    CoveredTableCell = NamedRange = None

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import BackendGuardMixin, merged_range_entry

# Defensive cap: ODS allows cells/rows to declare the repeated attribute,
# a malicious or pathological file can blow up memory with one cell declaring hundreds of thousands of repeats.
_MAX_REPEAT = 1024

# The qname sets of covered-table-cell and table-cell (for position alignment;
# covered is a merge placeholder that consumes a column position but yields no value). odfpy's odf.table.*
# are factory functions, not classes; the qname lives only on instances (a module-level getattr gets
# nothing but an empty set silently), so following getElementsByType we instantiate with check_grammar=False
# to obtain the qname.
def _factory_qnames(*factories) -> frozenset:
    qnames = set()
    for factory in factories:
        if factory is None:
            continue
        try:
            qnames.add(factory(check_grammar=False).qname)
        except (TypeError, ValueError):
            continue
    return frozenset(qnames)


_CELL_QNAMES = _factory_qnames(TableCell, CoveredTableCell)
_TABLE_CELL_QNAME = next(iter(_factory_qnames(TableCell)), None)


def _table_attribute(element, localname: str) -> Optional[str]:
    """Read element.attributes[(TABLENS, localname)] directly.

    odfpy's ``getAttribute`` raises ValueError for attribute names not in its built-in
    grammar table — ``table:display`` is not in it (measured: 23/23 corpus files crash),
    while the SAX load path (``setAttrNS``) does no grammar check and attributes stay
    in the attributes dict (keys are (namespace, localname) tuples). Evidence reads
    go through the raw dict — nothing invented, nothing lost.
    """
    if _TABLENS is None:
        return None
    return element.attributes.get((_TABLENS, localname))


class OdfBackend(BackendGuardMixin):
    """Read ODF Spreadsheet (.ods).

    odfpy provides a value view + content.xml declared attributes (table_backend_parity
    prereg v1 D3 deepening): formula text (``table:formula``, stripping the ``of:=`` /
    ``oooc:=`` namespace prefixes before landing on the cell's formula evidence key; the ODF formula syntax
    is kept verbatim, target resolution resolves where compatible and stays empty where not); merge ranges
    (the anchor cell's ``number-columns-spanned`` / ``number-rows-spanned``
    → contract merged_cells entry dicts (five keys, same shape as openpyxl);
    covered-table-cell consumes a column position but yields no value,
    fixing v1's column misalignment on files with merges); workbook-level named ranges
    (the NamedRange declaration text of ``table:named-expressions``); sheet
    visibility (``table:display="false"`` → hidden).

    Outputs a ``legacy_sheets`` wrapping dict (D2), converted into the shared spreadsheet contract
    by ExcelParser._contract_from_legacy_sheets.

    Resource guards (guards, not filters, prereg
    table_backends_limits_prereg_v1 §3): the DOM is already in memory, what the guards save is
    the raw_rows bloat after repeated expansion — an over-cap sheet does no row extraction (the skeleton
    keeps sheet_name+index); row/cell caps early-stop during row iteration; the column cap truncates
    per row; facts go through the streaming new kinds (declared totals are unknowable, never fabricated — prefer missing over fabricating).
    The ``_MAX_REPEAT`` defensive cap is orthogonal to the resource guards and stays as-is.
    """

    def __init__(self, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with optional resource guards (None means historical behaviour)."""
        self._init_guards(limits)

    def load(self, file_path) -> Dict[str, Any]:
        """Read .ods into a legacy_sheets wrapping dict; raises ImportError when odfpy is missing."""
        if _odf_load is None:
            raise ImportError("未安装odfpy，请先pip install odfpy")
        self._reset_guards()

        doc = _odf_load(file_path)
        # odfpy load() locates the spreadsheet body via the body's first child node (b[0].firstChild) —
        # but a pretty-printed content.xml has a whitespace Text node as the first child
        # (real-corpus odf-attribute-xref.ods,
        # table_backend_parity prereg §1/§7, D6 fix): doc.spreadsheet
        # is untrustworthy — enumerate Spreadsheet elements recursively from the body.
        spreadsheet = self._spreadsheet_element(doc)
        if spreadsheet is None:
            raise ValueError("ODF body 不含 office:spreadsheet 元素")
        tables = list(spreadsheet.getElementsByType(Table))
        defined_names = self._defined_names(spreadsheet)
        limits = self.limits
        max_sheets = limits.max_sheets if limits is not None else None
        total_sheets = len(tables)
        sheets: List[Dict[str, Any]] = []
        sheet_order: List[str] = []
        for table_index, table in enumerate(tables):
            sheet_name = (
                table.getAttribute("name") or f"Sheet{len(sheets) + 1}"
            )
            sheet_order.append(sheet_name)
            visibility = (
                "hidden"
                if _table_attribute(table, "display") == "false"
                else "visible"
            )
            if max_sheets is not None and table_index >= max_sheets:
                # Sheet cap: no row extraction; the declared shape is not foreknowable, the skeleton stays 0×0.
                self._guard(
                    f"sheet '{sheet_name}' (index {table_index}) kept as skeleton: "
                    f"workbook has {total_sheets} sheets, max_sheets={max_sheets}"
                )
                self._guard_fact({
                    "kind": "sheet_skeleton",
                    "sheet_name": sheet_name,
                    "sheet_index": table_index,
                    "total_sheets": total_sheets,
                    "max_sheets": max_sheets,
                })
                sheets.append({
                    "sheet_name": sheet_name,
                    "visibility": visibility,
                    "raw_rows": [],
                    "merged_cells": [],
                    "formulas": [],
                    "shape": {"rows": 0, "columns": 0},
                })
                continue
            raw_rows, formulas, merged = self._rows_with_guards(
                table, sheet_name, table_index,
            )
            sheets.append({
                "sheet_name": sheet_name,
                "visibility": visibility,
                "raw_rows": raw_rows,
                "merged_cells": merged,
                "formulas": formulas,
                "shape": {
                    "rows": len(raw_rows),
                    "columns": max((len(r) for r in raw_rows), default=0),
                },
            })
        return {
            "legacy_sheets": sheets,
            "defined_names": defined_names,
            "sheet_order": sheet_order,
            "relations": [],
            "extra": {"engine": "odf"},
        }

    @staticmethod
    def _spreadsheet_element(doc):
        """Recursively enumerate office:spreadsheet elements from the body (D6, odfpy defence)."""
        if _Spreadsheet is None:
            return None
        body = getattr(doc, "body", None)
        if body is None:
            return None
        found = body.getElementsByType(_Spreadsheet)
        return found[0] if found else None

    @staticmethod
    def _defined_names(spreadsheet) -> List[Dict[str, Any]]:
        """NamedRange of table:named-expressions → contract defined_names.

        Only NamedRange carries convertible declared-range text (cell-range-address /
        base-cell-address, ODF syntax ``SheetName.A1:B2``); NamedExpression
        is a formula expression, its attr_text stays None, never faked. The contract shape aligns with openpyxl
        (the local_sheet_id/hidden keys are always present).
        """
        if NamedRange is None:
            return []
        defined_names: List[Dict[str, Any]] = []
        for named_range in spreadsheet.getElementsByType(NamedRange):
            name = named_range.getAttribute("name")
            if not name:
                continue
            attr_text = (
                named_range.getAttribute("cellrangeaddress")
                or named_range.getAttribute("basecelladdress")
            )
            defined_names.append({
                "name": name,
                "attr_text": attr_text,
                "local_sheet_id": None,
                "hidden": False,
            })
        return defined_names

    def _rows_with_guards(
        self, table, sheet_name: str, sheet_index: int,
    ) -> tuple[List[List[Any]], List[dict], List[dict]]:
        """Extract row values and declared structure under the guards; returns (raw_rows, formulas, merged).

        With or without guards, extraction follows the same path (guards only early-stop/truncate).
        formulas is a list of ``{"row", "column", "formula"}`` (1-based,
        position-aligned with the raw_rows grid); merged is a list of contract merged_cells entry
        dicts (same shape as openpyxl).
        """
        limits = self.limits
        row_cap = limits.max_rows_per_sheet if limits is not None else None
        column_cap = (
            limits.max_columns_per_sheet if limits is not None else None
        )
        cell_budget = limits.max_cells_per_sheet if limits is not None else None
        raw_rows: List[List[Any]] = []
        formulas: List[dict] = []
        merged: List[dict] = []
        cells_seen = 0
        column_fact_done = False
        row_stop = cell_stop = False
        for row in table.getElementsByType(TableRow):
            repeated_rows = self._repeat_count(
                row.getAttribute("numberrowsrepeated"),
            )
            values: List[Any] = []
            column_index = 1
            row_formulas: List[dict] = []
            row_spans: List[tuple] = []
            for child in row.childNodes:
                qname = getattr(child, "qname", None)
                if qname not in _CELL_QNAMES:
                    continue
                repeated_cells = self._repeat_count(
                    child.getAttribute("numbercolumnsrepeated"),
                )
                is_covered = qname != _TABLE_CELL_QNAME
                if is_covered:
                    # merge placeholder: consumes a column position, yields no value and no formula.
                    values.extend([None] * repeated_cells)
                else:
                    value = extractString(child)
                    values.extend([value] * repeated_cells)
                    formula = self._formula_text(child)
                    if formula:
                        row_formulas.append({
                            "column": column_index,
                            "formula": formula,
                        })
                    row_spans.extend(
                        self._span_descriptors(child, column_index),
                    )
                column_index += repeated_cells
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
            # An all-empty, high-repeat row is ODS sparse padding — drop the tail directly.
            if repeated_rows > 1 and not any(v not in (None, "") for v in values):
                continue
            for _ in range(repeated_rows):
                if row_cap is not None and len(raw_rows) >= row_cap:
                    row_stop = True
                    break
                if cell_budget is not None and cells_seen + len(values) > cell_budget:
                    cell_stop = True
                    break
                row_position = len(raw_rows) + 1
                raw_rows.append(list(values))
                cells_seen += len(values)
                for row_formula in row_formulas:
                    formulas.append({
                        "row": row_position,
                        "column": row_formula["column"],
                        "formula": row_formula["formula"],
                    })
                for col_start, col_span, row_span in row_spans:
                    merged.append(merged_range_entry(
                        row_position, col_start,
                        row_position + row_span - 1, col_start + col_span - 1,
                    ))
            if row_stop or cell_stop:
                break
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
        return raw_rows, formulas, merged

    @staticmethod
    def _formula_text(cell) -> Optional[str]:
        """The declared table:formula text; strips the namespace prefixes (of:= / oooc:=).

        The ODF formula syntax (``[.A1]`` references) is kept verbatim — evidence is the declaration; target
        resolution reuses the Excel-syntax regexes, resolving where compatible and staying empty where not
        (table_backend_parity prereg §4 honesty boundary).
        """
        raw = cell.getAttribute("formula")
        if not raw:
            return None
        prefix, sep, rest = raw.partition("=")
        return f"={rest}" if sep else raw

    @staticmethod
    def _span_descriptors(cell, column_index: int) -> List[tuple]:
        """Anchor cell's row/column spans → (col_start, col_span, row_span) descriptor.

        The row number is settled only at row materialization (raw_rows position semantics); entry construction is done by the caller
        via merged_range_entry; the declared span facts are carried verbatim, illegal/negative values
        converge to 1 (no invented spans).
        """
        def _positive(attr) -> int:
            try:
                return max(1, int(attr)) if attr else 1
            except (TypeError, ValueError):
                return 1

        col_span = _positive(cell.getAttribute("numbercolumnsspanned"))
        row_span = _positive(cell.getAttribute("numberrowsspanned"))
        if col_span == 1 and row_span == 1:
            return []
        return [(column_index, col_span, row_span)]

    @staticmethod
    def _repeat_count(attr) -> int:
        """Parse the ODS repeated attribute; illegal/oversized values converge to the defensive cap."""
        try:
            return max(1, min(int(attr), _MAX_REPEAT)) if attr else 1
        except (TypeError, ValueError):
            return 1
