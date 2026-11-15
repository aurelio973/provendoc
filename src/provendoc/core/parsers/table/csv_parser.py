# ============================================================
# Spreadsheet Domain
# CSV / TSV / PSV parser
# ============================================================
"""Delimited-text parsing into the shared Spreadsheet canvas contract."""
from __future__ import annotations

import csv
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from charset_normalizer import from_path

try:
    import pandas as pd
except ImportError:
    pd = None

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.table import contracts as sc
from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.spreadsheet_unresolved import (
    derive_spreadsheet_unresolved,
)

_log = logging.getLogger(__name__)


class CsvParser(BaseParser):
    """Parse delimited text as a single-sheet Spreadsheet workbook.

    CSV-specific provenance is retained through the mapping from each logical
    record to the physical source line span.  The legacy ``table_data`` field
    remains available for callers that have not migrated to ``metadata[
    'spreadsheet']`` yet.
    """

    def __init__(
        self,
        encoding: str = "utf-8-sig",
        engine: str = "stdlib",
        delimiter: Optional[str] = None,
        quotechar: str = '"',
        has_header: Optional[bool] = None,
        tolerant: bool = True,
        limits: Optional[TableRuntimeLimits] = None,
    ):
        """Initialize with CSV reading parameters such as encoding, engine, delimiter.

        ``limits`` are resource guards (guards, not filters; default None means historical behaviour,
        prereg oversize_structure_limits_prereg_v1): max_sheets does not apply
        (always a single table), while max_rows_per_sheet/max_columns_per_sheet/
        max_cells_per_sheet take effect during streaming reads.
        """
        self.encoding = encoding
        self.engine = engine
        self.delimiter = delimiter
        self.quotechar = quotechar
        self.has_header = has_header
        self.tolerant = tolerant
        self.limits = limits
        self._guard_reasons: List[str] = []
        self._guard_facts: List[Dict[str, Any]] = []

    def parse(self, file_path) -> Document:
        """Parse a delimited file into a source-addressable Spreadsheet document."""
        start_time = time.perf_counter()
        self._guard_reasons = []
        self._guard_facts = []
        ctx = self._read_and_diagnose(
            file_path, "csv", self.encoding, self.tolerant,
        )
        metadata = ctx.metadata
        delimiter = self._detect_delimiter(file_path)

        try:
            records, delimiter, declared_separator = self._read_records(
                file_path, delimiter,
            )
            sheet = self._build_sheet(file_path, records)
            spreadsheet = sc.build_workbook_contract(
                source_kind="delimited_text",
                file_name=Path(file_path).name,
                sheets=[sheet],
                extra={
                    "dialect": {
                        "delimiter": delimiter,
                        "quotechar": self.quotechar,
                        "declared_separator": declared_separator,
                        "engine": self.engine,
                    },
                },
            )
            legacy_table_data = self._legacy_table_data(sheet)
        except Exception as exc:
            error = f"读取失败：{exc}"
            spreadsheet = sc.build_workbook_contract(
                source_kind="delimited_text",
                file_name=Path(file_path).name,
                sheets=[],
                extra={"parse_error": error},
            )
            legacy_table_data = {"_error": error}
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(exc)

        self._collect_resource_guard(metadata)
        # Object-level unresolved registration (spreadsheet_unresolved_prereg_v1):
        # derived from resource_guard structured facts, never parsing the reasons strings;
        # guarded silent degradation, never blocking the main parse (epub wiring precedent).
        try:
            stamp_unresolved_registry(
                metadata, "spreadsheet_unresolved",
                derive_spreadsheet_unresolved(metadata),
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            _log.debug("spreadsheet unresolved registry skipped: %s", exc)
        metadata["spreadsheet"] = spreadsheet
        metadata["table_data"] = legacy_table_data
        metadata["csv_engine"] = self.engine
        metadata["csv_delimiter"] = delimiter
        metadata["parse_duration_ms"] = round(
            (time.perf_counter() - start_time) * 1000, 3,
        )
        return Document(
            text=ctx.text,
            metadata=metadata,
            source_text=ctx.source_text,
        )

    def _collect_resource_guard(self, metadata):
        """Record resource-guard facts (guards, not filters).

        ``resource_guard`` is written only when a guard actually fires; the default path adds zero new keys.
        The structured ``facts`` sub-keys (spreadsheet_unresolved_prereg_v1)
        are persisted on the same trigger surface, for 1:1 derivation of the object-level unresolved registration.
        """
        if self.limits is None or not self._guard_reasons:
            return
        resource_guard = {
            "scheme": "resource_limits_v1",
            "limits": self.limits.as_dict(),
            "reasons": list(self._guard_reasons),
        }
        if self._guard_facts:
            resource_guard["facts"] = [dict(fact) for fact in self._guard_facts]
        metadata["resource_guard"] = resource_guard

    # ==================== encoding / dialect ====================

    def _detect_encoding(self, file_path: str) -> str:
        try:
            result = from_path(file_path).best()
            if result and result.encoding:
                return result.encoding
        # charset-detection libraries can raise arbitrary exceptions on weird bytes; on failure fall back to the declared encoding.
        except Exception:
            pass
        return self.encoding

    def _detect_delimiter(self, file_path: str) -> str:
        if self.delimiter is not None:
            return self.delimiter
        suffix = Path(file_path).suffix.lower()
        if suffix == ".tsv":
            return "\t"
        if suffix == ".psv":
            return "|"
        try:
            encoding = self._detect_encoding(file_path)
            with open(file_path, "r", encoding=encoding, errors="replace", newline="") as handle:
                sample = handle.read(4096)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            return dialect.delimiter
        except (csv.Error, OSError, UnicodeError):
            return ","

    def _read_records(
        self, file_path: str, delimiter: str,
    ) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
        """Read logical CSV records with physical line spans preserved."""
        encoding = self._detect_encoding(file_path)
        with open(
            file_path, "r", encoding=encoding, errors="replace", newline="",
        ) as handle:
            first_line = handle.readline()
            declared_separator = None
            if first_line.lower().startswith("sep=") and len(first_line.rstrip("\r\n")) > 4:
                declared_separator = first_line.rstrip("\r\n")[4:]
                delimiter = declared_separator
            else:
                handle.seek(0)

            line_offset = 1 if declared_separator is not None else 0
            reader = csv.reader(handle, delimiter=delimiter, quotechar=self.quotechar)
            limits = self.limits
            row_cap = limits.max_rows_per_sheet if limits is not None else None
            col_cap = limits.max_columns_per_sheet if limits is not None else None
            cell_budget = limits.max_cells_per_sheet if limits is not None else None
            cells_used = 0
            column_guard_emitted = False
            records: List[Dict[str, Any]] = []
            try:
                for logical_index, values in enumerate(reader, start=1):
                    if row_cap is not None and logical_index > row_cap:
                        self._guard_reasons.append(
                            f"records beyond {row_cap} not extracted "
                            f"(max_rows_per_sheet={row_cap})"
                        )
                        self._guard_facts.append({
                            "kind": "records_beyond_row_cap",
                            "axis": "logical_record",
                            "first_unextracted": logical_index,
                            "max_rows_per_sheet": row_cap,
                        })
                        break
                    if col_cap is not None and len(values) > col_cap:
                        if not column_guard_emitted:
                            column_guard_emitted = True
                            self._guard_reasons.append(
                                f"record columns truncated to {col_cap} "
                                f"(max_columns_per_sheet={col_cap}, first seen at "
                                f"record {logical_index} with {len(values)} columns)"
                            )
                            self._guard_facts.append({
                                "kind": "record_columns_truncated",
                                "axis": "logical_record",
                                "at": logical_index,
                                "record_columns": len(values),
                                "max_columns_per_sheet": col_cap,
                            })
                        values = values[:col_cap]
                    if cell_budget is not None and cells_used + len(values) > cell_budget:
                        self._guard_reasons.append(
                            f"records from record {logical_index} on not extracted "
                            f"(cumulative cell budget {cell_budget} would be exceeded; "
                            f"{cells_used} cells already extracted)"
                        )
                        self._guard_facts.append({
                            "kind": "records_beyond_cell_budget",
                            "axis": "logical_record",
                            "first_unextracted": logical_index,
                            "cells_extracted": cells_used,
                            "max_cells_per_sheet": cell_budget,
                        })
                        break
                    cells_used += len(values)
                    line_end = reader.line_num + line_offset
                    line_start = line_end
                    if records:
                        line_start = records[-1]["physical_line_end"] + 1
                    records.append({
                        "logical_record_index": logical_index,
                        "physical_line_start": line_start,
                        "physical_line_end": line_end,
                        "values": list(values),
                    })
            except csv.Error:
                if not self.tolerant:
                    raise
                # Salvage is intentionally record-local: malformed records must
                # not make us reinterpret every physical line as a new table row.
                handle.seek(0)
                lines = handle.readlines()
                if declared_separator:
                    lines = lines[1:]
                records = []
                cells_used = 0
                column_guard_emitted = False
                for physical_line, line in enumerate(lines, start=1 + line_offset):
                    if row_cap is not None and len(records) >= row_cap:
                        self._guard_reasons.append(
                            f"records beyond {row_cap} not extracted "
                            f"(max_rows_per_sheet={row_cap})"
                        )
                        self._guard_facts.append({
                            "kind": "records_beyond_row_cap",
                            "axis": "physical_line",
                            "first_unextracted": physical_line,
                            "max_rows_per_sheet": row_cap,
                        })
                        break
                    try:
                        values = next(csv.reader([line], delimiter=delimiter, quotechar=self.quotechar))
                    except csv.Error:
                        values = [line.rstrip("\r\n")]
                    if col_cap is not None and len(values) > col_cap:
                        if not column_guard_emitted:
                            column_guard_emitted = True
                            self._guard_reasons.append(
                                f"record columns truncated to {col_cap} "
                                f"(max_columns_per_sheet={col_cap}, first seen at "
                                f"physical line {physical_line} with {len(values)} columns)"
                            )
                            self._guard_facts.append({
                                "kind": "record_columns_truncated",
                                "axis": "physical_line",
                                "at": physical_line,
                                "record_columns": len(values),
                                "max_columns_per_sheet": col_cap,
                            })
                        values = values[:col_cap]
                    if cell_budget is not None and cells_used + len(values) > cell_budget:
                        self._guard_reasons.append(
                            f"records from physical line {physical_line} on not extracted "
                            f"(cumulative cell budget {cell_budget} would be exceeded; "
                            f"{cells_used} cells already extracted)"
                        )
                        self._guard_facts.append({
                            "kind": "records_beyond_cell_budget",
                            "axis": "physical_line",
                            "first_unextracted": physical_line,
                            "cells_extracted": cells_used,
                            "max_cells_per_sheet": cell_budget,
                        })
                        break
                    cells_used += len(values)
                    records.append({
                        "logical_record_index": len(records) + 1,
                        "physical_line_start": physical_line,
                        "physical_line_end": physical_line,
                        "values": values,
                    })
        return records, delimiter, declared_separator

    # ==================== spreadsheet contract ====================

    def _build_sheet(
        self, file_path: str, records: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        sheet_name = Path(file_path).stem
        raw_rows = [record["values"] for record in records]
        has_header = self._resolve_has_header(raw_rows)
        header_record = records[0] if records and has_header else None
        column_count = max((len(row) for row in raw_rows), default=0)

        rows: List[Dict[str, Any]] = []
        cells: List[Dict[str, Any]] = []
        for record in records:
            row_index = record["logical_record_index"]
            values = record["values"]
            cell_ids = []
            for column_index in range(1, column_count + 1):
                raw_value = values[column_index - 1] if column_index <= len(values) else None
                cell = sc.make_cell(
                    sheet_name=sheet_name,
                    row=row_index,
                    column=column_index,
                    raw_value=raw_value,
                    physical_line_start=record["physical_line_start"],
                    physical_line_end=record["physical_line_end"],
                )
                cells.append(cell)
                cell_ids.append(cell["id"])
            rows.append({
                "id": f"{sheet_name}!row:{row_index}",
                "row_index": row_index,
                "logical_record_index": record["logical_record_index"],
                "physical_line_start": record["physical_line_start"],
                "physical_line_end": record["physical_line_end"],
                "is_blank": not any(value.strip() for value in values if isinstance(value, str)),
                "cell_ids": cell_ids,
            })

        canvas = sc.build_canvas(
            canvas_id=f"{sheet_name}!canvas:1",
            shape_rows=len(records),
            shape_columns=column_count,
            cells=cells,
            rows=rows,
        )
        regions = sc.detect_regions(cells, sheet_name=sheet_name)
        if header_record is not None:
            regions = sc.apply_explicit_header(
                regions,
                header_row_index=header_record["logical_record_index"],
            )
        canvas["regions"] = regions

        cells_by_position = {(cell["row"], cell["column"]): cell for cell in cells}
        for region in regions:
            if region.get("region_type") == "table":
                region["headers"] = sc.region_headers(region, cells_by_position)

        return {
            "id": f"sheet:1:{sheet_name}",
            "name": sheet_name,
            "sheet_index": 0,
            "visibility": "visible",
            "canvas": canvas,
            "relations": [],
        }

    def _resolve_has_header(self, raw_rows: Sequence[Sequence[str]]) -> bool:
        if self.has_header is not None:
            return self.has_header
        return self._detect_header_simple(raw_rows[0]) if raw_rows else False

    @staticmethod
    def _detect_header_simple(first_row: Sequence[Any]) -> bool:
        if not first_row:
            return False
        nonempty = [str(value).strip() for value in first_row if str(value).strip()]
        if not nonempty:
            return False
        numeric_count = 0
        for value in nonempty:
            inferred_type, _ = sc.infer_scalar(value)
            numeric_count += inferred_type in {"integer", "number"}
        return numeric_count < len(nonempty) / 2

    @staticmethod
    def _deduplicate_headers(headers: Sequence[Any]) -> List[str]:
        seen: Dict[str, int] = {}
        result: List[str] = []
        for index, header in enumerate(headers, start=1):
            value = str(header).strip().lstrip("\ufeff") or f"column_{index}"
            seen[value] = seen.get(value, 0) + 1
            result.append(value if seen[value] == 1 else f"{value}_{seen[value]}")
        return result

    def _legacy_table_data(self, sheet: Dict[str, Any]) -> Dict[str, Any]:
        """Expose the original compact CSV structure for backward compatibility."""
        canvas = sheet["canvas"]
        rows = canvas["rows"]
        cells = {(cell["row"], cell["column"]): cell for cell in canvas["cells"]}
        regions = [region for region in canvas["regions"] if region.get("region_type") == "table"]
        table_region = regions[0] if regions else None
        header_row = table_region.get("header_row_index") if table_region else None
        if header_row:
            headers = self._deduplicate_headers([
                cells.get((header_row, column), {}).get("display_value")
                for column in range(1, canvas["shape"]["columns"] + 1)
            ])
        else:
            headers = [f"column_{column}" for column in range(1, canvas["shape"]["columns"] + 1)]

        data_rows = []
        for row in rows:
            if header_row and row["row_index"] == header_row:
                continue
            if row["is_blank"]:
                continue
            values = {
                header: cells.get((row["row_index"], index), {}).get("raw_value")
                for index, header in enumerate(headers, start=1)
            }
            data_rows.append({"row_index": row["row_index"], "values": values})
        return {"sheet_name": sheet["name"], "headers": headers, "rows": data_rows}

    # Compatibility methods retained for external callers using the old parser API.
    def _load_csv_stdlib(self, file_path, delimiter):
        records, _, _ = self._read_records(file_path, delimiter)
        return self._legacy_table_data(self._build_sheet(file_path, records))

    def _load_csv_pandas(self, file_path, delimiter):
        if pd is None:
            raise ImportError("未安装pandas，请先pip install pandas")
        return self._load_csv_stdlib(file_path, delimiter)
