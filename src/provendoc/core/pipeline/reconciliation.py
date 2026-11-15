"""Deterministic line-level reconciliation between pipeline stages.

Purpose: the raw text is the single ground-truth anchor of the whole
pipeline. Every stage that rewrites text must reconcile its output
against the anchor so content loss is exposed at the stage boundary,
not discovered downstream in chunks.

Two gates with different tolerances (caller decides which applies):

* parse gate -- zero tolerance: the parser must not lose content.
  Any unexplained missing line is a defect.
* clean gate -- ledger tolerance: cleaning intentionally removes lines
  (watermarks, headers, footers); every removal must be explained by a
  removal ledger entry. Unexplained missing lines are defects.

Honest boundaries of this gate (deliberately stated, not hidden):

* intra-line damage (truncation, character corruption inside a
  surviving line) is NOT caught here -- that belongs to content-level
  RSE checks;
* line reordering is NOT caught (multiset semantics);
* blank-only lines are not content and are ignored.

Pure functions only; no I/O, no exception paths on user data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

_MAX_SAMPLES = 5
_MAX_SAMPLE_CHARS = 60

VERDICT_OK = "ok"
VERDICT_LOSS = "loss"
VERDICT_EMPTY = "empty"


def _normalize_line(line: str) -> str:
    """Collapse internal whitespace; blank result means 'not content'.

    A leading BOM is not content either (it survives decoding of the
    raw source snapshot but is stripped from the parse snapshot).
    """
    return " ".join(line.split()).lstrip("\ufeff")


@dataclass(frozen=True)
class CoverageReport:
    """Reconciliation result between an anchor text and a stage output.

    ``missing_lines`` counts unexplained loss only (lines present in the
    anchor more often than in the output AND not covered by the removal
    ledger). ``explained_missing`` counts ledger-covered removals.
    ``added_lines`` counts output lines with no anchor counterpart
    (duplication / fabricated content signal).
    """

    total_lines: int
    covered_lines: int
    missing_lines: int
    explained_missing: int
    added_lines: int
    verdict: str
    missing_samples: tuple[tuple[int, str], ...] = field(default=())
    note: str = ""

    @property
    def is_clean(self) -> bool:
        """True only when the verdict is ``ok`` (no unexplained loss)."""
        return self.verdict == VERDICT_OK


def _anchor_lines(anchor: str) -> list[tuple[int, str]]:
    """(1-based index, normalized text) for every content line."""
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(anchor.splitlines(), start=1):
        norm = _normalize_line(raw)
        if norm:
            out.append((i, norm))
    return out


def _output_lines(stage_output: str) -> list[str]:
    return [norm for norm in map(_normalize_line, stage_output.splitlines()) if norm]


def reconcile(
    anchor: str,
    stage_output: str,
    removals: Iterable[tuple[int, int]] = (),
    substitutions: Iterable[tuple[int, str]] = (),
) -> CoverageReport:
    """Reconcile ``stage_output`` against ``anchor`` line multisets.

    Args:
        anchor: ground-truth text (e.g. ``original_text``).
        stage_output: the stage's rewritten text (e.g. ``text`` after
            cleaning, or a scenario-parsed representation round-trip).
        removals: ledger of intentional removals as 1-based inclusive
            anchor line-number spans ``(start, end)``. Empty for the
            zero-tolerance parse gate.
        substitutions: ledger of intentional rewrites as 1-based
            ``(line_number, replacement_text)`` pairs. A rewritten line
            counts as covered when the output contains the replacement;
            without this ledger a rewrite would surface as one missing
            plus one added line. Only one substitution per line is
            supported (later entries win).

    Returns:
        CoverageReport with an honest verdict and capped samples.
    """
    overrides = {lineno: _normalize_line(replacement)
                 for lineno, replacement in substitutions}
    anchor_pairs = [
        (i, overrides.get(i, norm)) if norm else (i, norm)
        for i, norm in _anchor_lines(anchor)
    ]
    anchor_counts = Counter(norm for _, norm in anchor_pairs)
    out_counts = Counter(_output_lines(stage_output))

    covered = sum(min(out_counts[norm], count) for norm, count in anchor_counts.items())
    total = sum(anchor_counts.values())
    added = max(0, sum(out_counts.values()) - covered)

    ledger: set[int] = set()
    for start, end in removals:
        ledger.update(range(start, end + 1))

    explained = 0
    unexplained: list[tuple[int, str]] = []
    out_remaining = Counter(out_counts)
    for idx, norm in anchor_pairs:
        if out_remaining.get(norm, 0) > 0:
            # Keep one occurrence in the output: consume it.
            out_remaining[norm] -= 1
            continue
        if idx in ledger:
            explained += 1
        else:
            unexplained.append((idx, norm))

    missing_unexplained = len(unexplained)

    samples: list[tuple[int, str]] = []
    for idx, norm in unexplained[:_MAX_SAMPLES]:
        samples.append((idx, norm[:_MAX_SAMPLE_CHARS]))

    if total == 0:
        # Anchor itself has no content lines: nothing to reconcile.
        verdict = VERDICT_EMPTY
    elif missing_unexplained == 0:
        verdict = VERDICT_OK
    else:
        verdict = VERDICT_LOSS

    return CoverageReport(
        total_lines=total,
        covered_lines=covered,
        missing_lines=missing_unexplained,
        explained_missing=explained,
        added_lines=added,
        verdict=verdict,
        missing_samples=tuple(samples),
        note=(
            "line-multiset reconciliation: intra-line damage and "
            "reordering are out of scope (content-level RSE); blank "
            "lines are not content; duplicate lines counted by copy"
        ),
    )


def parse_gate(anchor: str, stage_output: str) -> CoverageReport:
    """Zero-tolerance gate for the parse stage: no removals allowed."""
    return reconcile(anchor, stage_output, removals=())


def clean_gate(
    anchor: str,
    stage_output: str,
    removals: Sequence[tuple[int, int]],
    substitutions: Sequence[tuple[int, str]] = (),
) -> CoverageReport:
    """Ledger gate for the clean stage: removals must be explained."""
    return reconcile(
        anchor, stage_output, removals=removals, substitutions=substitutions
    )


def removals_from_decisions(decisions: Iterable[Any]) -> list[tuple[int, int]]:
    """Convert cleaning decisions into 1-based inclusive removal spans.

    Accepts any decision objects carrying the ``CleaningDecision``
    contract (duck-typed, no import of the cleaning package here):
    ``line_range`` is a 0-based half-open ``(start, end)`` span in the
    pre-clean line space and ``decision`` is one of
    ``"removed" / "collapsed" / "preserved"``.

    Semantics: ``removed`` drops the whole span; ``collapsed`` keeps
    the first line and drops the rest; ``preserved`` drops nothing.
    """
    spans: list[tuple[int, int]] = []
    for d in decisions:
        start, end = d.line_range
        if d.decision == "removed":
            spans.append((start + 1, end))
        elif d.decision == "collapsed" and end - start > 1:
            spans.append((start + 2, end))
    return spans
