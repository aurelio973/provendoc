# -*- coding: utf-8 -*-
"""Structural contract (Protocol) for table backends.

All backends (openpyxl/pandas/xlrd/pyxlsb/odf and parquet's
_ParquetBackend) implement ``load(file_path)``: openpyxl returns a dict with
sheets/defined_names/relations; the upgraded thin backends (xlrd/odf,
table_backend_parity prereg v1 D2) return a ``legacy_sheets`` wrapping dict,
the rest return a legacy sheet list. ExcelParser.backend is declared with this Protocol;
concrete implementations just need matching structure — no common base class required; the return-value shape is
validated by the isinstance dispatch layer in _spreadsheet_contract.
"""
from typing import Any, Dict, List, Optional, Protocol, Union

from provendoc.core.parsers.table.contracts import TableRuntimeLimits

LegacySheets = Union[Dict[str, Any], List[Dict[str, Any]]]


def column_letters(index: int) -> str:
    """1-based column number → Excel column letter (1→A, 27→AA).

    Used only to convert backend-declared half-open interval coordinates into the contract's merged_cells
    ``A1:D1`` string shape (the same shape as openpyxl's native output); the conversion is deterministic,
    inventing no grid facts.
    """
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def merged_range_text(
    row_start: int, col_start: int, row_end: int, col_end: int,
) -> str:
    """1-based closed-interval rows/columns → ``A1:D2`` shape string (shared by thin backends)."""
    start = f"{column_letters(col_start)}{row_start}"
    end = f"{column_letters(col_end)}{row_end}"
    return start if (row_start, col_start) == (row_end, col_end) else f"{start}:{end}"


def merged_range_entry(
    row_start: int, col_start: int, row_end: int, col_end: int,
) -> Dict[str, Any]:
    """1-based closed-interval rows/columns → contract merged_cells entries (shared by thin backends).

    The shape aligns with OpenpyxlBackend._merged_cells output (the five keys
    range/start_row/start_column/end_row/end_column); downstream (representation/quality layers)
    consumes dicts — thin backends must not invent alien shapes (measured: a str entry
    triggers ``'str' object has no attribute` in SpreadsheetRepresentation
    'get'``).
    """
    return {
        "range": merged_range_text(row_start, col_start, row_end, col_end),
        "start_row": row_start,
        "start_column": col_start,
        "end_row": row_end,
        "end_column": col_end,
    }


class BackendGuardMixin:
    """Guard-fact collection surface shared by table backends (guards, not filters).

    Structured extraction following the openpyxl precedent: when ``load()`` fires a guard, the backend records
    reasons and structured facts via ``_guard`` / ``_guard_fact``,
    ``ExcelParser._collect_resource_guard`` then persists
    them with ``metadata["resource_guard"]`` and derives the object-level registrations
    (prereg table_backends_limits_prereg_v1). The ``load()`` entry point
    must call ``_reset_guards()`` first (backend instances may be reused).
    """

    guard_reasons: List[str]
    guard_facts: List[Dict[str, Any]]
    limits: Optional[TableRuntimeLimits]

    def _init_guards(self, limits: Optional[TableRuntimeLimits] = None) -> None:
        """Initialize with optional resource guards (None means historical behaviour, zero guard-channel firing)."""
        self.limits = limits
        self.guard_reasons = []
        self.guard_facts = []

    def _reset_guards(self) -> None:
        """Reset guard facts on every load (backend instances may be reused)."""
        self.guard_reasons = []
        self.guard_facts = []

    def _guard(self, reason: str) -> None:
        """Record one guard reason (written to resource_guard.reasons)."""
        self.guard_reasons.append(reason)

    def _guard_fact(self, fact: Dict[str, Any]) -> None:
        """Record the structured companion fact of the same firing point (1:1 derivation source of the spreadsheet-domain registrations)."""
        self.guard_facts.append(fact)


class SpreadsheetBackend(Protocol):
    """``load(file_path)`` → openpyxl's sheet-facts dict or a legacy list."""

    def load(self, file_path) -> LegacySheets:
        """Read the table source into sheet facts (openpyxl) or a legacy list (the rest)."""
        ...
