"""Spreadsheet Domain representations.

The package path remains ``table`` for source compatibility; public domain
semantics use Spreadsheet and share one representation for CSV and Excel.
"""

from provendoc.core.representations.table.spreadsheet_representation import (
    SpreadsheetRepresentation,
)

__all__ = ["SpreadsheetRepresentation"]
