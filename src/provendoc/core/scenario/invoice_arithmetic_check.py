"""Invoice arithmetic self-consistency checks (prereg §1.D).

Three invariants over the salvaged invoice evidence, all REPORT-only:

* line-level:  qty x unit_price == amount   (per item);
* subtotal:    sum(item amounts) == subtotal (arbitrated witness);
* three-way:   subtotal + tax == total_due  (arbitrated witnesses).

Missing witnesses degrade to ``no_witnesses`` — absence is not
failure, and nothing is ever repaired or invented. Conflicted
arbitration (no selected witness) also degrades: an unsettled field
cannot anchor an invariant.

Honest boundaries:

* line items without proven qty/unit (unprovable column semantics)
  skip the line-level check — they still count toward the subtotal sum;
* discount/shipping intermediate lines are NOT modeled (v1): if present
  they break the subtotal invariant, which surfaces as a mismatch —
  a true report, awaiting v2 modeling;
* Chinese tax-inclusive arithmetic (价税合计 = 金额 + 税额) is v2.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from provendoc.core.scenario.invoice_arbitration import (
    FAMILY_WESTERN,
    ArbitrationResult,
)
from provendoc.core.scenario.invoice_line_items import (
    LineItemHit,
    LineItemResult,
    parse_amount_token,
)

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_NO_WITNESSES = "no_witnesses"

SCOPE_LINE = "line"
SCOPE_SUBTOTAL = "subtotal"
SCOPE_THREE_WAY = "three_way"


@dataclass(frozen=True)
class ArithmeticCheck:
    """One invariant record: scope, values, verdict, reason."""

    scope: str
    line: int | None  # 1-based for line-scope checks
    expected: Decimal | None
    actual: Decimal | None
    verdict: str
    reason: str


@dataclass(frozen=True)
class ArithmeticCheckResult:
    """Arithmetic verification output: all checks + stats."""

    checks: tuple[ArithmeticCheck, ...]
    stats: dict[str, int]

    @property
    def check_count(self) -> int:
        """Number of invariant checks produced."""
        return len(self.checks)

    def mismatches(self) -> tuple[ArithmeticCheck, ...]:
        """All failed invariants (the actionable evidence)."""
        return tuple(c for c in self.checks if c.verdict == VERDICT_MISMATCH)


def _selected_decimal(
    arbitration: ArbitrationResult, field: str
) -> tuple[Decimal, str, int] | None:
    """(decimal, raw value, line) of the arbitrated selection, or None."""
    decision = arbitration.get(field, FAMILY_WESTERN)
    if decision is None or decision.selected is None:
        return None
    dec = parse_amount_token(decision.selected.value)
    if dec is None:
        return None
    return dec, decision.selected.value, decision.selected.line


def _line_check(item: LineItemHit) -> ArithmeticCheck:
    """Line-level invariant: qty x unit == amount (or no witnesses)."""
    if item.qty is None or item.unit_price is None:
        return ArithmeticCheck(
            scope=SCOPE_LINE, line=item.line,
            expected=None, actual=None,
            verdict=VERDICT_NO_WITNESSES,
            reason="qty/unit semantics unproven for this line",
        )
    qty = parse_amount_token(item.qty) or Decimal("0")
    unit = parse_amount_token(item.unit_price) or Decimal("0")
    amount = parse_amount_token(item.amount) or Decimal("0")
    expected = qty * unit
    if expected == amount:
        return ArithmeticCheck(
            scope=SCOPE_LINE, line=item.line,
            expected=expected, actual=amount,
            verdict=VERDICT_MATCH, reason="",
        )
    return ArithmeticCheck(
        scope=SCOPE_LINE, line=item.line,
        expected=expected, actual=amount,
        verdict=VERDICT_MISMATCH,
        reason=f"qty x unit = {expected} != amount {amount}",
    )


def check_invoice_arithmetic(
    items: LineItemResult, arbitration: ArbitrationResult
) -> ArithmeticCheckResult:
    """Run the three invariants over extraction + arbitration evidence.

    Pure function; no I/O; verdicts only — nothing is repaired.
    """
    checks: list[ArithmeticCheck] = []
    stats = {
        VERDICT_MATCH: 0,
        VERDICT_MISMATCH: 0,
        VERDICT_NO_WITNESSES: 0,
    }

    # Line-level invariants: one per item (proven or not).
    for item in items.items:
        check = _line_check(item)
        checks.append(check)
        stats[check.verdict] += 1

    # Subtotal invariant: sum(items) == subtotal.
    subtotal = _selected_decimal(arbitration, "subtotal")
    if subtotal is None or not items.items:
        checks.append(ArithmeticCheck(
            scope=SCOPE_SUBTOTAL, line=None,
            expected=None, actual=None,
            verdict=VERDICT_NO_WITNESSES,
            reason="no arbitrated subtotal or no line items",
        ))
        stats[VERDICT_NO_WITNESSES] += 1
    else:
        dec, raw, line = subtotal
        actual = items.total_amount
        if actual == dec:
            checks.append(ArithmeticCheck(
                scope=SCOPE_SUBTOTAL, line=line,
                expected=dec, actual=actual,
                verdict=VERDICT_MATCH, reason="",
            ))
            stats[VERDICT_MATCH] += 1
        else:
            checks.append(ArithmeticCheck(
                scope=SCOPE_SUBTOTAL, line=line,
                expected=dec, actual=actual,
                verdict=VERDICT_MISMATCH,
                reason=f"subtotal witness {raw} != sum of items {actual}",
            ))
            stats[VERDICT_MISMATCH] += 1

    # Three-way invariant: subtotal + tax == total_due.
    tax = _selected_decimal(arbitration, "tax")
    total = _selected_decimal(arbitration, "total_due")
    if subtotal is None or tax is None or total is None:
        checks.append(ArithmeticCheck(
            scope=SCOPE_THREE_WAY, line=None,
            expected=None, actual=None,
            verdict=VERDICT_NO_WITNESSES,
            reason="three-way needs subtotal, tax and total_due witnesses",
        ))
        stats[VERDICT_NO_WITNESSES] += 1
    else:
        sub_dec = subtotal[0]
        tax_dec = tax[0]
        expected = sub_dec + tax_dec
        actual = total[0]
        if expected == actual:
            checks.append(ArithmeticCheck(
                scope=SCOPE_THREE_WAY, line=total[2],
                expected=expected, actual=actual,
                verdict=VERDICT_MATCH, reason="",
            ))
            stats[VERDICT_MATCH] += 1
        else:
            checks.append(ArithmeticCheck(
                scope=SCOPE_THREE_WAY, line=total[2],
                expected=expected, actual=actual,
                verdict=VERDICT_MISMATCH,
                reason=(f"subtotal {sub_dec} + tax {tax_dec} = {expected} "
                        f"!= total {actual}"),
            ))
            stats[VERDICT_MISMATCH] += 1

    return ArithmeticCheckResult(checks=tuple(checks), stats=stats)


__all__ = [
    "ArithmeticCheck",
    "ArithmeticCheckResult",
    "SCOPE_LINE",
    "SCOPE_SUBTOTAL",
    "SCOPE_THREE_WAY",
    "VERDICT_MATCH",
    "VERDICT_MISMATCH",
    "VERDICT_NO_WITNESSES",
    "check_invoice_arithmetic",
]
