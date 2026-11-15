"""Invoice line-item extraction (prereg invoice_scenario_prereg_v1 §1.D).

Extracts western line items from (salvaged) invoice text: a line whose
trailing token is a two-decimal amount is an amount witness; qty and
unit_price are recorded ONLY when the trailing three amounts satisfy
qty x unit_price == amount — the multiplication itself is the proof of
the column semantics. A three-amount line that fails the check is
still recorded (amount + raw tokens) but carries no qty/unit claim.

Summary lines (TOTAL / SUBTOTAL / TAX / BALANCE / AMOUNT DUE / ...) are
NOT line items — they are summary witnesses owned by the anchor layer;
they are excluded by keyword. Header rows and dates never match the
amount-token shape.

Discipline: evidence only, no repair, no invention — qty/unit that
cannot prove themselves stay None; every hit keeps its line number
and the raw amounts seen.

Honest boundaries:

* Chinese line items (货物名称/规格/数量/单价/金额 columns) are v2 —
  blocked on the Chinese GT bucket (prereg D1);
* descriptions containing summary keywords (e.g. "TAX CONSULTING
  SERVICES") are conservatively dropped as summary lines — a recall
  cost recorded here, not silently wrong;
* multi-column layouts where the amount is not the trailing token are
  not captured in v1 (rightmost-amount assumption);
* a trailing reference number with two decimals (``Reference no.
  1.22``) is indistinguishable from a small amount at this layer —
  tolerated: a false item breaks the subtotal invariant downstream,
  which surfaces as a mismatch report, not silent corruption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_AMOUNT_TOKEN_RE = re.compile(r"[-+]?[\d,]+\.\d{2}(?!\d)")

# Summary-line keyword gate (word-boundary). Exported for the
# chunking layer: a summary line is also a preferred chunk boundary.
SUMMARY_LINE_RE = re.compile(
    r"\b(?:TOTAL|SUB\s*[-\s]?\s*TOTAL|BALANCE|AMOUNT\s+DUE|"
    r"TAX|VAT|DISCOUNT|SHIPPING|NET\s+DUE|PREVIOUS)\b",
    re.IGNORECASE,
)


def parse_amount_token(token: str) -> Decimal | None:
    """Amount token -> Decimal; None on junk (never raised)."""
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class LineItemHit:
    """One line-item witness: amounts as-is + proven semantics."""

    line: int  # 1-based
    amount: str  # trailing amount token, raw
    qty: str | None  # raw qty token, only when multiplication proves it
    unit_price: str | None  # raw unit token, only when proven
    raw: str  # the raw line text


@dataclass(frozen=True)
class LineItemResult:
    """Line-item extraction output: all hits + stats."""

    items: tuple[LineItemHit, ...]
    stats: dict[str, int]

    @property
    def item_count(self) -> int:
        """Number of line-item witnesses recorded."""
        return len(self.items)

    @property
    def total_amount(self) -> Decimal:
        """Exact sum of all item amounts (0 when empty)."""
        return sum(
            (parse_amount_token(item.amount) or Decimal("0")
             for item in self.items),
            Decimal("0"),
        )


def _is_summary_line(line: str) -> bool:
    """True when the line carries a summary keyword (word-boundary)."""
    return SUMMARY_LINE_RE.search(line) is not None


def extract_line_items(text: str) -> LineItemResult:
    """Extract western line items from (salvaged) invoice text.

    Every non-summary line whose trailing token is a two-decimal
    amount becomes one LineItemHit; qty/unit_price semantics require
    the trailing three amounts to satisfy a x b == c exactly.
    Pure function; no I/O.
    """
    items: list[LineItemHit] = []
    stats = {
        "item_lines": 0,
        "with_qty_unit": 0,
        "summary_lines_skipped": 0,
    }
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        if _is_summary_line(line):
            stats["summary_lines_skipped"] += 1
            continue
        tokens = _AMOUNT_TOKEN_RE.findall(line)
        if not tokens:
            continue
        if not line.rstrip().endswith(tokens[-1]):
            continue  # amount must be the trailing token
        amount = tokens[-1]
        qty: str | None = None
        unit: str | None = None
        if len(tokens) >= 3:
            a = parse_amount_token(tokens[-3])
            b = parse_amount_token(tokens[-2])
            c = parse_amount_token(tokens[-1])
            if a is not None and b is not None and c is not None and a * b == c:
                qty, unit = tokens[-3], tokens[-2]
        hit = LineItemHit(
            line=lineno, amount=amount,
            qty=qty, unit_price=unit, raw=line,
        )
        items.append(hit)
        stats["item_lines"] += 1
        if qty is not None:
            stats["with_qty_unit"] += 1
    return LineItemResult(items=tuple(items), stats=stats)


__all__ = [
    "LineItemHit",
    "LineItemResult",
    "SUMMARY_LINE_RE",
    "extract_line_items",
    "parse_amount_token",
]
