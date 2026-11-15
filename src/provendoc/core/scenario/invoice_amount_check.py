"""Chinese upper-case amount verification (prereg invoice_scenario_prereg_v1 §1.C).

Converts a 大写 RMB amount (壹仟元整) to an exact Decimal and
cross-checks it against the 小写 numeric total (￥1000.00) recorded by
the anchor layer. This is the invoice scenario's core arithmetic
invariant: the two spellings of the same total must agree.

Discipline:

* exact Decimal arithmetic only — no float, no rounding, no tolerance;
* a mismatch is REPORTED, never auto-repaired: a genuine difference is
  evidence (upstream OCR damage, or a real invoice inconsistency);
  deciding which side wins is downstream arbitration, not this module;
* conservative parser: any form outside the v1 grammar yields
  unparseable (None), never a guess.

v1 grammar (subset, standard VAT-invoice usage):

  人民币? digits [拾佰仟 digits]* [万 ...]* [亿 ...]* 元|圆 [digits 角分]* [整|正]?

Honest boundaries (documented, not silently wrong):

* ``万亿`` as a compound section unit is NONSTANDARD — guarded to
  unparseable rather than risk a wrong value;
* historical variant numerals (弍, 叉, ...) are not in the grammar;
* 拾佰仟 units after 元 (mixed int/frac) are not in the grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from provendoc.core.scenario.invoice_anchors import AnchorResult

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UPPER_UNPARSEABLE = "upper_unparseable"
VERDICT_LOWER_UNPARSEABLE = "lower_unparseable"
VERDICT_SINGLE_WITNESS = "single_witness"

_PREFIX = "人民币"
_DIGITS = {
    "零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4,
    "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_INT_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
_SECTION_UNIT_WAN = 10_000
_SECTION_UNIT_YI = 100_000_000
_FRAC_UNITS = {"角": Decimal("0.1"), "分": Decimal("0.01")}
_TERMINALS = {"整", "正"}
_YUAN = {"元", "圆"}


def parse_cn_upper_amount(text: str) -> Decimal | None:
    """Parse a 大写 amount to an exact Decimal; None if unparseable.

    Pure function; no I/O; conservative — unknown characters,
    misplaced units or nonstandard compounds all yield None.
    """
    s = text.strip()
    if s.startswith(_PREFIX):
        s = s[len(_PREFIX):].strip()
    if not s:
        return None
    if "万亿" in s:
        return None  # nonstandard compound — refuse rather than misvalue
    yuan_positions = [i for i, ch in enumerate(s) if ch in _YUAN]
    if len(yuan_positions) != 1:
        return None
    int_part = s[:yuan_positions[0]]
    frac_part = s[yuan_positions[0] + 1:]
    integer = _parse_integer_part(int_part)
    if integer is None:
        return None
    frac = _parse_fraction_part(frac_part)
    if frac is None:
        return None
    return Decimal(integer) + frac


def _parse_integer_part(part: str) -> int | None:
    """Parse the pre-元 section; None on any unknown character."""
    if not part:
        return None
    total = 0  # accumulated 亿-scaled value
    section = 0  # current 万-section accumulator
    num = 0  # pending digit awaiting a unit
    for ch in part:
        digit = _DIGITS.get(ch)
        if digit is not None:
            if digit != 0:
                num = digit
            continue
        unit = _INT_UNITS.get(ch)
        if unit is not None:
            section += (num if num else 1) * unit
            num = 0
            continue
        if ch == "万":
            section = (section + num) * _SECTION_UNIT_WAN
            total += section
            section = 0
            num = 0
            continue
        if ch == "亿":
            total = (total + section + num) * _SECTION_UNIT_YI
            section = 0
            num = 0
            continue
        return None
    return total + section + num


def _parse_fraction_part(part: str) -> Decimal | None:
    """Parse the post-元 section (角/分/整/正); None on garbage."""
    if not part:
        return Decimal("0")
    if part[0] in _TERMINALS:
        return Decimal("0") if len(part) == 1 else None
    total = Decimal("0")
    num = 0
    seen_unit = False
    seen_nonzero = False
    for ch in part:
        digit = _DIGITS.get(ch)
        if digit is not None:
            if digit != 0:
                num = digit
                seen_nonzero = True
            continue
        unit = _FRAC_UNITS.get(ch)
        if unit is not None:
            total += Decimal(num) * unit
            num = 0
            seen_unit = True
            continue
        return None
    if num != 0:
        return None  # trailing digit without 角/分 unit — ambiguous
    if seen_unit and not seen_nonzero:
        return None  # units with no digits at all (e.g. bare 角分)
    return total


def parse_lower_amount(value: str) -> Decimal | None:
    """Parse a 小写 numeric total (with commas) to Decimal; None on junk."""
    cleaned = value.replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class AmountCheck:
    """One cross-check record: witnesses, both Decimals, verdict, reason."""

    upper_value: str | None
    upper_line: int | None
    lower_value: str | None
    lower_line: int | None
    upper_decimal: Decimal | None
    lower_decimal: Decimal | None
    verdict: str
    reason: str


@dataclass(frozen=True)
class AmountCheckResult:
    """Amount verification output: all check records + stats."""

    checks: tuple[AmountCheck, ...]
    stats: dict[str, int]

    @property
    def check_count(self) -> int:
        """Number of cross-check records produced."""
        return len(self.checks)


def check_cn_amount_consistency(anchor_result: AnchorResult) -> AmountCheckResult:
    """Cross-check 大写 vs 小写 totals from an anchor extraction.

    Every (upper, lower) witness pair produces one check record
    (cross product — multiple witnesses on either side are all
    reported; arbitration is downstream). A single-sided witness
    yields single_witness, not failure. No witnesses yields an empty
    result. Pure function; no I/O; never mutates or repairs values.
    """
    uppers = anchor_result.by_field("total_cn_upper")
    lowers = anchor_result.by_field("total_cn_lower")
    checks: list[AmountCheck] = []
    stats = {
        "match": 0,
        "mismatch": 0,
        "upper_unparseable": 0,
        "lower_unparseable": 0,
        "single_witness": 0,
    }

    if not uppers and not lowers:
        return AmountCheckResult(checks=(), stats=stats)

    if not lowers:
        for upper in uppers:
            dec = parse_cn_upper_amount(upper.value)
            verdict = (
                VERDICT_UPPER_UNPARSEABLE if dec is None
                else VERDICT_SINGLE_WITNESS
            )
            checks.append(AmountCheck(
                upper_value=upper.value, upper_line=upper.line,
                lower_value=None, lower_line=None,
                upper_decimal=dec, lower_decimal=None,
                verdict=verdict, reason="no lower-case witness",
            ))
            stats[verdict] += 1
        return _finish(checks, stats)

    if not uppers:
        for lower in lowers:
            dec = parse_lower_amount(lower.value)
            verdict = (
                VERDICT_LOWER_UNPARSEABLE if dec is None
                else VERDICT_SINGLE_WITNESS
            )
            checks.append(AmountCheck(
                upper_value=None, upper_line=None,
                lower_value=lower.value, lower_line=lower.line,
                upper_decimal=None, lower_decimal=dec,
                verdict=verdict, reason="no upper-case witness",
            ))
            stats[verdict] += 1
        return _finish(checks, stats)

    for upper in uppers:
        for lower in lowers:
            upper_dec = parse_cn_upper_amount(upper.value)
            lower_dec = parse_lower_amount(lower.value)
            if upper_dec is None:
                verdict = VERDICT_UPPER_UNPARSEABLE
                reason = "upper-case form outside v1 grammar"
            elif lower_dec is None:
                verdict = VERDICT_LOWER_UNPARSEABLE
                reason = "lower-case value not a clean decimal"
            elif upper_dec == lower_dec:
                verdict = VERDICT_MATCH
                reason = ""
            else:
                verdict = VERDICT_MISMATCH
                reason = f"upper={upper_dec} lower={lower_dec}"
            checks.append(AmountCheck(
                upper_value=upper.value, upper_line=upper.line,
                lower_value=lower.value, lower_line=lower.line,
                upper_decimal=upper_dec, lower_decimal=lower_dec,
                verdict=verdict, reason=reason,
            ))
            stats[verdict] += 1
    return _finish(checks, stats)


def _finish(
    checks: list[AmountCheck], stats: dict[str, int]
) -> AmountCheckResult:
    """Assemble the frozen result (internal helper)."""
    return AmountCheckResult(checks=tuple(checks), stats=stats)


__all__ = [
    "AmountCheck",
    "AmountCheckResult",
    "VERDICT_LOWER_UNPARSEABLE",
    "VERDICT_MATCH",
    "VERDICT_MISMATCH",
    "VERDICT_SINGLE_WITNESS",
    "VERDICT_UPPER_UNPARSEABLE",
    "check_cn_amount_consistency",
    "parse_cn_upper_amount",
]
