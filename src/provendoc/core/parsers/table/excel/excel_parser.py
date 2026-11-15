"""Excel parser producing Spreadsheet canvas facts with a legacy compatibility view."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    build_carrier_facts,
    detect_security_carriers,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)

# ZIP container guard (security checklist §75 v1 zip-bomb protection): central-directory limits adjudicated before extraction.
# Applies only to ZIP-container suffixes (.xls is OLE / .parquet is not zip, not gated).
from provendoc.core.parsers.container_guard import (
    check_zip_package_quietly as _check_zip_package_safe,
)
from provendoc.core.parsers.container_guard import (
    zip_guard_error,
)
from provendoc.core.parsers.table import contracts as sc
from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.backends.base import SpreadsheetBackend
from provendoc.core.parsers.table.excel.backends.odf_backend import OdfBackend
from provendoc.core.parsers.table.excel.backends.openpyxl_backend import OpenpyxlBackend
from provendoc.core.parsers.table.excel.backends.pandas_backend import PandasBackend
from provendoc.core.parsers.table.excel.backends.pyxlsb_backend import PyxlsbBackend
from provendoc.core.parsers.table.excel.backends.xlrd_backend import XlrdBackend
from provendoc.core.parsers.table.excel.formula_refs import formula_relations
from provendoc.core.parsers.table.spreadsheet_unresolved import (
    derive_spreadsheet_unresolved,
)

# ZIP-container suffixes (excel-family members that travel in a zip container; .xls is OLE,
# .parquet is a columnar format — neither is on this list).
_ZIP_CONTAINER_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xlsb", ".ods"})

_log = logging.getLogger(__name__)


class ExcelParser(BaseParser):
    """Parse a workbook as ordered sheets, canvases, regions, and relations.

    ``metadata['spreadsheet']`` is the canonical contract.  ``excel_data`` is
    retained as a compact, compatibility-only projection for existing callers.
    """

    file_type = "excel"

    def __init__(
        self,
        engine: str = "openpyxl",
        has_header=None,
        chunk_size: int = 200,
        skip_empty_rows: bool = True,
        read_only: bool = True,
        limits: Optional[TableRuntimeLimits] = None,
    ):
        """Initialize with engine, header strategy, block size and other parameters.

        ``limits`` are resource guards (guards, not filters; default None means historical behaviour).
        All five engines are wired (prereg table_backends_limits_prereg_v1):
        openpyxl has foreknown declared shapes, isomorphic in four dimensions; xlrd/pandas declared values
        are also foreknown (pandas after parse); pyxlsb/odf streaming early-stops go through the new kinds.
        Each backend adjudicates its own applicability; non-applicable dimensions never fire, never register.
        """
        self.engine = engine
        self.has_header = has_header
        self.chunk_size = chunk_size
        self.skip_empty_rows = skip_empty_rows
        self.read_only = read_only
        self.limits = limits
        # Declared via the structural contract (openpyxl/pandas/xlrd/pyxlsb/odf and the parquet subclass's
        # _ParquetBackend all implement load(file_path); see backends/base.py)
        self.backend: SpreadsheetBackend
        if engine == "openpyxl":
            self.backend = OpenpyxlBackend(read_only=read_only, limits=limits)
        elif engine == "pandas":
            self.backend = PandasBackend(limits=limits)
        elif engine == "xlrd":
            self.backend = XlrdBackend(limits=limits)
        elif engine == "pyxlsb":
            self.backend = PyxlsbBackend(limits=limits)
        elif engine == "odf":
            self.backend = OdfBackend(limits=limits)
        else:
            raise ValueError(f"Unsupported excel engine: {engine}")

    def parse(self, file_path) -> Document:
        """Parse an Excel workbook without collapsing its two-dimensional layout.

        Shared pipeline for the whole ExcelParser family (ods/parquet
        subclass it): backend → spreadsheet contract → legacy documents
        → text.  Subclasses customize it through the LEGACY_DATA_KEY /
        SOURCE_KIND class attributes and the _collect_backend_metadata
        hook instead of copying the whole method.
        """
        start_time = time.perf_counter()
        metadata = _build_metadata(file_path)
        metadata["file_type"] = self.file_type
        metadata["engine"] = self.engine
        metadata["has_header"] = self.has_header
        metadata["chunk_size"] = self.chunk_size

        # ZIP container guard (security checklist §75 v1): xlsx/xlsm/xlsb/ods are all
        # ZIP containers, adjudicated by central-directory limits before extraction; a violation is an honest
        # failure (evidence intact); when the guard itself cannot read the central directory, it lets the file pass.
        if Path(file_path).suffix.lower() in _ZIP_CONTAINER_SUFFIXES:
            _zip_guard = _check_zip_package_safe(file_path)
            if _zip_guard is not None:
                metadata["zip_package_guard"] = _zip_guard
                if _zip_guard.get("verdict") == "violation":
                    metadata["parse_failed"] = True
                    metadata["parse_error"] = zip_guard_error(_zip_guard)
                    return Document(text="", metadata=metadata)

        try:
            loaded = self.backend.load(file_path)
            self._collect_backend_metadata(metadata, loaded)
            self._collect_resource_guard(metadata)
            # Object-level unresolved registration (spreadsheet_unresolved_prereg_v1):
            # derived from resource_guard structured facts, never parsing the reasons strings;
            # all table backends (openpyxl/legacy/parquet) derive through the same channel;
            # guarded silent degradation, never blocking the main parse (epub wiring precedent).
            try:
                stamp_unresolved_registry(
                    metadata, "spreadsheet_unresolved",
                    derive_spreadsheet_unresolved(metadata),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug(
                    "spreadsheet unresolved registry skipped: %s", exc,
                )
            spreadsheet = self._spreadsheet_contract(file_path, loaded)
            legacy_data: Dict[str, Any] = {
                "documents": self._legacy_documents(spreadsheet)
            }
            sheets = spreadsheet["workbook"]["sheets"]
            metadata["sheet_count"] = len(sheets)
            metadata["table_count"] = sum(
                sum(
                    region.get("region_type") == "table"
                    for region in (sheet.get("canvas") or {}).get("regions", [])
                )
                for sheet in sheets
            )
            text = self._document_text(sheets)

            # Security carrier census (invisible_carrier_v1,
            # security_carrier_formats_prereg_v1 §2.2, the doc_cfb bypass
            # precedent applied likewise): the table-family Document.text is a structural overview with no cell
            # values (measured: ZW lands in cell raw_value), so the census runs on the cells value
            # view; mounted once in the ExcelParser common parse, covering
            # the three formats xlsx/ods/parquet (subclasses reuse this pipeline). After a guard truncation
            # the census covers the actually-extracted surface (honest); the failure tier (except) gets no census.
            carrier_census = detect_security_carriers(
                self._cells_value_text(sheets)
            )
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata.setdefault("warnings", []).extend(
                    build_carrier_facts(carrier_census)
                )
        except Exception as exc:
            error = f"读取失败：{exc}"
            spreadsheet = sc.build_workbook_contract(
                source_kind=self.SOURCE_KIND,
                file_name=Path(file_path).name,
                sheets=[],
                extra={"parse_error": error},
            )
            legacy_data = {"_error": error}
            text = ""
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(exc)

        metadata["spreadsheet"] = spreadsheet
        metadata[self.LEGACY_DATA_KEY] = legacy_data
        metadata["parse_duration_ms"] = round(
            (time.perf_counter() - start_time) * 1000, 3,
        )
        return Document(text=text, metadata=metadata)

    #: metadata key for the legacy documents payload (compat; parquet
    #: historically emits "table_data" instead of "excel_data")
    LEGACY_DATA_KEY = "excel_data"

    #: source_kind stamped on the parse-error fallback contract
    SOURCE_KIND = "excel_workbook"

    def _collect_backend_metadata(self, metadata, loaded):
        """Hook for subclasses to record backend-specific metadata."""

    def _collect_resource_guard(self, metadata):
        """Record resource-guard facts (guards, not filters).

        ``resource_guard`` is written only when a guard of any wired backend actually fires;
        the default path produces no new keys, guaranteeing zero golden drift. All five excel
        engines and the parquet backend fill guard_reasons /
        guard_facts (prereg table_backends_limits_prereg_v1), limits
        produce no fact declarations when None. The structured ``facts`` sub-keys
        (spreadsheet_unresolved_prereg_v1) persist on the same trigger surface, for
        1:1 derivation of the object-level unresolved registrations.
        """
        guard_reasons = getattr(self.backend, "guard_reasons", None) or []
        if self.limits is None or not guard_reasons:
            return
        guard_facts = getattr(self.backend, "guard_facts", None) or []
        resource_guard = {
            "scheme": "resource_limits_v1",
            "limits": self.limits.as_dict(),
            "reasons": list(guard_reasons),
        }
        if guard_facts:
            resource_guard["facts"] = [dict(fact) for fact in guard_facts]
        metadata["resource_guard"] = resource_guard

    @staticmethod
    def _spreadsheet_contract(file_path, loaded: Any) -> Dict[str, Any]:
        if isinstance(loaded, dict) and "sheets" in loaded:
            return sc.build_workbook_contract(
                source_kind="excel_workbook",
                file_name=Path(file_path).name,
                sheets=loaded.get("sheets") or [],
                defined_names=loaded.get("defined_names") or [],
                relations=loaded.get("relations") or [],
                extra={"engine": "openpyxl"},
            )
        # The upgraded thin backends (xlrd/odf, table_backend_parity prereg v1
        # D2) return a legacy_sheets wrapping dict: formulas/visibility/defined names/
        # sheet_order are wired into the shared contract in _contract_from_legacy_sheets.
        if isinstance(loaded, dict) and "legacy_sheets" in loaded:
            return ExcelParser._contract_from_legacy_sheets(
                file_path,
                loaded["legacy_sheets"],
                defined_names=loaded.get("defined_names") or [],
                sheet_order=loaded.get("sheet_order") or [],
                relations=loaded.get("relations") or [],
                extra=loaded.get("extra"),
            )
        # Compatibility for non-openpyxl backends until they emit the shared
        # canvas model themselves.  The fallback preserves their value grid.
        return ExcelParser._contract_from_legacy_sheets(file_path, loaded or [])

    @staticmethod
    def _contract_from_legacy_sheets(
        file_path,
        raw_sheets: Sequence[Dict[str, Any]],
        source_kind: str = "excel_workbook",
        extra: Optional[Dict[str, Any]] = None,
        defined_names: Optional[Sequence[Dict[str, Any]]] = None,
        sheet_order: Optional[Sequence[str]] = None,
        relations: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        defined_names = list(defined_names or [])
        # Workbook-level defined-name mapping: engine-independent reuse of the formula_refs common layer's
        # formula-target derivation (formula_refs_common_layer prereg v1; formerly the direct
        # static-method calls of table_backend_parity prereg v1 D1); sheet-
        # scoped names never enter the mapping, the same honesty boundary as
        # OpenpyxlBackend._defined_name_map.
        defined_name_map = {
            entry["name"].upper(): {
                "name": entry["name"],
                "attr_text": entry.get("attr_text"),
            }
            for entry in defined_names
            if entry.get("name") and entry.get("local_sheet_id") is None
        }
        sheet_order = list(sheet_order or [])
        sheets: List[Dict[str, Any]] = []
        for sheet_index, raw_sheet in enumerate(raw_sheets):
            name = raw_sheet.get("sheet_name") or f"Sheet{sheet_index + 1}"
            raw_rows = raw_sheet.get("raw_rows") or []
            if raw_rows:
                column_count = max((len(row) for row in raw_rows), default=0)
                shape_rows = len(raw_rows)
            else:
                # Skeleton sheet (sheet-cap guard, prereg
                # table_backends_limits_prereg_v1 §5): the declared shape is carried honestly when
                # knowable (the xlrd/pandas whole-sheet skip paths); when unknowable it
                # stays 0×0 (pyxlsb/odf streaming skeletons). In the pandas/parquet normal
                # path raw_rows always contains the header row — zero impact from this branch.
                declared = raw_sheet.get("shape") or {}
                column_count = int(declared.get("columns") or 0)
                shape_rows = int(declared.get("rows") or 0)
            cells = []
            rows = []
            formula_by_position = {
                (entry["row"], entry["column"]): entry["formula"]
                for entry in raw_sheet.get("formulas") or []
                if entry.get("formula")
            }
            for row_index, raw_row in enumerate(raw_rows, start=1):
                cell_ids = []
                for column_index in range(1, column_count + 1):
                    value = raw_row[column_index - 1] if column_index <= len(raw_row) else None
                    cell = sc.make_cell(
                        sheet_name=name,
                        row=row_index,
                        column=column_index,
                        raw_value=value,
                        formula=formula_by_position.get((row_index, column_index)),
                    )
                    cells.append(cell)
                    cell_ids.append(cell["id"])
                rows.append({
                    "id": f"{name}!row:{row_index}",
                    "row_index": row_index,
                    "is_blank": all(
                        cells_by_id.get(cell_id, {}).get("is_blank", True)
                        for cell_id in cell_ids
                    ) if (cells_by_id := {cell["id"]: cell for cell in cells}) else True,
                    "cell_ids": cell_ids,
                })
            canvas = sc.build_canvas(
                canvas_id=f"{name}!canvas:1",
                shape_rows=shape_rows,
                shape_columns=column_count,
                cells=cells,
                rows=rows,
                merged_cells=raw_sheet.get("merged_cells") or [],
            )
            canvas["regions"] = sc.detect_regions(cells, sheet_name=name)
            positions = {(cell["row"], cell["column"]): cell for cell in cells}
            for region in canvas["regions"]:
                if region.get("region_type") == "table":
                    region["headers"] = sc.region_headers(region, positions)
            sheets.append({
                "id": f"sheet:{sheet_index + 1}:{name}",
                "name": name,
                "sheet_index": sheet_index,
                "visibility": raw_sheet.get("visibility") or "visible",
                "canvas": canvas,
                "relations": formula_relations(
                    cells, name, defined_name_map, sheet_order,
                ),
            })
        return sc.build_workbook_contract(
            source_kind=source_kind,
            file_name=Path(file_path).name,
            sheets=sheets,
            defined_names=defined_names,
            relations=relations,
            extra=extra or {"engine": "legacy_backend_fallback"},
        )

    @staticmethod
    def _legacy_documents(spreadsheet: Dict[str, Any]) -> List[Dict[str, Any]]:
        documents = []
        for sheet in spreadsheet["workbook"]["sheets"]:
            canvas = sheet.get("canvas") or {}
            cells = {(cell["row"], cell["column"]): cell for cell in canvas.get("cells", [])}
            tables = []
            for table_index, region in enumerate(canvas.get("regions", []), start=1):
                if region.get("region_type") != "table":
                    continue
                bounds = region.get("bounds") or {}
                headers = [header["name"] for header in region.get("headers") or []]
                header_row = region.get("header_row_index")
                rows = []
                for row_index in range(bounds.get("start_row", 1), bounds.get("end_row", 0) + 1):
                    if row_index == header_row:
                        continue
                    values = {
                        header: cells.get((row_index, column), {}).get("raw_value")
                        for column, header in enumerate(
                            headers, start=bounds.get("start_column", 1),
                        )
                    }
                    if any(value is not None for value in values.values()):
                        rows.append({
                            "row_index": row_index,
                            "excel_row": row_index,
                            "values": values,
                        })
                tables.append({
                    "table_index": table_index,
                    "range": {
                        "start_row": bounds.get("start_row"),
                        "end_row": bounds.get("end_row"),
                    },
                    "headers": headers,
                    "rows": rows,
                    "chunks": [],
                    "dtypes": {},
                })
            documents.append({
                "sheet_name": sheet.get("name"),
                "tables": tables,
                "shape": canvas.get("shape", {}),
                "merged_cells": canvas.get("merged_cells", []),
            })
        return documents

    @staticmethod
    def _cells_value_text(sheets: Sequence[Dict[str, Any]]) -> str:
        """Concatenation of str-typed cell values across all sheets (the security-carrier census view).

        Non-string values (numbers/booleans etc.) carry no invisible carriers and are skipped; includes
        the parquet header-row cells (which are also in the canvas cells).
        """
        parts = []
        for sheet in sheets:
            canvas = sheet.get("canvas") or {}
            for cell in canvas.get("cells", []):
                value = cell.get("raw_value")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)

    @staticmethod
    def _document_text(sheets: Sequence[Dict[str, Any]]) -> str:
        parts = []
        for sheet in sheets:
            canvas = sheet.get("canvas") or {}
            shape = canvas.get("shape") or {}
            parts.append(
                f"Sheet: {sheet.get('name')} ({shape.get('rows', 0)} rows × "
                f"{shape.get('columns', 0)} columns)"
            )
            for region in canvas.get("regions", []):
                parts.append(
                    f"Region: {region.get('region_type', 'unknown')} "
                    f"{region.get('range', '')}"
                )
        return "\n".join(parts)
