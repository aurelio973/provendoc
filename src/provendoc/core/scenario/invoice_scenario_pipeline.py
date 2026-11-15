"""Full invoice scenario orchestration (prereg §1.A-§1.E end to end).

Chains the five prereg segments over one text, ledger end to end:

A  ``salvage_invoice_text``   numeric/channel/orphan salvage with a
                               merged original-coordinate ledger;
    ``line_map`` derivation    salvaged line number -> original line
                               number (A may physically drop lines, so
                               downstream coordinates need an explicit
                               anchor back to the original);
B  ``extract_invoice_anchors`` + ``arbitrate_anchors``
                               field witnesses + multi-witness verdicts;
C  ``check_cn_amount_consistency``
                               uppercase/digit amount cross-checks
                               (report-only);
D  ``extract_line_items`` + ``check_invoice_arithmetic``
                               line items + three arithmetic invariants;
E  ``chunk_invoice_text`` + ``check_chunk_invariants``
                               line-boundary chunks + coverage/integrity
                               reconciliation against the chunker input.

Degradation discipline: every stage is a pure function with no
exception path — empty or witness-free inputs degrade to empty/``no_
witnesses`` results, never raise; the floor is exactly the
user-mandated chunk_size/overlap fallback (E with no scenario
evidence still splits and still reconciles). Coordinates: B/C/D
witness line numbers live in the SALVAGED coordinate space (A's
post-drop numbering); ``line_map`` translates them to the original
space, where A's substitutions/removals also live.

Honest boundaries:

* scenario-LEVEL gating (deciding the text IS an invoice, via L2
  domain signals) is NOT here — this pipeline runs when the caller
  has already chosen the invoice scenario; running it on non-invoice
  text yields empty witnesses and conservative chunks, not errors;
* the line_map maps surviving lines only; dropped lines are already
  accounted for in ``salvage.removal_spans``;
* DQR/RSE wiring (metadata stamping) is a separate module by design —
  it landed in ``invoice_scenario_dqr.attach_invoice_scenario``,
  which ``universal_parser`` invokes after the L2 resolver labels
  the text ``invoice``; this pipeline stays a pure text-in/result-out
  chain with no document dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from provendoc.core.scenario.invoice_amount_check import (
    AmountCheckResult,
    check_cn_amount_consistency,
)
from provendoc.core.scenario.invoice_anchors import (
    AnchorResult,
    extract_invoice_anchors,
)
from provendoc.core.scenario.invoice_arbitration import (
    ArbitrationResult,
    arbitrate_anchors,
)
from provendoc.core.scenario.invoice_arithmetic_check import (
    ArithmeticCheckResult,
    check_invoice_arithmetic,
)
from provendoc.core.scenario.invoice_chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_LINES,
    ChunkInvariantResult,
    ChunkResult,
    check_chunk_invariants,
    chunk_invoice_text,
)
from provendoc.core.scenario.invoice_line_items import (
    LineItemResult,
    extract_line_items,
)
from provendoc.core.scenario.invoice_salvage_pipeline import (
    ScenarioSalvageResult,
    salvage_invoice_text,
)


@dataclass(frozen=True)
class InvoiceScenarioResult:
    """Full invoice scenario run: every segment's evidence, chained."""

    salvage: ScenarioSalvageResult
    line_map: tuple[int, ...]  # salvaged line no -> original line no
    anchors: AnchorResult
    arbitration: ArbitrationResult
    amount_checks: AmountCheckResult
    line_items: LineItemResult
    arithmetic_checks: ArithmeticCheckResult
    chunks: ChunkResult
    chunk_invariants: ChunkInvariantResult

    def to_original_line(self, salvaged_line: int) -> int | None:
        """Translate a salvaged-space line number to the original.

        Out-of-range or dropped lines return None (caller reports,
        never guesses).
        """
        if 1 <= salvaged_line <= len(self.line_map):
            return self.line_map[salvaged_line - 1]
        return None


def _derive_line_map(
    original_line_count: int, removal_spans: tuple[tuple[int, int], ...]
) -> tuple[int, ...]:
    """Salvaged line numbers -> original line numbers (survivors only).

    Dropped original lines (per the merged removal ledger) are absent
    from the map; surviving lines keep their original order.
    """
    dropped: set[int] = set()
    for start, end in removal_spans:
        dropped.update(range(start, end + 1))
    return tuple(
        no for no in range(1, original_line_count + 1) if no not in dropped
    )


def run_invoice_scenario(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> InvoiceScenarioResult:
    """Run the full A-E invoice scenario chain over one text.

    Pure function; no I/O; every stage's output is recorded verbatim
    (nothing is summarized away or silently dropped). Empty input
    degrades to empty evidence + zero/one conservative chunks.
    """
    # A: salvage with merged original-coordinate ledger.
    salvage = salvage_invoice_text(text)
    line_map = _derive_line_map(
        len(text.split("\n")), salvage.removal_spans
    )

    # B: witnesses on the salvaged text + arbitration verdicts.
    anchors = extract_invoice_anchors(salvage.text)
    arbitration = arbitrate_anchors(anchors)

    # C: Chinese-numeral/digit amount cross-checks (report-only).
    amount_checks = check_cn_amount_consistency(anchors)

    # D: line items + arithmetic invariants.
    line_items = extract_line_items(salvage.text)
    arithmetic_checks = check_invoice_arithmetic(line_items, arbitration)

    # E: line-boundary chunking + reconciliation against the chunker
    # input (the salvaged text — the coordinate space B/D live in).
    chunks = chunk_invoice_text(
        salvage.text, max_chars=max_chars, overlap_lines=overlap_lines
    )
    chunk_invariants = check_chunk_invariants(salvage.text, chunks.chunks)

    return InvoiceScenarioResult(
        salvage=salvage,
        line_map=line_map,
        anchors=anchors,
        arbitration=arbitration,
        amount_checks=amount_checks,
        line_items=line_items,
        arithmetic_checks=arithmetic_checks,
        chunks=chunks,
        chunk_invariants=chunk_invariants,
    )


__all__ = [
    "InvoiceScenarioResult",
    "run_invoice_scenario",
]
