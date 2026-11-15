"""Invoice anchor arbitration (prereg invoice_scenario_prereg_v1 §1.B, downstream).

Decides which witness (if any) represents a field when the anchor
layer recorded several. Evidence-ordered and fully recorded: every
decision carries all witnesses, the winning cluster key, and a
reason; nothing is silently dropped or overwritten.

Policies (per field x family):

* decimal fields (total_due, total_cn_lower): witnesses compared as
  exact Decimals — comma-insensitive (``32,390.03`` == ``32390.03``);
* total_cn_upper: compared via the §1.C parser (壹仟元整 == 壹仟元正);
* literal fields (invoice_number, invoice_code, tax_id,
  tax_registration_id): exact string comparison — OCR-damaged variants
  stay distinct, because that difference is evidence;
* date fields: witnesses cluster by their set of admissible
  (year, month, day) interpretations. A numeric d/m/y vs m/d/y
  witness joins a long-form cluster ONLY when exactly one of its
  interpretations is admissible (day>12 self-disambiguates) or when
  exactly one candidate cluster exists — ambiguous numerics never
  silently resolve;
* buyer_seller_name: NOT arbitratable to one value — 购买方 and 销售方
  are distinct roles; the role split needs role labels on the anchor
  (v2), so v1 records multi_role and defers.

Verdicts: unique (one witness) / unanimous (one cluster) / majority
(strictly largest cluster, dissent fully recorded) / conflict (no
majority — no selection, all candidates listed) / multi_role.

Honest boundaries:

* arbitration is family-scoped: a western+chinese co-occurrence is an
  L2 scenario-identification signal, never merged into one value here;
* majority is a size prior, not truth — the dissent is recorded and
  downstream (DQR / §1.C amount check) may overrule with better
  evidence; §1.C verdicts are not yet an arbitration input (v2);
* date clustering refuses to invent a resolution: an ambiguous
  numeric with zero or several candidate clusters stays literal.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from provendoc.core.scenario.invoice_amount_check import parse_cn_upper_amount
from provendoc.core.scenario.invoice_anchors import (
    FAMILY_CHINESE,
    FAMILY_WESTERN,
    AnchorHit,
    AnchorResult,
)

VERDICT_UNIQUE = "unique"
VERDICT_UNANIMOUS = "unanimous"
VERDICT_MAJORITY = "majority"
VERDICT_CONFLICT = "conflict"
VERDICT_MULTI_ROLE = "multi_role"

_POLICY_DECIMAL = "decimal"
_POLICY_CN_UPPER = "cn_upper"
_POLICY_LITERAL = "literal"
_POLICY_DATE = "date"
_POLICY_MULTI_ROLE = "multi_role"

_FIELD_POLICY: dict[tuple[str, str], str] = {
    ("invoice_number", FAMILY_WESTERN): _POLICY_LITERAL,
    ("invoice_number", FAMILY_CHINESE): _POLICY_LITERAL,
    ("invoice_code", FAMILY_CHINESE): _POLICY_LITERAL,
    ("invoice_date", FAMILY_WESTERN): _POLICY_DATE,
    ("invoice_date", FAMILY_CHINESE): _POLICY_DATE,
    ("total_due", FAMILY_WESTERN): _POLICY_DECIMAL,
    ("subtotal", FAMILY_WESTERN): _POLICY_DECIMAL,
    ("tax", FAMILY_WESTERN): _POLICY_DECIMAL,
    ("total_cn_lower", FAMILY_CHINESE): _POLICY_DECIMAL,
    ("total_cn_upper", FAMILY_CHINESE): _POLICY_CN_UPPER,
    ("tax_id", FAMILY_WESTERN): _POLICY_LITERAL,
    ("tax_registration_id", FAMILY_CHINESE): _POLICY_LITERAL,
    ("buyer_seller_name", FAMILY_CHINESE): _POLICY_MULTI_ROLE,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3,
    "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12,
}

_RE_MONTH_DAY_YEAR = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")
_RE_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})")
_RE_NUMERIC_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_RE_YEAR_FIRST = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def _valid_ymd(year: int, month: int, day: int) -> bool:
    """True if (year, month, day) is a real calendar date."""
    if not 1 <= month <= 12:
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def _ymd_key(year: int, month: int, day: int) -> str:
    """Canonical cluster key for one date interpretation."""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_month_day_year(value: str) -> list[tuple[int, int, int]]:
    """US long form: ``December 20, 2000`` -> (2000, 12, 20)."""
    match = _RE_MONTH_DAY_YEAR.fullmatch(value.strip())
    if not match:
        return []
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return []
    return [(int(match.group(3)), month, int(match.group(2)))]


def _parse_day_month_year(value: str) -> list[tuple[int, int, int]]:
    """UK long form: ``20 December 2000`` -> (2000, 12, 20)."""
    match = _RE_DAY_MONTH_YEAR.fullmatch(value.strip())
    if not match:
        return []
    month = _MONTHS.get(match.group(2).lower())
    if month is None:
        return []
    return [(int(match.group(3)), month, int(match.group(1)))]


def _parse_numeric_ambiguous(value: str) -> list[tuple[int, int, int]]:
    """Numeric a/b/yyyy: BOTH US (m/d) and EU (d/m) readings, unfiltered."""
    match = _RE_NUMERIC_SLASH.fullmatch(value.strip())
    if not match:
        return []
    a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return [(year, a, b), (year, b, a)]


def _parse_year_first(value: str) -> list[tuple[int, int, int]]:
    """ISO / CN numeric form: ``2000-12-20`` / ``2020.01.08`` -> (y, m, d)."""
    match = _RE_YEAR_FIRST.fullmatch(value.strip())
    if not match:
        return []
    return [(int(match.group(1)), int(match.group(2)), int(match.group(3)))]


_DATE_VALUE_PARSERS = {
    "invoice_date_us_long": _parse_month_day_year,
    "invoice_date_uk_long": _parse_day_month_year,
    "invoice_date_numeric": _parse_numeric_ambiguous,
    "invoice_date_iso": _parse_year_first,
    "invoice_date_cn": _parse_year_first,  # 年/月/日 normalized first
    "invoice_date_cn_numeric": _parse_year_first,
}


def _date_interpretations(hit: AnchorHit) -> tuple[str, ...] | None:
    """Admissible interpretation keys for a date witness.

    Returns None when the pattern id is not a known date form (literal
    fallback); an empty tuple when the form parsed to no valid date.
    """
    parser = _DATE_VALUE_PARSERS.get(hit.pattern)
    if parser is None:
        return None
    raw = hit.value.replace("年", "-").replace("月", "-").replace("日", "")
    keys: list[str] = []
    for year, month, day in parser(raw):
        if _valid_ymd(year, month, day):
            keys.append(_ymd_key(year, month, day))
    return tuple(dict.fromkeys(keys))


def _decimal_key(value: str) -> str | None:
    """Comma-insensitive exact Decimal key; None on junk."""
    try:
        return str(Decimal(value.replace(",", "").strip()))
    except InvalidOperation:
        return None


def _cn_upper_key(value: str) -> str | None:
    """§1.C parser key (壹仟元整 == 壹仟元正 == 1000); None if unparseable."""
    dec = parse_cn_upper_amount(value)
    return str(dec) if dec is not None else None


def _simple_key(policy: str, hit: AnchorHit) -> str:
    """Cluster key for non-date policies; literal-fallback on junk.

    A literal-policy key IS the raw value (no prefix); the
    ``literal:`` prefix marks un-normalizable junk in normalized
    fields (decimal / cn_upper) so downstream can tell them apart.
    """
    key: str | None = None
    if policy == _POLICY_DECIMAL:
        key = _decimal_key(hit.value)
    elif policy == _POLICY_CN_UPPER:
        key = _cn_upper_key(hit.value)
    elif policy == _POLICY_LITERAL:
        return hit.value
    return key if key is not None else f"literal:{hit.value}"


def _cluster_simple(
    witnesses: tuple[AnchorHit, ...], policy: str
) -> list[tuple[str, list[AnchorHit]]]:
    """Group witnesses by normalized key (dict order = first occurrence)."""
    clusters: dict[str, list[AnchorHit]] = {}
    for hit in witnesses:
        clusters.setdefault(_simple_key(policy, hit), []).append(hit)
    return list(clusters.items())


def _cluster_dates(
    witnesses: tuple[AnchorHit, ...]
) -> list[tuple[str, list[AnchorHit]]]:
    """Cluster date witnesses by admissible interpretation sets.

    Unambiguous witnesses (exactly one admissible key) form canonical
    clusters first; an ambiguous numeric witness joins a canonical
    cluster only when exactly one of its candidate keys exists there
    — otherwise it stays a literal cluster (conservative, never
    invented).
    """
    canonical: dict[str, list[AnchorHit]] = {}
    literal: dict[str, list[AnchorHit]] = {}
    pending: list[tuple[AnchorHit, tuple[str, ...]]] = []
    for hit in witnesses:
        keys = _date_interpretations(hit)
        if keys and len(keys) == 1:
            canonical.setdefault(keys[0], []).append(hit)
        elif keys:
            pending.append((hit, keys))
        else:
            literal.setdefault(f"literal:{hit.value}", []).append(hit)
    for hit, keys in pending:
        candidates = {k for k in keys if k in canonical}
        if len(candidates) == 1:
            canonical[next(iter(candidates))].append(hit)
        else:
            literal.setdefault(f"literal:{hit.value}", []).append(hit)
    return list(canonical.items()) + list(literal.items())


@dataclass(frozen=True)
class FieldDecision:
    """One arbitration outcome: verdict, selection, all witnesses."""

    field: str
    family: str
    verdict: str
    selected: AnchorHit | None  # None for conflict / multi_role
    selected_key: str | None
    witnesses: tuple[AnchorHit, ...]
    reason: str


@dataclass(frozen=True)
class ArbitrationResult:
    """Arbitration output: one decision per (field, family) group."""

    decisions: tuple[FieldDecision, ...]
    stats: dict[str, int]

    @property
    def decision_count(self) -> int:
        """Number of field-level decisions produced."""
        return len(self.decisions)

    def get(self, field: str, family: str) -> FieldDecision | None:
        """Decision for one (field, family) group, if any."""
        for decision in self.decisions:
            if decision.field == field and decision.family == family:
                return decision
        return None


def arbitrate_anchors(anchor_result: AnchorResult) -> ArbitrationResult:
    """Arbitrate multi-witness fields from an anchor extraction.

    Every (field, family) group with at least one witness produces one
    FieldDecision. Groups with no witnesses produce nothing (absence
    is not failure). Pure function; no I/O; never rewrites a value.
    """
    groups: dict[tuple[str, str], list[AnchorHit]] = {}
    for hit in anchor_result.hits:
        groups.setdefault((hit.field, hit.family), []).append(hit)

    decisions: list[FieldDecision] = []
    stats = {
        VERDICT_UNIQUE: 0,
        VERDICT_UNANIMOUS: 0,
        VERDICT_MAJORITY: 0,
        VERDICT_CONFLICT: 0,
        VERDICT_MULTI_ROLE: 0,
    }
    for (field, family), witnesses in groups.items():
        policy = _FIELD_POLICY.get((field, family), _POLICY_LITERAL)
        decision = _arbitrate_group(field, family, tuple(witnesses), policy)
        decisions.append(decision)
        stats[decision.verdict] += 1
    return ArbitrationResult(decisions=tuple(decisions), stats=stats)


def _arbitrate_group(
    field: str,
    family: str,
    witnesses: tuple[AnchorHit, ...],
    policy: str,
) -> FieldDecision:
    """Arbitrate one (field, family) group (internal helper)."""
    if policy == _POLICY_MULTI_ROLE:
        return FieldDecision(
            field=field, family=family, verdict=VERDICT_MULTI_ROLE,
            selected=None, selected_key=None, witnesses=witnesses,
            reason="购买方/销售方 are distinct roles; role split is v2",
        )

    if policy == _POLICY_DATE:
        clusters = _cluster_dates(witnesses)
    else:
        clusters = _cluster_simple(witnesses, policy)

    sizes_desc = ", ".join(
        f"{key}x{len(members)}"
        for key, members in sorted(
            clusters, key=lambda item: (-len(item[1]), item[0])
        )
    )

    if len(witnesses) == 1:
        return FieldDecision(
            field=field, family=family, verdict=VERDICT_UNIQUE,
            selected=witnesses[0], selected_key=clusters[0][0],
            witnesses=witnesses, reason="single witness",
        )
    if len(clusters) == 1:
        key, members = clusters[0]
        return FieldDecision(
            field=field, family=family, verdict=VERDICT_UNANIMOUS,
            selected=members[0], selected_key=key, witnesses=witnesses,
            reason=f"all {len(witnesses)} witnesses agree on {key}",
        )
    ordered = sorted(
        clusters, key=lambda item: (-len(item[1]), item[0])
    )
    top_size = len(ordered[0][1])
    second_size = len(ordered[1][1])
    if top_size >= 2 and top_size > second_size:
        key, members = ordered[0]
        return FieldDecision(
            field=field, family=family, verdict=VERDICT_MAJORITY,
            selected=members[0], selected_key=key, witnesses=witnesses,
            reason=f"clusters: {sizes_desc}; dissent recorded",
        )
    return FieldDecision(
        field=field, family=family, verdict=VERDICT_CONFLICT,
        selected=None, selected_key=None, witnesses=witnesses,
        reason=f"no majority; clusters: {sizes_desc}",
    )


__all__ = [
    "ArbitrationResult",
    "FieldDecision",
    "VERDICT_CONFLICT",
    "VERDICT_MAJORITY",
    "VERDICT_MULTI_ROLE",
    "VERDICT_UNIQUE",
    "VERDICT_UNANIMOUS",
    "arbitrate_anchors",
]
