r"""Invoice anchor extraction (prereg invoice_scenario_prereg_v1 §1.B).

Field witnesses with line evidence for the two anchor families.
Every hit carries: field name, raw value (as-is, never rewritten),
1-based line number, and the pattern that fired.

Discipline (along domain_signals): **an anchor hit is evidence, not a
claim** — patterns fire as field witnesses regardless of which family
"owns" them; family gating is the L2 scenario label's job, not this
module's.

Calibration status (honest): Western patterns shaped against the
Arnold & Porter corpse sample (post-salvage form: numbers tightened);
Chinese patterns shaped against standard VAT-invoice layout shapes.
Both await corpus calibration (prereg §1.B / D1: Chinese annotation
bucket not yet built).
Pattern changes must be re-calibrated.

2026-10-13 increment (western_invoice_anchor_prereg_v1, FROZEN):
western recall gap closed on 7-invoice real-corpus evidence —
total-label variants (INVOICE TOTAL / INVOICE AMOUNT / NET AMOUNT
DUE), table-pipe cell tolerance (``[\s|]*`` between label and
value), Inv# abbreviation, two-digit years, and label-only-line →
next-non-empty-line pairing (``*_nextline`` pattern ids, Invoice-
prefixed labels only).  Chinese patterns untouched.

2026-10-13 increment (western_invoice_tablerow_prereg_v1, FROZEN):
pass 3 — pipe-table header/value row pairing (``*_tablerow`` pattern
ids).  When a markdown [Table] row carries an Invoice-prefixed label
as a WHOLE cell (``| | Invoice # | | Invoice Date | |``) and the
first following non-separator table row carries the values at the
SAME cell index (``| | 2081683-1 | | 09/27/20 | |``, 002f9b8
evidence), the aligned value cell passes the same shape gate as
nextline pairing and becomes a witness.  Cell-index alignment is
real table structure, not word-order guessing.

v1 scope: field anchors only — invoice number, date, total, tax id
(western); 发票代码/号码/开票日期/价税合计大小写/购销双方+税号 (中文).
Line-item extraction is §1.D, table reconstruction v2.

2026-10-16 increment (western_invoice_anchor_ocrnoise_prereg_v1,
FROZEN): OCR camera-scan noise tolerance on business_records test-corpus
evidence — (a) separator noise class upgraded to
``(?:[\s|]*[.:：]){0,2}[\s|]*`` (space-before-punct and up to two
punct runs: ``Invoice no . : 99-SF2058``, fd5c11fe); (b) amount
internal space ``543 .00`` / ``$96,032 .26`` tolerated in
``_AMOUNT`` (value recorded as-is, never rewritten); (c) new
``invoice_number_colon_spaced`` for split-token values after a
colon — ``INVOICE : A 14708`` (012199d3): ≤2-letter prefix +
mandatory space + digit core, mutually exclusive with
``invoice_number_colon`` by that mandatory space.  Chinese
patterns untouched.  Prose guards re-verified: digit-bearing
lookaheads, ``(?![a-z])``-style word gates, trailing-% tax guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FAMILY_WESTERN = "western_invoice"
FAMILY_CHINESE = "chinese_invoice"

# 2026-10-16 (ocrnoise): dot may carry OCR split noise "543 .00" /
# "$96,032 .26" — whitespace tolerated AROUND the dot, never inside
# digits; value captured as-is (salvage may tighten upstream).
_AMOUNT = r"[-+]?[\d,]+\s*\.\s*\d{2}"
_CN_AMOUNT = r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+[元圆][零角分整正]{0,3}"

# (field, pattern_id, pattern) — Western family, line-by-line,
# case-insensitive. One field may own several pattern_ids (e.g. date
# forms); pattern_id is the stable provenance id recorded on each hit.
_WESTERN_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # Invoice number: requires a marker (#/No/Number/Num) OR a colon
    # separator; value must contain at least one digit (rejects prose
    # like "Invoice Number Three").
    # 2026-10-13 (western_invoice_anchor_prereg_v1): "Inv"
    # abbreviation (Inv # 7558669) + table-pipe cell tolerance —
    # label and value may sit in different cells of one table row
    # (| Invoice # | 2081683-1 |): [\s|]* swallows the pipe; the
    # value charset [A-Za-z0-9\-/] never eats past a cell.  No \b
    # after the marker group: "#" and whitespace are both non-word
    # chars, so \b never holds between them (Inv # 7558669 was
    # missed); the digit-bearing lookahead is the real prose guard.
    ("invoice_number", "invoice_number_marker", re.compile(
        r"\b(?:Invoice|Inv)\s*(?:#|No\.?|Number|Num)(?:[\s|]*[.:：]){0,2}[\s|]*"
        r"(?=[A-Za-z0-9\-/]*\d)([A-Za-z0-9][A-Za-z0-9\-/]{2,})",
        re.IGNORECASE)),
    ("invoice_number", "invoice_number_colon", re.compile(
        r"\bInvoice\s*[:：][\s|]*(?=[A-Za-z0-9\-/]*\d)"
        r"([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.IGNORECASE)),
    # 2026-10-16 (ocrnoise): OCR split-token form "INVOICE : A 14708"
    # (012199d3 evidence) — the value's first token is a ≤2-letter
    # short prefix, digits after the space.  The colon form's
    # digit-in-first-token lookahead always misses a pure-letter
    # first token; this variant closes that gap; mutually exclusive
    # with the colon form (mandatory space: without one, only the
    # colon form fires — no double witness).  The ≤2-letter prefix
    # cap is the prose guard (full words stay out: "Invoice:
    # December" does not match).
    ("invoice_number", "invoice_number_colon_spaced", re.compile(
        r"\bInvoice\s*[:：](?:[\s|]*[.:：]){0,2}[\s|]*"
        r"([A-Za-z]{1,2}\s+\d[\d\-/]{2,})", re.IGNORECASE)),
    # Date family: US long (December 20, 2000), UK long (20 December
    # 2000), numeric (12/20/2000 — US vs EU order is downstream
    # arbitration, value is recorded as-is), ISO (2000-12-20).
    # 2026-10-13: two-digit years (09/27/20) recorded as-is — century
    # is downstream ambiguity, never silently resolved; [\s|]* cell
    # tolerance as invoice_number.
    ("invoice_date", "invoice_date_us_long", re.compile(
        r"\b(?:Invoice\s*Date|Date\s*of\s*Invoice|Dated)\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)),
    ("invoice_date", "invoice_date_uk_long", re.compile(
        r"\b(?:Invoice\s*Date|Date\s*of\s*Invoice|Dated)\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        r"(\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4})", re.IGNORECASE)),
    ("invoice_date", "invoice_date_numeric", re.compile(
        r"\b(?:Invoice\s*Date|Date\s*of\s*Invoice|Dated)\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        r"(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)),
    ("invoice_date", "invoice_date_iso", re.compile(
        r"\b(?:Invoice\s*Date|Date\s*of\s*Invoice|Dated)\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        r"(\d{4}-\d{1,2}-\d{1,2})", re.IGNORECASE)),
    # Total due: keyword gate stays strict (bare TOTAL is prose bait);
    # currency may be $/€/£ symbol or EUR/GBP word.
    # 2026-10-13 variants (7-invoice evidence): INVOICE TOTAL /
    # INVOICE AMOUNT / NET AMOUNT DUE — label + two-decimal-amount
    # shapes that never occur in prose.
    ("total_due", "total_due_marker", re.compile(
        r"\b(?:TOTAL(?:\s+AMOUNT)?\s+DUE|AMOUNT\s+DUE|BALANCE\s+DUE"
        r"|INVOICE\s+TOTAL|INVOICE\s+AMOUNT|NET\s+AMOUNT\s+DUE)\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        rf"(?:(?:EUR|GBP)\s*)?(?:[$€£]\s*)?({_AMOUNT})", re.IGNORECASE)),
    # Subtotal: keyword-gated (SUBTOTAL / SUB-TOTAL / SUB TOTAL).
    ("subtotal", "subtotal_marker", re.compile(
        r"\bSUB\s*[-\s]?\s*TOTAL\b(?:[\s|]*[.:：]){0,2}[\s|]*"
        rf"(?:(?:EUR|GBP)\s*)?(?:[$€£]\s*)?({_AMOUNT})(?!\s*%)",
        re.IGNORECASE)),
    # Tax: percentage rates (TAX RATE 8.25%) are NOT amount witnesses —
    # guarded by the trailing-% lookahead.
    ("tax", "tax_marker", re.compile(
        r"\b(?:SALES\s+TAX|TAX|VAT)(?:\s*[.:：]){0,2}\s*"
        rf"(?:(?:EUR|GBP)\s*)?(?:[$€£]\s*)?({_AMOUNT})(?!\s*%)",
        re.IGNORECASE)),
    ("tax_id", "tax_id_marker", re.compile(
        r"\b(?:FEDERAL\s+TAX\s+IDENTIFICATION\s+NUMBER|TAX\s*(?:ID|No)|"
        r"VAT\s*(?:Reg(?:istration)?\.?\s*)?(?:Number|No)|EIN)(?:\s*[.:：]){0,2}\s*"
        # Value must contain at least one digit (same gate as
        # invoice_number): a pure-letter word such as the
        # "entifiers" tail of "tax identifiers" must not become a
        # tax-id witness.
        r"([0-9A-Za-z\-]*\d[0-9A-Za-z\-]{5,})", re.IGNORECASE)),
)

# 2026-10-13 (western_invoice_anchor_prereg_v1 §1.A3): label-only
# lines whose value sits on the NEXT non-empty line (reading-order
# reconstruction splits two-column label/value pairs; 0068358 real
# evidence: "Invoice #:" / "IN-11912129101").  Invoice-prefixed
# labels ONLY — bare "Number" / "Date" labels are prose FP bait and
# stay out (honest boundary, prereg §2).  Value lines must pass a shape gate
# (single token / date shape, digit-bearing) so a stray prose line
# never becomes a witness.
_WESTERN_NEXTLINE_LABELS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("invoice_number", "invoice_number_nextline", re.compile(
        r"^[ \t]*(?:Invoice|Inv)[ \t]*(?:#|No\.?|Number|Num)?[ \t]*[:：][ \t]*$",
        re.IGNORECASE)),
    ("invoice_date", "invoice_date_nextline", re.compile(
        r"^[ \t]*Invoice[ \t]*Date[ \t]*[:：]?[ \t]*$", re.IGNORECASE)),
)

_NEXTLINE_VALUE_SHAPES: dict[str, re.Pattern[str]] = {
    # single token (no spaces, no punctuation) with at least one
    # digit: prose sentences stay out
    "invoice_number": re.compile(r"[A-Za-z0-9\-/]*\d[A-Za-z0-9\-/]{2,}"),
    "invoice_date": re.compile(
        r"(?:\d{1,2}/\d{1,2}/\d{2,4})|(?:\d{4}-\d{1,2}-\d{1,2})"
        r"|(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"
        r"|(?:\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4})"),
}

# 2026-10-13 (western_invoice_tablerow_prereg_v1 §1.B): pass-3 table
# row shapes.  A pipe row strips to cells split on '|'; a separator
# row (dashes/colons only) is skipped without breaking adjacency; a
# label cell is a WHOLE-cell Invoice-prefixed label with no value in
# the cell; values pair at the SAME cell index in the first following
# non-separator pipe row.  A non-pipe line between them interrupts
# (no pairing — honest absence, same discipline as nextline pass 2).
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")
_TABLE_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("invoice_number", re.compile(
        r"^(?:Invoice|Inv)[ \t]*(?:#|No\.?|Number|Num)[ \t]*[:.]?$",
        re.IGNORECASE)),
    ("invoice_date", re.compile(
        r"^Invoice[ \t]*Date[ \t]*[:.]?$", re.IGNORECASE)),
)

# 2026-11-02 (business_records evidence 0175d161): table witness for
# bare Total.  pass 1's total_due_marker deliberately skips bare TOTAL
# (prose bait: "the total was"), but in a pipe table **cell alignment
# itself is structural evidence** — in "| Total |  |  | 4,320.00 |  |  |"
# label and value share one row, each in its own cell, which is not
# the same thing as a bare "total" in prose.  Constraints stay
# strict: the label cell must be a WHOLE-cell bare Total (optionally
# qualified by NET/DUE/AMOUNT), the value cell must be a WHOLE-cell
# amount, and only later cells in the same row count — "Total + noun"
# label cells like "| Total Spots |  |  | 25 |" stay out.
_TABLE_BARE_TOTAL = re.compile(
    r"^[ \t]*total(?:[ \t]+(?:net|due|amount))?[ \t]*[:.]?[ \t]*$",
    re.IGNORECASE,
)
_TABLE_AMOUNT_CELL = re.compile(
    r"^[ \t]*(?:(?:EUR|GBP)\s*)?(?:[$\u20ac\u00a3]\s*)?(" + _AMOUNT + r")[ \t]*$"
)


def _table_cells(line: str) -> list[str] | None:
    """Cells of a pipe-table row, or None if the line is not one."""
    stripped = line.strip()
    if not _TABLE_ROW_RE.fullmatch(stripped):
        return None
    return stripped[1:-1].split("|")

# (field, pattern_id, pattern) — Chinese family, line-by-line.
_CHINESE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("invoice_code", "invoice_code", re.compile(
        r"发票代码[:：]?\s*(\d{10,12})")),
    ("invoice_number", "invoice_number_cn", re.compile(
        r"发票号码[:：]?\s*(\d{8,20})")),
    ("invoice_date", "invoice_date_cn", re.compile(
        r"开票日期[:：]?\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)")),
    ("invoice_date", "invoice_date_cn_numeric", re.compile(
        r"开票日期[:：]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})")),
    # Uppercase (spelled-out) amount in three shapes: full/half-width
    # parens, no parens, and a standalone （大写） isolated on its own
    # line (the standalone lookbehind excludes double evidence when
    # 价税合计 is also present).
    ("total_cn_upper", "total_cn_upper_paren", re.compile(
        rf"价税合计\s*(?:（|\()\s*大写\s*(?:）|\))\s*[:：]?\s*(?:人民币)?\s*({_CN_AMOUNT})")),
    ("total_cn_upper", "total_cn_upper_plain", re.compile(
        rf"价税合计\s*大写\s*[:：]?\s*(?:人民币)?\s*({_CN_AMOUNT})")),
    ("total_cn_upper", "total_cn_upper_standalone", re.compile(
        rf"(?<!价税合计)\s*[（(]\s*大写\s*[）)]\s*[:：]?\s*(?:人民币)?\s*({_CN_AMOUNT})")),
    # Lowercase (numeric) amount: after 小写/合计 a closing paren or
    # colon may follow; the currency symbol includes the OCR halfwidth Y.
    ("total_cn_lower", "total_cn_lower", re.compile(
        r"(?:小写|合计)\s*[）)]?\s*[:：]?\s*[¥￥Y]\s*([\d,]+\.\d{2})")),
    # Buyer/seller name: value stops before any later label word
    # (negative lookahead prevents overrun).
    ("buyer_seller_name", "buyer_seller_name", re.compile(
        r"(?:购(?:买|货)方|销售方|销货方)(?:名称)?[:：]\s*"
        r"((?:(?!购(?:买|货)方|销(?:售|货)方|纳税人识别号|"
        r"统一社会信用代码|开户行|账号|电话|地址)\S)+)")),
    # Registration id: lowercase letters allowed (OCR damage); the
    # keyword gate alone already rejects prose.
    ("tax_registration_id", "tax_registration_id", re.compile(
        r"(?:统一社会信用代码|纳税人识别号)[:：]?\s*([0-9A-Za-z]{15,20})")),
    # 2026-10-27 (photo-scan evidence): value-shape witness when OCR
    # chews up the label — the 18-char unified social credit code
    # shape of GB 32100-2015 (starts with 9X, charset without
    # I/O/S/V/Z).  In real samples the label reads "WHRARBMS:" while
    # the 18-char value survives intact.  Anchor discipline: a bare
    # value shape lacks label support, but fixed length 18 + the
    # first-two-char gate + charset noise exclusion poses no
    # prose-miscapture risk under invoice L2 gating; the legacy
    # 15-digit tax id (440300083885931) is NOT rescued — without a
    # label it is indistinguishable from any long number: honest
    # absence.
    ("tax_registration_id", "tax_registration_id_cn_credit_code", re.compile(
        r"(?<![0-9A-Za-z])(9[0-9][0-9A-HJ-NP-RTUWXY]{16})(?![0-9A-Za-z])")),
)


@dataclass(frozen=True)
class AnchorHit:
    """One field witness: raw value + line evidence + firing pattern."""

    field: str
    value: str
    line: int  # 1-based
    pattern: str  # stable pattern id of the firing pattern (provenance)
    family: str  # FAMILY_WESTERN | FAMILY_CHINESE


@dataclass(frozen=True)
class AnchorResult:
    """Anchor extraction output: all hits + per-field view + stats."""

    hits: tuple[AnchorHit, ...]
    stats: dict[str, int]

    @property
    def hit_count(self) -> int:
        """Number of field witnesses recorded."""
        return len(self.hits)

    def by_field(self, field: str) -> tuple[AnchorHit, ...]:
        """All witnesses for one field, in line order."""
        return tuple(h for h in self.hits if h.field == field)


def extract_invoice_anchors(text: str) -> AnchorResult:
    """Extract field anchors from (salvaged) invoice text.

    Records ALL witnesses per field (a TOTAL may legitimately appear
    several times; arbitration is downstream, not here). Unmatched
    fields are simply absent — absence is not failure.
    Pure function; no I/O.

    Pass 1: per-line patterns (single-line label+value shapes).
    Pass 2 (2026-10-13): Invoice-prefixed label-only lines paired
    with their next non-empty line (decoupled two-column layouts);
    ``*_nextline`` pattern ids make the provenance explicit.
    Pass 3 (2026-10-13): pipe-table header/value row pairing at the
    same cell index; ``*_tablerow`` pattern ids (§1.B).
    """
    hits: list[AnchorHit] = []
    stats: dict[str, int] = {
        "western_hits": 0,
        "chinese_hits": 0,
    }
    lines = text.split("\n")

    for lineno, line in enumerate(lines, start=1):
        for field, pattern_id, pattern in _WESTERN_PATTERNS:
            for match in pattern.finditer(line):
                hits.append(AnchorHit(
                    field=field, value=match.group(1), line=lineno,
                    pattern=pattern_id, family=FAMILY_WESTERN,
                ))
                stats["western_hits"] += 1
        for field, pattern_id, pattern in _CHINESE_PATTERNS:
            for match in pattern.finditer(line):
                hits.append(AnchorHit(
                    field=field, value=match.group(1), line=lineno,
                    pattern=pattern_id, family=FAMILY_CHINESE,
                ))
                stats["chinese_hits"] += 1

    # pass 2: label-only line → next non-empty line pairing.  A label
    # line only pairs once (first next non-empty line); a prose FP
    # line stays out via the value shape gate.
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for field, pattern_id, pattern in _WESTERN_NEXTLINE_LABELS:
            if not pattern.fullmatch(stripped):
                continue
            for j in range(lineno, len(lines)):
                value_line = lines[j].strip()
                if not value_line:
                    continue
                value_shape = _NEXTLINE_VALUE_SHAPES[field]
                if value_shape.fullmatch(value_line):
                    hits.append(AnchorHit(
                        field=field, value=value_line, line=j + 1,
                        pattern=pattern_id, family=FAMILY_WESTERN,
                    ))
                    stats["western_hits"] += 1
                break  # pair with the first non-empty line only: skipping means no pairing (honest absence)
            break  # one label line resolves to a single field

    # pass 3 (2026-10-13, western_invoice_tablerow_prereg_v1 §1.B):
    # pipe-table header/value row pairing.  A label cell pairs with
    # the SAME-index cell of the first following non-separator pipe
    # row; a non-pipe line in between interrupts (honest absence).
    for lineno, line in enumerate(lines, start=1):
        cells = _table_cells(line)
        if cells is None or _TABLE_SEP_RE.fullmatch(line.strip()):
            continue
        labels: list[tuple[int, str]] = []
        for idx, cell in enumerate(cells):
            cell_text = cell.strip()
            for field, label_re in _TABLE_LABELS:
                if label_re.fullmatch(cell_text):
                    labels.append((idx, field))
        if not labels:
            continue
        for j in range(lineno, len(lines)):
            candidate = lines[j]
            if _TABLE_SEP_RE.fullmatch(candidate.strip()):
                continue  # separator row does not break adjacency
            value_cells = _table_cells(candidate)
            if value_cells is None:
                break  # non-table row interrupts: no pairing (honest absence)
            for idx, field in labels:
                if idx >= len(value_cells):
                    continue
                value = value_cells[idx].strip()
                if _NEXTLINE_VALUE_SHAPES[field].fullmatch(value):
                    hits.append(AnchorHit(
                        field=field, value=value, line=j + 1,
                        pattern=f"{field}_tablerow", family=FAMILY_WESTERN,
                    ))
                    stats["western_hits"] += 1
            break  # pair with the first following non-separator table row only

    # pass 4 (2026-11-02, business_records 0175d161 evidence): the
    # SAME-row table witness for bare Total.  pass 1's
    # total_due_marker does not take bare TOTAL (prose bait), and
    # pass 3 only pairs across rows at the same column; what is added
    # here is "label cell and amount cell in one row" — cell
    # alignment is structural evidence, outside the prose guard.
    for lineno, line in enumerate(lines, start=1):
        cells = _table_cells(line)
        if cells is None or _TABLE_SEP_RE.fullmatch(line.strip()):
            continue
        for idx, cell in enumerate(cells):
            if not _TABLE_BARE_TOTAL.fullmatch(cell):
                continue
            for later in cells[idx + 1:]:
                m = _TABLE_AMOUNT_CELL.fullmatch(later)
                if m:
                    hits.append(AnchorHit(
                        field="total_due", value=m.group(1), line=lineno,
                        pattern="total_due_tablerow_bare",
                        family=FAMILY_WESTERN,
                    ))
                    stats["western_hits"] += 1
                    break  # one row takes the first amount cell only

    return AnchorResult(hits=tuple(hits), stats=stats)
