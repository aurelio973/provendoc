"""ODF Spreadsheet (.ods) parser — reuses ExcelParser's contract construction."""
from __future__ import annotations

from typing import Optional

from provendoc.core.parsers.table.contracts import TableRuntimeLimits
from provendoc.core.parsers.table.excel.excel_parser import ExcelParser


class OdsParser(ExcelParser):
    """Parse an ODF spreadsheet (.ods) as ordered sheets and canvases.

    Reuses ExcelParser's legacy-sheet → spreadsheet contract conversion path;
    the value grid is provided by OdfBackend (odfpy).
    """

    file_type = "ods"

    def __init__(
        self,
        has_header=None,
        chunk_size: int = 200,
        skip_empty_rows: bool = True,
        limits: Optional[TableRuntimeLimits] = None,
    ):
        """Initialize with header strategy, block size, blank-row switch and optional guards (engine fixed to odf)."""
        super().__init__(
            engine="odf",
            has_header=has_header,
            chunk_size=chunk_size,
            skip_empty_rows=skip_empty_rows,
            limits=limits,
        )
