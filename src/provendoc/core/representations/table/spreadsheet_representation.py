"""Structure-first, multi-granularity representation for the spreadsheet domain."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.parsers.table import contracts as sc
from provendoc.core.representations.narrative.granularity import (
    MultiGranularityOutput,
)


class SpreadsheetRepresentation(BaseRepresentation):
    """Render Spreadsheet facts without breaking table, row, or sheet boundaries.

    Fine units are row-level facts (one unit per source row); base units are
    logical regions/tables; coarse units are sheet context.  Length is never a
    primary split boundary.  Large regions remain represented as complete row
    groups with the same bound header context in every group.
    """

    def __init__(self, strategy=None, rows_per_base_unit: int = 200):
        """Initialize the spreadsheet representation.

        Args:
            strategy: Kept for compatibility (length is never a primary split boundary).
            rows_per_base_unit: Max rows per base unit (min 1); oversized
                regions split into complete row groups, each carrying the
                same bound header context.
        """
        self.strategy = strategy
        self.rows_per_base_unit = max(1, rows_per_base_unit)

    def build(self, document) -> RepresentationResult:
        """Build the three granularities (row/region/sheet) per the spreadsheet contract.

        Args:
            document: Document (metadata carries the table contract;
                returns an empty result if missing).

        Returns:
            RepresentationResult: representation_type="spreadsheet",
            content is a MultiGranularityOutput.
        """
        contract = sc.get_contract(document)
        if contract is None:
            return self._empty_result(document)

        workbook = contract["workbook"]
        sheets = workbook["sheets"]
        fine: List[RepresentationUnit] = []
        base: List[RepresentationUnit] = []
        coarse: List[RepresentationUnit] = []
        counters = {"fine": 0, "base": 0, "coarse": 0}

        for sheet in sheets:
            sheet_fine, sheet_base = self._build_sheet_units(
                sheet, document, counters,
            )
            fine.extend(sheet_fine)
            base.extend(sheet_base)
            coarse.append(self._build_sheet_context(
                sheet, document, counters["coarse"], sheet_base,
                workbook.get("defined_names") or [],
            ))
            counters["coarse"] += 1

        output = MultiGranularityOutput(
            coarse=coarse,
            base=base,
            fine=fine,
            fine_enabled=True,
        )
        return RepresentationResult(
            representation_type="spreadsheet",
            content=output,
            metadata={
                "chunk_count": len(base),
                "unit_count": len(base),
                "granularity_stats": output.stats(),
                "sheet_count": len(sheets),
                "region_count": sum(
                    len((sheet.get("canvas") or {}).get("regions") or [])
                    for sheet in sheets
                ),
                "source_kind": contract.get("source_kind"),
                "contract_version": contract.get("contract_version"),
                "representation_invariants": [
                    "row_atomicity",
                    "table_header_binding",
                    "region_boundary",
                    "cell_source_traceability",
                    "formula_preservation",
                    "merged_cell_integrity",
                    "sheet_context_preservation",
                    "cross_sheet_relation_preservation",
                ],
                "transforms_applied": [
                    "spreadsheet_structural_rendering",
                    "row_atomic_grouping",
                    "chart_unit_rendering",
                    "sheet_context_rendering",
                    "cross_sheet_relation_rendering",
                ],
            },
        )

    def _empty_result(self, document) -> RepresentationResult:
        output = MultiGranularityOutput(fine_enabled=True)
        return RepresentationResult(
            representation_type="spreadsheet",
            content=output,
            metadata={
                "chunk_count": 0,
                "unit_count": 0,
                "granularity_stats": output.stats(),
                "contract_source": "missing_or_invalid",
                "representation_invariants": ["cell_source_traceability"],
            },
        )

    def _build_sheet_units(
        self,
        sheet: Dict[str, Any],
        document,
        counters: Dict[str, int],
    ) -> Tuple[List[RepresentationUnit], List[RepresentationUnit]]:
        canvas = sheet.get("canvas") or {}
        cells = canvas.get("cells") or []
        rows = canvas.get("rows") or []
        regions = canvas.get("regions") or []
        cells_by_position = {(cell["row"], cell["column"]): cell for cell in cells}
        cells_by_id = {cell["id"]: cell for cell in cells}
        rows_by_index = {row["row_index"]: row for row in rows}
        assigned_rows: set[Any] = set()
        fine: List[RepresentationUnit] = []
        base: List[RepresentationUnit] = []

        for region in regions:
            region_rows = self._region_rows(region, rows_by_index)
            if not region_rows:
                continue
            assigned_rows.update(row["row_index"] for row in region_rows)
            headers = self._headers(region, cells_by_position, cells_by_id)
            for row in region_rows:
                if row["row_index"] == region.get("header_row_index"):
                    continue
                text = self._render_row(region, row, headers, cells_by_position)
                if not text:
                    continue
                fine.append(self._make_unit(
                    text=text,
                    document=document,
                    sheet=sheet,
                    granularity="fine",
                    sequence_index=counters["fine"],
                    locality_scope="row",
                    row=row,
                    metadata={
                        "region_id": region["id"],
                        "region_type": region.get("region_type"),
                        "region_range": region.get("range"),
                        "row_index": row["row_index"],
                        "header_binding": headers,
                        "cell_ids": self._row_cell_ids(region, row, cells_by_position),
                    },
                ))
                counters["fine"] += 1

            data_rows = [
                row
                for row in region_rows
                if row["row_index"] != region.get("header_row_index")
            ]
            for group_index, row_group in enumerate(self._row_groups(data_rows)):
                text = self._render_region_group(
                    region, row_group, headers, cells_by_position,
                )
                if not text:
                    continue
                # The first row group owns the header row's cell ids: a
                # formula written *in* a header cell keeps its [formula:]
                # marker in this unit's rendered headers (prereg D7), and
                # header cells stay traceable exactly once.
                header_cell_ids = (
                    self._header_cell_ids(region, rows_by_index, cells_by_position)
                    if group_index == 0 else []
                )
                base.append(self._make_unit(
                    text=text,
                    document=document,
                    sheet=sheet,
                    granularity="base",
                    sequence_index=counters["base"],
                    locality_scope="table" if region.get("region_type") == "table" else "region",
                    row=row_group[0],
                    metadata={
                        "region_id": region["id"],
                        "region_type": region.get("region_type"),
                        "region_confidence": region.get("confidence"),
                        "region_range": region.get("range"),
                        "row_start": row_group[0]["row_index"],
                        "row_end": row_group[-1]["row_index"],
                        "row_count": len(row_group),
                        "header_binding": headers,
                        "cell_ids": header_cell_ids + [
                            cell_id
                            for grouped_row in row_group
                            for cell_id in self._row_cell_ids(
                                region, grouped_row, cells_by_position,
                            )
                        ],
                    },
                ))
                counters["base"] += 1

        # Isolated blank or unassigned rows have no semantic representation;
        # non-empty rows outside a detected region are retained as unknown
        # source-addressable units rather than being silently discarded.
        for row in rows:
            if row["row_index"] in assigned_rows or row.get("is_blank"):
                continue
            orphan_region = {
                "id": f"{sheet['id']}!orphan:{row['row_index']}",
                "region_type": "unknown",
                "range": f"row:{row['row_index']}",
                "bounds": {
                    "start_row": row["row_index"],
                    "end_row": row["row_index"],
                    "start_column": 1,
                    "end_column": canvas.get("shape", {}).get("columns", 0),
                },
            }
            text = self._render_row(orphan_region, row, [], cells_by_position)
            if not text:
                continue
            fine.append(self._make_unit(
                text=text,
                document=document,
                sheet=sheet,
                granularity="fine",
                sequence_index=counters["fine"],
                locality_scope="row",
                row=row,
                metadata={
                    "region_id": orphan_region["id"],
                    "region_type": "unknown",
                    "row_index": row["row_index"],
                    "cell_ids": row.get("cell_ids") or [],
                },
            ))
            counters["fine"] += 1

        # spreadsheet_canvas_v2 prereg v1 (D1): every chart is a first-class
        # base unit (region_type="chart", locality_scope="region") so its
        # series/axis/legend facts are retrievable, not just counted.
        for chart in canvas.get("charts") or []:
            text = self._render_chart(chart)
            if not text:
                continue
            base.append(self._make_unit(
                text=text,
                document=document,
                sheet=sheet,
                granularity="base",
                sequence_index=counters["base"],
                locality_scope="region",
                row=None,
                metadata={
                    "region_id": chart["id"],
                    "region_type": "chart",
                    "region_confidence": 1.0,
                    "region_range": f"anchor:{chart.get('anchor') or 'unanchored'}",
                    "chart_id": chart["id"],
                    "chart_type": chart.get("chart_type"),
                    "chart_anchor": chart.get("anchor"),
                    "series_count": chart.get("series_count"),
                    "cell_ids": [],
                },
            ))
            counters["base"] += 1
        return fine, base

    def _build_sheet_context(
        self,
        sheet: Dict[str, Any],
        document,
        sequence_index: int,
        base_units: Sequence[RepresentationUnit],
        defined_names: Sequence[Dict[str, Any]] = (),
    ) -> RepresentationUnit:
        """Render the sheet-level "what is where" inventory.

        The first four lines are format-frozen (sheet name / visibility /
        canvas size / counts of regions, charts, relations), followed by
        itemized detail: each region with its headers, merged cells,
        frozen panes, hidden rows/columns, each chart, each cross-sheet
        relation, and defined names referenced from this sheet.
        """
        canvas = sheet.get("canvas") or {}
        shape = canvas.get("shape") or {}
        regions = canvas.get("regions") or []
        charts = canvas.get("charts") or []
        relations = sheet.get("relations") or []
        lines = [
            f"Sheet: {sheet.get('name')}",
            f"Visibility: {sheet.get('visibility', 'visible')}",
            f"Canvas: {shape.get('rows', 0)} rows × {shape.get('columns', 0)} columns",
            f"Regions: {len(regions)}; Charts: {len(charts)}; Relations: {len(relations)}",
        ]
        if regions:
            lines.append("Regions:")
            cells_by_id = {
                cell["id"]: cell
                for cell in canvas.get("cells") or []
            }
            for region in regions:
                line = f"- {region.get('region_type', 'unknown')} {region.get('range', '')}"
                headers = region.get("headers") or []
                if headers:
                    line += " (headers: " + " | ".join(
                        self._header_name(header, cells_by_id)
                        for header in headers
                    ) + ")"
                lines.append(line)
        merged_cells = canvas.get("merged_cells") or []
        if merged_cells:
            lines.append("Merged: " + ", ".join(
                entry.get("range", "") for entry in merged_cells
            ))
        freeze_panes = canvas.get("freeze_panes")
        if freeze_panes:
            lines.append(
                f"Freeze panes: {freeze_panes.get('cell')} "
                f"({freeze_panes.get('frozen_rows', 0)} rows, "
                f"{freeze_panes.get('frozen_columns', 0)} columns)"
            )
        hidden_rows = canvas.get("hidden_rows") or []
        if hidden_rows:
            lines.append("Hidden rows: " + ", ".join(
                str(index) for index in hidden_rows
            ))
        hidden_columns = canvas.get("hidden_columns") or []
        if hidden_columns:
            lines.append("Hidden columns: " + ", ".join(
                str(key) for key in hidden_columns
            ))
        if charts:
            lines.append("Charts:")
            for chart in charts:
                line = (
                    f"- {chart.get('id')} {chart.get('chart_type')} "
                    f"at {chart.get('anchor') or 'unanchored'}"
                )
                if chart.get("title"):
                    line += f": {chart['title']}"
                line += f" ({chart.get('series_count', 0)} series)"
                lines.append(line)
        cross_sheet = self._cross_sheet_relation_lines(sheet, relations)
        if cross_sheet:
            lines.append("Cross-sheet relations:")
            lines.extend(cross_sheet)
        defined_name_lines = self._defined_name_lines(relations, defined_names)
        if defined_name_lines:
            lines.append("Defined names used:")
            lines.extend(defined_name_lines)
        summary = "\n".join(lines)
        return self._make_unit(
            text=summary,
            document=document,
            sheet=sheet,
            granularity="coarse",
            sequence_index=sequence_index,
            locality_scope="sheet",
            row=None,
            metadata={
                "sheet_id": sheet.get("id"),
                "sheet_visibility": sheet.get("visibility", "visible"),
                "canvas_shape": shape,
                "region_ids": [region.get("id") for region in regions],
                "chart_ids": [chart.get("id") for chart in charts],
                "relation_ids": [relation.get("id") for relation in relations],
                "cross_sheet_relation_count": len(cross_sheet),
                "base_unit_ids": [
                    unit.semantic_primitives.identity for unit in base_units
                ],
            },
        )

    def _make_unit(
        self,
        *,
        text: str,
        document,
        sheet: Dict[str, Any],
        granularity: Literal["coarse", "base", "fine"],
        sequence_index: int,
        locality_scope: str,
        row: Optional[Dict[str, Any]],
        metadata: Dict[str, Any],
    ) -> RepresentationUnit:
        meta = document.metadata or {}
        file_name = meta.get("file_name") or Path(meta.get("file_path", "")).name or None
        line_start = row.get("physical_line_start") if row else None
        line_end = row.get("physical_line_end") if row else None
        identity = f"{sheet.get('id')}:{granularity}:{sequence_index}"
        hierarchy = [str(sheet.get("name", "Sheet"))]
        guarantees = [
            "row_atomicity",
            "table_header_binding",
            "region_boundary",
            "cell_source_traceability",
            "formula_preservation",
            "merged_cell_integrity",
            "sheet_context_preservation",
            "cross_sheet_relation_preservation",
        ]
        return RepresentationUnit(
            text=text,
            unit_type=UnitType.TABLE,
            granularity=granularity,
            sequence_index=sequence_index,
            heading=str(sheet.get("name", "")),
            heading_path=hierarchy,
            source_context=SourceContext(
                file_name=file_name,
                file_type=meta.get("file_type"),
                line_start=line_start,
                line_end=line_end,
            ),
            semantic_primitives=SemanticPrimitives(
                identity=identity,
                hierarchy_path=hierarchy,
                locality_scope=locality_scope,
                semantic_role="body",
                integrity_guarantees=guarantees,
            ),
            metadata=metadata,
        )

    @staticmethod
    def _region_rows(
        region: Dict[str, Any], rows_by_index: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        bounds = region.get("bounds") or {}
        start = bounds.get("start_row")
        end = bounds.get("end_row")
        if start is None or end is None:
            return []
        return [
            rows_by_index[index]
            for index in range(start, end + 1)
            if index in rows_by_index and not rows_by_index[index].get("is_blank")
        ]

    @staticmethod
    def _headers(
        region: Dict[str, Any],
        cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
        cells_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Region headers, with cached values attached for formula headers.

        When a header cell holds a formula: ``display_value`` is the raw
        formula text; attach the cached computed value (``display``) here
        so the renderer can use the computed value as the name while
        preserving the original via a ``[formula: ...]`` marker.
        """
        headers = [
            dict(header)
            for header in (
                region.get("headers")
                or sc.region_headers(region, cells_by_position)
            )
        ]
        cells_by_id = cells_by_id or {}
        for header in headers:
            if not (header.get("name") or "").startswith("="):
                continue
            cell_id = header.get("cell_id")
            cell = cells_by_id.get(cell_id) if isinstance(cell_id, str) else None
            if cell is not None and cell.get("cached_value") is not None:
                header["display"] = cell["cached_value"]
        return headers

    @staticmethod
    def _header_name(
        header: Dict[str, Any],
        cells_by_id: Dict[str, Dict[str, Any]],
    ) -> str:
        """Computed-value name for a formula header, looked up on demand."""
        name = header.get("name") or ""
        if name.startswith("="):
            cell_id = header.get("cell_id")
            cell = cells_by_id.get(cell_id) if isinstance(cell_id, str) else None
            if cell is not None and cell.get("cached_value") is not None:
                return str(cell["cached_value"])
        return name

    @staticmethod
    def _header_display_name(header: Dict[str, Any]) -> str:
        """Rendered header name: computed value when the header is a formula."""
        name = header.get("name") or ""
        if name.startswith("=") and header.get("display") is not None:
            return str(header["display"])
        return name

    def _header_cell_ids(
        self,
        region: Dict[str, Any],
        rows_by_index: Dict[int, Dict[str, Any]],
        cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
    ) -> List[str]:
        """Cell ids of the region's header row (empty when absent/blank)."""
        header_row_index = region.get("header_row_index")
        if header_row_index is None:
            return []
        row = rows_by_index.get(header_row_index)
        if row is None or row.get("is_blank"):
            return []
        return self._row_cell_ids(region, row, cells_by_position)

    @staticmethod
    def _cross_sheet_relation_lines(
        sheet: Dict[str, Any],
        relations: Sequence[Dict[str, Any]],
    ) -> List[str]:
        """One line per outbound relation target on another sheet (D6)."""
        sheet_name = sheet.get("name")
        lines: List[str] = []
        for relation in relations:
            source = (
                relation.get("source_cell_id")
                or relation.get("source_chart_id")
                or relation.get("source_sheet")
            )
            for target in relation.get("targets") or []:
                target_sheet = target.get("sheet")
                if not target_sheet or target_sheet == sheet_name:
                    continue
                line = (
                    f"- {source} → {target_sheet}!{target.get('coordinate')}"
                )
                if target.get("via"):
                    line += f" (via {target['via']})"
                lines.append(line)
        return lines

    @staticmethod
    def _defined_name_lines(
        relations: Sequence[Dict[str, Any]],
        defined_names: Sequence[Dict[str, Any]],
    ) -> List[str]:
        """Defined names referenced from this sheet's relations.

        Only names actually referenced by this sheet's relations are listed:
        the coarse layer is an inventory, not the full dictionary.
        """
        used = set()
        for relation in relations:
            for target in relation.get("targets") or []:
                via = target.get("via") or ""
                if via.startswith("defined_name:"):
                    used.add(via[len("defined_name:"):])
        if not used:
            return []
        return [
            f"- {entry.get('name')} = {entry.get('attr_text')}"
            for entry in defined_names
            if entry.get("name") in used
        ]

    def _row_groups(self, rows: Sequence[Dict[str, Any]]) -> Iterable[Sequence[Dict[str, Any]]]:
        data_rows = list(rows)
        for start in range(0, len(data_rows), self.rows_per_base_unit):
            yield data_rows[start:start + self.rows_per_base_unit]

    @staticmethod
    def _row_cell_ids(
        region: Dict[str, Any], row: Dict[str, Any], cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
    ) -> List[str]:
        bounds = region.get("bounds") or {}
        return [
            cell["id"]
            for column in range(bounds.get("start_column", 1), bounds.get("end_column", 0) + 1)
            if (cell := cells_by_position.get((row["row_index"], column))) is not None
        ]

    def _render_region_group(
        self,
        region: Dict[str, Any],
        rows: Sequence[Dict[str, Any]],
        headers: Sequence[Dict[str, Any]],
        cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
    ) -> str:
        if not rows:
            return ""
        title = f"Region {region.get('range', '')} ({region.get('region_type', 'unknown')})"
        header_text = self._render_header(headers)
        row_text = "\n".join(
            self._render_row(region, row, headers, cells_by_position)
            for row in rows
        )
        return "\n".join(part for part in (title, header_text, row_text) if part)

    def _render_chart(self, chart: Dict[str, Any]) -> str:
        """Render chart facts as a retrievable base-unit text.

        Chart text carries: type / anchor / title / per-series names with
        value/category references / legend / axis titles / bar direction.
        All come from ``inference`` facts; missing items are omitted.
        """
        lines = [
            f"Chart {chart.get('id')} ({chart.get('chart_type')}) "
            f"anchored at {chart.get('anchor') or 'unanchored'}"
        ]
        if chart.get("title"):
            lines.append(f"Title: {chart['title']}")
        inference = chart.get("inference") or {}
        for index, series in enumerate(inference.get("series") or [], start=1):
            line = f"Series {index}"
            if series.get("name"):
                line += f": {series['name']}"
            ref_parts = []
            if series.get("value_ref"):
                ref_parts.append(f"values {series['value_ref']}")
            if series.get("category_ref"):
                ref_parts.append(f"categories {series['category_ref']}")
            if series.get("name_ref"):
                ref_parts.append(f"name {series['name_ref']}")
            if ref_parts:
                line += " (" + "; ".join(ref_parts) + ")"
            lines.append(line)
        if inference.get("legend"):
            lines.append(f"Legend: {inference['legend']}")
        axes = inference.get("axes") or {}
        if axes:
            axis_parts = [
                f"{key}={axis.get('title')}"
                for key, axis in sorted(axes.items())
            ]
            lines.append("Axes: " + ", ".join(axis_parts))
        if inference.get("grouping"):
            lines.append(f"Grouping: {inference['grouping']}")
        if inference.get("bar_direction"):
            lines.append(f"Bar direction: {inference['bar_direction']}")
        return "\n".join(lines)

    def _render_row(
        self,
        region: Dict[str, Any],
        row: Dict[str, Any],
        headers: Sequence[Dict[str, Any]],
        cells_by_position: Dict[Tuple[int, int], Dict[str, Any]],
    ) -> str:
        bounds = region.get("bounds") or {}
        header_by_column = {
            header["column"]: self._header_display_name(header)
            for header in headers
        }
        entries = []
        for column in range(bounds.get("start_column", 1), bounds.get("end_column", 0) + 1):
            cell = cells_by_position.get((row["row_index"], column))
            if cell is None or cell.get("is_blank"):
                continue
            key = header_by_column.get(column, cell.get("coordinate"))
            value = self._cell_text(cell)
            entries.append(f"{key}={value}")
        return f"Row {row['row_index']}: " + "; ".join(entries) if entries else ""

    @staticmethod
    def _render_header(headers: Sequence[Dict[str, Any]]) -> str:
        if not headers:
            return ""
        parts = []
        for header in headers:
            name = header.get("name") or ""
            if name.startswith("="):
                # Formula written in a header cell: rendered name is the
                # computed value when a cached one exists; either way the
                # formula stays preserved via its marker (prereg D7).
                display = header.get("display")
                if display is not None:
                    parts.append(
                        f"{header['coordinate']}={display} [formula: {name}]"
                    )
                else:
                    parts.append(f"{header['coordinate']}={name} [formula: {name}]")
                continue
            parts.append(f"{header['coordinate']}={name}")
        return "Headers: " + " | ".join(parts)

    @staticmethod
    def _cell_text(cell: Dict[str, Any]) -> str:
        display = cell.get("display_value")
        if display is None:
            display = ""
        if cell.get("formula"):
            return f"{display} [formula: {cell['formula']}]"
        return str(display)
