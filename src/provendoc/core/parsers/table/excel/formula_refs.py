"""Engine-neutral formula reference derivation for the Excel family.

The formula-reference grammar (A1 / ``'S 1'!$B$2`` / 3D / structured / defined names) is an engine-independent
domain layer: the xlsx (openpyxl main path) and ods/xls/xlsb legacy paths consume it together.
This module was cleanly relocated from openpyxl_backend (formula_refs_common_layer
prereg v1 D1/D2): the implementation is byte-for-byte unchanged; only class-private static methods became module-level public
functions — it is no longer an implementation detail of any engine class.

Honesty boundaries (inherited from spreadsheet_canvas_v2 §4 and table_backend_parity §4):
- evidence-only: formulas keep their source text; dynamic/indirect references that cannot be statically identified are never invented into
  dependency targets;
- ``range_boundaries`` comes from openpyxl.utils.cell (a pure A1 string utility),
  when openpyxl is not installed the guard degrades to not resolving structured reference targets (only the xlsx main path
  carries table_map; the other engines naturally pass None and are unaffected).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:
    from openpyxl.utils.cell import range_boundaries
except Exception:
    range_boundaries = None

from provendoc.core.parsers.table import contracts as sc

# spreadsheet_canvas_v2 prereg v1 (D3/D4): range-aware formula reference
# scanning and workbook-scope defined-name resolution.  A full A1-style
# reference with optional quoted sheet prefix, captured as ONE target
# (``B2:B6`` stays a range, not two cells).
_FULL_REFERENCE_PATTERN = re.compile(
    r"^(?:(?:'([^']+)'|([A-Za-z0-9_ ]+))!)?"
    r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)"
    r"(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]*))?$"
)
# Same shape for scanning inside formula text: guarded so function names
# (LOG10() never yields a phantom ``G10``) and word-internal fragments do
# not match; ranges are captured whole.
_FORMULA_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_$!'])"
    r"(?:(?:'([^']+)'|([A-Za-z0-9_ ]+))!)?"
    r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)"
    r"(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]*))?"
    r"(?![0-9A-Za-z_(])"
)
# 3D references (``Sheet1:Sheet3!A1``): both endpoints are sheet names and
# the colon is excluded from the lookbehind so plain ranges (``A1:B2``)
# can never masquerade as a 3D span (prereg §7 fix 3).
_THREE_D_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_$!':])"
    r"(?:'([^']+)'|([A-Za-z0-9_ ]+?)):(?:'([^']+)'|([A-Za-z0-9_ ]+?))!"
    r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)"
    r"(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]*))?"
    r"(?![0-9A-Za-z_(])"
)
# Structured references (``tblExpenses[Amount]``): the bracket payload may
# not contain brackets, ``@`` (current-row) or ``#`` (special items like
# ``#All``) — those stay honestly unresolved (prereg §7 fix 4). The table
# name may carry a leading ``\`` escape (real-corpus fact: POI fixture
# StructuredReferences.xlsx declares displayName ``\_Prime.1``), so the
# name group optionally includes it — matching stays a strict
# case-insensitive equality against the declared displayName.
_STRUCTURED_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"((?:\\)?[A-Za-z_][A-Za-z0-9_.]*)"
    r"\[([^\[\]@#]+)\]"
)


def formula_relations(
    cells: List[Dict[str, Any]],
    sheet_name: str,
    defined_names: Optional[Dict[str, Dict[str, Any]]] = None,
    sheet_order: Optional[List[str]] = None,
    table_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """One ``formula_reference`` relation per formula-bearing cell.

    evidence-only: a relation carries the source text and the statically identifiable targets
    (derived via formula_targets); no dependencies are invented.
    """
    relations = []
    for cell in cells:
        formula = cell.get("formula")
        if not formula:
            continue
        relations.append({
            "id": f"relation:{cell['id']}",
            "relation_type": "formula_reference",
            "source_cell_id": cell["id"],
            "source_sheet": sheet_name,
            "formula": formula,
            "targets": formula_targets(
                formula, sheet_name, defined_names, sheet_order, table_map,
            ),
            "evidence": "formula_text",
        })
    return relations


def formula_targets(
    formula: str,
    source_sheet: str,
    defined_names: Optional[Dict[str, Dict[str, Any]]] = None,
    sheet_order: Optional[List[str]] = None,
    table_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """Derive static reference targets from one formula's source text.

    Engine-independent Excel reference-grammar derivation (see the canvas_v2 design comments
    inherited inside the function body); see the docstring and the in-implementation comments.
    """
    # This is intentionally evidence-only: formulas remain source text and
    # no dependency target is invented when a dynamic/indirect reference is
    # not statically recognizable.  Match against the original formula so
    # sheet names retain their source casing in provenance metadata.
    #
    # spreadsheet_canvas_v2 prereg v1 (D3/D4 + §7 fixes 3/4): ranges are
    # captured as one target (``B2:B6``), sheet attribution follows the
    # explicit prefix, workbook-scope defined names resolve to their
    # declared range, 3D references (``Sheet1:Sheet3!A1``) expand across
    # the workbook's declared sheet order, and structured references
    # (``tbl[Column]``) resolve to the declared column data range.
    targets: List[Dict[str, str]] = []
    masked = formula
    if defined_names:
        # Longest names first so overlapping names mask correctly; a
        # matched name is blanked out before cell scanning so it can not
        # also produce a phantom cell-shaped target.
        for upper_name in sorted(defined_names, key=len, reverse=True):
            info = defined_names[upper_name]
            name_pattern = re.compile(
                r"(?<![A-Za-z0-9_.])" + re.escape(info["name"])
                + r"(?![A-Za-z0-9_.])",
                re.IGNORECASE,
            )
            if not name_pattern.search(masked):
                continue
            for target in reference_targets(
                info["attr_text"]
            ) + structured_reference_targets(
                info["attr_text"], table_map,
            ):
                named = dict(target)
                named["via"] = f"defined_name:{info['name']}"
                if named not in targets:
                    targets.append(named)
            masked = name_pattern.sub(
                lambda match: " " * len(match.group(0)), masked,
            )
    # 3D references: both endpoints resolvable in the workbook's declared
    # sheet order -> one target per sheet in the span (facts, not
    # invention); the matched span is blanked so the single-reference scan
    # can not also emit the trailing-sheet fallback target. Unresolvable
    # endpoints keep the honest single-reference fallback.
    if sheet_order:
        expanded: List[Any] = []
        for match in _THREE_D_REFERENCE_PATTERN.finditer(masked):
            first = match.group(1) or match.group(2)
            last = match.group(3) or match.group(4)
            if first not in sheet_order or last not in sheet_order:
                continue
            start_index = sheet_order.index(first)
            end_index = sheet_order.index(last)
            if start_index > end_index:
                continue
            column, row = match.group(5), match.group(6)
            if match.group(7):
                coordinate = (
                    f"{column.upper()}{row}"
                    f":{match.group(7).upper()}{match.group(8)}"
                )
            else:
                coordinate = f"{column.upper()}{row}"
            for sheet in sheet_order[start_index:end_index + 1]:
                target = {"sheet": sheet, "coordinate": coordinate}
                if target not in targets:
                    targets.append(target)
            expanded.append((match.start(), match.end()))
        for start, end in reversed(expanded):
            masked = masked[:start] + " " * (end - start) + masked[end:]
    # Structured references: ``tbl[Column]`` resolves via the native
    # Table's declared ref/tableColumns/headerRowCount.
    for match in _STRUCTURED_REFERENCE_PATTERN.finditer(masked):
        if table_map:
            column_target = table_column_target(
                table_map.get(match.group(1).upper()), match.group(2),
            )
            if column_target and column_target not in targets:
                targets.append(column_target)
    # Bracket payloads are never cell references in Excel grammar, so
    # every bracket span is blanked regardless of resolution — a column
    # named ``Q1`` must not leak a phantom ``Q1`` target.
    masked = _STRUCTURED_REFERENCE_PATTERN.sub(
        lambda match: " " * len(match.group(0)), masked,
    )
    for match in _FORMULA_REFERENCE_PATTERN.finditer(masked):
        if re.match(r"\s*\(", masked[match.end():]):
            # Cell-shaped function name (``LOG10 (``): letters belong to
            # a function call, not a cell reference.
            continue
        sheet = match.group(1) or match.group(2) or source_sheet
        if match.group(5):
            coordinate = (
                f"{match.group(3).upper()}{match.group(4)}"
                f":{match.group(5).upper()}{match.group(6)}"
            )
        else:
            coordinate = f"{match.group(3).upper()}{match.group(4)}"
        target = {"sheet": sheet, "coordinate": coordinate}
        if target not in targets:
            targets.append(target)
    return targets


def structured_reference_targets(
    text: Optional[str],
    table_map: Optional[Dict[str, Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Resolve ``tbl[Column]`` spans in a declared reference text
    (defined-name attr_text) to table column data-range targets."""
    if not text or not table_map:
        return []
    targets: List[Dict[str, str]] = []
    for match in _STRUCTURED_REFERENCE_PATTERN.finditer(str(text)):
        target = table_column_target(
            table_map.get(match.group(1).upper()), match.group(2),
        )
        if target and target not in targets:
            targets.append(target)
    return targets


def table_column_target(
    table_info: Optional[Dict[str, Any]],
    column_spec: str,
) -> Optional[Dict[str, str]]:
    """One ``tbl[Column]`` -> ``sheet!C11:C23`` target (evidence-only).

    Column letters come from the declared ref left bound + the tableColumns sequential index; data rows exclude the
    header rows declared by headerRowCount. On column-name mismatch, column index beyond the declared width,
    or a table with no data rows, returns None (no invented targets).
    """
    if not table_info:
        return None
    if range_boundaries is None:
        # openpyxl not installed (its pure string utility range_boundaries is unavailable):
        # guard degrades, structured reference targets stay unresolved (only the xlsx main path carries
        # table_map; the other engines are unaffected).
        return None
    spec = column_spec.strip()
    column_index = next(
        (
            index
            for index, name in enumerate(table_info["columns"])
            if name.lower() == spec.lower()
        ),
        None,
    )
    if column_index is None:
        return None
    min_col, min_row, max_col, max_row = range_boundaries(table_info["ref"])
    column = min_col + column_index
    if column > max_col:
        # Declared tableColumns disagree with the declared ref width —
        # refuse to invent a target outside the declared range.
        return None
    letter = sc.column_index_to_label(column)
    data_start = min_row + table_info["header_row_count"]
    if data_start > max_row:
        return None
    return {
        "sheet": table_info["sheet"],
        "coordinate": f"{letter}{data_start}:{letter}{max_row}",
    }


def reference_targets(reference: Optional[str]) -> List[Dict[str, str]]:
    """Parse a full A1-style reference (``'Sheet'!$B$2:$B$6``) to targets.

    Un-attributable (no sheet prefix) or non-single-range references (multi-area / ``#REF!``) return empty:
    no invented targets. Coordinates are normalized by stripping ``$`` into ``B2:B6``.
    """
    if not reference:
        return []
    match = _FULL_REFERENCE_PATTERN.match(str(reference).strip())
    if match is None:
        return []
    sheet = match.group(1) or match.group(2)
    if sheet is None:
        return []
    start = f"{match.group(3).upper()}{match.group(4)}"
    if match.group(5):
        coordinate = f"{start}:{match.group(5).upper()}{match.group(6)}"
    else:
        coordinate = start
    return [{"sheet": sheet, "coordinate": coordinate}]
