# ============================================================
# Narrative Domain
# Domain-family document signals (shared text-layer detector)
# ============================================================
"""Domain-family text-convention detection: contracts, medical reports,
drug instructions, invoices, academic papers, financial statements,
tech standards (RFC-style documents), email and announcement
conventions (added in extractor v2, 2026-10-14), exam papers
(added in extractor v6,
2026-11-08, exam-vs-questionnaire boundary), government
administration documents (added in extractor v7, 2026-11-11,
gov-doc vs announcement/form/regulation-explainer boundary), insurance-clause contract
markers that promote contract out of the watchlist (added in
extractor v8, 2026-11-11, iachina 30-clause GT + two-sided
negative-survey calibration) and English lab-report markers that
promote medical_report out of the watchlist (added in extractor v9,
2026-11-13, Mendeley 260-scan GT + two-sided negative census; the
same round demotes the English signature kinds to weak for contract
after the first full-chain negative pass exposed 7 letter-novel /
sphinx-doc quorum flips).  The two v2 families passed their first
corpus calibration on 2026-10-15 (4533-file scan, numbers recorded
in specs/domain_signals_calibration_2026-10-10/README.md round5):
two marker guards landed with that pass — the announcement closing
formula is line-anchored (inline novel dialogue 「招為婿，特此通知。」
used to fire the strong kind at min_count=1) and email from/to
headers must carry an addr-spec (business_records invoice party blocks
"FROM:  W" / "TO: C" used to feed the email quorum and steal the
invoice label).  Extractor bumped to v3 for the marker changes.

Families are detected from *text conventions only* — marker words,
full-line 【…】 sections, abnormal-result arrows, approval numbers,
RFC header lines, email header lines — never from guessed document
types.  The detector
records counts and bounded samples for every family; ``present`` is a
deterministic quorum (by default >= 2 distinct marker kinds), and
"strong" single kinds (a 国药准字 approval number, an RFC header line,
2+ non-RFC DOIs, a Message-ID header) qualify a family alone because
they are near-decisive
strings.

Marker tables were calibrated offline against the real corpus
(2026-10-10, 4533 files: arxiv/pmc papers, business_records business documents,
RFCs, gutenberg prose, thucnews, readmes, licenses, sphinx docs).
Each pattern carries the false-positive it is shaped to avoid; do not
loosen a pattern without re-running the corpus calibration.  The
v2 families (email / announcement) are exempt only until their
first corpus pass — the exemption is recorded in the payload note,
not silently assumed.

Fact boundaries (documented, not guessed):

- this is convention evidence, not a document-type claim: a novel that
  happens to contain the word "signature" twice records the counts but
  does not meet the quorum and stays ``present: false``
- evidence overlap between families is allowed when conventions are
  genuinely shared: RFCs carry abstracts, references and DOIs, so the
  academic_paper family fires on some RFCs.  The decision layer
  resolves this by specificity (a strong tech_standard header wins);
  this module never suppresses honest evidence
- weak kinds (e.g. bare currency amounts) are recorded but never vote
  toward the quorum: a price is not a financial statement
- some kinds only vote above a minimum count (e.g. author-year
  citations >= 3): a single "(Smith, 1998)" is prose, three of them
  are a citation convention
- markers are counted over the extracted text as-is; no line is
  reinterpreted, promoted or demoted (heading recovery stays in
  heading_patterns; this module never touches structure)
- samples are the first few distinct matched strings, bounded in
  count and length; the full counts are always recorded alongside

Output shape (``metadata["domain_signals"]``)::

    {
        "extractor": "domain_signals_v1",
        "families": {
            "contract": {
                "present": bool,          # quorum decision
                "distinct_kinds": int,    # all kinds with count > 0
                "voting_kinds": int,      # kinds eligible to vote
                "total_count": int,
                "marker_counts": {label: count},
                "sample_matches": [str, ...],
            },
            ...
        },
        "note": "...",
    }
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------

_MAX_SAMPLE_MATCHES = 5
_MAX_SAMPLE_CHARS = 40


# ---------------------------------------------------------------------------
# marker tables
# ---------------------------------------------------------------------------

# Contracts: signature/seal conventions plus insurance-clause
# vocabulary.  签署日期/签订日期 are signing-date conventions; the
# underscore line is the blank signature field; 甲方/乙方 are the
# canonical Chinese contracting parties.  v8 (2026-11-11) adds the
# insurance kinds that promote contract from the watchlist to eligible:
# 投保人|被保险人|保险人|受益人 are the party appellations of insurance
# clauses, 保险金额/保险费/保险责任/犹豫期/免赔率 the term vocabulary.
# Alternation order matters: 被保险人 must precede 保险人 or the short
# form eats the long one's count.
# NOTE: inline (?i) flags must sit at the start of the pattern (Python
# 3.11+ rejects them mid-expression).
_CONTRACT_MARKERS = {
    "signature_words_zh": re.compile(r"签字|签名|签章|盖章|签署"),
    "signature_words_en": re.compile(r"(?i)\bsignature\b|\bsigned?\s+by\b|\bsign\s+here\b"),
    "contracting_parties": re.compile(r"甲方|乙方|丙方|见证方|委托方|受托方|出让方|受让方"),
    "seal_words": re.compile(r"(?i)合同专用章|公章|专用章|\bcompany\s+seal\b"),
    "signature_line": re.compile(r"_{4,}"),
    "signing_date": re.compile(r"(?i)签署日期|签订日期|签字日期|\bdated\s+this\b"),
    "insurance_parties": re.compile(r"投保人|被保险人|保险人|受益人"),
    "insurance_terms": re.compile(r"保险金额|保险费|保险责任|犹豫期|免赔率"),
}

# Medical lab & checkup reports (体检报告 / 检验报告): reference-range
# headers, abnormal-result arrows, report/reviewer conventions.
# v9 (2026-11-13, p11 medical promotion — first English-body
# calibration, Mendeley 260-scan lab-report GT).  Three two-sided
# measured changes (p11 negative raw-text survey + full-chain
# negative census; numbers recorded in
# specs/domain_signals_calibration_2026-10-10/README.md):
# - reference_range takes the OCR abbreviation shapes "Ref. Range" /
#   "Ref,Range" / "Ref Range" — the v8 form missed the column header
#   the OCR actually emits (68/93 of the early corpus); FP=0 across
#   1464 negative files.
# - lab_departments ("Microbiology Unit", "Urine Analysis",
#   "Hormone Unit", biochem/haematology …): department
#   headers are a lab-report-only convention.  The patholog\w*
#   stem was dropped after the full-chain negative census: three
#   gutenberg biography dictionaries ("pathologist" profession
#   entries, "Pathological" subject headings — popular_19146 / 57724
#   / 71046) pair it with "reviewed by" reviewer_terms to reach
#   quorum.  Measured cost on the positives: zero — the stem fires
#   in only 4/260 scans (pathology x3, pathologists x1) and all
#   four keep quorum through other kinds (232/260 unchanged;
#   marker-sim recount over the cached OCR texts — recorded in
#   specs/domain_signals_calibration_2026-10-10/README.md,
#   p11 commit 7ea0dbcf).
# - result_table_headers: the Test…Result…Unit/Range header triplet
#   within 40 chars — the analytic-table shape.  The loose "test
#   result" phrase was rejected: it fires in sphinx changelog prose
#   (3 negative files).
# Dropped candidates (measured, not guessed): bare "specimen"
# (15 gutenberg prose hits, 0 positive hits in the corpus),
# "normal range" (2 positives), "reporting date" (1 positive) —
# no recall value for the FP surface they open.
_MEDICAL_REPORT_MARKERS = {
    "reference_range": re.compile(
        r"(?i)参考区间|参考范围|参考值|reference\s+(range|interval)"
        r"|\bref[.,]?\s*(range|interval)\b"
    ),
    "abnormal_arrows": re.compile(r"[↑↓]"),
    "report_terms": re.compile(r"(?i)检验报告|体检报告|化验单|检验科|\blab(?:oratory)?\s+report\b"),
    "reviewer_terms": re.compile(
        r"(?i)检验者|审核者|报告医师|检验医师|采集时间|报告日期|\breviewed\s+by\b"
    ),
    "lab_departments": re.compile(
        r"(?i)\b(?:haematolog\w*|hematolog\w*|biochem\w*|microbiolog\w*"
        r"|serolog\w*|virolog\w*|immunolog\w*|urinalysis"
        r"|urine\s+analysis|coagulation)\w*"
    ),
    "result_table_headers": re.compile(
        r"(?i)\btest\b[\s\S]{0,40}\bresult\b[\s\S]{0,40}\b(?:unit|range|flag)\b"
    ),
}

# Drug instructions: full-line 【…】 sections (already a
# heading convention elsewhere — here they are counted as family
# evidence), the near-decisive 国药准字 approval number, usage and
# safety section vocabulary.  [^】\n] / [ \t] keep the pattern inside a
# single line: a \s* next to ^/$ can consume the newline itself and
# backtrack quadratically on whitespace-ocean inputs (st2_19 hang,
# 2026-10-10 scan).
_DRUG_INSTRUCTION_MARKERS = {
    "bracket_sections": re.compile(r"^【[^】\n]{1,30}】[ \t]*$", re.MULTILINE),
    "approval_number": re.compile(r"国药准字[A-Z]\d{8}|批准文号"),
    "usage_terms": re.compile(r"用法用量|用法与用量|药物相互作用"),
    "safety_terms": re.compile(r"不良反应|禁忌|注意事项|孕妇及哺乳期妇女用药"),
}

# Invoices: field-label conventions, calibrated 2026-10-10
# against business_records business documents (recall) and gutenberg/thucnews
# prose (precision).  Field labels are anchored to a line start and/or
# a trailing colon because the same words occur in prose ("paid the
# bill to the innkeeper"; "days before the date of issue, Pierre…").
# 2026-10-27 (v4, camera-scan invoice evidence): camera-scan OCR chews
# off half the label ("名称"→"称:", "纳税人识别号"→"WHRARBMS:") so the
# label-shaped markers all miss; two OCR-robust witnesses were added —
# the party-column hard fields 开户行及账号/地址电话 (fully present in
# the damaged samples) and the unified social credit code value shape
# (fixed 18 chars; chewing the label cannot chew the value). Loose-bucket
# precision must be re-verified via the calibration harness
# (specs/domain_signals_calibration README discipline).
# 2026-11-01 (explainer_docs evidence): the **line-start shape** of the
# English invoice-number field label. The label must be immediately
# followed by a separator (: ： |) or a value; a blank must precede the
# label, otherwise [ \t]* eats the "The " of "The invoice number..." as a
# prefix word and the narrative sentence matches again. Prefix words are
# limited to a few capitalized words on one line ("Proforma Invoice No:"
# / "Tax Invoice No:"); prose line starts never have this shape.
#
# 2026-11-01 (business_records holdout evidence): the label must be
# **case-sensitive**. The (?i:) variant also accepts lowercase narrative
# — "The invoice number is issued" was taken for a field, and that is
# exactly the identity-strong evidence source of this marker. Real
# ticket field labels are written "Invoice" / "INVOICE"; a lowercase
# "invoice" mid-sentence is a word, not a label.
# The same evidence batch also requires two things:
#   - prefix words allow digits and separators (OCR line "Invoice Date
#     DECL1/01 INVOICE Number 3140", where DECL breaks [A-Za-z]+ and the
#     whole line used to miss);
#   - the label may be directly followed by a number ("INVOICE
#     PR12-018795"), but the value must contain a digit and be >= 4
#     chars, else "An Invoice is a document" would be eaten as a field.
#
# 2026-11-01 (business_records schedule-sheet/contract evidence): two
# reverse constraints.
#   - the prefix-word quantifier must be {1,3}, not *: with zero prefix
#     words allowed, table pipes at line start ("| DAYS OF | DAYS" /
#     "| MC | LN") were taken for field starts, and schedule sheets and
#     radio contracts were wholesale promoted to invoice (8 flips, all
#     from such tables).
#   - a real blank must precede the pipe separator: the middle | of
#     "DAYS OF | DAYS" is a column separator, not a field separator;
#     only a colon may sit flush against the label.
_INVOICE_EN_PREFIX = (
    r"(?:[A-Z][A-Za-z0-9]*(?:[ \t]*[/\-#][ \t]*[A-Za-z0-9]+)*[ \t]+){1,3}"
)
_INVOICE_EN_LABEL = r"(?:Invoice|INVOICE|Inv\.?|INV)(?![A-Za-z0-9])"
_INVOICE_EN_WORD = r"[ \t]*(?:No\.?|NO|Number|NUMBER|#)(?![A-Za-z])"
_INVOICE_EN_VALUE = r"#?[ \t]*[A-Za-z0-9][A-Za-z0-9\-/]*(?![A-Za-z0-9\-/])"
_INVOICE_EN_BARE_VALUE = (
    r"(?=[A-Za-z0-9\-/]*[0-9])#?[ \t]*[A-Za-z0-9][A-Za-z0-9\-/]{3,}(?![A-Za-z0-9\-/])"
)
_INVOICE_EN_SEP = r"[:：]|[ \t]\|"
_INVOICE_EN_FIELD = (
    r"[ \t]*\|?[ \t]*"
    + r"(?:"
    + _INVOICE_EN_PREFIX
    + r")?"
    + r"(?:"
    # label + separator (+ optional value): "Invoice #:" / "| Invoice # |".
    # A bare label with trailing whitespace and no separator (an "Invoice
    # Number" table header) is not a field.
    + _INVOICE_EN_LABEL
    + _INVOICE_EN_WORD
    + r"[ \t]*(?:"
    + _INVOICE_EN_SEP
    + r")\s*(?:"
    + _INVOICE_EN_VALUE
    + r"|(?=\||$|\)))?"
    # label + whitespace + value (no separator): "Invoice No. 12345"
    + r"|"
    + _INVOICE_EN_LABEL
    + _INVOICE_EN_WORD
    + r"[ \t]+"
    + _INVOICE_EN_VALUE
    # label directly followed by a number: "INVOICE PR12-018795"
    + r"|"
    + _INVOICE_EN_LABEL
    + r"[ \t]+"
    + _INVOICE_EN_BARE_VALUE
    # label with zero whitespace glued to a # number: "INVOICE #99-D08025A".
    # After OCR swallows the "NO" of "INVOICE NO 99-D08025A" only this
    # shape remains — it is the sole field evidence on a real ticket and
    # must be accepted. Constraints stay strict: # required and value
    # 4+ chars, so residue like "Invoice No." is never eaten.
    + r"|"
    + _INVOICE_EN_LABEL
    + r"[ \t]?#[ \t]*[A-Za-z0-9][A-Za-z0-9\-/]{3,}(?![A-Za-z0-9\-/])"
    + r")"
)

# 2026-11-01 (explainer_docs evidence): shape-purity table.
#
# Some markers accept two shapes at once: the **field label** of a real
# document ("开票日期:" / "价税合计 ¥1,234.00") and the **term mention**
# in prose ("服务日期早于开票日期是常见的审计疑点"). The difference is
# not the word — it is whether a value follows it.
#
# Articles that explain invoices necessarily mention these terms yet
# only give label shapes inside quote blocks, so quoted_only judged the
# family's evidence "valid". This table gives each such marker a
# **shape-pure variant**: only when it also matches does that match
# count as quoted-region evidence.
# Full marker_counts are unaffected (the extraction layer still honestly
# records "what was seen").
# Decided with match() — it eats the exact text this marker already
# matched, no fresh full-text scan.
_CN_VALUE_TAIL = r"(?:[:：|]|[ \t]+(?:[¥￥]|[0-9])|[ \t]*[¥￥])"
_SHAPE_PURE: dict[str, re.Pattern[str]] = {
    # Chinese date label: "开票日期:" / "开票日期 2026-01-05" / "开票日期 | …".
    # In prose "…早于开票日期是…" the next word is "是", not a value.
    "invoice_date_field": re.compile(r"开票日期" + _CN_VALUE_TAIL),
    # Chinese tax-id label: "纳税人识别号:" / "| 纳税人识别号 | 9111…".
    # The code value shape is separately witnessed by cn_credit_code.
    "tax_fields": re.compile(
        r"(?:纳税人识别号|统一社会信用代码)" + _CN_VALUE_TAIL
        + r"|(?i:\btax\s+id\b|\bvat\s*(?:no\.?|number|id|reg(?:istration)?)\b)"
        r"[ \t]*[:：|]"
    ),
    # Chinese total label: "价税合计:" / "价税合计（大写）：" / "价税合计 ¥1,234.00".
    # In prose "…价税合计三种面孔…" what follows is not a value.
    "total_fields": re.compile(
        r"(?:价税合计|合计金额)(?:[ \t]*[（(][^）)\n]{1,12}[）)])?" + _CN_VALUE_TAIL
    ),
}

_INVOICE_MARKERS = {
    # 2026-11-01 (explainer_docs evidence): invoice_word only claims "this
    # document mentions invoices", not "this document is an invoice" —
    # articles explaining invoices necessarily carry the word in body
    # text. It counts toward records and quorum (same "mention" tier as
    # email's header_from) but never participates in the quoted_only
    # decision (see _MENTION_ONLY_KINDS).
    "invoice_word": re.compile(r"(?i)\binvoices?\b|发票"),
    "invoice_number_field": re.compile(
        # 2026-10-13 (western_invoice_tablerow_prereg_v1 §1.A): a trailing
        # \b never holds between "#" (non-word char) followed by a
        # space/colon/pipe (also non-word) — "Invoice # 2081683-1" /
        # "Invoice #:" / "| Invoice # |" all missed (same family of bug
        # as the anchor layer).
        #
        # 2026-11-01 (explainer_docs evidence): the English branch used to
        # match on a bare word and so ate narrative sentences too — "The
        # invoice number is issued, not invented" used to hit this
        # marker, giving an explainer article identity-declaring strong
        # evidence. The only difference between a field label and a
        # narrative sentence is that **a separator immediately follows the
        # label**, so every branch must end in a separator (: ： | /
        # whitespace before the value / end of line); bare-word shapes are
        # never accepted.
        #
        # A blank must precede the label: [ \t]* eats the "The " of
        # "The invoice number..." as a prefix word, and the narrative
        # sentence matches again. Prefix words are limited to at most two
        # all-caps words on one line ("Proforma Invoice No:" / "Tax
        # Invoice No:"); prose never has this shape.
        #
        # No global (?im) inline flag: it must sit at the very start of
        # the whole expression and raises re.error mid-alternation;
        # re.MULTILINE is passed at compile time instead.
        rf"^[ \t]*(?:发票号码|发票代码)(?:[ \t]*[:：]|[ \t]+[0-9]|[ \t]*$)"
        rf"|^{_INVOICE_EN_FIELD}"
        # Inline shapes ("Total Invoice No:" / "编号 Invoice No:") are
        # likewise case-sensitive and must end in a colon or pipe, so the
        # narrative sentence "The invoice number is issued..." has no
        # separator to end in and never matches.
        r"|(?:Invoice|INVOICE)[ \t]*(?:No\.?|NO|Number|NUMBER)[ \t]*(?::|：|\|)",
        re.MULTILINE,
    ),
    # colon required for the english fields: "the date of issue" is
    # ordinary prose without one
    "invoice_date_field": re.compile(
        r"(?i)开票日期|(?:\binvoice\s+date|\bbilling\s+date|\bdate\s+of\s+issue)[ \t]*[:：]"
    ),
    # line-start + colon: a "Bill To:" field label, never prose
    # 2026-10-27 (v4): 开户行及账号/地址电话 — hard field labels of the
    # Chinese invoice party column, fully surviving OCR damage
    # ("| 2 站开户行及账号:"); the words are distinctive enough to skip
    # a line-start anchor and barely appear in loose buckets.
    "parties_fields": re.compile(
        r"(?im)购买方|销售方|开户行及账号|地址电话|^[ \t]*(?:bill|ship|remit|sold)\s+to[ \t]*[:：]"
    ),
    # "tax id" not "tax identification": the Project Gutenberg license
    # boilerplate says "tax identification number" in every ebook
    # 2026-11-01 (explainer_docs evidence): "Tax Invoice" is a term, not
    # a field — articles explaining invoices are literally titled "Reading
    # a Tax Invoice". It testifies only as a **label**: line start or
    # table cell, followed by a separator or value; bare inline mentions
    # are re-checked by the quoted-region + _SHAPE_PURE layer.
    "tax_fields": re.compile(
        r"纳税人识别号|统一社会信用代码"
        r"|^[ \t]*(?:\|[ \t]*)?(?i:tax\s+id|tax\s+invoice|vat\s*(?:no\.?|number|id|reg(?:istration)?)|GSTIN)\b"
        r"|\btax\s+id\b|\bvat\s*(?:no\.?|number|id|reg(?:istration)?)\b|\bGSTIN\b",
        re.MULTILINE,
    ),
    # 2026-10-27 (v4, camera-scan invoice evidence): unified social
    # credit code value shape (GB 32100-2015: fixed 18 chars, leading 9X,
    # charset excluding I/O/S/V/Z).
    # Value-shape witness: after OCR chews off the "纳税人识别号" label
    # (sample read "WHRARBMS:") the 18-char code is still fully present.
    # Fixed length + first-two-char gate + charset noise exclusion make
    # a prose false hit nearly impossible; the lookarounds on both sides
    # reject segments embedded in longer alphanumeric runs.
    "cn_credit_code": re.compile(
        r"(?<![0-9A-Za-z])9[0-9][0-9A-HJ-NP-RTUWXY]{16}(?![0-9A-Za-z])"
    ),
    "total_fields": re.compile(
        r"(?i)价税合计|合计金额|\bamount\s+due\b|\bbalance\s+due\b"
        r"|\btotal\s+(?:due|inc(?:luding)?\.?\s*(?:tax|vat|gst))\b"
        # "Invoice Total:" field-label shape — invoice total is rare in
        # prose and must pass the quorum's two-kinds gate anyway
        r"|\binvoice\s+total\b"
    ),
    "issuer_fields": re.compile(r"校验码|机器编号|收款人|开票人|复核"),
}

# Academic papers (txt_engineering_gaps_prereg_v1 G7),
# calibrated 2026-10-10 against arxiv pdf (50/50), pmc (132/133) and
# arxiv tex (project-level 92.9%; file-level misses are multi-file
# project fragments like __macros.tex that carry no conventions of
# their own).  Bracket citations [12] are deliberately NOT a marker:
# RFCs and tech docs share that convention, so it discriminates
# nothing.  [ \t] not \s* near ^/$: \s* can swallow newlines and
# backtrack quadratically on whitespace-ocean inputs (st2_19 hang,
# 2026-10-10 scan).
_ACADEMIC_PAPER_MARKERS = {
    "abstract_heading": re.compile(r"(?im)^[ \t]*(?:abstract|摘\s*要)[ \t]*[:：]?[ \t]*$"),
    # colon-only + capital K: prose lines like "arbitrary keywords."
    # end with a period and are not keyword lines
    "keyword_line": re.compile(r"(?m)^[ \t]*(?:Keywords?|关键词|关键字)[ \t]*[:：]"),
    "references_heading": re.compile(r"(?im)^[ \t]*(?:参考文献|references|bibliography)[ \t]*$"),
    # comma + 19xx/20xx required: novels cite people as "(Byron, 1788)"
    # and scholarly books as "(Koln, 1889)"; real citations are modern
    "author_year_citations": re.compile(
        r"\([A-Z][A-Za-zÀ-ÿ'\-]+(?:\s+et\s+al\.?)?,\s*\(?(?:19|20)\d{2}[a-z]?\)"
    ),
    "tex_paper_commands": re.compile(
        r"\\(?:cite|bibliography|bibliographystyle|begin\{abstract\}|begin\{thebibliography\})"
    ),
    # RFC DOIs (10.17487/…) excluded: they mark standards, not papers
    "doi": re.compile(r"(?i)\b10\.(?!17487/)\d{4,9}/\S+"),
    "arxiv_id": re.compile(r"(?i)\barXiv:\d{4}\.\d{4,5}"),
}

# Financial statements (txt_engineering_gaps_prereg_v1
# G7), calibrated 2026-10-10.  Only statement vocabulary votes: bare
# currency/thousands amounts fire in 57-100% of novels and are
# recorded as weak kinds (they never qualify the family).  Recall is
# unverifiable on the current corpus (no real statement documents);
# precision is 0 FP across all prose buckets.
_FINANCIAL_STATEMENT_MARKERS = {
    "statement_terms_zh": re.compile(
        r"资产负债表|利润表|现金流量表|财务报表|合并报表|年度报告|年报"
    ),
    "statement_terms_en": re.compile(
        r"(?i)\bbalance\s+sheet\b|\bincome\s+statement\b|\bcash\s+flow\s+statement\b"
        r"|\bannual\s+report\b|\bstatement\s+of\s+(?:financial\s+position|cash\s+flows"
        r"|comprehensive\s+income)\b"
    ),
    # weak kinds below: recorded, never voting (see _WEAK_KINDS)
    "currency_amounts": re.compile(r"[¥￥$€]\s?\d[\d,]*(?:\.\d{1,2})?"),
    "thousands_amounts": re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b"),
    "amount_words_zh": re.compile(r"人民币|金额|余额|元整|大写"),
}

# Tech standards (RFC-style standards documents), added 2026-10-10.  This
# family exists so the decision layer can resolve the genuine
# academic_paper ∩ RFC convention overlap by specificity: RFCs carry
# abstracts, references and DOIs, but their header lines are decisive.
_TECH_STANDARD_MARKERS = {
    "rfc_header": re.compile(r"(?im)^[ \t]*Request for Comments:\s*\d+"),
    "category_line": re.compile(
        r"(?im)^[ \t]*Category:\s*(?:Standards Track|Informational"
        r"|Experimental|Best Current Practice|Historic)"
    ),
    "stream_line": re.compile(r"(?im)^[ \t]*(?:Stream|Obsoletes|Updates):"),
    "internet_draft": re.compile(r"(?i)\binternet-draft\b"),
    "issn_line": re.compile(r"(?im)^[ \t]*ISSN:\s*2070-1721"),
    # pre-1989 rfcs open with a bare "RFC:  791" line and carry no
    # "Request for Comments:" header at all
    "rfc_bare_header": re.compile(r"(?m)^RFC:[ \t]*\d{3,4}[ \t]*$"),
    # 1980-era layout: "RFC 768                J. Postel" — the number
    # is followed by a name after wide spacing; prose like "RFC 9000
    # specifies QUIC" uses a single space and must not match
    "rfc_number_line": re.compile(r"(?m)^RFC[ \t]+\d{3,4}(?:[ \t]{2,}\S|[ \t]*$)"),
}

# Email: RFC 5322 header-line conventions. Line-start +
# colon + non-empty value is the shape — prose sentences never start
# a line with "From:" followed by content. YAML front matter in
# markdown notes may carry a lone "date:" but a single kind never
# reaches the quorum; Message-ID with its angle-bracket form is
# near-decisive and is the family's strong kind.
# 2026-10-15 (business_records evidence): invoice party columns
# "FROM:  W" / "TO: C" (no addr-spec) used to feed the email quorum
# and steal the label from invoice in the 2-2 tie by alphabetical
# order. from/to header lines now require an inline @: RFC 5322 from/to
# header values always contain an addr-spec; invoice party columns and
# OCR fragments do not.
_EMAIL_MARKERS = {
    "header_from": re.compile(r"(?im)^[ \t]*from:[^\n]*@"),
    "header_to": re.compile(r"(?im)^[ \t]*to:[^\n]*@"),
    "header_subject": re.compile(r"(?im)^[ \t]*subject:[ \t]+\S"),
    "header_date": re.compile(r"(?im)^[ \t]*date:[ \t]+\S"),
    "message_id": re.compile(r"(?im)^[ \t]*message-id:[ \t]*<[^>\n]+>"),
    "quoted_reply": re.compile(r"(?im)^[ \t]*(?:>+[ \t]*)*.{0,200}\bwrote:\s*$"),
}

# Announcements: Chinese public-notice conventions.  特此公告
# closing formula is near-decisive (strong kind); a bare line-initial 公告
# heading alone is not — "公告" also appears inside prose, so the
# heading kind votes only toward the quorum like any other kind.
# 2026-10-15 (gutenberg_zh evidence, zh_shuihu_houzhuan): inline
# dialogue 「招為婿，特此通知。」 used to fire the strong kind outright
# at min_count=1. The closing formula is now whole-line anchored
# (trailing punctuation / carriage returns allowed): a real
# announcement's closing stands alone on its own line, while the
# novel-quote 特此通知 sits mid-sentence; "特此通知如下：" is an opening
# formula, not a closing, and likewise does not match — exactly the
# distinction wanted.
_ANNOUNCEMENT_MARKERS = {
    "closing_formula": re.compile(
        r"(?m)^[ \t]*(?:特此公告|特此通告|特此通知|特此说明)[ \t]*[。．.!！]?[ \t\r]*$"
    ),
    "announcement_heading": re.compile(
        r"(?im)^[ \t]*(?:公告|通告|公示|通报|讣告)[ \t]*[:：]?[ \t]*$"
    ),
    "issuance_fields": re.compile(
        r"(?im)^[ \t]*(?:发布日期|公告日期|发布时间|发布单位|印发日期|印发单位)[ \t]*[:：]"
    ),
    "document_number": re.compile(
        r"(?im)^[ \t]*(?:文号|通告字|公告字|通报字)[ \t]*[:：]"
    ),
    "formal_opening": re.compile(r"兹定于|兹因|兹有|现将有关|现将"),
    "forwarded_notice": re.compile(r"(?im)^[ \t]*转发[ \t]*[:：]?[ \t]*\S"),
}

# Exam papers, added 2026-11-08.  The exam-vs-questionnaire
# boundary is the design spine: exams carry exam apparatus (point values,
# score blanks, student identity fields, session discipline formulas,
# self-naming titles), questionnaires have numbered stems but never
# any of those.  Calibrated 2026-11-08 in tmp_survey/exam_marker_probe.py
# (v0→v3) against: GT mit_ocw_exams 21/26 + nysed_regents_exams 22/22;
# questionnaire boundary (brfss 19 / gss 21 / annotated_forms 62) all
# zero flips; 12 FP-guard scenario buckets all zero.
#
# Marker-level FP notes (do not loosen without re-calibration):
#   - the three numbering kinds (question_numbering / subitem_letters /
#     option_lines) are WEAK — in the v0 probe every questionnaire/paper FP
#     was driven by them (BRFSS 13/19, arxiv_pdf 26/50).  Question
#     stems and reference lists share the numbering convention; only
#     counted, never voting.
#   - exam_title requires the number on the SAME line: BRFSS
#     "routine exam\n1" answer options fed the old \s* form (8
#     partials, v2→v3 fix).  Bare line-start "Examination" (gutenberg
#     prose) and bare "midterm" (midterm elections) stay excluded.
#   - instruction_conventions excludes bare open/closed-book prose
#     (gutenberg_epub novels hit 20/311 on the loose v0 form).
#   - gutenberg novels still record "passed my final exam" prose on
#     exam_title (9 partials) — single kind, never reaches quorum on
#     prose buckets.
#
# Honest gap (registered, do not "fix" by loosening): MIT 18.01
# exam1/2/3/4/final are bare problem sheets — no title, no point
# values, no session discipline; text-convention-wise they are
# indistinguishable from textbook exercise pages.  Registered in
# scenario/README known-boundaries.
_EXAM_MARKERS = {
    # an exam names itself (Final Exam / Quiz 1 / Exam 1 / Regents
    # examination lines / 期末考试). "Exam N" must be same-line.
    "exam_title": re.compile(
        r"(?im)^[^\n]{0,40}\b(?:final\s+exa(?:m|mination)"
        r"|midterm\s+exa(?:m|mination)|quiz[ \t]*\d|exam[ \t]*\d{1,2}\b"
        r"|exa(?:m|mination)\s+booklet"
        r"|regents\s+exa(?:m|mination)"
        r"|(?:期末|期中|模拟|升学|毕业)(?:考试|试卷)|模拟卷|入学考试)"
    ),
    # point/credit annotations with explicit units; questionnaires
    # never assign marks per question
    "points_annotation": re.compile(
        r"(?i)\(\s*\d{1,3}\s*(?:points|pts|分)\s*\)|\[\s*\d{1,3}\s*(?:points|pts|分)\s*\]"
        r"|\b(?:worth|receive\w*)\s+\d{1,3}\s+credits?\b"
        r"|每[题问]\s*\d{1,3}\s*分|共\s*\d{1,3}\s*分"
    ),
    # bare per-item point values without the unit word: "a) (6) Express",
    # "Problem 2. (10) On the contour plot" (18.02 exams)
    "per_item_points": re.compile(
        r"(?im)^(?:[ \t]*[a-e]\)?[ \t]*\(\s*\d{1,3}\s*\)[ \t]+\S"
        r"|[ \t]*Problem\s+\d{1,2}\s*[.:]\s*\(\s*\d{1,3}\s*\))"
    ),
    # per-question score blanks: "_______/15", "__/48"
    "score_blank": re.compile(r"_{2,}\s*/\s*\d{1,3}\b"),
    # US exam-booklet opening formula (near-decisive strong kind)
    "exam_booklet_formula": re.compile(
        r"(?i)do\s+not\s+open\s+this\s+examination\s+booklet"
    ),
    # exam-session discipline formulas
    "instruction_conventions": re.compile(
        r"(?i)\bno\s+calculators?\b|\bshow\s+(?:all\s+)?your\s+work\b"
        r"|\bexplain\s+your\s+answer|\bjustify\s+your\s+(?:answer|reasoning)\b"
        r"|\banswer\s+all\s+(?:of\s+the\s+)?(?:questions|problems)\b"
        r"|\bscrap\s+paper\b|密封线|不得携带"
    ),
    # student identity block (form-generic: GSS respondent-name fields
    # hit this too, hence voting-only)
    "student_fields": re.compile(
        r"(?im)^[ \t]*(?:student\s+name|last\s+name|first\s+name"
        r"|candidate\s+name|考生姓名|准考证号|考\s*号|学\s*号)"
        r"[ \t]*[:：_]"
    ),
    # multiple-choice wording (voting only — education articles
    # mention the term without being exams)
    "multiple_choice": re.compile(r"(?i)\bmultiple[- ]choice\b|选择题|单项选择"),
    # zh question-type labels (X+题 compound words); min 2 to vote
    "question_types_zh": re.compile(
        r"选择题|填空题|判断题|简答题|名词解释|计算题|论述题|完形填空|作文题"
    ),
    # rhetorical "Question N" / "Problem N." stem headings — exams
    # and problem sets use them, questionnaires number stems bare
    "question_stem": re.compile(r"(?m)^Question\s+\d{1,2}\b[.:)]?\s"),
    "problem_stem": re.compile(r"(?m)^Problem\s+\d{1,2}\b[.:]"),
    # WEAK: exam papers, questionnaires, reference lists and equation
    # labels all share bare numbering — recorded, never voting
    "question_numbering": re.compile(r"(?im)^[ \t]*\d{1,2}[).]\s+\S"),
    "subitem_letters": re.compile(r"(?im)^[ \t]*\([a-h]\)\s+\S"),
    "option_lines": re.compile(r"(?im)^[ \t]*\(\d{1,2}\)\s+\S"),
}

# Government administration documents: regulations, rules,
# gazette-published notices, drafting statements), added 2026-11-11.
# The design boundary is gov-docs vs the three straddling genres it grows
# next to: corporate announcements (share the sectioning/sign-off
# conventions but never the article-clause/main-recipient/dispatch-number machinery), application
# forms (第X条 article lines can appear inside table-rules text but the
# doc is a form), and explainer prose about regulations (explainers —
# quotes article lines but as a sample, caught by the quoted-region
# layer like every other explainer).
#
# Calibrated 2026-11-11 (p8 probes v0→v6, commit 4c160f72) against
# the samr_gov_documents corpus (376 docx, samr.gov.cn notice listings):
# on the gov-narrative genre slice (278 files, forms 90 / TSG 8
# straddlers excluded by genre rule) production-chain coverage is
# 256/278 — strong 188 / quorum 68 / miss 22 (registered honestly in
# the bucket note, not
# trimmed: 附件1-type bare article lists, definition-style explainer-flavoured
# rules, gazette-3+分部-1 docs below both thresholds).  Chinese
# negative buckets (thucnews / gutenberg_zh / explainer / readme /
# license / sphinx / rfc) FP = 0 in the final probe round; the only
# mid-calibration FP (gutenberg 汉书 3 files, cn_section_head single
# kind) never reaches quorum and did not survive v5 tightening
# (min_count_to_vote = 2).
#
# 2026-11-11 (production-chain measurement): the first production run
# measured only 231/278 because the probe had calibrated on RAW docx XML
# text while the DocxParser heading-recovery layer emits heading lines
# with a markdown ``# `` prefix (「# 一、产品备案时…」). Every
# line-start-anchored marker missed its own heading lines. The shared
# heading-prefix tolerance ``(?:#{1,6}[ 	]+)?`` was added to the
# anchored kinds and the corpus re-measured on the production chain
# (numbers in the bucket note / ledger #208).  Standing rule: markers
# are calibrated on the PRODUCTION text, not on raw-format XML.
#
# gov-vs-announcement boundary (parse-side probe, 50 cninfo announcement PDFs):
# 34 announcements could have flipped gov quorum under the mid-round
#     v5 table via cn_section_head + preamble(特此公告); with
# preamble_formula narrowed (see below) 33 of them lose their second
# kind and the remaining 1 boundary case (gazette_number +
# cn_section_head) is registered in the bucket note.
#
# Marker-level notes (do not loosen without re-calibration):
#   - preamble_formula is the gov OPENING formula set, deliberately
#     WITHOUT 特此公告: the closing formula is the announcement
#     family's strong kind, and carrying it here too double-books the
#     same evidence into two families (34 cninfo announcements flipped
#     gov quorum via cn_section + 特此公告 in the v5 mid-round; 33/33
#     verified preamble-特此公告-only). A first decomposition
#     experiment concluded preamble could be removed with zero recall
#     loss — the production-chain reconciliation CORRECTED that: 3 SAMR
#     files (consultative_draft + preamble pairs) did depend on it on
#     raw text, so the non-特此公告 variants stay. Announcement side:
#     33/34 flips lose their second kind, the remaining boundary case
#     (gazette_number + cn_section_head) is registered in the bucket
#     note.
#   - article_formula is deliberately two-tier: a handful of 第X条
#     lines is weak (votes only, min 3) because explainer prose and
#     regulatory quote runs also carry them; >= 12 is a near-decisive
#     density (bare regulation bodies) and qualifies the family alone.
#   - article_gist_bracket (第X条【条旨】) is the legislative
#     drafting convention — 3+ of them is a real code/ordinance body.
#   - drafting_statement_heading is near-decisive at 1: a
#     「起草说明/关于《…》的说明」 title line is a document identity
#     claim, not prose.
#   - cn_section_head votes only at >= 2: a lone 「一、」 section
#     header appears in ordinary news/announcement prose (gutenberg
#     汉书 FP datum).
#   - gazette_number requires the 〔20xx〕 brack form: plain
#     「第123号」 sequences appear in serial-numbered prose.
_GOV_DOCUMENT_MARKERS = {
    # 第X条【条旨】 — legislative article lines carrying the gist
    # bracket; the bracket form is drafting machinery, prose never
    # has it.  Strong at >= 3.
    "article_gist_bracket": re.compile(
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?第[一二三四五六七八九十百零]+条[ \t]*[【\[]"
    ),
    # bare 第X条 article lines (no gist bracket); votes at >= 3,
    # near-decisive at >= 12 (a regulation body is dense with them)
    "article_formula": re.compile(
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?第[一二三四五六七八九十百零]+条(?![ \t]*[【\[])"
    ),
    "chapter_formula": re.compile(
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?第[一二三四五六七八九十百零]+章"
    ),
    # effective/annulment clauses: 自…起施行 variants + 自发布之日起实施
    "effective_clause": re.compile(
        r"自[^，。\n]{0,30}?起(?:施行|执行|试行|废止)|自发布之日起实施"
    ),
    # Drafting/revision/rationale statement title lines, or
    # 「关于《…》的说明」 title lines.
    # Strong at 1 — a drafting-statement self-naming title is a
    # document identity claim.
    "drafting_statement_heading": re.compile(
        r"(?m)(^[^\n]{0,40}(?:起草|修订|制定)说明[ \t]*$)"
        r"|(^[ \t]*(?:#{1,6}[ \t]+)?关于《[^》\n]{1,60}》[^\n]{0,20}的说明[ \t]*$)"
    ),
    # gazette number: 〔20xx〕第X号 — gazette publication numbering
    "gazette_number": re.compile(r"〔20[0-2]\d〕[ \t]*第?[0-9]{0,4}[ \t]*号"),
    # main-recipient line: 各省、自治区、直辖市…： — gov dispatch header
    "main_recipient_line": re.compile(
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?各省、自治区、直辖市[^。\n]{0,80}："
    ),
    # Chinese section heads 「一、二、…」; min 2 to vote (single-occ
    # FP datum: one 汉书 file)
    "cn_section_head": re.compile(
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?[一二三四五六七八九十百]+、[^，。\n]{0,40}$"
    ),
    # （…征求意见稿/送审稿） — consultative draft annotation
    "consultative_draft": re.compile(r"（[^（）]{0,12}(?:征求意见稿|送审稿)）"),
    # gov OPENING formulas; 特此公告 (announcement-family strong
    # closing formula) deliberately excluded — one piece of evidence
    # must not double-book into two families. Reconciliation facts:
    # 3 SAMR files depend on the non-特此公告 variants; on the
    # announcement side all 33 double-booked flips relied on
    # 特此公告 alone.
    "preamble_formula": re.compile(
        r"现将有关事项通知如下|现就有关事项公告如下|现公告如下|现印发给你们"
    ),
}

_FAMILIES: dict[str, dict[str, re.Pattern[str]]] = {
    "contract": _CONTRACT_MARKERS,
    "medical_report": _MEDICAL_REPORT_MARKERS,
    "drug_instruction": _DRUG_INSTRUCTION_MARKERS,
    "invoice": _INVOICE_MARKERS,
    "academic_paper": _ACADEMIC_PAPER_MARKERS,
    "financial_statement": _FINANCIAL_STATEMENT_MARKERS,
    "tech_standard": _TECH_STANDARD_MARKERS,
    "email": _EMAIL_MARKERS,
    "announcement": _ANNOUNCEMENT_MARKERS,
    "exam": _EXAM_MARKERS,
    "government_document": _GOV_DOCUMENT_MARKERS,
}

# Document-identity strong kinds (frozen 2026-10-15, consumed by
# resolver v3): these markers declare WHAT THIS DOCUMENT IS (an RFC
# header identity block, the invoice's own number field, the drug's
# own approval number) and, when both are strong, outweigh
# content-convention evidence of the "what this document talks
# about" kind (paper citations, email example header lines,
# announcement closing formulas). Observed co-occurrence:
# tech_standard ∩ email (rfc2046 / rfc5322 / rfc977, email
# voting_kinds 5 > 4 but all of them are explained-away example
# content). drug_instruction is on the watchlist; its identity marker
# is reserved for a future promotion.
_IDENTITY_STRONG_KINDS: dict[str, frozenset[str]] = {
    "drug_instruction": frozenset({"approval_number"}),
    "invoice": frozenset({"invoice_number_field"}),
    "tech_standard": frozenset(
        {"rfc_header", "rfc_bare_header", "rfc_number_line"}
    ),
}

# Near-decisive single kinds: (marker label, min count) per family.
# A 国药准字 approval number or >= 3 full-line bracket sections is
# drug-instruction evidence on its own; an RFC header line is decisive;
# 2+ non-RFC DOIs mark a paper (a lone DOI can appear in a readme).
# Everything else needs the 2-distinct-kinds quorum so ordinary prose
# never qualifies.
_STRONG_KINDS: dict[str, list[tuple[str, int]]] = {
    "drug_instruction": [("approval_number", 1), ("bracket_sections", 3)],
    "invoice": [("invoice_number_field", 1)],
    "academic_paper": [
        ("arxiv_id", 1),
        ("tex_paper_commands", 3),
        ("doi", 2),
    ],
    "tech_standard": [
        ("rfc_header", 1),
        ("rfc_bare_header", 1),
        ("rfc_number_line", 1),
    ],
    "email": [("message_id", 1)],
    "announcement": [("closing_formula", 1)],
    "exam": [("exam_booklet_formula", 1)],
    "government_document": [
        ("article_gist_bracket", 3),
        ("drafting_statement_heading", 1),
        ("article_formula", 12),
    ],
    # Insurance-clause party-term density: 3 occurrences of
    # 投保人/被保险人/保险人/受益人 qualifies insurance-contract
    # text on its own. Two-sided measurement (2026-11-11): 30/30
    # iachina clauses have parties>=3 (the smallest are 3 rider
    # addenda of 458-597 chars, covered by this strong kind alone);
    # across the 9-bucket negative census parties tops out at 1
    # (one commercial-bribery compliance guideline in the gov
    # bucket), a 3x margin.
    "contract": [("insurance_parties", 3)],
}

# Weak kinds: counted and recorded, but they never vote toward the
# quorum and never qualify as strong.  A price or a comma-grouped
# number is not a financial statement (gutenberg novels fired the old
# financial family on 57-100% of books before this, 2026-10-10).
_WEAK_KINDS: dict[str, frozenset[str]] = {
    "financial_statement": frozenset({"currency_amounts", "thousands_amounts", "amount_words_zh"}),
    "exam": frozenset(
        {"question_numbering", "subitem_letters", "option_lines"}
    ),
    # v9 (2026-11-13, p11 first full-chain negative census over all
    # 1464 files): the English signing conventions are prose-ubiquitous —
    # gutenberg letter novels ("signed by" + blank-line signatures) and
    # sphinx docs (the word "signature" + ASCII underscore dividers)
    # reached the 2-kind quorum on 7 negative files and flipped contract
    # labels; p10's 184-file spot check never sampled them.  Demoted to
    # weak for contract: a signature block alone is not a contract —
    # party or term structure is.  Two-sided: the iachina 30/30 GT recall
    # is unaffected (all strong via insurance_parties>=3, zero
    # quorum-path positives), and the zh signing kind keeps its vote
    # (no zh FP observed in the census).
    "contract": frozenset({"signature_words_en", "signature_line"}),
}

# Mention-only kinds (2026-11-01): these markers only claim "this
# document TALKS ABOUT the topic", not "this document HAS the
# shape". They are counted and recorded normally and still vote
# toward the quorum, but in the quoted-region override
# (quoted_only) they are excluded — an explainer article writing
# 发票号码 in its body is explaining, not bearing the shape.
# The quoted_only override looks only at structural markers (field
# labels, numbering, header lines, code-value shapes).
# 2026-11-11 (explainer_rfc_zh_02 datum): "Internet-Drafts share
# most header fields with RFCs; the difference is lifecycle, not
# shape" — an explainer mentioning the format name in its body is
# explaining, not bearing the shape. It hit one body marker, which
# broke the quoted_only override, and then the rfc_header strong
# kind single-handedly stamped the teaching material as
# tech_standard. (The English explainer escaped by luck: the plural
# "Internet-Drafts" fails \binternet-draft\b; the rule must not
# depend on plural forms.)
_MENTION_ONLY_KINDS: dict[str, frozenset[str]] = {
    "invoice": frozenset({"invoice_word"}),
    "tech_standard": frozenset({"internet_draft"}),
}

# Per-kind minimum counts to vote toward the quorum: raw counts are
# always recorded; only the vote is thresholded.  A single parenthetic
# year is prose ("(Koln, 1889)" in a scholarly book); three modern
# author-year citations are a citation convention.
_MIN_COUNT_TO_VOTE: dict[str, dict[str, int]] = {
    "academic_paper": {"author_year_citations": 3},
    # "现将" alone is ordinary prose; two occurrences are a notice
    # convention voting toward the announcement quorum
    "announcement": {"formal_opening": 2},
    "exam": {"question_types_zh": 2},
    # gov: a single 「一、」 section head is ordinary news/announcement
    # prose (gutenberg 汉书 FP datum); two are a sectioning
    # convention. A scattered handful of 第X条 lines is common in
    # explainers/quotes too; three is where it starts voting, and
    # >= 12 is a separate strong density gate.
    "government_document": {"article_formula": 3, "cn_section_head": 2},
    # contract: insurance party terms vote from 2 occurrences,
    # insurance terminology from 3. Two-sided measurement
    # (2026-11-11): across the 9-bucket negative census both kinds
    # top out below their vote thresholds (parties max 1, terms max
    # 2) — they structurally never vote on negatives, so neither
    # quorum nor strong can fire; on the positives 30/30 clauses
    # have parties>=2 and 27/30 have terms>=3 (3 rider addenda sit at
    # terms 1-2 and are covered by the insurance_parties strong
    # kind). Signature-convention kinds keep the default 1-occurrence
    # vote threshold.
    "contract": {"insurance_parties": 2, "insurance_terms": 3},
}

_DEFAULT_QUORUM_KINDS = 2


# ---------------------------------------------------------------------------
# quoted-sample regions (extractor v5)
# ---------------------------------------------------------------------------
# 2026-11-01 (explainer_docs datum): explainer documents paste raw
# samples of the target format into their body — RFC headers, email
# header blocks, invoice field columns quoted verbatim as examples.
# Text-layer regexes are blind to WHERE the string lives, so 45/48
# explainer documents were classified as the very format they were
# explaining. The discriminating evidence is not more strings but
# positional belonging: whether the sample is explicitly framed as
# a block of "we are showing what it looks like, not exercising it".
#
# Three kinds of deterministic outer evidence (they only exist with
# an explicit boundary; no heuristic guessing):
#   * RST literal block   — a line ending with ``::`` + the indented
#     block after it
#   * fenced block        — ``` / ~~~
#   * doxygen/sphinx code — ``.. code-block::`` directive + indented
#     block
#
# A block is judged a quoted region when it has >= 3 non-blank lines
# and matches ANY of the layout shapes below (ratio gate
# _QUOTED_MIN_LABEL_RATIO = 50%):
#
#   * field-record shape — lines start with ``word:`` / ``词：``
#     (header snapshots, YAML heads);
#   * vertically-aligned shape — lines carry >= 2 runs of 2+ spaces
#     forming column breaks (fixed-width layouts like RFC header
#     snapshots: ``Request for Comments: 1035`` flush left, the
#     author/organization flush right, the middle stretched by
#     spaces).
#
# 2026-11-01 (explainer_docs datum): the first version recognized
# only the field-record shape, so an RFC header snapshot ("Network
# Working Group / Request for Comments: 1035 / November 1987")
# failed to qualify — it is indeed a vertically aligned table, but
# its lines do not start with ``word:``. After adding the second
# shape, articles explaining RFC headers are correctly read as
# "demonstrating someone else's layout". Indentation alone is not
# enough (indented body paragraphs are everywhere), and boundaries
# alone are not enough (the whole document may live inside a code
# block).
#
# This signal is document-level and shared across families: it
# describes the LAYOUT belonging of a text span and favors no
# family.
_QUOTED_MIN_BODY_LINES = 3
_QUOTED_MIN_LABEL_RATIO = 0.5
# A run of 2+ spaces counts as one column break; leading line
# indentation does not (lines are lstripped first).
_COLUMN_BREAK = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")

_FENCE_OPEN = re.compile(r"^[ \t]*(```+|~~~+)")
_RST_LITERAL = re.compile(r"::[ \t]*$")
_RST_DIRECTIVE = re.compile(r"^[ \t]*\.\.[ \t]+code(-block)?::")
_FIELD_LABEL = re.compile(
    r"^[ \t]*(?:[A-Za-z][A-Za-z0-9 .()/#&'-]{0,40}|[\u4e00-\u9fff]{1,12})[ \t]*[:：]"
)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _indented_block_end(lines: list[str], start: int, base_indent: int) -> int:
    """Last line index of the indented block beginning at ``start``.

    Blank lines inside the block are tolerated; the block ends at the
    first non-blank line indented at or below ``base_indent``.
    """
    last = start
    idx = start
    while idx < len(lines):
        if not lines[idx].strip():
            idx += 1
            continue
        if _indent_of(lines[idx]) <= base_indent:
            break
        last = idx
        idx += 1
    return last


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """Bounded line spans that look like an explicitly delimited sample."""
    lines = text.splitlines()
    count = len(lines)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < count:
        line = lines[i]
        fence = _FENCE_OPEN.match(line)
        if fence:
            marker = fence.group(1)
            j = i + 1
            while j < count and not lines[j].lstrip().startswith(marker):
                j += 1
            spans.append((i + 1, min(j, count - 1)))
            i = j + 1
            continue
        is_literal = bool(_RST_LITERAL.search(line)) and not _RST_DIRECTIVE.match(line)
        is_directive = bool(_RST_DIRECTIVE.match(line))
        if is_literal or is_directive:
            base = _indent_of(line)
            j = i + 1
            while j < count and not lines[j].strip():
                j += 1
            if j < count and _indent_of(lines[j]) > base:
                last = _indented_block_end(lines, j, base)
                spans.append((j, last))
                i = last + 1
                continue
        i += 1
    return spans


def _is_quoted_region(lines: list[str], start: int, end: int) -> bool:
    body = [lines[k] for k in range(start, min(end, len(lines) - 1) + 1) if lines[k].strip()]
    if len(body) < _QUOTED_MIN_BODY_LINES:
        return False
    labelled = sum(1 for line in body if _FIELD_LABEL.match(line))
    if labelled / len(body) >= _QUOTED_MIN_LABEL_RATIO:
        return True
    # Vertically-aligned shape: fixed-width snapshots (RFC headers
    # etc.) have no ``word:`` line starts, but they carry
    # space-stretched columns. Prose lines vary in width and rarely
    # align two lines in a row.
    aligned = sum(1 for line in body if len(_COLUMN_BREAK.findall(line.strip())) >= 1)
    return aligned / len(body) >= _QUOTED_MIN_LABEL_RATIO


def _quoted_sample_regions(text: str) -> list[dict[str, Any]]:
    """Explicit sample blocks that read as field records, not as prose."""
    lines = text.splitlines()
    regions: list[dict[str, Any]] = []
    for start, end in _quoted_spans(text):
        if _is_quoted_region(lines, start, end):
            first = next(k for k in range(start, end + 1) if lines[k].strip())
            regions.append(
                {
                    "start_line": first,
                    "end_line": end,
                    "sample": lines[first].strip()[: _MAX_SAMPLE_CHARS],
                }
            )
    return regions


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset)


# 2026-11-01 (explainer_docs datum): deictic reference.
#
# The earlier evidence layers (quoted regions, shape purity) describe
# what the sample looks like and whether it sits inside a demo
# block; neither distinguishes a plainer fact: a real document never
# points at itself saying "see the figure above".
#
#   * A real RFC's header IS the body — nobody writes "the header
#     shown above";
#   * An article explaining RFC headers must write "shown above /
#     如上图所示", or the reader cannot know the earlier block was
#     a sample.
#
# So "a deictic phrase pointing at a sample appearing OUTSIDE the
# quoted region" is syntactic evidence unique to explaining. Only
# phrases that point at an ADJACENT layout position count (above /
# below / 上图 / 下图 / 上例 / following snippet ...); ordinary
# in-text cross references like "see Section 3" do not — real
# standards documents write those every day.
_DEICTIC_REFERENCE = re.compile(
    r"(?i:\b(?:shown|listed|printed|displayed|given|illustrated)\s+(?:above|below)\b)"
    r"|(?i:\b(?:the|this|that)\s+(?:example|snippet|excerpt|sample|layout|header)"
    r"\s+(?:above|below)\b)"
    r"|(?i:\b(?:above|below)\s+(?:is|are|shows|we\s+see)\b)"
    r"|(?i:\b(?:following|next|preceding|previous)\s+"
    r"(?:example|snippet|excerpt|sample|block|listing)\b)"
    r"|(?i:\bsee\s+(?:the\s+)?(?:example|snippet|excerpt|sample)\s+(?:above|below)\b)"
    r"|(?:如|见|像)?[上下]\s*(?:图|例|表|面)(?:所示|展示|给出|中)"
    r"|(?:如|见)[上下]\s*文"
    r"|(?<![\u4e00-\u9fff])示例(?:如下|见上|如上|如下所示)"
)


def _count_deictic_outside(text: str, spans: list[tuple[int, int]]) -> int:
    """How many sample-pointing phrases the document uses outside its samples.

    A real document's own field labels are never accompanied by "as shown
    above"; a page that explains a format has to say where the sample is.
    Counted separately from marker families because it is about the
    document's stance toward its own text, not about any one convention.
    """
    hit = 0
    for match in _DEICTIC_REFERENCE.finditer(text):
        line = _line_of(text, match.start())
        if not any(start <= line <= end for start, end in spans):
            hit += 1
    return hit


def _count_in_spans(
    text: str,
    matches: list[re.Match[str]],
    spans: list[tuple[int, int]],
    label: str | None = None,
) -> int:
    """Number of matches whose start line falls inside one of ``spans``.

    ``label`` names the marker so a shape-pure variant (``_SHAPE_PURE``)
    can narrow what counts; without it every match counts.
    """
    if not spans:
        return 0
    strict = _SHAPE_PURE.get(label) if label else None
    hit = 0
    for match in matches:
        if strict is not None and not strict.match(match.group(0)):
            continue
        line = _line_of(text, match.start())
        if any(start <= line <= end for start, end in spans):
            hit += 1
    return hit


def _family_facts(
    name: str,
    markers: dict[str, re.Pattern[str]],
    text: str,
    quoted_spans: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Count marker kinds, bounded samples and the quorum decision."""
    marker_counts: dict[str, int] = {}
    quoted_counts: dict[str, int] = {}
    match_lists: dict[str, list[re.Match[str]]] = {}
    samples: list[str] = []
    total = 0
    for label, pattern in markers.items():
        matches = list(pattern.finditer(text))
        match_lists[label] = matches
        marker_counts[label] = len(matches)
        total += len(matches)
        if quoted_spans is not None:
            quoted_counts[label] = _count_in_spans(text, matches, quoted_spans, label)
        for match in matches:
            sample = match.group(0)[:_MAX_SAMPLE_CHARS]
            if sample not in samples:
                if len(samples) >= _MAX_SAMPLE_MATCHES:
                    break
                samples.append(sample)
    weak = _WEAK_KINDS.get(name, frozenset())
    min_counts = _MIN_COUNT_TO_VOTE.get(name, {})
    voting_kinds = sum(
        1
        for label, count in marker_counts.items()
        if label not in weak and count >= min_counts.get(label, 1)
    )
    distinct_kinds = sum(1 for count in marker_counts.values() if count > 0)
    strong = any(
        marker_counts.get(label, 0) >= min_count for label, min_count in _STRONG_KINDS.get(name, [])
    )
    present = voting_kinds >= _DEFAULT_QUORUM_KINDS or strong
    facts: dict[str, Any] = {
        "present": present,
        "distinct_kinds": distinct_kinds,
        "voting_kinds": voting_kinds,
        "total_count": total,
        "marker_counts": marker_counts,
        "sample_matches": samples,
    }
    if quoted_spans is not None:
        # Family projection of the document-level quoted-region
        # evidence: how many of this family's markers fall inside line
        # spans already judged "quoted sample blocks". Total counts
        # stay untouched; this entry only answers "is this evidence
        # demonstrating someone else's format".
        #
        # quoted_only looks only at STRUCTURAL markers (see
        # _MENTION_ONLY_KINDS): an explainer writing 发票 in its body
        # is explaining, not bearing shape evidence; one occurrence
        # outside the quoted region must not validate the whole
        # family.
        #
        # 2026-11-01 (explainer_docs datum): "structural" goes one
        # step further — hits of term-mention markers (those with a
        # _SHAPE_PURE strict variant) in NON-LABEL shapes are also
        # mere mentions: "…早于开票日期是常见的审计疑点" is not
        # structural evidence. Structural counts are therefore
        # filtered by shape, not by marker name; the inside-quoted and
        # outside-quoted criteria stay consistent (both consume the
        # same match text and judge shape via match()).
        mention_only = _MENTION_ONLY_KINDS.get(name, frozenset())
        structural_total = 0
        structural_quoted = 0
        for label, matches in match_lists.items():
            if label in mention_only:
                continue
            strict = _SHAPE_PURE.get(label)
            for match in matches:
                if strict is not None and not strict.match(match.group(0)):
                    continue
                structural_total += 1
                line = _line_of(text, match.start())
                if any(start <= line <= end for start, end in quoted_spans):
                    structural_quoted += 1
        facts["quoted_marker_counts"] = quoted_counts
        facts["quoted_only"] = bool(
            structural_total > 0 and structural_quoted == structural_total
        )
    return facts


def detect_domain_signals(text: str) -> dict[str, Any]:
    """Detect domain-family text conventions in extracted narrative text.

    Args:
        text: the extracted narrative text (as-is; never rewritten).

    Returns:
        dict with ``extractor``, per-family facts and an honest note;
        counts are always recorded, ``present`` follows the documented
        quorum so ordinary prose never qualifies a family.
    """
    quoted_regions = _quoted_sample_regions(text)
    quoted_spans = [(r["start_line"], r["end_line"]) for r in quoted_regions]
    return {
        "extractor": "domain_signals_v9",
        "quorum": {
            "default_distinct_kinds": _DEFAULT_QUORUM_KINDS,
            "strong_kinds": [
                {
                    "family": family,
                    "marker": label,
                    "min_count": min_count,
                    "identity": label
                    in _IDENTITY_STRONG_KINDS.get(family, frozenset()),
                }
                for family, kinds in _STRONG_KINDS.items()
                for label, min_count in kinds
            ],
            "weak_kinds": [
                {"family": family, "marker": label}
                for family, labels in _WEAK_KINDS.items()
                for label in sorted(labels)
            ],
            "min_count_to_vote": [
                {"family": family, "marker": label, "min_count": min_count}
                for family, kinds in _MIN_COUNT_TO_VOTE.items()
                for label, min_count in kinds.items()
            ],
        },
        "quoted_sample_regions": quoted_regions,
        "deictic_references_outside_samples": _count_deictic_outside(text, quoted_spans),
        "families": {
            name: _family_facts(name, markers, text, quoted_spans)
            for name, markers in _FAMILIES.items()
        },
        "note": (
            "text-convention evidence, not a document-type claim: counts "
            "are recorded for every family; present follows a documented "
            "quorum (>= 2 distinct voting kinds, or a strong kind) so "
            "ordinary prose never qualifies; evidence overlap between "
            "families is allowed where conventions are genuinely shared "
            "(e.g. academic_paper vs tech_standard on RFCs) and is "
            "resolved by the decision layer, not by suppressing evidence. "
            "v2 (2026-10-14) adds email / announcement; their first corpus "
            "calibration pass landed 2026-10-15 with two marker guards "
            "(closing formula line-anchored, email from/to require an "
            "addr-spec) — extractor v3 carries both. v1 family numbers "
            "are unchanged (round5 verified against round4). v6 "
            "(2026-11-08) adds the exam family (exam-vs-questionnaire "
            "boundary calibrated in tmp_survey/exam_marker_probe v0-v3: "
            "GT 21/26 + 22/22, questionnaire/FP buckets all zero) with "
            "the numbering kinds held weak and the booklet formula as "
            "the family's single strong kind. v7 "
            "(2026-11-11) adds the government_document family "
            "(公文 vs 公告/表格/法规讲解 boundary, calibrated in "
            "_adv_tmp/p8 probes v0-v6 on the samr 376-file corpus: "
            "gov-narrative slice 256/278 on the production chain — "
            "strong 188 / quorum 68, "
            "22 honest misses registered in the bucket note; chinese "
            "negative buckets FP=0; the preamble_formula keeps only "
            "the gov opening variants (特此公告 stays announcement-"
            "only — no double-booked evidence). v8 "
            "(2026-11-11) promotes contract from the watchlist to "
            "eligible: insurance_parties (投保人/被保险人/保险人/"
            "受益人, long-form-first alternation) and insurance_terms "
            "(保险金额/保险费/保险责任/犹豫期/免赔率) join the "
            "signature kinds; thresholds are two-sided measured — "
            "negative survey 9 buckets full-scan caps at parties=1 / "
            "terms=2 (both below the vote minimums, structurally no "
            "quorum), positive iachina 30-clause GT hits 30/30 (27/30 "
            "quorum + 3 riders via the parties>=3 strong), and the "
            "pre-freeze gov capture (3/4 of the corpus) is released "
            "for the resolver to adjudicate (resolver v7 + frameworks "
            "label-only)"
        ),
    }
