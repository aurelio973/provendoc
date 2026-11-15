"""Scenario invariants (RSE, gatekeeping-class — a violation means unqualified).

Implements scenario_framework_prereg_v1 §3.2 (landed ahead of the
freeze). Each invariant set is named by
the matching ``ScenarioFramework.rse_profile`` (see
``core/scenario/frameworks.py``); unknown profile names yield a
``not_applicable`` result — the fallback is never penalized
(prereg §3.2: penalizing general_prose would invite score-gaming).

Invariant sets (v1):

* ``invoice_v1`` — every extracted line item must fall inside some
  chunk's line range (the ledger-vs-chunks coverage discipline;
  chunks are line-boundary-only by construction, so an item outside
  every chunk is a real defect, not an expected overlap artifact);
* ``academic_paper_v1`` — ledger self-consistency: every located
  line (abstract / keywords / references / citations / section
  headings) must fall inside ``1..total_lines`` (the paper chain
  does no salvage, so there is exactly one coordinate space; an
  out-of-range coordinate is a ledger defect). Since v1.5, when the
  ledger carries chunks
  the set ALSO checks protected-region atomicity: formula blocks
  (recomputed from ``formula_lines`` as contiguous runs — never
  trusting the chunker's own view) and References entries
  (``references_entries``, a ledger fact like invoice line_items)
  must each fall entirely inside a single chunk's line range; a
  region straddling a boundary is a real defect, while overlap
  duplication of a whole region is by-design, never a violation.

Honest boundaries:

* checks are INDEPENDENT of the producers (the chunker never grades
  itself — same discipline as check_chunk_invariants);
* overlap duplicates are not violations (overlap is by design);
* a missing ledger or an empty invariant set is ``not_applicable``,
  never a pass or fail.
"""

from __future__ import annotations

from typing import Any

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NOT_APPLICABLE = "not_applicable"

_CLOSE_MARGIN_KINDS = 1  # runner-up voting_kinds diff <= 1 (v1, report)


def _result(verdict: str, violations: list[str], checked: int) -> dict[str, Any]:
    return {
        "extractor": "scenario_invariants_v1",
        "verdict": verdict,
        "checked": checked,
        "violations": violations,
    }


def check_invoice_invariants(ledger: Any) -> dict[str, Any]:
    """invoice_v1: line items must be covered by some chunk range."""
    if not isinstance(ledger, dict):
        return _result(VERDICT_NOT_APPLICABLE, [], 0)
    chunks = ledger.get("chunks", {}).get("chunks", [])
    if not isinstance(chunks, list) or not chunks:
        return _result(VERDICT_NOT_APPLICABLE, [], 0)
    ranges: list[tuple[int, int]] = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        start = c.get("start_line")
        end = c.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            ranges.append((start, end))
    items = ledger.get("line_items", {}).get("items", [])
    if not isinstance(items, list):
        items = []
    violations: list[str] = []
    checked = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        if not isinstance(line, int):
            continue
        checked += 1
        if not any(lo <= line <= hi for lo, hi in ranges):
            violations.append(
                f"line item at line {line} falls outside every chunk range"
            )
    if violations:
        return _result(VERDICT_FAIL, violations, checked)
    return _result(VERDICT_PASS, [], checked)


def _contiguous_runs(values: list[int]) -> list[tuple[int, int]]:
    """1-based closed runs of consecutive ints (independent of any
    producer's run-merging code — the checker recomputes)."""
    runs: list[tuple[int, int]] = []
    for value in values:
        if not isinstance(value, int) or value <= 0:
            continue
        if runs and value == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


def check_academic_paper_invariants(ledger: Any) -> dict[str, Any]:
    """academic_paper_v1: ledger coordinates + region atomicity."""
    if not isinstance(ledger, dict):
        return _result(VERDICT_NOT_APPLICABLE, [], 0)
    total = ledger.get("total_lines")
    if not isinstance(total, int) or total <= 0:
        return _result(VERDICT_NOT_APPLICABLE, [], 0)
    violations: list[str] = []
    checked = 0
    single_keys = ("abstract_line", "keywords_line", "references_line")
    for key in single_keys:
        value = ledger.get(key)
        if value is None:
            continue
        checked += 1
        if not isinstance(value, int) or not (1 <= value <= total):
            violations.append(f"{key}={value!r} outside 1..{total}")
    for key in (
        "citation_lines",
        "tex_citation_lines",
        "formula_lines",
        "numbered_heading_lines",
        "section_heading_lines",
    ):
        values = ledger.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            checked += 1
            if not isinstance(value, int) or not (1 <= value <= total):
                violations.append(f"{key} entry {value!r} outside 1..{total}")

    # v1.5 atomicity: regions must sit inside ONE chunk each
    chunks = ledger.get("chunks", {})
    chunk_list = chunks.get("chunks", []) if isinstance(chunks, dict) else []
    if isinstance(chunk_list, list) and chunk_list:
        ranges: list[tuple[int, int]] = []
        for c in chunk_list:
            if not isinstance(c, dict):
                continue
            start = c.get("start_line")
            end = c.get("end_line")
            if isinstance(start, int) and isinstance(end, int):
                ranges.append((start, end))
        formula_lines = ledger.get("formula_lines")
        regions: list[tuple[int, int]] = _contiguous_runs(
            formula_lines if isinstance(formula_lines, list) else []
        )
        entries = ledger.get("references_entries")
        if isinstance(entries, list):
            for entry in entries:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("start_line"), int)
                    and isinstance(entry.get("end_line"), int)
                ):
                    regions.append((entry["start_line"], entry["end_line"]))
        for start, end in regions:
            checked += 1
            if not any(lo <= start and end <= hi for lo, hi in ranges):
                violations.append(
                    f"protected region {start}-{end} split across "
                    f"chunk boundaries"
                )
    if violations:
        return _result(VERDICT_FAIL, violations, checked)
    return _result(VERDICT_PASS, [], checked)


def check_scenario_invariants(
    rse_profile: str, ledger: Any
) -> dict[str, Any]:
    """Dispatch by the framework's rse_profile name.

    Unknown/empty profile (incl. general_prose) → ``not_applicable``
    — the fallback label is never penalized.
    """
    if rse_profile == "invoice_v1":
        return check_invoice_invariants(ledger)
    if rse_profile == "academic_paper_v1":
        return check_academic_paper_invariants(ledger)
    return _result(VERDICT_NOT_APPLICABLE, [], 0)


__all__ = [
    "VERDICT_FAIL",
    "VERDICT_NOT_APPLICABLE",
    "VERDICT_PASS",
    "check_academic_paper_invariants",
    "check_invoice_invariants",
    "check_scenario_invariants",
]
