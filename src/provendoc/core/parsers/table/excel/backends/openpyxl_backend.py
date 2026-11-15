"""OpenPyXL backend preserving Spreadsheet canvas evidence."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from openpyxl import load_workbook
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
except Exception:
    load_workbook = None
    coordinate_to_tuple = None
    range_boundaries = None

from provendoc.core.parsers.table import contracts as sc
from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.formula_refs import (
    formula_relations,
    reference_targets,
)


class OpenpyxlBackend:
    """Load Excel with cell-, layout-, table-, chart-, and relation-level facts.

    Workbook parsing intentionally uses normal mode: OpenPyXL's read-only
    worksheets do not expose enough merged-cell, table, drawing, and visibility
    evidence to honour the Spreadsheet provenance contract.

    Resource guards (guards, not filters, prereg oversize_structure_limits_prereg_v1):
    the optional ``TableRuntimeLimits`` is adjudicated before the dense ``iter_rows`` iteration —
    sheets beyond the sheet cap get only a skeleton, row/column caps truncate at the cap, and a declared-shape product over
    max_cells_per_sheet skips cell extraction for the whole sheet; facts are recorded into ``guard_reasons``,
    persisted by ExcelParser into ``metadata["resource_guard"]``.

    Object-level unresolved registration (spreadsheet_unresolved_prereg_v1): the same firing point
    synchronously collects structured companion facts ``guard_facts`` (kind + positioning fields),
    persisted by ExcelParser with ``resource_guard["facts"]``; the reasons strings
    stay byte-identical, and the derivation layer never parses strings.
    """

    def __init__(self, read_only: bool = True, limits: Optional[TableRuntimeLimits] = None):
        """Initialize with the read_only compatibility parameter (reads always happen in normal mode)."""
        self.read_only = read_only
        self.limits = limits
        self.guard_reasons: List[str] = []
        self.guard_facts: List[Dict[str, Any]] = []

    def _guard(self, reason: str) -> None:
        """Record a resource-guard fact (surfaced via resource_guard metadata)."""
        self.guard_reasons.append(reason)

    def _guard_fact(self, fact: Dict[str, Any]) -> None:
        """Record the structured companion fact for the same trigger point
        (spreadsheet_unresolved_prereg_v1 §1: same point, same information as the reasons)."""
        self.guard_facts.append(fact)

    def load(self, file_path):
        """Read the workbook into sheet facts and relation facts; raises ImportError when openpyxl is missing."""
        if load_workbook is None:
            raise ImportError("未安装openpyxl，请先pip install openpyxl")
        # Guard facts reset on every independent load (backend instances may be reused).
        self.guard_reasons = []
        self.guard_facts = []
        workbook = load_workbook(file_path, data_only=False, read_only=False)
        cached_workbook = self._load_cached_workbook(file_path)
        sheets: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []

        total_sheets = len(workbook.sheetnames)
        max_sheets = self.limits.max_sheets if self.limits is not None else None
        defined_name_map = self._defined_name_map(workbook)
        sheet_order = list(workbook.sheetnames)
        table_map = self._table_map(workbook)
        for sheet_index, sheet_name in enumerate(workbook.sheetnames):
            if max_sheets is not None and sheet_index >= max_sheets:
                # Sheet cap: the overflow keeps a skeleton (name/index/visibility/declared shape),
                # never entering the expensive cells/rows extraction.
                worksheet = workbook[sheet_name]
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
                sheets.append(self._guarded_sheet_skeleton(worksheet, sheet_index))
                continue
            worksheet = workbook[sheet_name]
            if not hasattr(worksheet, "iter_rows"):
                # Chartsheet (chart sheet): no cell grid (max_row/iter_rows
                # both ABSENT), but _charts/title/sheet_state evidence is complete — take the
                # empty-canvas + chart deep-dig path. The real-corpus regression (2026-10-22,
                # SimpleScatterChart.xlsx) found that the v1 AttributeError here would
                # drag the whole workbook into parse_failed; canvas_v2 charts are first-class facts,
                # one chart sheet must not destroy all the cell evidence.
                sheet_data, sheet_relations = self._chartsheet_data(
                    worksheet, sheet_index,
                )
                sheets.append(sheet_data)
                relations.extend(sheet_relations)
                continue
            cached_sheet = cached_workbook[sheet_name] if cached_workbook else None
            sheet_data, sheet_relations = self._sheet_data(
                worksheet, cached_sheet, sheet_index, defined_name_map,
                sheet_order, table_map,
            )
            sheets.append(sheet_data)
            relations.extend(sheet_relations)

        return {
            "sheets": sheets,
            "defined_names": self._defined_names(workbook),
            "relations": relations,
        }

    def _guarded_sheet_skeleton(self, worksheet, sheet_index: int) -> Dict[str, Any]:
        """Skeleton sheet with complete shape facts and empty content (reused only by over-cap sheets)."""
        sheet_name = worksheet.title
        canvas = sc.build_canvas(
            canvas_id=f"{sheet_name}!canvas:1",
            shape_rows=getattr(worksheet, "max_row", 0) or 0,
            shape_columns=getattr(worksheet, "max_column", 0) or 0,
            cells=[],
            rows=[],
        )
        canvas["regions"] = []
        return {
            "id": f"sheet:{sheet_index + 1}:{sheet_name}",
            "name": sheet_name,
            "sheet_index": sheet_index,
            "visibility": worksheet.sheet_state,
            "canvas": canvas,
            "relations": [],
        }

    def _chartsheet_data(self, worksheet, sheet_index: int):
        """Extract a Chartsheet (chart-only sheet) as an empty canvas plus
        its chart facts and chart-series relations.

        A Chartsheet has no cell grid: shape records 0x0, cells/rows stay empty — no grid facts
        are invented; charts and relations get their usual deep dig (a series reference pointing at another sheet still
        generates a chart_series_reference, same rule as spreadsheet_canvas_v2 D1/D5).
        """
        sheet_name = worksheet.title
        charts = self._charts(worksheet, sheet_name)
        relations = self._chart_relations(charts, sheet_name)
        canvas = sc.build_canvas(
            canvas_id=f"{sheet_name}!canvas:1",
            shape_rows=0,
            shape_columns=0,
            cells=[],
            rows=[],
            charts=charts,
        )
        canvas["regions"] = []
        return {
            "id": f"sheet:{sheet_index + 1}:{sheet_name}",
            "name": sheet_name,
            "sheet_index": sheet_index,
            "visibility": worksheet.sheet_state,
            "canvas": canvas,
            "relations": relations,
        }, relations

    @staticmethod
    def _load_cached_workbook(file_path):
        try:
            return load_workbook(file_path, data_only=True, read_only=False)
        except Exception:
            return None

    def _effective_shape(self, worksheet, sheet_name: str, sheet_index: int):
        """Apply row/column/cells guards to a sheet's declared shape.

        Returns ``(effective_max_row, effective_max_column, skip_cells)``;
        any firing is recorded into ``guard_reasons`` (and the structured ``guard_facts``,
        spreadsheet_unresolved_prereg_v1). The declared shape facts always keep their original values.
        """
        max_row = worksheet.max_row or 0
        max_column = worksheet.max_column or 0
        limits = self.limits
        if limits is None:
            return max_row, max_column, False
        if (
            limits.max_cells_per_sheet is not None
            and max_row * max_column > limits.max_cells_per_sheet
        ):
            # Product cap first: the declared R×C alone exceeds the extraction budget — the whole sheet skips
            # cell extraction (the skeleton canvas keeps the declared shape).
            self._guard(
                f"sheet '{sheet_name}': cell extraction skipped, declared shape "
                f"{max_row}x{max_column} exceeds max_cells_per_sheet={limits.max_cells_per_sheet}"
            )
            self._guard_fact({
                "kind": "cells_skipped",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "declared_rows": max_row,
                "declared_columns": max_column,
                "max_cells_per_sheet": limits.max_cells_per_sheet,
            })
            return max_row, max_column, True
        effective_row = max_row
        effective_column = max_column
        if limits.max_rows_per_sheet is not None and max_row > limits.max_rows_per_sheet:
            effective_row = limits.max_rows_per_sheet
            self._guard(
                f"sheet '{sheet_name}': rows {limits.max_rows_per_sheet + 1}-{max_row} "
                f"not extracted (max_rows_per_sheet={limits.max_rows_per_sheet})"
            )
            self._guard_fact({
                "kind": "rows_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_row": limits.max_rows_per_sheet + 1,
                "last_declared_row": max_row,
                "max_rows_per_sheet": limits.max_rows_per_sheet,
            })
        if (
            limits.max_columns_per_sheet is not None
            and max_column > limits.max_columns_per_sheet
        ):
            effective_column = limits.max_columns_per_sheet
            self._guard(
                f"sheet '{sheet_name}': columns {limits.max_columns_per_sheet + 1}-{max_column} "
                f"not extracted (max_columns_per_sheet={limits.max_columns_per_sheet})"
            )
            self._guard_fact({
                "kind": "columns_not_extracted",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "first_not_extracted_column": limits.max_columns_per_sheet + 1,
                "last_declared_column": max_column,
                "max_columns_per_sheet": limits.max_columns_per_sheet,
            })
        return effective_row, effective_column, False

    def _sheet_data(
        self, worksheet, cached_sheet, sheet_index: int, defined_names=None,
        sheet_order=None, table_map=None,
    ):
        """Extract one sheet's canvas facts plus its formula/chart relations.

        ``defined_names`` is the workbook-scope name mapping, ``sheet_order`` is
        the workbook's declared sheet order, ``table_map`` is the native Table object mapping
        (spreadsheet_canvas_v2 prereg v1 D4/D5 plus §7 fixes 3/4):
        name references, 3D references and structured references in formula text all resolve into attributable
        relation targets. None (legacy callers/skeleton paths) disables the corresponding resolution.
        """
        sheet_name = worksheet.title
        effective_row, effective_column, skip_cells = self._effective_shape(
            worksheet, sheet_name, sheet_index,
        )
        merged_cells = self._merged_cells(worksheet)
        hidden_rows = [
            row_index
            for row_index, dimension in worksheet.row_dimensions.items()
            if dimension.hidden
        ]
        hidden_columns = [
            str(column_key)
            for column_key, dimension in worksheet.column_dimensions.items()
            if dimension.hidden
        ]
        outline_rows = self._outline_rows(worksheet)
        outline_by_row = {entry["row"]: entry for entry in outline_rows}
        freeze_panes = self._freeze_panes(worksheet)
        if skip_cells:
            # Product cap fired: the declared shape stays in the canvas, the content is empty.
            cells: List[Dict[str, Any]] = []
            rows: List[Dict[str, Any]] = []
        else:
            cells = self._cells(
                worksheet, cached_sheet, effective_row, effective_column,
            )
            rows = self._rows(worksheet, cells, outline_by_row, effective_row)
        table_regions = self._table_regions(worksheet, sheet_name)
        known_regions = table_regions + self._autofilter_regions(
            worksheet, sheet_name, table_regions,
        )
        charts = self._charts(worksheet, sheet_name)
        relations = formula_relations(
            cells, sheet_name, defined_names, sheet_order, table_map,
        )
        relations.extend(self._chart_relations(charts, sheet_name))
        canvas = sc.build_canvas(
            canvas_id=f"{sheet_name}!canvas:1",
            shape_rows=worksheet.max_row,
            shape_columns=worksheet.max_column,
            cells=cells,
            rows=rows,
            merged_cells=merged_cells,
            hidden_rows=hidden_rows,
            hidden_columns=hidden_columns,
            charts=charts,
            freeze_panes=freeze_panes,
            row_groups=self._row_groups(outline_rows),
            data_validations=self._data_validations(worksheet),
            conditional_formats=self._conditional_formats(worksheet),
        )
        canvas["regions"] = sc.detect_regions(
            cells,
            sheet_name=sheet_name,
            known_regions=known_regions,
        )
        if freeze_panes and freeze_panes["frozen_rows"] >= 1:
            # Freezing the top row(s) is an author-declared header anchor: the
            # row directly above the split line is promoted to header evidence.
            canvas["regions"] = sc.apply_explicit_header(
                canvas["regions"],
                header_row_index=freeze_panes["frozen_rows"],
                header_confidence=1.0,
            )
        cells_by_position = {(cell["row"], cell["column"]): cell for cell in cells}
        for region in canvas["regions"]:
            if region.get("region_type") == "table":
                region["headers"] = sc.region_headers(region, cells_by_position)
        return {
            "id": f"sheet:{sheet_index + 1}:{sheet_name}",
            "name": sheet_name,
            "sheet_index": sheet_index,
            "visibility": worksheet.sheet_state,
            "canvas": canvas,
            "relations": relations,
        }, relations

    @staticmethod
    def _merged_cells(worksheet) -> List[Dict[str, Any]]:
        merged_cells = []
        for rng in worksheet.merged_cells.ranges:
            min_column, min_row, max_column, max_row = rng.bounds
            merged_cells.append({
                "range": str(rng),
                "start_row": min_row,
                "start_column": min_column,
                "end_row": max_row,
                "end_column": max_column,
            })
        return merged_cells

    def _cells(
        self,
        worksheet,
        cached_sheet,
        max_row: Optional[int] = None,
        max_column: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        cells: List[Dict[str, Any]] = []
        merged_coordinates = {
            merged_cell.coordinate
            for merged_range in worksheet.merged_cells.ranges
            for row in worksheet[merged_range.coord]
            for merged_cell in row
            if merged_cell.coordinate != merged_range.start_cell.coordinate
        }
        for row in worksheet.iter_rows(
            min_row=1,
            max_row=max_row if max_row is not None else worksheet.max_row,
            min_col=1,
            max_col=max_column if max_column is not None else worksheet.max_column,
        ):
            for cell in row:
                cached_value = None
                if cached_sheet is not None:
                    cached_value = cached_sheet.cell(cell.row, cell.column).value
                formula = cell.value if cell.data_type == "f" else None
                raw_value = cell.value
                cells.append(sc.make_cell(
                    sheet_name=worksheet.title,
                    row=cell.row,
                    column=cell.column,
                    raw_value=raw_value,
                    display_value=cell.value,
                    formula=formula,
                    cached_value=cached_value,
                    extra={
                        "number_format": getattr(cell, "number_format", None),
                        "is_date": bool(getattr(cell, "is_date", False)),
                        "data_type": getattr(cell, "data_type", None),
                        "hyperlink": cell.hyperlink.target if getattr(cell, "hyperlink", None) else None,
                        "comment": cell.comment.text if getattr(cell, "comment", None) else None,
                        "is_merged_placeholder": cell.coordinate in merged_coordinates,
                    },
                ))
        return cells

    @staticmethod
    def _rows(
        worksheet,
        cells: List[Dict[str, Any]],
        outline_by_row: Optional[Dict[int, Dict[str, Any]]] = None,
        max_row: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        outline_by_row = outline_by_row or {}
        ids_by_row: Dict[int, List[str]] = {}
        nonempty_by_row: Dict[int, bool] = {}
        for cell in cells:
            ids_by_row.setdefault(cell["row"], []).append(cell["id"])
            nonempty_by_row[cell["row"]] = (
                nonempty_by_row.get(cell["row"], False)
                or not cell.get("is_blank")
                or bool(cell.get("formula"))
            )
        return [
            {
                "id": f"{worksheet.title}!row:{row_index}",
                "row_index": row_index,
                "is_blank": not nonempty_by_row.get(row_index, False),
                "is_hidden": bool(worksheet.row_dimensions[row_index].hidden),
                "outline_level": int(
                    (outline_by_row.get(row_index) or {}).get("level", 0)
                ),
                "is_collapsed": bool(
                    (outline_by_row.get(row_index) or {}).get("collapsed", False)
                ),
                "cell_ids": ids_by_row.get(row_index, []),
            }
            for row_index in range(1, (max_row if max_row is not None else worksheet.max_row) + 1)
        ]

    @staticmethod
    def _table_regions(worksheet, sheet_name: str) -> List[Dict[str, Any]]:
        regions = []
        for index, table in enumerate(worksheet.tables.values(), start=1):
            if range_boundaries is None:
                continue
            min_column, min_row, max_column, max_row = range_boundaries(table.ref)
            regions.append({
                "id": f"{sheet_name}!table:{index}",
                "name": table.name,
                "display_name": table.displayName,
                "region_type": "table",
                "confidence": 1.0,
                "detection_method": "excel_table_object",
                "bounds": {
                    "start_row": min_row,
                    "end_row": max_row,
                    "start_column": min_column,
                    "end_column": max_column,
                },
                "range": table.ref,
                "header_row_index": min_row if table.headerRowCount else None,
                "header_confidence": 1.0 if table.headerRowCount else 0.0,
            })
        return regions

    @staticmethod
    def _autofilter_regions(
        worksheet,
        sheet_name: str,
        table_regions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """An AutoFilter range declares its first row as header and the whole
        range as a logical table (confidence 1.0, evidence-first)."""
        if range_boundaries is None:
            return []
        reference = getattr(getattr(worksheet, "auto_filter", None), "ref", None)
        if not reference:
            return []
        min_column, min_row, max_column, max_row = range_boundaries(str(reference))
        area = (max_row - min_row + 1) * (max_column - min_column + 1)
        for region in table_regions:
            other = region.get("bounds") or {}
            overlap_rows = max(
                0,
                min(max_row, other.get("end_row", 0))
                - max(min_row, other.get("start_row", 0)) + 1,
            )
            overlap_columns = max(
                0,
                min(max_column, other.get("end_column", 0))
                - max(min_column, other.get("start_column", 1)) + 1,
            )
            if area and overlap_rows * overlap_columns / area >= 0.8:
                # A native Table object already owns this range; its filter
                # state stays an attribute of the table, not a second region.
                return []
        return [{
            "id": f"{sheet_name}!autofilter:1",
            "region_type": "table",
            "confidence": 1.0,
            "detection_method": "excel_autofilter",
            "bounds": {
                "start_row": min_row,
                "end_row": max_row,
                "start_column": min_column,
                "end_column": max_column,
            },
            "range": str(reference),
            "header_row_index": min_row,
            "header_confidence": 1.0,
        }]

    @staticmethod
    def _outline_rows(worksheet) -> List[Dict[str, Any]]:
        outline_rows = []
        for row_index, dimension in sorted(worksheet.row_dimensions.items()):
            level = getattr(dimension, "outline_level", 0)
            level = int(level) if level else 0
            if level > 0:
                outline_rows.append({
                    "row": row_index,
                    "level": level,
                    "collapsed": bool(dimension.collapsed),
                    "hidden": bool(dimension.hidden),
                })
        return outline_rows

    @staticmethod
    def _row_groups(outline_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Summarize contiguous runs of equal outline level into row groups."""
        row_groups: List[Dict[str, Any]] = []
        for entry in outline_rows:
            previous = row_groups[-1] if row_groups else None
            if (
                previous
                and previous["level"] == entry["level"]
                and previous["end_row"] == entry["row"] - 1
            ):
                previous["end_row"] = entry["row"]
                previous["collapsed"] = previous["collapsed"] or entry["collapsed"]
                previous["hidden"] = previous["hidden"] or entry["hidden"]
            else:
                row_groups.append({
                    "level": entry["level"],
                    "start_row": entry["row"],
                    "end_row": entry["row"],
                    "collapsed": entry["collapsed"],
                    "hidden": entry["hidden"],
                })
        return row_groups

    @staticmethod
    def _freeze_panes(worksheet) -> Optional[Dict[str, Any]]:
        reference = getattr(worksheet, "freeze_panes", None)
        if not reference or coordinate_to_tuple is None:
            return None
        try:
            row, column = coordinate_to_tuple(str(reference))
        except (ValueError, TypeError):
            return None
        frozen_rows, frozen_columns = row - 1, column - 1
        if frozen_rows <= 0 and frozen_columns <= 0:
            return None
        return {
            "cell": str(reference),
            "frozen_rows": frozen_rows,
            "frozen_columns": frozen_columns,
        }

    @staticmethod
    def _data_validations(worksheet) -> List[Dict[str, Any]]:
        holder = getattr(worksheet, "data_validations", None)
        validations = getattr(holder, "dataValidation", None) or []
        return [
            {
                "range": str(validation.sqref),
                "validation_type": validation.type,
                "operator": validation.operator,
                "formula1": validation.formula1,
                "formula2": validation.formula2,
            }
            for validation in validations
        ]

    @staticmethod
    def _conditional_formats(worksheet) -> List[Dict[str, Any]]:
        try:
            formatting_list = list(worksheet.conditional_formatting)
        except Exception:
            return []
        conditional_formats = []
        for formatting in formatting_list:
            rules = [
                {
                    "type": rule.type,
                    "operator": rule.operator,
                    "formula": [str(value) for value in (rule.formula or [])],
                    "priority": rule.priority,
                }
                for rule in (getattr(formatting, "rules", None) or [])
            ]
            conditional_formats.append({
                "range": str(formatting.sqref),
                "rules": rules,
            })
        return conditional_formats

    @staticmethod
    def _charts(worksheet, sheet_name: str) -> List[Dict[str, Any]]:
        """Chart facts with the frozen seven top-level keys (spreadsheet_v1)
        plus canvas_v2 deep facts under ``inference`` (prereg D2).

        The top-level key set aligns with DOCX charts (see tests/test_docx_charts_images.py's
        SPREADSHEET_CHART_KEYS）：id/title/chart_type/anchor/series_count/
        source/inference. Deep-dig facts are all evidence-only: keys are omitted when the object is missing,
        no placeholder values invented.
        """
        charts = []
        for index, chart in enumerate(getattr(worksheet, "_charts", []), start=1):
            anchor = getattr(chart, "anchor", None)
            anchor_coordinate = None
            marker = getattr(anchor, "_from", None)
            if marker is not None:
                anchor_coordinate = sc.coordinate_for(marker.row + 1, marker.col + 1)
            inference: Dict[str, Any] = {
                "series": OpenpyxlBackend._chart_series(chart),
            }
            legend = getattr(chart, "legend", None)
            if legend is not None:
                position = getattr(legend, "position", None)
                if position:
                    inference["legend"] = str(position)
            axes = OpenpyxlBackend._chart_axes(chart)
            if axes:
                inference["axes"] = axes
            grouping = getattr(chart, "grouping", None)
            if grouping:
                inference["grouping"] = str(grouping)
            if "Bar" in chart.__class__.__name__:
                bar_direction = getattr(chart, "type", None)
                if bar_direction:
                    inference["bar_direction"] = str(bar_direction)
            charts.append({
                "id": f"{sheet_name}!chart:{index}",
                "title": OpenpyxlBackend._chart_title(chart),
                "chart_type": chart.__class__.__name__,
                "anchor": anchor_coordinate,
                "series_count": len(getattr(chart, "ser", []) or []),
                "source": "openpyxl_chart_object",
                "inference": inference,
            })
        return charts

    @staticmethod
    def _chart_series(chart) -> List[Dict[str, Any]]:
        """Per-series structural facts: name, name/value/category references.

        References keep openpyxl's original form (including ``$`` absolute markers) — author declarations, not inferences;
        name prefers the cached string and is omitted when missing.
        """
        series: List[Dict[str, Any]] = []
        for ser in getattr(chart, "ser", None) or []:
            try:
                entry: Dict[str, Any] = {}
                label = getattr(ser, "tx", None)
                name_ref = getattr(getattr(label, "strRef", None), "f", None)
                if name_ref:
                    entry["name_ref"] = str(name_ref)
                literal_name = getattr(label, "v", None)
                if literal_name:
                    entry["name"] = str(literal_name)
                else:
                    cache = getattr(getattr(label, "strRef", None), "strCache", None)
                    points = getattr(cache, "pt", None) or []
                    if points and points[0] is not None and points[0].v is not None:
                        entry["name"] = str(points[0].v)
                value_ref = getattr(getattr(getattr(ser, "val", None), "numRef", None), "f", None)
                if value_ref:
                    entry["value_ref"] = str(value_ref)
                category = getattr(ser, "cat", None)
                if category is not None:
                    category_ref = (
                        getattr(getattr(category, "numRef", None), "f", None)
                        or getattr(getattr(category, "strRef", None), "f", None)
                    )
                    if category_ref:
                        entry["category_ref"] = str(category_ref)
                # Scatter XYSeries: values live in xVal/yVal, not val/cat (a real
                # corpus regression 2026-10-22 found the omission: series in scatter files like SimpleScatterChart /
                # DataTableCities were left with only the name reference).
                x_source = getattr(ser, "xVal", None)
                if x_source is not None:
                    x_ref = (
                        getattr(getattr(x_source, "numRef", None), "f", None)
                        or getattr(getattr(x_source, "strRef", None), "f", None)
                    )
                    if x_ref:
                        entry["x_value_ref"] = str(x_ref)
                y_source = getattr(ser, "yVal", None)
                if y_source is not None:
                    y_ref = getattr(getattr(y_source, "numRef", None), "f", None)
                    if y_ref:
                        entry["y_value_ref"] = str(y_ref)
                if entry:
                    series.append(entry)
            except Exception:
                continue
        return series

    @staticmethod
    def _chart_axes(chart) -> Dict[str, Dict[str, Any]]:
        """Axis facts for charts that declare axes (pie/radar may not)."""
        axes: Dict[str, Dict[str, Any]] = {}
        for axis_attr, axis_key in (("x_axis", "x"), ("y_axis", "y")):
            axis = getattr(chart, axis_attr, None)
            if axis is None:
                continue
            title = OpenpyxlBackend._title_text(getattr(axis, "title", None))
            if title:
                axes[axis_key] = {"title": title}
        return axes

    @staticmethod
    def _chart_title(chart) -> str:
        return OpenpyxlBackend._title_text(getattr(chart, "title", None))

    @staticmethod
    def _title_text(title) -> str:
        if title is None:
            return ""
        try:
            return str(title.tx.rich.p[0].r[0].t)
        except (AttributeError, IndexError, TypeError):
            return ""

    @staticmethod
    def _chart_relations(
        charts: List[Dict[str, Any]],
        sheet_name: str,
    ) -> List[Dict[str, Any]]:
        """Chart series references as first-class relations (prereg D5).

        Each series (when its name/value/category reference resolves into a sheet-attributed
        target) generates a ``chart_series_reference`` relation; the source is the chart
        id (``source_chart_id``), not a cell.
        """
        relations: List[Dict[str, Any]] = []
        for chart in charts:
            series_entries = (chart.get("inference") or {}).get("series") or []
            for series_index, series in enumerate(series_entries, start=1):
                refs: Dict[str, str] = {}
                for key in (
                    "name_ref", "value_ref", "category_ref",
                    "x_value_ref", "y_value_ref",
                ):
                    if series.get(key):
                        refs[key] = series[key]
                targets: List[Dict[str, str]] = []
                for ref in refs.values():
                    for target in reference_targets(ref):
                        if target not in targets:
                            targets.append(target)
                if not targets:
                    continue
                relations.append({
                    "id": f"relation:{chart['id']}:series:{series_index}",
                    "relation_type": "chart_series_reference",
                    "source_chart_id": chart["id"],
                    "source_sheet": sheet_name,
                    "series_name": series.get("name"),
                    "series_refs": refs,
                    "targets": targets,
                    "evidence": "chart_series_reference",
                })
        return relations

    @staticmethod
    def _table_map(workbook) -> Dict[str, Dict[str, Any]]:
        """Workbook-wide native Table map keyed by uppercase displayName
        (spreadsheet_canvas_v2 §7 fix 4).

        Each entry registers declared facts: the sheet it lives on, the ref range, the tableColumns column-name order,
        headerRowCount. A Chartsheet has no tables attribute (workbook.worksheets naturally
        excludes chartsheets too).
        """
        tables: Dict[str, Dict[str, Any]] = {}
        for worksheet in workbook.worksheets:
            for display_name in getattr(worksheet, "tables", {}) or {}:
                table = worksheet.tables[display_name]
                ref = getattr(table, "ref", None)
                if not ref:
                    continue
                columns = [
                    getattr(column, "name", None)
                    for column in getattr(table, "tableColumns", None) or []
                ]
                header_row_count = getattr(table, "headerRowCount", None)
                tables[str(display_name).upper()] = {
                    "name": str(display_name),
                    "sheet": worksheet.title,
                    "ref": str(ref),
                    "columns": [name for name in columns if name],
                    "header_row_count": (
                        int(header_row_count)
                        if header_row_count is not None
                        else 1
                    ),
                }
        return tables

    @staticmethod
    def _defined_name_map(workbook) -> Dict[str, Dict[str, Any]]:
        """Workbook-scope defined names keyed by uppercase name (prereg D4).

        Sheet-scoped names never enter the mapping (honesty boundary: no resolution, no faked targets).
        """
        names: Dict[str, Dict[str, Any]] = {}
        for defined_name in workbook.defined_names.values():
            if getattr(defined_name, "localSheetId", None) is not None:
                continue
            names[defined_name.name.upper()] = {
                "name": defined_name.name,
                "attr_text": getattr(defined_name, "attr_text", None),
            }
        return names

    @staticmethod
    def _defined_names(workbook) -> List[Dict[str, Any]]:
        defined_names = []
        for defined_name in workbook.defined_names.values():
            defined_names.append({
                "name": defined_name.name,
                "attr_text": defined_name.attr_text,
                "local_sheet_id": defined_name.localSheetId,
                "hidden": bool(defined_name.hidden),
            })
        return defined_names
