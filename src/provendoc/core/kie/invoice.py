"""Invoice key-information extraction (KIE) for Provendoc parse output.

Why this module exists
----------------------
Provendoc's parse() contract deliberately stops at canonical narrative
text + metadata.  ``domain_signals`` counts invoice marker words (quorum)
but never pairs labels with values.  This module is the missing layer:
given parsed text it returns structured fields, each with provenance and
a confidence grade.

Template families
-----------------
Two invoice template families are supported, dispatched on *anchor
evidence* (``core/scenario/invoice_anchors.py``), never on file names
or guesses:

* ``chinese_invoice``  — the Chinese electronic-invoice (电子发票)
  template.  Field shapes: 20-digit number, ``YYYY年MM月DD日`` date,
  名称/税号 grid, ``*类别*名称`` items, 价税合计（大写）…（小写）.
* ``western_invoice``  — the Western invoice template (Invoice #,
  Invoice Date, Bill To / From, TOTAL AMOUNT DUE, trailing-amount line
  items).  Values come from the anchor + arbitration layer
  (``invoice_anchors`` / ``invoice_arbitration``) which works on real
  foreign invoices.

Design principles
-----------------
1. **Never trust label-value adjacency.**  The CN invoice header is a
   two-column label/value checkerboard; PDF reading-order
   reconstruction interleaves labels and values.  CN fields are located
   by keyword-anchored search over the whole text; Western fields are
   located by the anchor layer (same discipline, shared patterns).
   Party labels (Bill To / From) are the one adjacency-tolerant
   exception — same-line value is high confidence, next-line value is
   explicitly noted as decoupled.
2. **Separate evidence tiers.**  Native chart text (vector graphics)
   arrives scrambled and comma-glued; body text is usually clean.
   Scrambled evidence is only used for cross-checking, never to
   fabricate values (the old ``chart_text_rescrambled`` 20-digit slice
   tier was removed: an unverified digit-slice is a guess presented
   as a field — worse than an honest null).
3. **Honest failures.**  A field that cannot be extracted is ``null``
   with a reason, never a guess presented as fact.  Fields that do not
   exist in the detected template family (开票人 on a Western invoice)
   are ``null`` with a family-difference note, not a failure.

Public API: ``extract_invoice_fields(text, filename=None) -> dict``
"""

from __future__ import annotations

import re
from typing import Any, Optional

from provendoc.core.scenario.invoice_anchors import (
    FAMILY_CHINESE,
    FAMILY_WESTERN,
    AnchorResult,
    extract_invoice_anchors,
)
from provendoc.core.scenario.invoice_arbitration import (
    VERDICT_CONFLICT,
    VERDICT_MAJORITY,
    VERDICT_MULTI_ROLE,
    VERDICT_UNANIMOUS,
    ArbitrationResult,
    FieldDecision,
    arbitrate_anchors,
)
from provendoc.core.scenario.invoice_line_items import (
    LineItemResult,
    extract_line_items,
)

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

CJK_AMOUNT_CHARS = r"壹贰叁肆伍陆柒捌玖拾佰仟万亿零圆元角分整\s"


def _field(value: Any, confidence: str, source: str, note: Optional[str] = None) -> dict:
    """Uniform per-field envelope: value + provenance."""
    out = {"value": value, "confidence": confidence, "source": source}
    if note:
        out["note"] = note
    return out


def _null_field(reason: str) -> dict:
    return {"value": None, "confidence": "none", "source": None, "note": reason}


def _nonempty_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _to_float(token: Optional[str]) -> Optional[float]:
    if token is None:
        return None
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _verdict_confidence(decision: FieldDecision) -> str:
    """Arbitration verdict -> KIE confidence grade."""
    if decision.verdict == VERDICT_UNANIMOUS:
        return "high"
    if decision.verdict in (VERDICT_MAJORITY,):
        return "medium"
    return "medium"  # unique: single witness, honest medium


# ---------------------------------------------------------------------------
# evidence tiers
# ---------------------------------------------------------------------------

_CHART_MARKER = re.compile(r"\[Figure on page \d+\]\s*Native chart text:", re.I)


def _split_evidence(text: str) -> tuple[str, str]:
    """Split parse output into (body text, chart text).

    Chart text = segments introduced by Provendoc's
    ``[Figure on page N] Native chart text:`` marker.  On invoices this is
    where the vector header band (发票号码 / INVOICE NUMBER band) lives,
    usually scrambled by reading-order reconstruction.
    """
    chart_parts: list[str] = []
    body_lines: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CHART_MARKER.search(lines[i])
        if m:
            seg = [lines[i][m.end():].strip()]
            # continuation: following non-empty lines until a blank line or
            # the next structural marker
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].lstrip().startswith(("[", "|")):
                seg.append(lines[j].strip())
                j += 1
            chart_parts.append("\n".join(p for p in seg if p))
            i = j
        else:
            body_lines.append(lines[i])
            i += 1
    return "\n".join(body_lines), "\n".join(chart_parts)


# ---------------------------------------------------------------------------
# Chinese family extractors
# ---------------------------------------------------------------------------

def _extract_invoice_number(body: str, chart: str, filename: Optional[str]) -> dict:
    """发票号码 (invoice number): 20-digit string.

    Tiers: (a) clean 20-digit run in text; (b) 20-digit run in the file
    name, cross-verified as an ordered digit subsequence of the
    scrambled chart text.

    The old tier (c) sliced a 20-digit window out of the scrambled
    chart digit soup — an unverified guess that fired on Western
    invoices as a fake invoice number.  Removed: no cross-verification,
    no field.
    """
    m = re.search(r"(?<!\d)(\d{20})(?!\d)", body)
    if m:
        return _field(m.group(1), "high", "body_text")

    if filename:
        m = re.search(r"(?<!\d)(\d{20})(?!\d)", filename)
        if m:
            cand = m.group(1)
            soup_digits = re.sub(r"\D", "", chart + "\n" + body[:2000])
            if cand in soup_digits:
                return _field(cand, "high", "filename+chart_text",
                              "取自文件名，并已验证其数字按序出现在乱序矢量文本中")
            return _field(cand, "medium", "filename",
                          "取自文件名，未能在正文中交叉验证")

    return _null_field(
        "发票号码以图片区域渲染或不存在于文本层；无交叉验证证据时不猜测"
        "（乱序数字流截取 tier 已于 2026-10-12 移除，登记簿 #126；"
        "OCR 路由缺口见登记簿 #127）"
    )


def _extract_issue_date(body: str, chart: str) -> dict:
    """开票日期: YYYY年MM月DD日, normalized to ISO."""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", body)
    tier = "body_text"
    if not m:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", chart)
        tier = "chart_text"
    if m:
        y, mo, d = (int(g) for g in m.groups())
        iso = f"{y:04d}-{mo:02d}-{d:02d}"
        # cross-check against scrambled digit pairs like "2026" + "0822"
        soup = re.sub(r"\D", "", chart)
        verified = f"{y:04d}{mo:02d}{d:02d}" in soup or f"{y:04d}" in soup and f"{mo:02d}{d:02d}" in soup
        conf = "high" if tier == "body_text" else ("medium" if verified else "low")
        return _field(iso, conf, tier, f"原文 {m.group(0)}")
    return _null_field("未找到 YYYY年MM月DD日 形态的日期")


# 名称 (party-name) label witness, two shapes — the full label
# "名称:" and the OCR-chewed bare "称:" (a bare 称 must have no CJK
# character in front of it: 职称/简称/别称/全称 compounds are excluded
# by that lookbehind; the full "名称" shape is unrestricted).  The
# value is the rest of the line, or the next non-empty line when the
# label stands alone, cut at the first "|" or at any other field label
# squeezed onto the same line.
_CN_PARTY_NAME_LABEL = re.compile(r"(?:名\s*称|(?<![一-鿿])称)\s*[：:]\s*")
# Tax-id label witness (value may wrap across lines: \s* swallows the
# newline).  Lowercase letters tolerated (OCR damage), same charset
# discipline as the anchor layer's tax_registration_id.
_CN_TAX_LABEL = re.compile(
    r"(?:统一社会信用代码|纳税人识别号)[^\n：:]*[：:]\s*([0-9A-Za-z]{15,20})")
# Other field labels that may be squeezed onto a name line (the value
# is cut at these when sharing a cell).
_CN_OTHER_FIELD_LABEL = re.compile(
    r"(?:纳税人识别号|统一社会信用代码|开户行|地址|账号|电话|开票人|备注|合计)")
# Line-adjacency window pairing a tax label/witness to a name block
# (must fall within N lines after the name-value line; measured gap was
# 5, window 6 catches it without bleeding into the next block).
_CN_PARTY_TAX_WINDOW = 6


def _cn_party_value(raw: str) -> Optional[str]:
    """Name-candidate cleanup + gate: cut at "|" and at field labels
    squeezed onto the line; reject pure digit/number-like shapes (a
    company name must contain CJK or a run of letters).  Returns None
    when not qualified (honest absence, no guessing)."""
    if not raw:
        return None
    value = raw.split("|", 1)[0]
    squeeze = _CN_OTHER_FIELD_LABEL.search(value)
    if squeeze:
        value = value[: squeeze.start()]
    value = value.strip()
    if len(value) < 2:
        return None
    if not re.search(r"[一-鿿]", value) and not re.search(r"[A-Za-z]{2,}", value):
        return None
    # Pure alphanumeric token with a digit majority = tax-id/number
    # shape, not a company name ("91440300MA05D6E7F8" has letters but
    # still fails the majority test).
    if re.fullmatch(r"[0-9A-Za-z]+", value):
        digits = len(re.findall(r"\d", value))
        letters = len(re.findall(r"[A-Za-z]", value))
        if digits > letters:
            return None
    return value


def _extract_parties(body: str, anchors: AnchorResult) -> tuple[dict, dict]:
    """购买方 (buyer) / 销售方 (seller): name + unified social credit
    code (taxpayer id).

    Blocks are rebuilt line-wise:

    - Role evidence first: 购/销 fragments inside the label-line
      prefix (the "销" in "区销|名称:") pin a block to its slot;
      blocks without fragments back-fill empty slots in line order
      (template order — buyer first);
    - Tax ids are paired by text offset + line adjacency (within N
      lines after the name-value line): label witness first; the
      block's upper bound is the next block's name-label offset —
      under two-column tight packing (previous block's tax id on
      the same line as the next block's name) offset order still
      pairs without false rejection; a witness stealing across
      blocks (offset after the next block's name) is still rejected.
      When the label is OCR-chewed, the anchor layer's 18-digit
      unified-social-credit-code value-shape witness is consumed
      (same single-witness-to-seller discipline as the Western
      family, noted); 15-digit legacy tax ids without labels are
      not rescued.
    """
    lines = body.split("\n")
    line_offsets: list[int] = []
    _off = 0
    for _ln in lines:
        line_offsets.append(_off)
        _off += len(_ln) + 1

    # Name blocks: name / value line / role-fragment evidence / label offset
    blocks: list[dict] = []
    for i, ln in enumerate(lines):
        for m in _CN_PARTY_NAME_LABEL.finditer(ln):
            prefix = ln[: m.start()]
            has_buy, has_sell = "购" in prefix, "销" in prefix
            role = None
            if has_buy != has_sell:  # 双字同在=证据冲突, 按无角色处理
                role = "buyer" if has_buy else "seller"
            value = _cn_party_value(ln[m.end():])
            value_line = i + 1
            if value is None:
                # label stands alone: value is the next non-empty line
                # (skip → no pairing, same discipline)
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if not nxt:
                        continue
                    if (_CN_PARTY_NAME_LABEL.search(nxt)
                            or _CN_OTHER_FIELD_LABEL.match(nxt)):
                        break  # 下一行是别的标签 → 本块无值
                    value = _cn_party_value(nxt)
                    if value is not None:
                        value_line = j + 1
                    break
            if value is not None:
                blocks.append({"name": value, "line": value_line, "role": role,
                               "offset": line_offsets[i] + m.start()})

    # Tax ids: label witnesses (line + offset) + the anchor layer's
    # 18-digit value-shape witnesses
    tax_label_hits = [
        (body.count("\n", 0, m.start()) + 1, m.start(), m.group(1))
        for m in _CN_TAX_LABEL.finditer(body)
    ]
    credit_witnesses = [
        h for h in anchors.by_field("tax_registration_id")
        if h.pattern == "tax_registration_id_cn_credit_code"
    ]
    for idx, block in enumerate(blocks):
        # Line window: the witness must fall within N lines after the
        # name-value line (measured gap 5, window 6 catches it); a tax
        # label squeezed onto the same line (value and label on one
        # line) also counts.
        hi_line = block["line"] + _CN_PARTY_TAX_WINDOW
        # Block upper bound (char offset): candidates must not cross
        # the next block's name label.
        upper_off = blocks[idx + 1]["offset"] if idx + 1 < len(blocks) else None
        tax = tax_src = None
        for line_no, cand_off, value in tax_label_hits:
            if (cand_off > block["offset"] and line_no <= hi_line
                    and (upper_off is None or cand_off < upper_off)):
                tax, tax_src = value, "label"
                break
        if tax is None:
            # Witness path keeps line-number bounds (AnchorHit has no
            # offset; conservatively stays within the block)
            hi = hi_line
            if idx + 1 < len(blocks):
                hi = min(hi, blocks[idx + 1]["line"] - 1)
            for witness in credit_witnesses:
                if block["line"] < witness.line <= hi:
                    tax, tax_src = witness.value, "witness"
                    break
        block["tax_id"] = tax
        block["tax_src"] = tax_src

    # Slots: role fragments pin blocks; unroled blocks back-fill in
    # line order (template order)
    buyer: Optional[dict] = None
    seller: Optional[dict] = None
    unroled: list[dict] = []
    for block in blocks:
        if block["role"] == "buyer" and buyer is None:
            buyer = block
        elif block["role"] == "seller" and seller is None:
            seller = block
        else:
            unroled.append(block)
    if buyer is None and unroled:
        buyer = unroled.pop(0)
    if seller is None and unroled:
        seller = unroled.pop(0)

    def party(block: Optional[dict], slot: str) -> dict:
        if block is None:
            if blocks:
                hint = "（仅存的名称块已归对方/无角色证据，购方块疑似标签被 OCR 啃半）"
                return _null_field(f"未找到{slot}名称块{hint}")
            return _null_field(f"未找到第 {1 if slot == '购买方' else 2} 组名称/税号（模板顺序：先购后销）")
        notes: list[str] = []
        if block["role"] == "buyer":
            notes.append("角色由行内购方标签碎片佐证")
        elif block["role"] == "seller":
            notes.append("角色由行内销方标签碎片佐证")
        else:
            notes.append("无购/销标签碎片，按模板顺序（先购后销）推断")
        if block["tax_id"] is not None and block["tax_src"] == "witness":
            notes.append("税号由 18 位统一社会信用代码值形态证人按行邻接配对（标签被 OCR 啃半）")
        conf = "high" if block["tax_id"] is not None else "medium"
        return _field({"name": block["name"], "tax_id": block["tax_id"]},
                      conf, "body_text", "；".join(notes))

    return party(buyer, "购买方"), party(seller, "销售方")


def _extract_items(body: str) -> dict:
    """明细行: lines shaped ``*category*name ... 金额 税率% 税额``.

    Returns best-effort rows; 金额/税额 are anchored on the tax-rate %
    sign, which is the most stable column in scrambled rows.  Wrapped
    multi-line item names keep their raw text for transparency.
    """
    items: list[dict] = []
    skipped = 0
    for ln in _nonempty_lines(body):
        if "*" not in ln or re.match(r"^(合\s*计|小\s*计)", ln):
            continue
        if not re.search(r"\*[^*]+\*", ln):
            continue
        rate_m = list(re.finditer(r"(\d+(?:\.\d+)?)\s*%", ln))
        if not rate_m:
            skipped += 1
            continue
        rate = rate_m[0].group(1)
        after = ln[rate_m[0].end():]
        before = ln[:rate_m[0].start()]
        tax_m = re.search(r"(-?[\d,]+\.\d{2})", after)
        amt_cands = re.findall(r"-?[\d,]+\.\d{2}", before)
        amount = amt_cands[-1] if amt_cands else None
        # item name: strip trailing numeric columns (qty/unit-price/amount)
        name = re.sub(r"(\s+-?\d[\d,]*\.?\d*)+\s*$", "", before).strip()
        items.append({
            "项目名称": name,
            "金额": _to_float(amount),
            "税率": f"{rate}%",
            "税额": _to_float(tax_m.group(1)) if tax_m else None,
            "raw": ln,
        })
    if items:
        note = f"税率% 锚定解析；跳过无税率行 {skipped} 行" if skipped else None
        return _field(items, "medium", "body_text", note)
    return _null_field("未找到 *类别*名称 形态的明细行")


def _extract_totals(body: str) -> dict:
    """合计金额 / 合计税额 (also accepts 小计)."""
    m = re.search(r"(?:^|\n)\s*(?:合\s*计|小\s*计)\s*¥\s*([\d,]+\.\d{2})\s*¥\s*([\d,]+\.\d{2})", body)
    if m:
        return _field({"金额合计": _to_float(m.group(1)), "税额合计": _to_float(m.group(2))},
                      "high", "body_text", f"原文行锚定: {m.group(0).strip()}")
    return _null_field("未找到 合计 ¥金额 ¥税额 行")


def _extract_grand_total(body: str) -> dict:
    """价税合计: 大写 + 小写."""
    m = re.search(
        r"价税合计（大写）[\s|]*([" + CJK_AMOUNT_CHARS + r"]+?)\s*（小写）\s*¥?\s*([\d,]+\.\d{2})",
        body,
    )
    if m:
        return _field({"大写": m.group(1).strip(), "小写": _to_float(m.group(2))},
                      "high", "body_text")
    m = re.search(r"（小写）\s*¥\s*([\d,]+\.\d{2})", body)
    if m:
        return _field({"大写": None, "小写": _to_float(m.group(1))}, "medium",
                      "body_text", "仅取到小写，大写缺失")
    return _null_field("未找到 价税合计（大写）…（小写） 行")


def _extract_drawer(body: str) -> dict:
    """开票人: value may sit on the next line (label/value decoupled)."""
    lines = _nonempty_lines(body)
    for i, ln in enumerate(lines):
        m = re.search(r"开\s*票\s*人\s*[：:]\s*(.+)", ln)
        if m:
            return _field(m.group(1).strip(), "high", "body_text")
        if re.fullmatch(r"开\s*票\s*人\s*[：:]?", ln) and i + 1 < len(lines):
            return _field(lines[i + 1], "medium", "body_text",
                          "标签与取值分行（阅读顺序错位所致），按下一非空行取值")
    return _null_field("未找到 开票人 标签")


def _extract_remark(body: str) -> dict:
    """备注: keyword-anchored subfields (支付渠道/交易单号) or raw row."""
    remark: dict[str, str] = {}
    m = re.search(r"支付渠道\s*[:：]\s*([^;；|\n]+)", body)
    if m:
        remark["支付渠道"] = m.group(1).strip()
    m = re.search(r"交易单号\s*[:：]\s*([0-9A-Za-z]+)", body)
    if m:
        remark["交易单号"] = m.group(1)
    if remark:
        return _field(remark, "high", "body_text")
    m = re.search(r"备\s*注\s*\|?\s*([^\n]+)", body)
    if m:
        raw = m.group(1).strip()
        if raw.strip("| "):  # empty remark cells like "|  |  |" are not data
            return _field({"raw": raw}, "low", "body_text", "未识别备注结构，保留原文")
    return _null_field("无备注")


def _extract_title(body: str) -> dict:
    m = re.search(r"(电子发票（[^）]+）|电子发票\([^)]+\))", body)
    if m:
        return _field(m.group(1).replace("(", "（").replace(")", "）"), "high", "body_text")
    return _null_field("未找到发票标题")


# ---------------------------------------------------------------------------
# Western family extractors
# ---------------------------------------------------------------------------

# Party label patterns stay KIE-local (field-pattern ownership —
# anchor module vs KIE — is not frozen; the anchor module is
# calibration-frozen and adding patterns there requires
# re-calibration).
# Line-start anchored, same discipline as domain_signals parties_fields.
_RECIPIENT_LABEL = re.compile(
    r"^[ \t]*(?:Bill|Ship|Sold|Remit(?:ted)?)\s+To[ \t]*[:：]?", re.IGNORECASE)
_ISSUER_LABEL = re.compile(
    r"^[ \t]*(?:From|Invoice\s+From|Seller|Issued\s+By|Supplier)"
    r"[ \t]*[:：]", re.IGNORECASE)
_WESTERN_TITLE = re.compile(
    r"^[ \t]*(?:TAX\s+)?INVOICE(?:\s+(?:No\.?|#|NUMBER))?[ \t]*"
    r"[A-Za-z0-9\-/]*[ \t]*$", re.IGNORECASE)
# Two-column serialization: a right-column heading like "Invoice
# Totals" can ride on the label line ("Remit To: Invoice Totals") and
# be mistaken for a high-confidence same-line party value.  Candidate
# values pass a two-part gate:
# (a) must not start like a field label/heading (Invoice/Total/Tax/
#     Date ... — "Invoice Totals" is a heading, not a company name);
# (b) must contain at least one letter/CJK (a bare amount line
#     "$1,411.00" is not a party).
# Rejection = honest absence — after a same-line value is rejected,
# no falling through to the next line to guess (that line is polluted
# by the adjacent-column heading; the next line is an address
# fragment, garbage either way).
_WESTERN_LABELISH_VALUE = re.compile(
    r"^[ \t]*(?:(?:tax[ \t]+)?invoice|inv|sub[ \t]*-?[ \t]*total|total"
    r"|sales[ \t]+tax|tax|balance|amount|net|gross|page|contract"
    r"|estimate|terms|date|commission|remit|ship|sold|bill)\b",
    re.IGNORECASE)
_WESTERN_VALUEISH = re.compile(r"[A-Za-z一-鿿]")
# Trailing-amount strip keeps currency symbols: "…NOV 60 $50.00"
# leaves a stray "$" after the strip.
_TRAILING_AMOUNTS = re.compile(r"(?:\s*[$€£¥]?\s*[-+]?[\d,]+\.\d{2}){1,3}\s*$")


def _western_value_ok(candidate: str) -> bool:
    """Party candidate gate: not label/heading-shaped, and contains a
    letter/CJK."""
    return (
        bool(candidate.strip())
        and _WESTERN_VALUEISH.search(candidate) is not None
        and _WESTERN_LABELISH_VALUE.match(candidate) is None
    )


def _party_by_label(lines: list[str], label_re: re.Pattern[str]) -> dict:
    """One party from a line-start label (Bill To / From: …).

    Same-line value → high; label-only line → next non-empty line,
    medium, with a decoupled note (reading-order reconstruction splits
    two-column label/value pairs).

    Same-line values and the next non-empty line must both pass the
    _western_value_ok gate (two-column serialization puts
    right-column headings on the label's line); after a same-line
    value is rejected, scanning continues with later label lines
    (never falls through to the next line to guess).  All rejected →
    honest absence, with the rejections recorded in the note.
    """
    rejected = 0
    for i, ln in enumerate(lines):
        m = label_re.match(ln)
        if not m:
            continue
        same_line = ln[m.end():].strip()
        if same_line and not label_re.fullmatch(ln):
            if _western_value_ok(same_line):
                return _field({"name": same_line, "tax_id": None},
                              "high", "body_text", f"标签行锚定: {ln.strip()[:60]}")
            rejected += 1
            continue
        # Label stands alone: value is the next non-empty line (if that
        # line is a party label or a label/heading shape → skip, no
        # pairing, honest absence)
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if (not label_re.match(nxt)
                    and not _RECIPIENT_LABEL.match(nxt)
                    and not _ISSUER_LABEL.match(nxt)
                    and _western_value_ok(nxt)):
                return _field({"name": nxt.strip(), "tax_id": None}, "medium",
                              "body_text",
                              "标签与取值分行（阅读顺序错位所致），按下一非空行取值")
    if rejected:
        return _null_field(
            f"标签行在场但候选值均为标题/标签形态（{rejected} 处被拒），不猜测")
    return _null_field("未找到对应标签（西式族：Bill/Ship/Sold/Remit To 或 From/Seller/Issued By）")


def _western_selected(
    arbitration: ArbitrationResult, field: str
) -> Optional[FieldDecision]:
    return arbitration.get(field, FAMILY_WESTERN)


def _western_anchor_field(
    arbitration: ArbitrationResult, field: str, miss_note: str
) -> dict:
    """One field straight from the western anchor arbitration."""
    decision = _western_selected(arbitration, field)
    if decision is None:
        return _null_field(miss_note)
    if decision.verdict == VERDICT_CONFLICT:
        cands = ", ".join(sorted({w.value for w in decision.witnesses})[:5])
        return _null_field(f"锚点证人冲突未仲裁：{cands}（不选边）")
    if decision.verdict == VERDICT_MULTI_ROLE:
        return _null_field("多角色字段，v1 不拆分")
    conf = _verdict_confidence(decision)
    assert decision.selected is not None
    note = (f"锚点 {decision.selected.pattern} 行 {decision.selected.line}；"
            f"{decision.verdict}（{decision.reason}）")
    return _field(decision.selected.value, conf, f"anchor:{decision.selected.pattern}", note)


def _western_issue_date(arbitration: ArbitrationResult) -> dict:
    """Invoice date: canonical ISO when the cluster key resolved a real
    calendar date; ambiguous numerics (7/12/2020, US vs EU order) keep
    their raw value with an ambiguity note — never silently resolved."""
    decision = _western_selected(arbitration, "invoice_date")
    if decision is None:
        return _null_field("未找到 Invoice Date 锚点（四种形态均未命中）")
    if decision.verdict == VERDICT_CONFLICT:
        cands = ", ".join(sorted({w.value for w in decision.witnesses})[:5])
        return _null_field(f"日期证人冲突未仲裁：{cands}（不选边）")
    assert decision.selected is not None
    key = decision.selected_key or ""
    conf = _verdict_confidence(decision)
    # Two-digit years (09/27/20) check before the literal branch: the
    # arbitration layer's _RE_NUMERIC_SLASH only recognizes four-digit
    # years, so two-digit values always land in the literal cluster —
    # but century ambiguity is more specific than generic shape
    # ambiguity, and the note should say so (as-is discipline
    # unchanged, just a sharper diagnosis)
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", decision.selected.value):
        return _field(decision.selected.value, conf,
                      f"anchor:{decision.selected.pattern}",
                      "两位年日期：世纪歧义，保留原文不归一（as-is 纪律）")
    if key.startswith("literal:"):
        return _field(decision.selected.value, conf,
                      f"anchor:{decision.selected.pattern}",
                      "日期形态有歧义（如美式/欧式 a/b/yyyy 序未知），保留原文不归一")
    return _field(key, conf, f"anchor:{decision.selected.pattern}",
                  f"原文 {decision.selected.value}，归一 ISO")


def _western_title(body: str) -> dict:
    lines = _nonempty_lines(body)
    for ln in lines[:12]:  # letterhead zone: title lives near the top
        if _WESTERN_TITLE.fullmatch(ln):
            return _field(ln.strip().upper(), "high", "body_text")
    return _null_field("未找到 INVOICE 标题行（前 12 行内无独立抬头）")


def _western_items(line_items: LineItemResult) -> dict:
    """Western line items anchored on trailing amounts.

    Descriptions must contain a letter/CJK — bare amount lines (a
    serialized "$1,820.00" left behind after the label line of a
    summary amount was skipped by the summary gate) and leftover
    currency-symbol tails ("…$50.00" leaving a stray "$" after the
    strip) are not line items; they are counted, skipped and recorded
    in the note.  All skipped → honest absence.
    """
    items: list[dict] = []
    bare_amount_skipped = 0
    for hit in line_items.items:
        description = _TRAILING_AMOUNTS.sub("", hit.raw).strip()
        if _WESTERN_VALUEISH.search(description) is None:
            bare_amount_skipped += 1
            continue
        items.append({
            "description": description,
            "amount": _to_float(hit.amount),
            "qty": hit.qty,
            "unit_price": hit.unit_price,
            "raw": hit.raw,
        })
    if items:
        note = f"尾随金额锚定解析；跳过汇总行 {line_items.stats['summary_lines_skipped']} 行"
        if bare_amount_skipped:
            note += f"；跳过纯金额行 {bare_amount_skipped} 行（无描述内容，疑为汇总金额串行）"
        if line_items.stats["with_qty_unit"]:
            note += f"；{line_items.stats['with_qty_unit']} 行经数量×单价=金额自证列语义"
        return _field(items, "medium", "body_text", note)
    if bare_amount_skipped:
        return _null_field(
            f"未找到含描述的明细行（跳过纯金额行 {bare_amount_skipped} 行："
            "无描述内容，疑为汇总金额串行）")
    return _null_field("未找到尾随两位小数金额的明细行")


def _western_parties(body: str, arbitration: ArbitrationResult) -> tuple[dict, dict]:
    lines = _nonempty_lines(body)
    buyer = _party_by_label(lines, _RECIPIENT_LABEL)
    seller = _party_by_label(lines, _ISSUER_LABEL)
    # Single tax-id witness attaches to the seller (issuer) envelope with
    # an explicit v1 assumption note; multiple witnesses stay unattached
    # (role split is v2, same honest boundary as CN multi_role).
    tax_decision = _western_selected(arbitration, "tax_id")
    if (
        tax_decision is not None
        and tax_decision.verdict not in (VERDICT_CONFLICT, VERDICT_MULTI_ROLE)
        and tax_decision.selected is not None
        and isinstance(seller.get("value"), dict)
    ):
        seller["value"]["tax_id"] = tax_decision.selected.value
        seller["note"] = (
            f"税号锚点 {tax_decision.selected.pattern}；v1 假设单证人税号归出票方，"
            "多证人/分方归位是 v2"
        )
    return buyer, seller


def _western_validate(fields: dict[str, dict]) -> list[dict]:
    checks: list[dict] = []
    totals = fields["totals"]["value"] or {}
    grand = fields["grand_total"]["value"] or {}
    total_due = grand.get("total_due")
    subtotal = totals.get("subtotal")
    tax = totals.get("tax")
    if subtotal is not None and tax is not None and total_due is not None:
        s = round(subtotal + tax, 2)
        checks.append({
            "check": "subtotal + tax == total_due",
            "expected": total_due, "actual": s,
            "pass": abs(s - total_due) < 0.005,
        })
    items = fields["items"]["value"]
    if items and subtotal is not None:
        item_sum = round(sum(it["amount"] or 0 for it in items), 2)
        checks.append({
            "check": "明细金额之和 == subtotal",
            "expected": subtotal, "actual": item_sum,
            "pass": abs(item_sum - subtotal) < 0.005,
        })
    return checks


def _extract_western_fields(
    body: str,
    arbitration: ArbitrationResult,
    line_items: LineItemResult,
) -> dict[str, dict]:
    buyer, seller = _western_parties(body, arbitration)
    totals_val: dict[str, Any] = {}
    for name in ("subtotal", "tax"):
        decision = _western_selected(arbitration, name)
        if decision is not None and decision.selected is not None:
            totals_val[name] = _to_float(decision.selected.value)
    totals = (
        _field(totals_val, "medium", "anchor:subtotal/tax_marker",
               f"锚点仲裁：{ {k: v for k, v in totals_val.items()} }")
        if totals_val
        else _null_field("未找到 SUBTOTAL / TAX(SALES TAX/VAT) 锚点")
    )
    grand = _western_anchor_field(arbitration, "total_due",
                                  "未找到 TOTAL AMOUNT DUE / BALANCE DUE 锚点")
    if grand["value"] is not None:
        grand = _field({"total_due": _to_float(grand["value"])},
                       grand["confidence"], grand["source"],
                       grand.get("note"))
    return {
        "title": _western_title(body),
        "invoice_number": _western_anchor_field(
            arbitration, "invoice_number",
            "文本层无 Invoice #/No/Number 证据（图片区域渲染时不可恢复，"
            "OCR 路由缺口见登记簿 #127）"),
        "issue_date": _western_issue_date(arbitration),
        "buyer": buyer,
        "seller": seller,
        "items": _western_items(line_items),
        "totals": totals,
        "grand_total": grand,
        "drawer": _null_field("西式发票无开票人字段（模板族差异，非解析失败）"),
        "remark": _null_field("西式发票 v1 不解析备注（诚实边界）"),
    }


# ---------------------------------------------------------------------------
# cross-validation (Chinese family)
# ---------------------------------------------------------------------------

def _validate(fields: dict[str, dict]) -> list[dict]:
    checks: list[dict] = []
    totals = fields["totals"]["value"]
    grand = fields["grand_total"]["value"]
    if totals and grand and grand.get("小写") is not None:
        s = round((totals["金额合计"] or 0) + (totals["税额合计"] or 0), 2)
        checks.append({
            "check": "金额合计 + 税额合计 == 价税合计(小写)",
            "expected": grand["小写"], "actual": s,
            "pass": abs(s - grand["小写"]) < 0.005,
        })
    items = fields["items"]["value"]
    if items and totals:
        item_sum = round(sum(it["金额"] or 0 for it in items), 2)
        checks.append({
            "check": "明细金额之和 == 金额合计",
            "expected": totals["金额合计"], "actual": item_sum,
            "pass": abs(item_sum - totals["金额合计"]) < 0.005,
        })
        item_tax = round(sum(it["税额"] or 0 for it in items), 2)
        checks.append({
            "check": "明细税额之和 == 税额合计",
            "expected": totals["税额合计"], "actual": item_tax,
            "pass": abs(item_tax - totals["税额合计"]) < 0.005,
        })
    return checks


# ---------------------------------------------------------------------------
# family dispatch
# ---------------------------------------------------------------------------

def _detect_family(anchors: AnchorResult) -> tuple[str, str]:
    """Pick the template family from anchor evidence (never file names).

    Zero hits on both sides defaults to the Chinese family — the
    historically supported template — with an honest note, so callers
    keep the old behavior instead of an error.
    """
    cn = anchors.stats.get("chinese_hits", 0)
    west = anchors.stats.get("western_hits", 0)
    if west and not cn:
        return FAMILY_WESTERN, "锚点证据：西式命中 %d、中文命中 0" % west
    if cn and not west:
        return FAMILY_CHINESE, "锚点证据：中文命中 %d、西式命中 0" % cn
    if west > cn:
        return FAMILY_WESTERN, "锚点证据：西式 %d > 中文 %d（混合，按多数）" % (west, cn)
    if cn > west:
        return FAMILY_CHINESE, "锚点证据：中文 %d > 西式 %d（混合，按多数）" % (cn, west)
    return FAMILY_CHINESE, "锚点证据：双侧零命中（或持平），按默认中文模板解析"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def extract_invoice_fields(text: str, filename: Optional[str] = None) -> dict:
    """Extract structured fields from Provendoc-parsed invoice text.

    Template family is dispatched on anchor evidence (see module
    docstring).  Both families produce the same envelope: per-field
    ``{value, confidence, source, note?}`` under the same ten field
    keys (family-specific value shapes documented per field), a
    ``validation`` list of arithmetic cross-checks, and an ``evidence``
    summary of which text tiers and anchors were usable.

    Parameters
    ----------
    text : str
        ``parse(path).document.text`` for an invoice.
    filename : str, optional
        Source file name; used only as a *fallback evidence tier* for
        the Chinese-family invoice number (many billing systems embed
        it in the file name), and only with cross-verification notes.

    Returns
    -------
    dict with ``fields`` / ``validation`` / ``evidence``.
    """
    body, chart = _split_evidence(text)
    anchors = extract_invoice_anchors(body)
    arbitration = arbitrate_anchors(anchors)
    family, family_reason = _detect_family(anchors)

    if family == FAMILY_WESTERN:
        line_items = extract_line_items(body)
        fields = _extract_western_fields(body, arbitration, line_items)
        validation = _western_validate(fields)
    else:
        buyer, seller = _extract_parties(body, anchors)
        fields = {
            "title": _extract_title(body),
            "invoice_number": _extract_invoice_number(body, chart, filename),
            "issue_date": _extract_issue_date(body, chart),
            "buyer": buyer,
            "seller": seller,
            "items": _extract_items(body),
            "totals": _extract_totals(body),
            "grand_total": _extract_grand_total(body),
            "drawer": _extract_drawer(body),
            "remark": _extract_remark(body),
        }
        validation = _validate(fields)

    return {
        "fields": fields,
        "validation": validation,
        "evidence": {
            "family": family,
            "family_reason": family_reason,
            "family_evidence": {
                "chinese_anchor_hits": anchors.stats.get("chinese_hits", 0),
                "western_anchor_hits": anchors.stats.get("western_hits", 0),
            },
            "arbitration_stats": dict(arbitration.stats),
            "body_chars": len(body),
            "chart_chars": len(chart),
            "chart_text_usable": bool(chart.strip()),
        },
    }


def flatten(result: dict) -> dict:
    """Convenience view: field name -> plain value (null when missing)."""
    return {k: v["value"] for k, v in result["fields"].items()}
