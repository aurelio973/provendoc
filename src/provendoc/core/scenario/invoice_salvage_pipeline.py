"""Invoice scenario L1 salvage orchestration (prereg §1.A entry point).

Fixed stage order, each stage ledger-recorded:

1. ``salvage_numbers`` — R1/R2 numeric damage repair (line rewrites);
2. ``dedup_native_chart_text`` — figure-channel arbitration (line
   rewrites / payload drops; MUST run after salvage so digit-damage
   variants unify before matching);
3. ``remove_orphan_noise`` — orphan punct lines + stray quotes (line
   drops / rewrites).

Coordinate guarantee (the reason composition is trivial): stages 1-2
never change the line count, so every stage's line numbers live in the
original-text coordinate space, and the only line drops happen in the
final stage. The orchestrator therefore emits a merged ledger —
``substitutions`` + ``removal_spans`` in ORIGINAL line coordinates —
directly consumable by ``reconciliation.clean_gate``.

Honest boundaries: stage ledgers are kept per-stage for audit; the
merged ledger is derived by exact per-line comparison, so any
unaccounted drift between stages would surface as an unexplained
substitution rather than pass silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from provendoc.core.scenario.figure_channel_dedup import dedup_native_chart_text
from provendoc.core.scenario.invoice_salvage import salvage_numbers
from provendoc.core.scenario.orphan_noise import (
    ACTION_DROPPED_LINE,
    ACTION_STRIPPED_QUOTE,
    remove_orphan_noise,
)


@dataclass(frozen=True)
class ScenarioSalvageResult:
    """Merged salvage output: final text + original-coordinate ledger."""

    text: str
    substitutions: tuple[tuple[int, str], ...]
    removal_spans: tuple[tuple[int, int], ...]
    stage_stats: dict[str, dict[str, int]]

    @property
    def edit_count(self) -> int:
        """Total ledger decisions across all stages."""
        return len(self.substitutions) + len(self.removal_spans)


def salvage_invoice_text(text: str) -> ScenarioSalvageResult:
    """Run the full invoice L1 salvage chain with a merged ledger.

    Idempotent; pure; stage order is part of the contract (see module
    docstring) and must not be reordered.
    """
    stage1 = salvage_numbers(text)
    stage2 = dedup_native_chart_text(stage1.text)
    stage3 = remove_orphan_noise(stage2.text)

    dropped = {
        e.line for e in stage3.edits if e.action == ACTION_DROPPED_LINE
    }
    stripped: dict[int, str] = {
        e.line: e.after
        for e in stage3.edits
        if e.action == ACTION_STRIPPED_QUOTE and isinstance(e.after, str)
    }

    original_lines = text.split("\n")
    # Stage 1-2 preserve line count, so stage-2 lines are aligned 1:1
    # with the original; stage 3 drops/rewrites on top of that.
    mid_lines = stage2.text.split("\n")
    if len(mid_lines) != len(original_lines):  # pragma: no cover - invariant
        raise RuntimeError(
            "salvage pipeline invariant violated: line count changed "
            "before the orphan stage"
        )

    substitutions: list[tuple[int, str]] = []
    removal_spans: list[tuple[int, int]] = []
    for lineno, mid in enumerate(mid_lines, start=1):
        if lineno in dropped:
            removal_spans.append((lineno, lineno))
            continue
        final = stripped.get(lineno, mid)
        if final != original_lines[lineno - 1]:
            substitutions.append((lineno, final))

    return ScenarioSalvageResult(
        text=stage3.text,
        substitutions=tuple(substitutions),
        removal_spans=tuple(removal_spans),
        stage_stats={
            "salvage_numbers": {"edits": stage1.edit_count},
            "figure_channel_dedup": dict(stage2.stats),
            "orphan_noise": dict(stage3.stats),
        },
    )
