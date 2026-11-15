# -*- coding: utf-8 -*-
"""Scenario understanding layer v1 (layer 2 of the three-layer soft-semantics architecture, 2026-11-04).

Projects the generic parsing layer's text output into an
intermediate representation of "candidate field + soft constraints +
evidence + confidence".  Design principles (user direction, settled
2026-11-04):

* The scenario skeleton is soft-semantic guidance, not a hard
  template — fields may be missing/renamed/reordered; every
  candidate carries evidence position and confidence, and
  "uncertain" is an allowed output;
* Only consumes existing fact sources (invoice anchors /
  arbitration / KIE, academic paper DQR ledger) — no re-derivation:
  this module is a composition layer, not a second extractor;
* The skeleton is extensible: fields outside the catalog go into
  ``dynamic_fields`` instead of being dropped;
* Prefer absence over guessing: when unsure, emit absent / low
  confidence — never guess a value;
* Pure functions / no IO / no exceptions (empty text → empty
  understanding, a legitimate honest result);
* Hard scenario classification (resolver) remains the single source
  of truth; this layer never overrides it.

Honest boundaries (v1):

* The invoice skeleton's 15-field catalog is registration data
  from the business_records field-level GT round
  (field_labels.json), not a contract — later corpora may extend
  it;
* ``present`` semantics match the GT convention: a field is present
  when either its label or value hits the invoice face; but
  "present" is not "value trustworthy" — the invoice-number
  value-shape guard (value_form) is a separate soft constraint, and
  lexical traps on negative cases (remittance-address field names
  etc.) can come out present=true with value_form=fail: the two
  coexisting is exactly the soft-semantics shape;
* The paper skeleton's title/author have macro evidence on raw
  TeX, but only first-line heuristics on PDF narrative text (low
  confidence, noted) — PDF title extraction v1 does no layout
  evidence: honest low confidence;
* The date ambiguity (m/d vs d/m) is gated by arbitration
  clustering; this layer only passes the verdict through and never
  guesses on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Optional

from provendoc.core.kie.invoice import (
    FAMILY_WESTERN,
    extract_invoice_fields,
)
from provendoc.core.scenario.academic_paper_scenario_dqr import (
    build_academic_paper_payload,
)
from provendoc.core.scenario.invoice_anchors import (
    extract_invoice_anchors,
)
from provendoc.core.scenario.invoice_arbitration import (
    VERDICT_MAJORITY,
    VERDICT_UNANIMOUS,
    VERDICT_UNIQUE,
    arbitrate_anchors,
)

UNDERSTANDING_VERSION = "scenario_understanding_v1"

# ---------------------------------------------------------------------------
# Unified intermediate representation (candidate field + soft constraints + evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence item: position + raw text, never rewritten."""

    kind: str        # label_form | value_witness | label_value_pair | macro | position
    raw: str         # as-is, trimmed, capped
    line: int        # 1-based, 0 = no line number (e.g. filename tier)
    source: str      # pattern id / engine name


@dataclass(frozen=True)
class ConstraintResult:
    """Result of one soft-constraint check. status=na means not checkable (honest unknown)."""

    kind: str        # value_form | amount_form | arithmetic | ...
    status: str      # pass | fail | na
    detail: str


@dataclass(frozen=True)
class CandidateField:
    """One candidate field: present ≠ value-trustworthy, recorded separately."""

    name: str
    role: str                          # core | extended | dynamic
    present: bool                      # label or value hits the invoice face (GT convention)
    value: Optional[str]               # as-is value, None when unsure
    value_confidence: str             # high | medium | low | none
    label_forms: tuple[str, ...] = ()   # matched label raw forms
    evidence: tuple[EvidenceItem, ...] = ()
    constraints: tuple[ConstraintResult, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (lists copied, no aliasing)."""
        return {
            "name": self.name,
            "role": self.role,
            "present": self.present,
            "value": self.value,
            "value_confidence": self.value_confidence,
            "label_forms": list(self.label_forms),
            "evidence": [
                {"kind": e.kind, "raw": e.raw, "line": e.line, "source": e.source}
                for e in self.evidence
            ],
            "constraints": [
                {"kind": c.kind, "status": c.status, "detail": c.detail}
                for c in self.constraints
            ],
            "note": self.note,
        }


@dataclass(frozen=True)
class ScenarioUnderstanding:
    """A document's soft-semantic understanding under one scenario skeleton."""

    family: str
    version: str
    fields: dict[str, CandidateField] = dc_field(default_factory=dict)
    dynamic_fields: dict[str, CandidateField] = dc_field(default_factory=dict)
    arithmetic: tuple[dict[str, Any], ...] = ()
    gaps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (fields copied, no aliasing)."""
        return {
            "family": self.family,
            "version": self.version,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "dynamic_fields": {
                k: v.to_dict() for k, v in self.dynamic_fields.items()
            },
            "arithmetic": [dict(a) for a in self.arithmetic],
            "gaps": list(self.gaps),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# invoice skeleton (catalog = business_records field_labels registration data)
# ---------------------------------------------------------------------------

# (field, role) — core = fields required for settlement, extended = common workflow fields.
INVOICE_FIELD_CATALOG: tuple[tuple[str, str], ...] = (
    ("invoice_number", "core"),
    ("invoice_date", "core"),
    ("total_amount", "core"),
    ("subtotal_or_gross", "core"),
    ("tax_amount", "core"),
    ("tax_id", "core"),
    ("bill_to", "core"),
    ("sold_to", "core"),
    ("ship_to", "extended"),
    ("remit_to", "extended"),
    ("payment_request", "extended"),
    ("po_number", "extended"),
    ("contract_number", "extended"),
    ("estimate_number", "extended"),
    ("order_number", "extended"),
)

# Label-form detection: word boundaries + context qualifiers, to keep prose out.
_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "invoice_number": re.compile(
        r"(?i)\b(?:invoice|inv)\.?\s*(?:#|no\.?|num(?:ber)?)(?![A-Za-z])"
        r"|\binvoice\s*[:：]"
        r"|\b发票号码\b|\b发票代码\b"
    ),
    "invoice_date": re.compile(
        r"(?i)\b(?:invoice|issue|billing)\s+date\b"
        r"|\bdate\s+of\s+invoice\b|\b开票日期\b"
    ),
    "bill_to": re.compile(
        r"(?i)\bbill(?:ed)?\s+to\b|\b购买方\b|\b客户名称\b"
    ),
    "sold_to": re.compile(r"(?i)\bsold\s+to\b|\b销售方\b"),
    "ship_to": re.compile(r"(?i)\bship(?:ped|ping)?\s+to\b|\b收货地址\b"),
    "remit_to": re.compile(
        r"(?i)\bremit(?:tance)?\s*to\b|\b汇款至\b|\b收款方\b"
    ),
    "tax_id": re.compile(
        r"(?i)\btax\s*(?:id(?:ent(?:ification)?)?|reg(?:istration)?)"
        r"\s*(?:#|no\.?|num(?:ber)?)?\b"
        r"|\bv\.?a\.?t\.?\s*(?:#|no|reg|num)\b|\be\.?i\.?n\.?\b"
        r"|\bfederal\s+id\b|\b纳税人识别号\b|\b税号\b"
    ),
    "subtotal_or_gross": re.compile(
        r"(?i)\bsub\s*-?\s*total\b|\bgross\s+(?:amount|total)\b|\b金额合计\b"
    ),
    "tax_amount": re.compile(
        r"(?i)\b(?:sales\s+)?tax\b(?:\s*(?:#|amount|total)\b)?|\bv\.?a\.?t\.?\b"
        r"|\b税额\b"
    ),
    "total_amount": re.compile(
        r"(?i)\b(?:grand\s+)?total\b|\bamount\s+due\b|\bbalance\s+due\b"
        r"|\bnet\s+(?:amount\s+)?due\b|\b价税合计\b|\b合计\b"
    ),
    "payment_request": re.compile(
        r"(?i)\bpayment\s+terms?\b|\bnet\s+due\b|\bdue\s+(?:upon|within|in)\b"
        r"|\bplease\s+(?:pay|remit)\b|\b付款条件\b"
    ),
    "po_number": re.compile(
        # Must carry a number marker: bare P.O. would match P.O. Box
        # addresses (2026-11-04 confirmed).  The tail constraint is
        # (?![A-Za-z]) instead of \b (same-family prefix trap, found
        # 2026-11-05 while validating register #175): \b never holds
        # after the non-word char #, so "PO # 12345" / "PO#: 12345"
        # would never match; aligned with the invoice_number
        # precedent.
        r"(?i)\bp\.?\s*o\.?\s*(?:#|no\.?|num(?:ber)?)(?![A-Za-z])"
        r"|\bpurchase\s+order\b|\b订单号\b"
    ),
    "contract_number": re.compile(
        r"(?i)\bcontract\s*(?:#|no\.?|num(?:ber)?)?(?![A-Za-z])|\b合同号(?:码)?\b"
    ),
    "estimate_number": re.compile(
        r"(?i)\bestimate\s*(?:#|no\.?|num(?:ber)?)?(?![A-Za-z])|\bquotation\s*(?:#|no\.?)?\b"
    ),
    "order_number": re.compile(
        r"(?i)\border\s*(?:#|no\.?|num(?:ber)?)(?![A-Za-z])|\b订单号\b"
    ),
}

# Fields whose label is followed by a value on the same line: after
# a label hit, the value must match a value shape (digit core).
# Negative invoice-number trap (2026-11-05 confirmed): values
# starting with a hyphen glued to digits (e.g. "-120-0054056"
# left behind by OCR column tearing) must be captured verbatim —
# without a leading hyphen in the first char class, the regex
# silently skips "-" and truncates the value to "120-0054056"
# (silent value mutation is unacceptable); the -\d... capture
# branch keeps the hyphen as-is, with no numeric interpretation.
_PAIR_VALUE = re.compile(
    r"(?i)[\s|]*(?:[#:]|\.[\s|]*)?[\s|]*"
    r"(?=[A-Za-z0-9\-/]*\d)(-\d[A-Za-z0-9\-/]{0,31}|[A-Za-z0-9][A-Za-z0-9\-/]{1,31})"
)
_PAIR_FIELDS = {
    "invoice_number", "po_number", "contract_number",
    "estimate_number", "order_number", "tax_id",
}

# Short-label-line guard: bare word/amount/date/remit-to labels
# easily false-hit clause prose ("advertising contracts...",
# "within ninety days of the invoice date...").  Real label lines
# are short; prose paragraphs are long — hits on lines longer than
# _MAX_LABEL_LINE do not count.
_SHORT_LINE_LABEL_FIELDS = {
    "po_number", "contract_number", "estimate_number", "order_number",
    "total_amount", "subtotal_or_gross", "tax_amount", "payment_request",
    "invoice_date", "bill_to", "sold_to", "ship_to", "remit_to",
}
_MAX_LABEL_LINE = 70

# Amount-pairing fields: label line → .dd amount shape on the same line or in the following window.
_AMOUNT_PAIR_FIELDS = {"total_amount", "subtotal_or_gross", "tax_amount"}

# Split-line pairing (PDF reading order separates label and value):
# bare label line → value-shape gate on following non-empty lines.
# Same discipline as KIE *_nextline, but covers bare Invoice/Date
# label lines (00136a27 instance).
_BARE_LABEL_LINE = {
    "invoice_number": re.compile(r"(?i)^[ \t]*invoice(?:\s*(?:#|no\.?|num(?:ber)?))?\s*$"),
    "invoice_date": re.compile(r"(?i)^[ \t]*date\s*$"),
}
# Line-leading bare Date label (with same-line value): "Date: August
# 24, 2001" (00f290a2).  ^date excludes Due/Ship/Order Date; the
# value shape is full-matched to guard against half-line noise.
_BARE_DATE_INLINE = re.compile(r"(?i)^[ \t]*date\b\s*[:#]?\s*(.*)$")
_INVNO_VALUE_FORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/]{1,31}$")
_DATE_VALUE_FORM = re.compile(
    r"(?i)^(?:[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{2,4}"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|\d{4}[/.-]\d{1,2}[/.-]\d{1,2})$"
)

# Anchor field-name map (western-side field names).
_ANCHOR_FIELD_MAP = {
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "tax_id": "tax_id",
    "subtotal_or_gross": "subtotal",
    "tax_amount": "tax",
    "total_amount": "total_due",
}

# Invoice-number value-shape guard (2026-11-04 trap conclusion): a value only counts with a digit core.
_VALUE_FORM_CORE = re.compile(r"\d")
_AMOUNT_FORM = re.compile(r"[-+]?\s*[\d,]+\s*\.\s*\d{2}\b")

_MAX_RAW = 80


def _cap(raw: str) -> str:
    raw = raw.strip()
    return raw if len(raw) <= _MAX_RAW else raw[:_MAX_RAW] + "…"


def _label_hits(text: str, fname: str) -> list[tuple[int, str]]:
    """Find label-form hits line by line: (1-based line, matched text).

    Weak-field labels (number/amount/date/remit-to) false-hit clause
    prose ("advertising contracts on the basis of..."); real label
    lines are short, prose paragraphs long — the line-length guard
    (≤_MAX_LABEL_LINE) keeps prose out (002f9b82 confirmed).
    """
    pattern = _LABEL_PATTERNS[fname]
    short_line_only = fname in _SHORT_LINE_LABEL_FIELDS
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = pattern.search(line)
        if m and short_line_only and len(line.strip()) > _MAX_LABEL_LINE:
            continue
        if m:
            hits.append((i, m.group(0)))
    return hits


def _is_labelish(line: str) -> bool:
    """Whether the line itself is another known label line (used to skip label blocks during window scans).

    TEGNA vertical layout: a label column (Invoice # / Invoice Date /
    Invoice Month...) followed by a value column — window pairing must
    skip the label lines in between, or the whole 3-line window is
    spent on labels (002f9b82 confirmed).
    """
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > _MAX_LABEL_LINE:
        return False
    if any(bare.match(line) for bare in _BARE_LABEL_LINE.values()):
        return True
    return any(
        pat.search(stripped) for pat in _LABEL_PATTERNS.values()
    )


def _pair_value_on_line(line: str, match: re.Match) -> Optional[str]:
    """Pair a value on the same line after the label hit (only value shapes count)."""
    m = _PAIR_VALUE.search(line, pos=match.end())
    return m.group(1) if m else None


def _kie_value(raw: Any) -> Optional[str]:
    """KIE field value → as-is string (dict sub-values joined, never rewritten)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, dict):
        parts = []
        for v in raw.values():
            if v is None:
                continue
            if isinstance(v, (int, float)):
                parts.append(str(v))
            elif isinstance(v, str) and v.strip():
                parts.append(v.strip())
        return "; ".join(parts) if parts else None
    return None


def _verdict_to_confidence(verdict: str) -> str:
    return {"unanimous": "high", "majority": "medium", "unique": "medium"}.get(
        verdict, "low"
    )


def understand_invoice(
    text: str, filename: Optional[str] = None
) -> ScenarioUnderstanding:
    """Invoice skeleton: 15-field catalog + value-shape guards + arithmetic evidence.

    Pure function; empty text → an all-absent empty understanding (a
    legitimate result, no exception).  ``filename`` is only passed
    through to KIE (filename-evidence tier for Chinese-family
    numbers).
    """
    text = text or ""
    lines = text.splitlines()

    kie = extract_invoice_fields(text, filename)
    family = kie["evidence"]["family"]
    anchors = extract_invoice_anchors(text)
    arbitration = arbitrate_anchors(anchors)

    fields: dict[str, CandidateField] = {}
    gaps: list[str] = []
    notes = [f"template_family={family}"]

    # --- core value source: KIE (anchors + arbitration + family template) ---
    kie_map: dict[str, Any] = {}
    kf = kie["fields"]
    if family == FAMILY_WESTERN:
        totals = (kf.get("totals") or {}).get("value") or {}
        grand = (kf.get("grand_total") or {}).get("value") or {}
        kie_map = {
            "invoice_number": kf.get("invoice_number"),
            "invoice_date": kf.get("issue_date"),
            "bill_to": kf.get("buyer"),
            "sold_to": kf.get("seller"),
            "subtotal_or_gross": None if not totals.get("subtotal") else {
                "value": totals.get("subtotal"),
                "confidence": (kf.get("totals") or {}).get("confidence"),
            },
            "tax_amount": None if not totals.get("tax") else {
                "value": totals.get("tax"),
                "confidence": (kf.get("totals") or {}).get("confidence"),
            },
            "total_amount": None if not grand.get("total_due") else {
                "value": grand.get("total_due"),
                "confidence": (kf.get("grand_total") or {}).get("confidence"),
            },
        }
    else:  # chinese family
        grand = (kf.get("grand_total") or {}).get("value") or {}
        totals = (kf.get("totals") or {}).get("value") or {}
        small = grand.get("小写") if isinstance(grand, dict) else None
        kie_map = {
            "invoice_number": kf.get("invoice_number"),
            "invoice_date": kf.get("issue_date"),
            "bill_to": kf.get("buyer"),
            "sold_to": kf.get("seller"),
            "total_amount": None if small is None else {
                "value": small,
                "confidence": (kf.get("grand_total") or {}).get("confidence"),
            },
            "subtotal_or_gross": None if totals.get("金额合计") is None else {
                "value": totals.get("金额合计"),
                "confidence": (kf.get("totals") or {}).get("confidence"),
            },
            "tax_amount": None if totals.get("税额合计") is None else {
                "value": totals.get("税额合计"),
                "confidence": (kf.get("totals") or {}).get("confidence"),
            },
        }

    # --- per-field assembly -------------------------------------------------
    for fname, role in INVOICE_FIELD_CATALOG:
        evidence: list[EvidenceItem] = []
        label_forms: list[str] = []
        constraints: list[ConstraintResult] = []
        value: Optional[str] = None
        value_confidence = "none"
        note = ""

        # 1) label-form hits (line by line)
        hits = _label_hits(text, fname)
        for ln, matched in hits:
            label_forms.append(_cap(matched))
            evidence.append(EvidenceItem(
                kind="label_form", raw=_cap(matched), line=ln,
                source="label_form",
            ))

        # 2) same-line paired value (_PAIR_FIELDS)
        if fname in _PAIR_FIELDS:
            for ln, _ in hits:
                line = lines[ln - 1] if ln - 1 < len(lines) else ""
                m = _LABEL_PATTERNS[fname].search(line)
                v = _pair_value_on_line(line, m) if m else None
                if v and value is None:
                    value = v
                    value_confidence = "low"   # single-label pairing, lowest tier
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(v), line=ln,
                        source="label_value_pair",
                    ))

        # 2b) split-line pairing: bare label line → value shape within
        # the next ≤3 non-empty lines (two-column reading order is
        # interleaved, the value need not be adjacent; 00136a27:
        # Invoice→17685-2 two lines apart)
        bare_re = _BARE_LABEL_LINE.get(fname)
        if bare_re is not None and value is None:
            form_re = (
                _DATE_VALUE_FORM if fname == "invoice_date"
                else _INVNO_VALUE_FORM
            )
            for i, line in enumerate(lines):
                if not bare_re.match(line):
                    continue
                seen = 0
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if not nxt:
                        continue
                    if _is_labelish(lines[j]):
                        continue   # vertical label block: label lines in between do not consume the window
                    seen += 1
                    if seen > 3:
                        break
                    m2 = form_re.match(nxt)
                    if (
                        m2
                        and (fname == "invoice_date"
                             or _VALUE_FORM_CORE.search(nxt))
                    ):
                        value = m2.group(0)
                        value_confidence = "low"
                        note = "标签行与值分行 (阅读顺序错位), 窗口内配对"
                        evidence.append(EvidenceItem(
                            kind="label_value_pair", raw=_cap(nxt),
                            line=j + 1, source="nextline_pair",
                        ))
                        break
                    if value is not None:
                        break
                if value is not None:
                    break

        # 2c) invoice_date soft witness: shape evidence when no
        #   "invoice date" label exists
        #   (i)  line-leading bare Date: the same line is the date
        #        (00f290a2: "Date: August 24, 2001")
        #   (ii) bare Date label line, value within ≤3 non-empty lines
        #        BEFORE it (00136a27 reverse reading order:
        #        "10/31/2020" / "Date" / "Invoice")
        #   (iii) invoice-number label line carries a date, or the
        #        adjacent previous non-empty line is a pure date
        #        (0133788f: "Invoice # 843153 December 20, 2000";
        #         00405d76: "September 3, 1999" + "Invoice #99-D08025A")
        #   All low confidence + evidence recorded; never competes
        #   with the main paths outside 2b/2c.
        if fname == "invoice_date" and value is None:
            date_hit: Optional[tuple[int, str]] = None
            date_src = ""
            for i, line in enumerate(lines):
                m = _BARE_DATE_INLINE.match(line)
                if m:
                    rest = m.group(1).strip()
                    if _DATE_VALUE_FORM.match(rest):
                        date_hit = (i + 1, rest)
                        date_src = "bare_date_inline"
                        break
            if date_hit is None:
                for i, line in enumerate(lines):
                    if _BARE_LABEL_LINE["invoice_date"].match(line):
                        seen = 0
                        for j in range(i - 1, -1, -1):
                            prev = lines[j].strip()
                            if not prev:
                                continue
                            seen += 1
                            if seen > 3:
                                break
                            if _DATE_VALUE_FORM.match(prev):
                                date_hit = (j + 1, prev)
                                date_src = "bare_date_prevline"
                                break
                        if date_hit is not None:
                            break
            if date_hit is None:
                invno_re = _LABEL_PATTERNS["invoice_number"]
                invno_bare = _BARE_LABEL_LINE["invoice_number"]
                for i, line in enumerate(lines):
                    # Invoice-number label line: full label or bare
                    # line ("Invoice #" with a trailing # has no \b; the
                    # bare form closes the gap)
                    if not (invno_re.search(line) or invno_bare.match(line)):
                        continue
                    m2 = _DATE_VALUE_FORM.search(line)
                    if m2:
                        date_hit = (i + 1, m2.group(0))
                        date_src = "invno_line_date"
                        break
                    prev = lines[i - 1].strip() if i > 0 else ""
                    if _DATE_VALUE_FORM.match(prev):
                        date_hit = (i, prev)
                        date_src = "invno_prevline_date"
                        break
                    # Invoice-number vertical label block: skip label lines in between, find a date within ≤5 candidate lines
                    seen = 0
                    for j in range(i + 1, len(lines)):
                        nxt = lines[j].strip()
                        if not nxt:
                            continue
                        if _is_labelish(lines[j]):
                            continue
                        seen += 1
                        if seen > 5:
                            break
                        m3 = _DATE_VALUE_FORM.match(nxt)
                        if m3:
                            date_hit = (j + 1, m3.group(0))
                            date_src = "invno_block_date"
                            break
                    if date_hit is not None:
                        break
            if date_hit is not None:
                value = date_hit[1]
                value_confidence = "low"
                note = "软 witness: 无 invoice date 标签, 邻近日期形态"
                evidence.append(EvidenceItem(
                    kind="value_witness", raw=_cap(date_hit[1]),
                    line=date_hit[0], source=date_src,
                ))

        # 2d) amount pairing: label line → first .dd amount after the
        #   label on the same line or in the window (TEGNA vertical
        #   layout: "$1,820.00" on the line below "Gross Total").
        #   Fallback only — anchor testimony / arbitration-selected
        #   values still take precedence.
        if fname in _AMOUNT_PAIR_FIELDS and value is None:
            for ln, _ in hits:
                line = lines[ln - 1] if ln - 1 < len(lines) else ""
                m = _LABEL_PATTERNS[fname].search(line)
                if m is None:
                    continue
                am = _AMOUNT_FORM.search(line, pos=m.end())
                if am is not None:
                    value = am.group(0).strip()
                    value_confidence = "low"
                    note = "金额标签同行配对"
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(value),
                        line=ln, source="amount_sameline",
                    ))
                    break
            if value is None:
                for ln, _ in hits:
                    seen = 0
                    for j in range(ln, len(lines)):
                        nxt = lines[j].strip()
                        if not nxt:
                            continue
                        if _is_labelish(lines[j]):
                            continue
                        seen += 1
                        if seen > 3:
                            break
                        am = _AMOUNT_FORM.search(nxt)
                        if am is not None:
                            value = am.group(0).strip()
                            value_confidence = "low"
                            note = "金额标签分行窗口配对"
                            evidence.append(EvidenceItem(
                                kind="label_value_pair", raw=_cap(value),
                                line=j + 1, source="amount_nextline",
                            ))
                            break
                    if value is not None:
                        break

        # 3) anchor value testimony (all witnesses only register evidence; used as a low-confidence value only if nothing was selected)
        anchor_name = _ANCHOR_FIELD_MAP.get(fname)
        if anchor_name:
            for hit in anchors.by_field(anchor_name):
                evidence.append(EvidenceItem(
                    kind="value_witness", raw=_cap(hit.value),
                    line=hit.line, source=f"anchor:{hit.pattern}",
                ))
            if value is None and anchors.by_field(anchor_name):
                value = anchors.by_field(anchor_name)[0].value
                value_confidence = "low"
                note = "anchor witness, 未经仲裁"

        # 4) arbitration-selected value (western; as-is string + verdict confidence, never rewritten through float)
        if (
            family == FAMILY_WESTERN
            and anchor_name
            and value_confidence in ("none", "low")
        ):
            decision = arbitration.get(anchor_name, FAMILY_WESTERN)
            if (
                decision is not None
                and decision.selected is not None
                and decision.verdict in (
                    VERDICT_UNIQUE, VERDICT_UNANIMOUS, VERDICT_MAJORITY,
                )
            ):
                value = decision.selected.value
                value_confidence = _verdict_to_confidence(decision.verdict)
                evidence.append(EvidenceItem(
                    kind="value_witness", raw=_cap(value),
                    line=decision.selected.line,
                    source=f"arbitration:{decision.selected.pattern}",
                ))
                note = f"{decision.verdict}: {decision.reason}"

        # 5) KIE fallback / Chinese-family main value (used only when still no value; amounts go through float, shape downgraded to na)
        kfield = kie_map.get(fname)
        if (
            isinstance(kfield, dict)
            and kfield.get("value") is not None
            and (family != FAMILY_WESTERN or value is None)
        ):
            kvalue = _kie_value(kfield.get("value"))
            if kvalue is not None:
                value = kvalue
                value_confidence = kfield.get("confidence") or "medium"
                evidence.append(EvidenceItem(
                    kind="value_witness", raw=_cap(kvalue), line=0,
                    source=f"kie:{family}",
                ))
                if kfield.get("note"):
                    note = kfield["note"]

        # 5b) western tax_id hidden inside the KIE seller value dict (a single-witness tax id is attributed to the issuer)
        if (
            fname == "tax_id"
            and family == FAMILY_WESTERN
            and value is None
        ):
            seller_val = (kie["fields"].get("seller") or {}).get("value")
            if isinstance(seller_val, dict) and seller_val.get("tax_id"):
                value = str(seller_val["tax_id"])
                value_confidence = "low"
                evidence.append(EvidenceItem(
                    kind="value_witness", raw=_cap(value), line=0,
                    source="kie:seller_tax_id",
                ))
                note = "单证人税号归出票方 (KIE v1 假设)"

        present = bool(label_forms) or value is not None

        # 5) soft constraints
        if fname == "invoice_number":
            ok = bool(value and _VALUE_FORM_CORE.search(value))
            constraints.append(ConstraintResult(
                kind="value_form", status="pass" if ok else "fail",
                detail=(
                    "票号值含数字核心" if ok
                    else "无值形态票号 (仅标签命中或值无数字核心) — "
                         "negative 陷阱形状, 不构成身份证据"
                ),
            ))
        if fname in ("total_amount", "subtotal_or_gross", "tax_amount"):
            ok = bool(value and _AMOUNT_FORM.search(value))
            constraints.append(ConstraintResult(
                kind="amount_form", status="pass" if ok else "na",
                detail="金额尾部形态 (.dd)" if ok else "无标准金额形态值",
            ))

        if not present and role == "core":
            gaps.append(f"core 缺失: {fname}")

        fields[fname] = CandidateField(
            name=fname, role=role, present=present, value=value,
            value_confidence=value_confidence if value else "none",
            label_forms=tuple(label_forms),
            evidence=tuple(evidence),
            constraints=tuple(constraints),
            note=note,
        )

    # --- arithmetic evidence (KIE cross-validation, passed through as-is) ---
    arithmetic = tuple(
        dict(check=chk.get("check"), expected=chk.get("expected"),
             actual=chk.get("actual"), status="pass" if chk.get("pass") else "fail")
        for chk in (kie.get("validation") or [])
    )
    if not arithmetic:
        arithmetic = ({"check": "no_checkable_amounts",
                       "status": "na",
                       "detail": "可验算的金额组合不足"},)

    return ScenarioUnderstanding(
        family="invoice",
        version=UNDERSTANDING_VERSION,
        fields=fields,
        dynamic_fields={},
        arithmetic=arithmetic,
        gaps=tuple(gaps),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# academic_paper skeleton
# ---------------------------------------------------------------------------

_TEX_MACRO_TITLE = re.compile(r"\\title\s*\{")
_TEX_MACRO_AUTHOR = re.compile(r"\\author\s*\{")
_TEX_MACRO_DATE = re.compile(r"\\date\s*\{")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[\w.\-()/]+")
_ARXIV_ID_RE = re.compile(
    r"(?i)\barxiv[:\s]*/?\s*([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})\b"
)

_ABSTRACT_SNIPPET_CAP = 2000

# raw-TeX abstract environment location (DQR v1.8 does not cover it; supplied by the understanding layer, to be lifted into DQR).
_TEX_ABSTRACT_BEGIN_RE = re.compile(r"(?im)^[ \t]*\\begin\{abstract\}")
_TEX_ABSTRACT_END_RE = re.compile(r"(?im)^[ \t]*\\end\{abstract\}")


def _macro_body(text: str, macro: re.Pattern[str]) -> Optional[tuple[str, int]]:
    """Raw TeX macro body extraction (balanced braces, may span lines).

    Returns (body, 1-based start line).  Nested braces are scanned by
    depth; unclosed → None (honest failure — never truncate a half
    body and call it a title).
    """
    m = macro.search(text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = text[start:i].strip()
                if not body:
                    return None
                line = text.count("\n", 0, m.start()) + 1
                return body, line
        i += 1
    return None


def _line_text(lines: list[str], ln: Optional[int]) -> str:
    if ln is None or ln < 1 or ln > len(lines):
        return ""
    return lines[ln - 1].strip()


def _bounded_snippet(
    lines: list[str], start: int, stop_lines: set[int], cap: int
) -> str:
    """Raw-text snippet from the start line until a blank line / stop line / cap."""
    out: list[str] = []
    total = 0
    for ln in range(start, len(lines) + 1):
        if ln in stop_lines:
            break
        piece = lines[ln - 1].strip()
        if not piece and out:
            break
        if not piece and not out:
            continue
        out.append(piece)
        total += len(piece)
        if total >= cap:
            break
    return "\n".join(out)


def _first_content_line(lines: list[str]) -> Optional[int]:
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if s and not s.startswith(("%", "\\")):
            return i
    return None


def understand_academic_paper(text: str) -> ScenarioUnderstanding:
    """Paper skeleton: title/authors/abstract/keywords/references + dynamic fields.

    Pure function; empty text → empty understanding.  title/author
    have macro evidence on raw TeX (balanced braces, unclosed →
    None); PDF narrative text goes through the first-line heuristic
    with explicit low confidence.
    """
    text = text or ""
    lines = text.splitlines()
    payload = build_academic_paper_payload(text)

    fields: dict[str, CandidateField] = {}
    dynamic: dict[str, CandidateField] = {}
    gaps: list[str] = []

    def _mk(name: str, *, present: bool, value: Optional[str],
            conf: str, evidence: tuple[EvidenceItem, ...] = (),
            note: str = "", role: str = "core") -> CandidateField:
        return CandidateField(
            name=name, role=role, present=present, value=value,
            value_confidence=conf if value else "none",
            evidence=evidence, note=note,
        )

    # --- title -------------------------------------------------------------
    title_macro = _macro_body(text, _TEX_MACRO_TITLE)
    if title_macro is not None:
        body, ln = title_macro
        fields["title"] = _mk(
            "title", present=True, value=_cap(body), conf="high",
            evidence=(EvidenceItem("macro", _cap(body), ln, "tex:title"),),
            note="TeX \\title 宏证据",
        )
    else:
        first = _first_content_line(lines)
        if first is not None:
            fields["title"] = _mk(
                "title", present=True, value=_cap(_line_text(lines, first)),
                conf="low",
                evidence=(EvidenceItem(
                    "position", _cap(_line_text(lines, first)), first,
                    "first_content_line"),),
                note="非 TeX 源, 首行启发式 (低置信, 版面证据缺)",
            )
        else:
            fields["title"] = _mk("title", present=False, value=None, conf="none")
            gaps.append("title 未定位")

    # --- authors -----------------------------------------------------------
    author_macro = _macro_body(text, _TEX_MACRO_AUTHOR)
    if author_macro is not None:
        body, ln = author_macro
        fields["authors"] = _mk(
            "authors", present=True, value=_cap(body), conf="high",
            evidence=(EvidenceItem("macro", _cap(body), ln, "tex:author"),),
            note="TeX \\author 宏证据",
        )
    else:
        fields["authors"] = _mk(
            "authors", present=False, value=None, conf="none",
            note="v1 不做 PDF 作者区版面证据 (诚实缺口)",
        )

    # --- abstract ----------------------------------------------------------
    abstract_line = payload.get("abstract_line")
    abstract_source = "dqr:abstract_line"
    abstract_end: Optional[int] = None
    if abstract_line is None:
        # DQR v1.8 only recognizes a heading-style Abstract line; the
        # raw-TeX \begin{abstract} environment is located here as a
        # supplement (evidence source kept explicit, to be lifted
        # into DQR).
        for i, line in enumerate(lines, start=1):
            if _TEX_ABSTRACT_BEGIN_RE.match(line):
                abstract_line = i
                abstract_source = "understanding:tex_begin_abstract"
                for j in range(i + 1, len(lines) + 1):
                    if _TEX_ABSTRACT_END_RE.match(lines[j - 1]):
                        abstract_end = j
                        break
                break
    if abstract_line is not None:
        stop: set[int] = set()
        kw_line = payload.get("keywords_line")
        if kw_line is not None:
            stop.add(kw_line)
        if abstract_end is not None:
            stop.add(abstract_end)
        snippet = _bounded_snippet(
            lines, abstract_line + 1, stop, _ABSTRACT_SNIPPET_CAP
        )
        fields["abstract"] = _mk(
            "abstract", present=True, value=snippet or None, conf="medium",
            evidence=(EvidenceItem(
                "position", _cap(_line_text(lines, abstract_line)),
                abstract_line, abstract_source),),
            note="位置锚定 + 有界原文片段 (medium: 边界靠启发式)"
                 + ("; raw-TeX 环境定位待上移 DQR"
                    if abstract_source.startswith("understanding") else ""),
        )
    else:
        fields["abstract"] = _mk("abstract", present=False, value=None, conf="none")
        gaps.append("abstract 未定位")

    # --- keywords -----------------------------------------------------------
    kw_line = payload.get("keywords_line")
    if kw_line is not None:
        fields["keywords"] = _mk(
            "keywords", present=True, value=_cap(_line_text(lines, kw_line)),
            conf="medium",
            evidence=(EvidenceItem(
                "position", _cap(_line_text(lines, kw_line)), kw_line,
                "dqr:keywords_line"),),
        )
    else:
        fields["keywords"] = _mk("keywords", present=False, value=None, conf="none")
        gaps.append("keywords 未定位")

    # --- references ----------------------------------------------------------
    refs_line = payload.get("references_line")
    n_refs = int((payload.get("stats") or {}).get("references_entries") or 0)
    if refs_line is not None:
        fields["references"] = _mk(
            "references", present=True,
            value=str(n_refs) if n_refs else None, conf="medium",
            evidence=(EvidenceItem(
                "position", _cap(_line_text(lines, refs_line)), refs_line,
                "dqr:references_line"),),
            note=f"region={refs_line}..{payload.get('references_end_line') or 'EOF'}, "
                 f"entries={n_refs} (form-derived)",
        )
    else:
        fields["references"] = _mk(
            "references", present=False, value=None, conf="none")
        gaps.append("references 未定位")

    # --- dynamic fields (outside the catalog, extensible) --------------------
    doi_m = _DOI_RE.search(text)
    if doi_m:
        arxiv_ln = text.count("\n", 0, doi_m.start()) + 1
        dynamic["doi"] = _mk(
            "doi", present=True, value=doi_m.group(0), conf="medium",
            evidence=(EvidenceItem(
                "value_witness", doi_m.group(0), arxiv_ln, "regex:doi"),),
            role="dynamic",
        )
    ax_m = _ARXIV_ID_RE.search(text)
    if ax_m:
        ax_ln = text.count("\n", 0, ax_m.start()) + 1
        dynamic["arxiv_id"] = _mk(
            "arxiv_id", present=True, value=ax_m.group(1), conf="medium",
            evidence=(EvidenceItem(
                "value_witness", ax_m.group(0), ax_ln, "regex:arxiv_id"),),
            role="dynamic",
        )
    stats = payload.get("stats") or {}
    if stats.get("section_headings"):
        dynamic["section_count"] = _mk(
            "section_count", present=True,
            value=str(stats["section_headings"]), conf="medium",
            evidence=(EvidenceItem(
                "position", f"{stats['section_headings']} section headings",
                0, "dqr:stats"),),
            role="dynamic",
        )

    return ScenarioUnderstanding(
        family="academic_paper",
        version=UNDERSTANDING_VERSION,
        fields=fields,
        dynamic_fields=dynamic,
        arithmetic=(),
        gaps=tuple(gaps),
        notes=(
            "paper 骨架是结构事实组合 (DQR ledger), title/author 的宏证据"
            "仅 raw TeX 可用; PDF narrative 的标题抽取是低置信启发式",
        ),
    )


# ---------------------------------------------------------------------------
# order skeleton (order/contract documents; 2026-11-05, driven by the root-cause of 6 negative false positives)
# ---------------------------------------------------------------------------

# (field, role) — order skeleton v1: common fields of media/broadcast
# orders (Purchase/Sales/Network Order, Contract, Order Worksheet).
# Soft catalog, extensible.
ORDER_FIELD_CATALOG: tuple[tuple[str, str], ...] = (
    ("order_number", "core"),
    ("order_date", "core"),
    ("total_amount", "core"),
    ("advertiser", "core"),
    ("agency", "core"),
    ("station", "extended"),
    ("product", "extended"),
    ("flight_dates", "extended"),
    ("vendor", "extended"),
    ("ship_to", "extended"),
    ("buyer", "extended"),
    ("contract_number", "extended"),
)

# All order labels are line-start anchored (^ is the line start when
# searching line by line): order worksheets are wide tables with
# noisy in-line content — line-start anchoring is more
# contamination-resistant than the invoice-side whole-text search.
_ORDER_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "order_number": re.compile(
        r"(?i)^[ \t]*(?:ORDER\s*(?:#|NO\.?|NUM(?:BER)?\.?)"
        r"|P\.?\s*O\.?\s*(?:#|NO\.?|NUM(?:BER)?\.?))"
    ),
    "order_date": re.compile(
        r"(?i)^[ \t]*(?:ORDER\s+DATE\b|DATE\b\s*[:#])"
    ),
    "total_amount": re.compile(
        r"(?i)^[ \t]*(?:GRAND[ \t]+)?TOTAL\b|^[ \t]*AMOUNT\b"
        r"|^[ \t]*AMT\b"
    ),
    "advertiser": re.compile(
        r"(?i)^[ \t]*(?:ADVERTISER\b|ADV\.?[ \t]+NAME\b)"
    ),
    "agency": re.compile(r"(?i)^[ \t]*AGENCY\b|^[ \t]*AGY\.?[ \t]+NAME\b"),
    "station": re.compile(r"(?i)^[ \t]*STATION\b"),
    "product": re.compile(r"(?i)^[ \t]*(?:PRODUCT\b|PRDCT\b)"),
    "flight_dates": re.compile(r"(?i)^[ \t]*FLIGHT\b(?:\s+DATES?)?"),
    "vendor": re.compile(r"(?i)^[ \t]*VENDOR\b"),
    "ship_to": re.compile(r"(?i)^[ \t]*SHIP(?:PED|PING)?[ \t]+TO\b"),
    "buyer": re.compile(r"(?i)^[ \t]*BUYER(?:[ \t]+NAME)?\b"),
    "contract_number": re.compile(
        r"(?i)^[ \t]*CONTRACT\s*(?:#|NO\.?|NUM(?:BER)?\.?)?\s*[:#]?"
    ),
}

# Text-value fields: value taken on the same line after the label
# (up to a double-space/tab column boundary — orders are wide
# tables, the same line often carries the next column's label, so
# the whole line must not be swallowed).
_ORDER_TEXT_VALUE_CUT = re.compile(r"\s{2,}|\t")
_ORDER_TEXT_VALUE_MAX = 60

# Number/amount/date fields reuse the invoice-side shape guards (same soft-semantics discipline).
_ORDER_NUMBERISH_FIELDS = {"order_number", "contract_number"}


def _order_text_value(line: str, m: re.Match) -> Optional[str]:
    """Same-line value after a text-field label: separators stripped, cut at the column boundary, None if empty."""
    rest = line[m.end():].lstrip(" \t:#|-")
    if not rest:
        return None
    chunk = _ORDER_TEXT_VALUE_CUT.split(rest, maxsplit=1)[0].strip(" \t|#-")
    if not chunk or len(chunk) > _ORDER_TEXT_VALUE_MAX:
        return None
    return chunk


def understand_order(
    text: str, filename: Optional[str] = None
) -> ScenarioUnderstanding:
    """Order skeleton: order number/date/total/advertiser/agency etc., line-start-anchored labels + value-shape guards.

    Pure function; empty text → an all-absent empty understanding.
    v1 does label/pairing evidence only — no order line-item table
    parsing (honest boundary, noted).
    """
    text = text or ""
    lines = text.splitlines()

    fields: dict[str, CandidateField] = {}
    gaps: list[str] = []

    for fname, role in ORDER_FIELD_CATALOG:
        pattern = _ORDER_LABEL_PATTERNS[fname]
        evidence: list[EvidenceItem] = []
        label_forms: list[str] = []
        value: Optional[str] = None
        value_confidence = "none"
        note = ""

        hits: list[tuple[int, re.Match]] = []
        for i, line in enumerate(lines, start=1):
            m = pattern.search(line)
            if m is None:
                continue
            hits.append((i, m))
            label_forms.append(_cap(m.group(0)))
            evidence.append(EvidenceItem(
                kind="label_form", raw=_cap(m.group(0)), line=i,
                source="label_form",
            ))

        # --- value pairing (number / date / amount / text, each with its own shape gate) ---
        if fname in _ORDER_NUMBERISH_FIELDS:
            for ln, m in hits:
                v = _pair_value_on_line(lines[ln - 1], m)
                if v and _VALUE_FORM_CORE.search(v):
                    value = v
                    value_confidence = "low"
                    note = "编号标签同行配对 (低置信, 单证人)"
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(v), line=ln,
                        source="label_value_pair",
                    ))
                    break
        elif fname == "order_date":
            for ln, m in hits:
                dm = _DATE_VALUE_FORM.search(lines[ln - 1], pos=m.end())
                if dm:
                    value = dm.group(0)
                    value_confidence = "low"
                    note = "日期标签同行配对"
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(value), line=ln,
                        source="date_sameline",
                    ))
                    break
            if value is None:
                for ln, _ in hits:
                    seen = 0
                    for j in range(ln, len(lines)):
                        nxt = lines[j].strip()
                        if not nxt:
                            continue
                        if _is_labelish(lines[j]):
                            continue
                        seen += 1
                        if seen > 3:
                            break
                        dm = _DATE_VALUE_FORM.match(nxt)
                        if dm:
                            value = dm.group(0)
                            value_confidence = "low"
                            note = "日期标签分行窗口配对"
                            evidence.append(EvidenceItem(
                                kind="label_value_pair", raw=_cap(value),
                                line=j + 1, source="date_nextline",
                            ))
                            break
                    if value is not None:
                        break
        elif fname == "total_amount":
            for ln, m in hits:
                am = _AMOUNT_FORM.search(lines[ln - 1], pos=m.end())
                if am is not None:
                    value = am.group(0).strip()
                    value_confidence = "low"
                    note = "金额标签同行配对"
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(value), line=ln,
                        source="amount_sameline",
                    ))
                    break
            if value is None:
                for ln, _ in hits:
                    seen = 0
                    for j in range(ln, len(lines)):
                        nxt = lines[j].strip()
                        if not nxt:
                            continue
                        if _is_labelish(lines[j]):
                            continue
                        seen += 1
                        if seen > 3:
                            break
                        am = _AMOUNT_FORM.search(nxt)
                        if am is not None:
                            value = am.group(0).strip()
                            value_confidence = "low"
                            note = "金额标签分行窗口配对"
                            evidence.append(EvidenceItem(
                                kind="label_value_pair", raw=_cap(value),
                                line=j + 1, source="amount_nextline",
                            ))
                            break
                    if value is not None:
                        break
        else:  # text-value fields (advertiser/agency/station/...)
            for ln, m in hits:
                v = _order_text_value(lines[ln - 1], m)
                if v:
                    value = v
                    value_confidence = "low"
                    note = "文本标签同行取值 (宽表列边界截断)"
                    evidence.append(EvidenceItem(
                        kind="label_value_pair", raw=_cap(v), line=ln,
                        source="text_sameline",
                    ))
                    break

        present = bool(label_forms) or value is not None
        if not present and role == "core":
            gaps.append(f"core 缺失: {fname}")

        fields[fname] = CandidateField(
            name=fname, role=role, present=present, value=value,
            value_confidence=value_confidence if value else "none",
            label_forms=tuple(label_forms),
            evidence=tuple(evidence),
            note=note,
        )

    return ScenarioUnderstanding(
        family="order",
        version=UNDERSTANDING_VERSION,
        fields=fields,
        dynamic_fields={},
        arithmetic=(),
        gaps=tuple(gaps),
        notes=(
            "order 骨架 v1: 只做标签级/同行配对证据, 行项目表格与"
            "金额算式校验未做 (诚实边界); 订单族内模板差异大, 宁缺毋滥",
        ),
    )


# ---------------------------------------------------------------------------
# Scenario detection (layer 1 → layer 2 routing): soft-evidence
# weighted scoring, not a hard template.
# Goal: a file comes in → it recognizes invoice/paper by itself →
# the matching skeleton is dispatched automatically.
# ---------------------------------------------------------------------------

# Each soft evidence carries a weight: weighted score ≥4 high /
# 2-3 medium / 1 low; <1 counts as no evidence.
# Shared regexes (detection positives and invoice negatives use the
# same shapes, defined once to prevent drift):
#   an order's self-declared identity must be a HEADING-shaped line
#   not followed by a number marker — inside a real invoice,
#   "Purchase Order No: ..." is an in-line field reference, not a
#   document identity (2026-11-05 confirmed).
_RE_ORDER_PO_HEADING = re.compile(
    r"(?i)^[ \t]*PURCHASE[ \t]+ORDER\b(?!\s*[:#]?\s*(?:NO\.?|NUM(?:BER)?|#))",
    re.M,
)
_RE_ORDER_SO_HEADING = re.compile(
    r"(?i)^[ \t]*SALES[ \t]+ORDER\b(?!\s*[:#]?\s*(?:NO\.?|NUM(?:BER)?|#))",
    re.M,
)
_RE_ORDER_WORKSHEET = re.compile(r"(?i)\bORDER\s+WORKSHEET\b")
_RE_NETWORK_ORDER = re.compile(r"(?i)\bNETWORK\s+ORDER\b")
_RE_ORDER_CONTRACT_HEADING = re.compile(
    r"(?i)^[ \t]*Contract\b(?!\s*[:#])(?!\s*(?:NO\.?|NUM(?:BER)?|#))", re.M,
)

_DETECT_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str], int], ...]] = {
    "invoice": (
        # Strong invoice-face structure: heading, number label,
        # issue date, remit-to party
        # (line-anchored patterns must use re.M, otherwise ^ matches
        # only the text's first line)
        ("det_invoice_heading", re.compile(
            r"(?i)^[ \t]*INVOICE\b", re.M), 2),
        # Short heading line containing INVOICE (e.g.
        # "PROFESSIONAL SERVICES INVOICE"); the char class allows
        # uppercase/digits/spaces only → prose lines stay out
        # (lowercase-led lines never match).
        ("det_invoice_wordline", re.compile(
            r"(?i)^[ \t]*[A-Z0-9 .,/'#&()-]{0,50}\bINVOICE\b[ \t]*$",
            re.M), 1),
        ("det_invoice_no_label", re.compile(
            r"(?i)\binvoice\s*(?:#|no\.?|num(?:ber)?)(?![A-Za-z])"), 2),
        ("det_invoice_date_label", re.compile(
            r"(?i)\b(?:invoice\s+date|date\s+of\s+invoice)\b"), 1),
        ("det_party_label", re.compile(
            r"(?i)\b(?:bill(?:ed)?\s+to|ship(?:ped|ping)?\s+to|"
            r"sold\s+to|remit(?:tance)?\s+to)\b"), 1),
        ("det_money_label", re.compile(
            r"(?i)\b(?:grand\s+)?total\b|\b(?:amount|balance)\s+due\b"
            r"|\bsub\s*-?\s*total\b"), 1),
        ("det_terms_label", re.compile(
            r"(?i)\bpayment\s+terms?\b|\bnet\s+\d+\b"), 1),
        # Line-leading bare Date label (00f290a2: "Date: August 24, 2001")
        ("det_bare_date_label", re.compile(
            r"(?i)^[ \t]*date\b\s*[:#]", re.M), 1),
        # Tax-id / bill-number labels (law-firm invoices: "Federal
        # Tax ID #", "TAXPAYER'S I.D.", "Bill number"; OCR space
        # noise tolerated via I\s*\.\s*D; the apostrophe accepts both
        # ASCII ' and the Unicode right quote ')
        ("det_tax_id_label", re.compile(
            r"(?i)\bfederal\s+tax\s+id\b"
            r"|\btaxpayer[\u2019']?s?\s+i[ .]*d\b"), 1),
        ("det_bill_number_label", re.compile(
            r"(?i)\bbill(?:ed)?\s+(?:number|no\.?|#)"), 1),
        ("det_发票", re.compile(r"发票|开票日期|纳税人识别号"), 2),
    ),
    "academic_paper": (
        # raw TeX structure macros (strongest) / weak PDF narrative evidence
        ("det_tex_documentclass", re.compile(r"\\documentclass"), 3),
        ("det_tex_title", re.compile(r"\\title\{"), 2),
        ("det_tex_author", re.compile(r"\\author\{"), 2),
        ("det_tex_abstract", re.compile(r"\\begin\{abstract\}"), 2),
        ("det_tex_bib", re.compile(
            r"\\bibliography\{|\\begin\{thebibliography\}"), 2),
        ("det_pdf_abstract", re.compile(r"(?i)^Abstract\.?\s*$", re.M), 1),
        ("det_pdf_references", re.compile(r"(?i)^References\s*$", re.M), 1),
        ("det_arxiv_id", re.compile(r"(?i)arxiv:\s*[\d.]{4,9}"), 1),
    ),
    # Order-type documents (media/broadcast Purchase/Sales/Network
    # Order, Contract, Order Worksheet — 2026-11-05, root cause seen
    # first-hand on 6 negative false positives): only self-declared
    # identity headings count — industry fields
    # (STATION/ADVERTISER/AGENCY/SPOTS) also appear on real
    # broadcast-industry invoices (0079863f Sinclair station invoice),
    # so they have no discriminative power and cannot serve as
    # positive evidence.
    "order": (
        ("det_order_po_heading", _RE_ORDER_PO_HEADING, 3),
        ("det_order_so_heading", _RE_ORDER_SO_HEADING, 3),
        ("det_order_worksheet", _RE_ORDER_WORKSHEET, 3),
        ("det_network_order", _RE_NETWORK_ORDER, 3),
        ("det_order_contract_heading", _RE_ORDER_CONTRACT_HEADING, 2),
        ("det_flight_dates", re.compile(r"(?i)\bFLIGHT\s+DATES?\b"), 1),
        ("det_run_dates", re.compile(r"(?i)\bRUN\s+DATES?\b"), 1),
        ("det_buyer_name", re.compile(r"(?i)\bBUYER\s+NAME\b"), 1),
    ),
}

# Counter-evidence: when a document declares another identity, the
# conflicting scenario loses points (soft veto, not a hard template).
# E.g. a "Purchase Order" heading costs invoice 3 points — but an
# in-line "Purchase Order No. 123" reference inside a real invoice
# does not fire (heading-line anchoring + number-marker exclusion).
_DETECT_NEGATIVE_PATTERNS: dict[
    str, tuple[tuple[str, re.Pattern[str], int], ...]
] = {
    "invoice": (
        ("neg_purchase_order_heading", _RE_ORDER_PO_HEADING, 3),
        ("neg_sales_order_heading", _RE_ORDER_SO_HEADING, 3),
        ("neg_order_worksheet", _RE_ORDER_WORKSHEET, 3),
        ("neg_network_order", _RE_NETWORK_ORDER, 3),
        ("neg_order_contract_heading", _RE_ORDER_CONTRACT_HEADING, 2),
    ),
}
_DETECT_MIN_SCORE = 2


def _score_of(total: int) -> str:
    if total >= 4:
        return "high"
    if total >= _DETECT_MIN_SCORE:
        return "medium"
    return "low"


@dataclass(frozen=True)
class ScenarioDetection:
    """One scenario's detection result: weighted evidence + confidence, no unique verdict."""

    label: str
    score: int
    confidence: str
    evidence: tuple[EvidenceItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (evidence list copied, no aliasing)."""
        return {
            "label": self.label,
            "score": self.score,
            "confidence": self.confidence,
            "evidence": [
                {"kind": e.kind, "raw": e.raw, "line": e.line, "source": e.source}
                for e in self.evidence
            ],
        }


@dataclass(frozen=True)
class DocumentInterpretation:
    """End-to-end result: detected scenario + structured understanding under it."""

    detection: ScenarioDetection
    understanding: Optional[ScenarioUnderstanding]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (detection + understanding combined)."""
        return {
            "scenario": self.detection.to_dict(),
            "understanding": (
                self.understanding.to_dict() if self.understanding else None
            ),
        }


def detect_scenario(
    text: str, filename: Optional[str] = None
) -> tuple[ScenarioDetection, ...]:
    """Soft-evidence weighted scoring: candidates returned sorted by total score, no unique verdict.

    A paper's tex mentioning "invoice" (as a term) is not
    misclassified — each scenario accumulates independently, and
    documentclass (3 points) outweighs scattered invoice labels.
    """
    by_label: dict[str, ScenarioDetection] = {}
    for label, patterns in _DETECT_PATTERNS.items():
        score = 0
        ev: list[EvidenceItem] = []
        for pid, pat, weight in patterns:
            m = pat.search(text)
            if m is None:
                continue
            score += weight
            line = text.count("\n", 0, m.start()) + 1
            ev.append(EvidenceItem(
                kind="label_form", raw=_cap(m.group(0)), line=line, source=pid,
            ))
        # Counter-evidence: self-declared identity conflict loses points (evidence still recorded, kind=negative_evidence)
        for pid, pat, weight in _DETECT_NEGATIVE_PATTERNS.get(label, ()):
            m = pat.search(text)
            if m is None:
                continue
            score -= weight
            line = text.count("\n", 0, m.start()) + 1
            ev.append(EvidenceItem(
                kind="negative_evidence", raw=_cap(m.group(0)), line=line,
                source=pid,
            ))
        if score > 0:
            by_label[label] = ScenarioDetection(
                label=label, score=score,
                confidence=_score_of(score), evidence=tuple(ev),
            )
    ranked = tuple(sorted(
        by_label.values(), key=lambda d: d.score, reverse=True,
    ))
    if ranked and ranked[0].score < _DETECT_MIN_SCORE:
        return ()   # below threshold: insufficient evidence — absence is not failure
    return ranked


# ---------------------------------------------------------------------------
# Weak-signal diagnostics (warnings): honest hints when evidence is
# insufficient; they never change the routing result.
# Design stance (settled): this project records
# problems rather than forcing a scenario onto every file —
# fragments go to generic parsing, but the observation "this may be
# a paper-project fragment" is still registered as-is for upstream
# to consume optionally.
# ---------------------------------------------------------------------------

# Weak structural signs of a LaTeX paper: worth hinting even when the detection score is insufficient.
_PAPER_FRAGMENT_HINTS = re.compile(
    r"\\(?:section|subsection|paragraph)\{"
    r"|\\begin\{(?:theorem|lemma|definition|proof|corollary|equation|align)\}"
    r"|\\cite[tp]?\{|\\ref\{|\\label\{"
)

# Naming shape of arxiv project-split files: 1601.00236__appendix.tex.
# When the filename matches, "it is a paper-project fragment" is
# almost certainly true even with no content signs — companion
# files (macros/header/caption) usually carry no body signs.
_RE_SPLIT_FRAGMENT_NAME = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?__")


def _paper_raw_score(text: str) -> tuple[int, list[EvidenceItem]]:
    """Recompute the academic_paper raw score (same source as _DETECT_PATTERNS)."""
    score = 0
    ev: list[EvidenceItem] = []
    for pid, pat, weight in _DETECT_PATTERNS["academic_paper"]:
        m = pat.search(text)
        if m is None:
            continue
        score += weight
        line = text.count("\n", 0, m.start()) + 1
        ev.append(EvidenceItem(
            kind="label_form", raw=_cap(m.group(0)), line=line, source=pid,
        ))
    return score, ev


def scenario_warnings(
    text: str, filename: Optional[str] = None
) -> tuple[str, ...]:
    """Weak-signal diagnostics: honest hints when scenario evidence is insufficient; they never change the routing result.

    Current diagnostics:
    * ``paper_like_fragment`` — the text carries several LaTeX paper
      structural signs (section macros/theorem environments/citation
      commands) but no document-level evidence (missing
      documentclass etc.), or the filename shows the project-split
      naming shape (1601.00236__appendix.tex).  The typical form is
      an arxiv project's split fragment files (including
      body-less companions such as macros/header/caption); they go
      to generic parsing, hinting upstream may aggregate at project
      level.

    No warning is produced when detection succeeds
    (detect_scenario non-empty) — this serves only files that are
    "absent" and never competes with the detection layer for the
    final say.
    """
    text = text or ""
    warnings: list[str] = []
    if detect_scenario(text, filename):
        return ()   # already has a scenario assignment, stay quiet
    score, ev = _paper_raw_score(text)
    hint_hits = len(_PAPER_FRAGMENT_HINTS.findall(text))
    split_named = bool(filename and _RE_SPLIT_FRAGMENT_NAME.match(filename))
    # ≥2 structural signs (a single \cite may be a coincidental
    # citation; several are a paper shape); or the filename itself
    # is project-split naming (companions often have no body signs)
    if hint_hits >= 2 or split_named:
        ev_parts: list[str] = []
        if hint_hits >= 2:
            ev_parts.append(f"LaTeX 结构迹象 {hint_hits} 处")
        if split_named:
            ev_parts.append("filename 为项目拆分命名形态")
        ev_desc = ", ".join(ev_parts)
        doc_ev = ", ".join(e.source for e in ev) or "无文档级证据"
        warnings.append(
            f"paper_like_fragment: {ev_desc} 但文档级证据不足 "
            f"({doc_ev}; score={score} < {_DETECT_MIN_SCORE}), "
            "可能是论文项目的拆分碎片; 已归入普通解析"
        )
    return tuple(warnings)


def interpret_document(
    text: str, filename: Optional[str] = None
) -> Optional[DocumentInterpretation]:
    """End-to-end entry: detect the scenario → parse with the matching skeleton → structured understanding.

    Returns None = insufficient scenario evidence (absence is not
    failure); when evidence exists but no skeleton is registered,
    understanding=None while detection is still returned as-is.
    """
    ranked = detect_scenario(text, filename)
    if not ranked:
        return None
    top = ranked[0]
    understanding = understand_scenario(top.label, text, filename)
    return DocumentInterpretation(detection=top, understanding=understanding)


# ---------------------------------------------------------------------------
# dispatch (label → skeleton; unregistered skeletons return None,
# absence is not failure)
# ---------------------------------------------------------------------------


def understand_scenario(
    label: str, text: str, filename: Optional[str] = None
) -> Optional[ScenarioUnderstanding]:
    """Dispatch the skeleton by scenario label; unregistered skeletons return None (absence is not failure)."""
    if label == "invoice":
        return understand_invoice(text, filename)
    if label == "academic_paper":
        return understand_academic_paper(text)
    if label == "order":
        return understand_order(text, filename)
    return None


__all__ = [
    "CandidateField",
    "ConstraintResult",
    "DocumentInterpretation",
    "EvidenceItem",
    "INVOICE_FIELD_CATALOG",
    "ORDER_FIELD_CATALOG",
    "ScenarioDetection",
    "ScenarioUnderstanding",
    "UNDERSTANDING_VERSION",
    "detect_scenario",
    "interpret_document",
    "scenario_warnings",
    "understand_academic_paper",
    "understand_invoice",
    "understand_order",
    "understand_scenario",
]
