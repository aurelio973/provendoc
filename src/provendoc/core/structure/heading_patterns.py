import re
from dataclasses import dataclass


@dataclass
class HeadingCandidate:
    """A heading-like line match (raw regex evidence)."""

    raw: str
    pattern: str
    depth_hint: int
    start: int
    end: int
    provenance: str = "inferred"
    """结构证据来源 (structure provenance)。

    区分两种不可等量齐观的证据层级:

    - ``"declared"``: 作者显式声明的结构 — markdown ``#`` 标记、
      DOCX heading 样式、PDF 书签等。文本自身携带结构标记,
      可信度高。
    - ``"inferred"``: parser 猜测的结构 — 中文编号启发式
      (``一、``/``（一）``/``第X章``)、点分编号、全大写惯例、
      setext 下划线惯例等。作者未写任何结构标记, 标题身份
      是启发式推断的结果, 可能误判。

    下游 (RSE 信封说明 / 不变量轨权重) 据此区分“声明结构”与
    “猜测结构”的证据强度; 见 narrative_invariants
    (结构完整性三档梯度)。
    """


# Pattern family -> structure evidence source. Only ``markdown``
# (explicit ``#`` marker) is declared; every other family
# (cn_lv1/cn_lv2/cn_chapter/numeric/paren_numeric/roman/alpha/
# circled/lettered_section/decoration) is a typography-convention
# heuristic -> inferred.
PROVENANCE_BY_PATTERN = {
    "markdown": "declared",
}


def classify_heading_provenance(pattern_name: str) -> str:
    """Classify structure evidence source for a pattern family
    (declared / inferred).

    Unknown families default conservatively to inferred --
    declared structure requires explicit markers in the text.
    """
    return PROVENANCE_BY_PATTERN.get(pattern_name, "inferred")


@dataclass(frozen=True)
class HeadingLineMatch:
    """Result of matching structural heading syntax on a single line.

    ``numbering`` is the leading marker ("2.3.1", "一、", "###") and
    ``rest`` is the title text that follows it.  ``pattern`` is the pattern
    name from :data:`PATTERNS` (a string, not a compiled regex).
    """

    raw: str
    pattern: str
    depth_hint: int
    start: int
    end: int
    numbering: str
    rest: str

MAX_HEADING_LEN = 100

# Code fence (``` or ~~~ opener, language tag allowed).
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# ---- setext underline heading (title line + full-width underline) ----
# Classic plain-text heading convention (man pages / old READMEs /
# Gutenberg chapters); same shape as markdown setext. '=' -> 1,
# '-' (>=3) -> 2.
_SETEXT_EQ_RE = re.compile(r"=+")
_SETEXT_DASH_RE = re.compile(r"-{3,}")

# A setext title line must carry readable text (letter/digit/CJK) --
# symbol-only lines are ASCII art.
_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")

# key: value shaped line (YAML/frontmatter entry) -- under a '---'
# underline it is almost surely frontmatter, not a setext heading.
_KEY_VALUE_LINE_RE = re.compile(r"^[\w.-]+\s*:\s+\S")

# ---- all-caps standalone heading (RFC / contract / spec convention) ----
# Whole line of uppercase letters/digits/interpunctuation, at
# least 2 letters. Em/en dashes included: classic novel titles
# wrap across lines joined by em-dash; rejecting them would miss
# the true first line while catching its wrapped tail instead.
_ALL_CAPS_RE = re.compile(r"[A-Z0-9][A-Z0-9 \-&'/(),.:;!?—–]*")
_CAPS_LETTER_RE = re.compile(r"[A-Z]")
# Tail-punctuation shape: ends with .;,!?. Tail-punct candidates
# are not separable by the punctuation itself (true titles like
# 'CHAPTER I.' / 'CONTENTS.' also end with a period) but by
# neighbors: corpus scan (609 files, 1334 hits) shows a non-empty
# prev line is 100% wrapped tails and a blank prev line 100% true
# headings. So tail-punct candidates require a blank prev line.
_CAPS_TAIL_PUNCT_RE = re.compile(r"[.;,!?]$")
# Numeric table-row trait: digit tokens with thousands/decimal
# separators (e.g. 76,800.00). An all-caps line with such tokens
# is a data/total row, not a heading. One thousands-group is
# enough to reject; loose decimal tokens need two or more
# ("SECTION 2.1" style section numbers stay legal headings).
_CAPS_NUMERIC_TOKEN_RE = re.compile(r"\d+[,.]\d+")
_CAPS_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
# Loose heading shape (hash without a space): tolerant terminator
# for unclosed fences.
_LOOSE_HEADING_RE = re.compile(r"^#{1,6}\S")
# Multi-hash heading (##+): never a code comment (bash/python/C all
# use single #); an unclosed fence terminates at it unconditionally.
_MULTI_HASH_RE = re.compile(r"^#{2,6}")
# Hash-without-space markdown heading; the main regex stays strict,
# the recovery scan handles it.
_HASH_NOSPACE_RE = re.compile(r"^(#{1,6})(\S.*)$")
# Pseudo-hash guard: shebang and C preprocessor lines are not
# headings.
_PSEUDO_HASH_RE = re.compile(
    r"^(?:!|include|define|undef|ifdef|ifndef|if|elif|else|endif"
    r"|pragma|error|warning|line|region|endregion)\b",
    re.IGNORECASE,
)
# ASCII art guard: heading content made only of symbols/whitespace
# (no letter/digit/CJK) that itself contains # is an ASCII art line
# ("#  #   #" / "###    ###"), not a heading. Real heading content
# almost always carries text.
_ASCII_ART_CONTENT_RE = re.compile(r"\W+$")

# Unnumbered functional-section word set (for the word_section
# recovery scan): conventional front/back-matter words of papers,
# reports and books. All-caps shapes (REFERENCES) belong to the
# caps recovery; single-character words are too noisy to keep.
_WORD_HEADING_SET = frozenset({
    # Chinese functional-section words.
    "目录", "图目录", "表目录", "前言", "序言", "引言", "导言", "绪论",
    "摘要", "关键词", "结语", "结论", "后记", "参考文献", "致谢",
    "附录", "索引", "术语表", "缩略语", "名词解释",
    # English (callers compare lowercased).
    "abstract", "references", "bibliography", "contents", "index",
    "introduction", "conclusion", "appendix", "glossary", "preface",
    "foreword", "acknowledgements", "acknowledgments", "summary",
})

# Code-feature lines (context guard for single-# headings):
#   - block statement heads: while/for/if/.../with/def/class ...:
#   - common code lines: import / return / pass / raise / print( /
#     assignment
_CODE_STMT_RE = re.compile(
    r"^(?:while|for|if|elif|else|try|except|finally|with|def|class"
    r"|switch|case|do)\b.*:\s*$"
)
_CODE_LINE_RE = re.compile(
    r"^(?:import\s|from\s+\S+\s+import\s|return\b|pass\b|raise\b"
    r"|print\(|[A-Za-z_][\w.]*\s*=\s*[^=])"
)

# Dotted outline item (x.y[.z][.], indent and trailing dot allowed).
_DOTTED_ITEM_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})+\.?\s")

# TOC entry guard: a dot leader plus trailing page number is
# typographic proof of a TOC entry ("1.1 Purpose ......... 7").
# Dotted-number TOC entries and real headings share the same
# shape; the leader is the only observable difference -- without
# this guard a whole TOC becomes hundreds of outline-only
# fragments. CJK ellipsis leaders and spaced leaders (". . . 7",
# RFC style, >= 3 space-separated dots + trailing page number)
# are allowed; prose ellipses ("..." with no spaces) never match.
_TOC_LEADER_RE = re.compile(
    r"(?:[.\u2026]{2,}|\.(?:\s+\.){2,})\s*\d{1,4}\s*$"
)

# All-caps byline shape: a single surname word (hyphen/apostrophe
# allowed, no spaces/commas), e.g. "J. Mogul" / "T. Berners-Lee".
# Multi-word rests ("A. First Letter Section") are not this shape.
_BYLINE_SURNAME_RE = re.compile(r"^[A-Z][A-Za-z'’-]*$")

# Reference-word tail guard: when the previous non-empty line ends
# with section/chapter/part/..., the current line is a wrapped
# reference tail, not a heading. \b keeps subsection/counterpart
# from matching. Applies to numeric/paren_numeric/alpha only (the
# caps family has real chapter-title hits and is excluded).
_REF_WORD_TAIL_RE = re.compile(
    r"\b(?:section|chapter|part|appendix|clause|annex|paragraph)"
    r"\s*[.:]?\s*$",
    re.IGNORECASE,
)

PATTERNS = [
# markdown
    (
        "markdown",
        re.compile(r"^(#{1,6})\s+(.+)$"),
    ),

    # Chinese level-1 numbering (ideographic period allowed).
    (
        "cn_lv1",
        re.compile(
            r"^([一二三四五六七八九十百千万]+)[、.．]\s*(.+)$"
        ),
    ),

    # Chinese level-2 numbering.
    (
        "cn_lv2",
        re.compile(
            r"^[（(]([一二三四五六七八九十百千万]+)[）)]\s*(.+)$"
        ),
    ),

    # 第X条/章/编 -- Chinese numbered sections. The suffix word sets
    # the level: 编/章/篇/部分/卷/回 -> 1, 节 -> 2, 条 -> 3. 条 is the
    # core unit of legal documents (章 > 节 > 条); 讲/课/单元/幕 are
    # top-level dividers of textbooks and scripts. Compound-word
    # guard: a rest starting with 时 after 课 is a prose line
    # ("第X课时安排" -- 课时 is one morpheme), not a section heading.
    # Prose counting words (第二日/第几人名) are never accepted --
    # the closed suffix whitelist is what separates them.
    (
        "cn_chapter",
        re.compile(
            r"^第([一二三四五六七八九十百千万\d]+)"
            r"(编|章|节|篇|部分|卷|回|条|讲|课|单元|幕)\s*(.+)$"
        ),
    ),

    # 1. / 1.1 / 1.1.1
    # Three alternatives:
    #   A) dotted number (each segment <= 2 digits, 2-3 segments only)
    #      + optional trailing dot (documents often write "1.1.")
    #      + bare space. Max 3 segments (1.1, 1.1.1) to reject
    #      4-segment IP addresses like 10.0.1.50.
    #   B) number + 、 + optional space (Chinese style: "2、标题")
    #   C) number + . or ) + required space (Western style: "1. Heading", "3) Heading")
    #      The separator includes the ideographic period (Chinese
    #      formal-document convention, GB style, no space required:
    #      "1．用法"); the ASCII dot requires a following space to
    #      reject "1.5 cups".
    (
        "numeric",
        re.compile(
            r"^(?:"
            r"(\d{1,2}(?:\.\d{1,2}){1,2})\.?\s+(?=[^-])(.+)"
            r"|"
            r"(\d+)、\s*(.+)"
            r"|"
            r"(\d+)(?:[.)]\s+|．\s*)(.+)"
            r")$"
        ),
    ),

    # (1)(2)
    (
        "paren_numeric",
        re.compile(
            r"^[（(](\d+)[）)]\s*(.+)$"
        ),
    ),

    # I II III -- must come BEFORE alpha so "I. First"
    # I II III -- must come BEFORE alpha so "I. First"
    # matches roman, not alpha. (Ideographic period allowed
    # without a space; ASCII dot/paren still require one, same
    # discipline as the numeric family.)
    (
        "roman",
        re.compile(
            r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)(?:[.)]\s+|．\s*)(.+)$"
        ),
    ),

    # a. b. c. (ideographic period allowed without a space, same
    # discipline as numeric).
    (
        "alpha",
        re.compile(
            r"^([a-zA-Z])(?:[.)]\s+|．\s*)(.+)$"
        ),
    ),

    # ①②③
    (
        "circled",
        re.compile(
            r"^[①②③④⑤⑥⑦⑧⑨⑩]\s*(.+)$"
        ),
    ),

    # Section A / Part B / Chapter 3 / Appendix C -- unmarked
    # English section headings. Whole-line constraint (keyword +
    # number + optional tail punctuation, anchored at end):
    # rejects prose references ("In Section A, we discuss ...").
    (
        "lettered_section",
        re.compile(
            r"^(Section|Part|Chapter|Appendix)\s+"
            r"([A-Z]|\d{1,3}|[IVXLC]{1,7})[.:：]?\s*$"
        ),
    ),

    # 【XX】Chinese bracket sections (drug-manual/spec convention,
    # e.g. "【功能主治】"). Whole line only: brackets with trailing
    # prose are inline markers, not headings; content <= 30 chars.
    (
        "bracket_cn",
        re.compile(
            r"^【([^】]{1,30})】\s*$"
        ),
    ),

    # 附录A / 附录一 / 附录 1 -- Chinese appendix headings (trailing
    # title text allowed). Mutually exclusive with lettered_section
    # (English Appendix keyword only). The number must end at a
    # boundary (separator/space/EOL) so "附录一些说明" is not read
    # as appendix number 一.
    (
        "appendix_cn",
        re.compile(
            r"^附录\s*([A-Za-z0-9一二三四五六七八九十]{1,3})"
            r"(?=[\s.:：、]|$)[.:：、]?\s*(.*)$"
        ),
    ),

    # §1 / § 1.2 / §IV -- section-sign headings (European legal /
    # academic convention). Decimal-hierarchical or roman number,
    # optional separator, then title text.
    (
        "section_sign",
        re.compile(
            r"^§\s*([0-9]+(?:\.[0-9]+)*|[IVXLC]{1,7})"
            r"[.:：)]?\s*(.*)$"
        ),
    ),

    # == Title == / === Title === -- symmetric decorated headings
    # (MediaWiki style). Both markers must be the same length
    # (backreference) and >= 2 '='; the marker count is the level.
    # Only '=' is accepted: corpus-wide scans show * / _ / - / ~ are
    # almost always noise (MIME boundaries, PEM armor, Gutenberg
    # markers, emphasis, separators); the '=' family is handled by
    # the run/prev guards. Content must carry readable text --
    # symbol-only lines are separators.
    (
        "decoration",
        re.compile(
            r"^(={2,6})\s*(.+?)\s*\1$"
        ),
    ),
]

# Indented list item: an unordered marker (- * +) or ordered marker
# (1. / 2)) appearing AFTER leading whitespace.  Flush-left markers do
# not count -- a heading's first paragraph is rarely indented.
_INDENTED_LIST_ITEM_RE = re.compile(r"^\s+(?:[-*+]|\d{1,2}[.)])\s+")

# Indented heading marker ("\t# Title" / "    ## Title"): the stripped
# line starts with #{1,6} + whitespace.  Used to keep indented headings
# from being mistaken for indented code bodies (tab/space heading
# confusion).
_INDENTED_HEADING_MARKER_RE = re.compile(r"^#{1,6}\s")

_NUMERIC_PATTERN = dict(PATTERNS)["numeric"]


def _fence_line_spans(lines):
    """Compute the line spans covered by code fences (inclusive of
    the fence lines themselves).

    Closed fences: the whole open-to-close block is excluded.
    Unclosed fences: tolerant termination -- scan forward for a
    blank line followed by a loose heading-shaped next non-empty
    line as the leak end; otherwise extend to EOF.
    """
    spans = []
    i, n = 0, len(lines)
    while i < n:
        if _FENCE_RE.match(lines[i]):
            j = i + 1
            closed = False
            while j < n:
                if _FENCE_RE.match(lines[j]):
                    closed = True
                    break
                j += 1
            if closed:
                spans.append((i, j))
                i = j + 1
            else:
                end = n - 1
                k = i + 1
                while k < n:
                    # A multi-hash heading terminates unconditionally
                    # -- never a code comment (the cleaning layer
                    # may drop the blank line after the fence).
                    if _MULTI_HASH_RE.match(lines[k]):
                        end = k - 1
                        break
                    if not lines[k].strip():
                        m = k + 1
                        while m < n and not lines[m].strip():
                            m += 1
                        if m < n and _LOOSE_HEADING_RE.match(lines[m].strip()):
                            end = k - 1
                            break
                    k += 1
                spans.append((i, max(end, i)))
                i = max(end, i) + 1
        else:
            i += 1
    return spans


def _in_spans(idx, spans):
    # Retired (kept, zero callers): extract_heading_candidates
    # materializes fence spans into a per-line boolean array, so
    # this linear scan is no longer called.
    return any(s <= idx <= e for s, e in spans)


def _frontmatter_span(lines):
    """Line span covered by leading YAML frontmatter (inclusive of
    both '---' lines).

    Only when line 0 is exactly '---'; scan forward for the
    closing '---'. A broken frontmatter returns None (treated as
    a plain separator, no exclusion). Purpose: setext/caps
    recovery scans must not read frontmatter entries as
    underline headings.
    """
    if not lines or lines[0].strip() != "---":
        return None
    for j in range(1, len(lines)):
        if lines[j].strip() == "---":
            return (0, j)
    return None


def _single_segment_number(raw):
    """Return the leading number if ``raw`` is a SINGLE-segment numeric
    heading candidate ("1. xxx" / "2、xxx"), else ``None``.

    Dotted candidates ("1.1" / "1.1.1") return ``None`` -- multi-segment
    numbering is far more likely to denote real heading hierarchy than
    ordered-list items.
    """
    match = _NUMERIC_PATTERN.match(raw)
    if not match:
        return None
    if match.group(1):  # dotted alternative
        return None
    num_str = match.group(3) or match.group(5)
    if not num_str:
        return None
    return int(num_str)


def _filter_ordered_list_items(lines, candidates):
    """Drop single-segment numeric candidates that are actually ordered
    list items, not headings.

    Two disambiguation rules (either marks the candidate as a list item):

    a) sublist rule -- the next non-empty line is an INDENTED list item
       ("1. 应用服务器" followed by "   - Python 3.10+");
    b) run rule -- the next single-segment numeric candidate has the
       number n+1 and every non-blank line between them is indented
       (consecutive "1." "2." "3." lines form an ordered list).

    Dotted candidates ("1.1"), markdown/cn/roman/etc. patterns are never
    filtered here.
    """
    # char offset of each line -- identical arithmetic to the extraction
    # loop above, so candidate.start matches line_starts[i] exactly.
    line_starts = []
    position = 0
    for line in lines:
        line_starts.append(position)
        position += len(line) + 1
    offset_to_line = {start: i for i, start in enumerate(line_starts)}

    numbers = [
        (_single_segment_number(c.raw), offset_to_line.get(c.start))
        for c in candidates
    ]
    keep = [True] * len(candidates)

    for idx, (num, line_idx) in enumerate(numbers):
        if num is None or line_idx is None:
            continue

        # rule a): indented sublist directly below
        j = line_idx + 1
        is_list_item = False
        while j < len(lines):
            if not lines[j].strip():
                j += 1
                continue
            if _INDENTED_LIST_ITEM_RE.match(lines[j]):
                is_list_item = True
            break
        if is_list_item:
            keep[idx] = False
            # No early continue: must fall through to rule b) -- in
            # an indented run every item's next line is an indented
            # list item so rule a) hits each one; skipping here would
            # short-circuit the previous candidate's run pairing and
            # the run's last item would always escape. Rule b)
            # carries its own dotted-outline protection, so
            # re-checking is safe.

        # rule b): consecutive incrementing numeric candidates with only
        # indented (or blank) lines between them -- UNLESS the lines between
        # contain dotted outline items (x.y. form): that is a hierarchical
        # outline ("1." followed by "   1.1." children), not a flat list.
        next_num, next_line = numbers[idx + 1] if idx + 1 < len(numbers) else (None, None)
        if next_num is not None and next_line is not None and next_num == num + 1:
            between = lines[line_idx + 1:next_line]
            if any(_DOTTED_ITEM_RE.match(line) for line in between):
                continue
            if all((not line.strip()) or line[:1].isspace() for line in between):
                keep[idx] = False
                keep[idx + 1] = False

    return [c for k, c in zip(keep, candidates) if k]


# Fallback-family guard scope: numbered/lettered
# typography-convention candidates. markdown excluded (stacked #
# headings with no blank lines are a legal document shape);
# setext/word excluded (mixed evidence of true headings). Shared
# by the run guard (_filter_candidate_runs) and the prev guard
# (_filter_prev_nonblank_neighbors).
_FALLBACK_GUARD_FAMILIES = frozenset(
    {"numeric", "paren_numeric", "alpha", "roman", "circled", "decoration"}
)

# Run chaining: consecutive run members <= _RUN_MAX_GAP lines
# apart, all gap lines non-blank (TOC entries wrap to 1-2 lines;
# rfc3501 status lists carry an "OR" line between members).
_RUN_MAX_GAP = 4


def _filter_candidate_runs(lines, candidates):
    """Drop fallback-family candidates stacked in heading-form runs.

    A run is a maximal sequence of >=3 candidates (numeric /
    paren_numeric / alpha / roman / circled) where consecutive members
    are separated by at most ``_RUN_MAX_GAP`` lines, all non-blank --
    a heading-like stack with no blank-line separation anywhere.

    Real txt headings are always blank-separated from surrounding text
    (rfc2616 calibration: 237 true numeric headings, prev all blank);
    a stack of heading-form lines is a TOC block (rfc2246 "6.2. Record
    layer  16"), an ordered list (zh_leetcode "1. ... 1. ... 1. ..."),
    or a version/data table (license_cpython "1.6  1.5.2  2000 CNRI").
    Corpus-wide: 992 dropped, all false; the 26 blank-prev members
    verified one-by-one as TOC/list/table block starts.

    Isolated candidates (blank prev AND blank next) can never be run
    members by construction -- a blank line breaks the chain on both
    sides.
    """
    line_starts = []
    position = 0
    for line in lines:
        line_starts.append(position)
        position += len(line) + 1
    offset_to_line = {start: i for i, start in enumerate(line_starts)}

    members = [
        (offset_to_line[c.start], c)
        for c in candidates
        if c.pattern in _FALLBACK_GUARD_FAMILIES and c.start in offset_to_line
    ]
    # candidates are already start-sorted (main loop order +
    # post-recovery sort), so members stay in line order.
    drop_starts = set()
    n = len(members)
    k = 0
    while k < n:
        j = k
        while j + 1 < n:
            i_prev = members[j][0]
            i_cur = members[j + 1][0]
            if i_cur - i_prev <= _RUN_MAX_GAP and all(
                line.strip() for line in lines[i_prev + 1:i_cur]
            ):
                j += 1
            else:
                break
        if j - k + 1 >= 3:
            drop_starts.update(c.start for _, c in members[k:j + 1])
        k = j + 1
    if not drop_starts:
        return candidates
    return [c for c in candidates if c.start not in drop_starts]


def _filter_prev_nonblank_neighbors(lines, candidates):
    """Fallback-family prev-convergence guard: a candidate with a
    non-empty previous line is not a heading.

    Typography rationale: a heading glued to the previous text
    line has no heading function -- real plain-text headings are
    always blank-line separated from surrounding text. Corpus
    calibration: every fallback-family candidate with non-empty
    prev that survives the main-loop guards and the run guard is
    one of three false shapes -- bibliography author wraps,
    wrapped prose tails, or TOC/list members.

    Must run after _filter_candidate_runs: the run guard needs the
    full candidate sequence to spot unseparated TOC/list/table
    blocks; deleting middle members first would break the chain
    and let the block head escape as an isolated candidate.

    Boundaries: line 0 (no prev) is exempt; markdown/setext/
    caps/word/cn families are exempt -- caps has mixed evidence
    of true chapter titles with non-empty prev, so it is left
    alone.
    """
    line_starts = []
    position = 0
    for line in lines:
        line_starts.append(position)
        position += len(line) + 1
    offset_to_line = {start: i for i, start in enumerate(line_starts)}

    drop_starts = set()
    for c in candidates:
        if c.pattern not in _FALLBACK_GUARD_FAMILIES:
            continue
        i = offset_to_line.get(c.start)
        if i is None or i == 0:
            continue
        if lines[i - 1].strip():
            drop_starts.add(c.start)
    if not drop_starts:
        return candidates
    return [c for c in candidates if c.start not in drop_starts]


def _is_code_feature_line(line):
    """Whether a line carries code features (single-# heading
    guard)."""
    stripped = line.strip()
    if _CODE_STMT_RE.match(stripped):
        return True
    if _CODE_LINE_RE.match(stripped):
        return True
    # An indented plain line (code body) -- indented list items do
    # not count, and indented heading lines ("\t# Title") do not
    # either: otherwise indented headings would act as each
    # other's "code features" and swallow real single-#
    # candidates.
    if (
        line[:1] in (" ", "\t")
        and not _INDENTED_LIST_ITEM_RE.match(line)
        and not _INDENTED_HEADING_MARKER_RE.match(stripped)
    ):
        return True
    return False


def _code_context_guard(lines, idx, window=2):
    """Code-context guard for single-# heading candidates.

    In interrupted/fragmented documents, unfenced "# TODO"
    comment lines match the markdown pattern as level-1
    headings. If any of the surrounding window non-empty lines
    carries a code feature, the candidate is in code context
    and is not a heading.
    """
    checked = 0
    k = idx - 1
    while k >= 0 and checked < window:
        if lines[k].strip():
            checked += 1
            if _is_code_feature_line(lines[k]):
                return True
        k -= 1
    checked = 0
    k = idx + 1
    n = len(lines)
    while k < n and checked < window:
        if lines[k].strip():
            checked += 1
            if _is_code_feature_line(lines[k]):
                return True
        k += 1
    return False


def match_heading_line(text):
    """Match structural heading syntax at the start of a single line.

    Single-line counterpart of :func:`extract_heading_candidates`, for
    callers that hold one line at a time (DOCX paragraphs, PDF bookmark
    titles, TOC lines) rather than a whole document.  Only the first line of
    ``text`` is inspected and the pattern must start at offset 0.

    Returns a :class:`HeadingLineMatch` or ``None``.  Document-level
    disambiguation (fence spans, ordered-list filtering, code-context
    guards) is NOT applied here -- callers must gate usage on their own
    structural signals (styles, outline levels, numbering XML) before
    trusting a fallback match.
    """
    line = (text or "").split("\n", 1)[0]
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_HEADING_LEN:
        return None

    for pattern_name, regex in PATTERNS:
        match = regex.match(stripped)
        if not match:
            continue

        # ASCII art guard (same as extract_heading_candidates).
        if (
            pattern_name == "markdown"
            and "#" in (match.group(2) or "")
            and _ASCII_ART_CONTENT_RE.fullmatch(match.group(2))
        ):
            return None

        depth_hint = _resolve_depth_hint(pattern_name, match)

        # lettered_section: the whole line is the marker
        # ("Chapter 3"), no separate title text; every other
        # pattern keeps the title text in the last group.
        if pattern_name == "lettered_section":
            return HeadingLineMatch(
                raw=stripped,
                pattern=pattern_name,
                depth_hint=depth_hint,
                start=0,
                end=len(stripped),
                numbering=stripped,
                rest="",
            )

        # bracket_cn: the marker is the whole bracket pair; the
        # title text is the inner content.
        if pattern_name == "bracket_cn":
            return HeadingLineMatch(
                raw=stripped,
                pattern=pattern_name,
                depth_hint=depth_hint,
                start=0,
                end=len(stripped),
                numbering=stripped,
                rest=match.group(1),
            )

        # decoration: the marker is the symmetric pair ("=="), the
        # title is the middle; symbol-only content or an '=' tail
        # (asymmetric leftover) is a separator or broken marker,
        # not a heading.
        if pattern_name == "decoration":
            if not _WORD_CHAR_RE.search(match.group(2)) \
                    or match.group(2).endswith("="):
                return None
            return HeadingLineMatch(
                raw=stripped,
                pattern=pattern_name,
                depth_hint=depth_hint,
                start=0,
                end=len(stripped),
                numbering=match.group(1),
                rest=match.group(2),
            )

        # cn_chapter compound guard (same as the main loop):
        # "第X课时安排" is prose, not a section heading.
        if (
            pattern_name == "cn_chapter"
            and match.group(2) == "课"
            and (match.group(3) or "").startswith("时")
        ):
            return None

        groups = [group for group in match.groups() if group is not None]
        rest = groups[-1] if groups else ""
        if stripped.endswith(rest) and rest:
            numbering = stripped[: len(stripped) - len(rest)].strip()
        else:
            numbering = stripped.strip()
            rest = ""

        return HeadingLineMatch(
            raw=stripped,
            pattern=pattern_name,
            depth_hint=depth_hint,
            start=0,
            end=len(stripped),
            numbering=numbering,
            rest=rest.strip(),
        )

    return None


def extract_heading_candidates(text):
    """Extract all heading candidates from plain text.

    Args:
        text: full document text.

    Returns:
        list[HeadingCandidate]: candidates in order of appearance;
        overlong lines and lines inside code fences are excluded.
    """
    lines = text.splitlines()
    fence_spans = _fence_line_spans(lines)
    # fence spans are materialized once into a per-line boolean
    # array -- a per-line linear scan over all spans was O(L*F)
    # and degraded to minutes on fence-heavy documents; the
    # span semantics (s <= idx <= e) is preserved exactly.
    fence_flags = [False] * len(lines)
    for _fs, _fe in fence_spans:
        for _fk in range(_fs, min(_fe, len(lines) - 1) + 1):
            fence_flags[_fk] = True

    # Per-line char offsets (same accumulation as the extraction
    # loop).
    line_starts = []
    position = 0
    for line in lines:
        line_starts.append(position)
        position += len(line) + 1

    candidates = []
    for idx, line in enumerate(lines):
        stripped = line.strip()

        if len(stripped) > MAX_HEADING_LEN:
            continue
        # Lines inside code fences never join heading recognition.
        if fence_flags[idx]:
            continue
        # TOC entry guard: dot leader + trailing page number -> a
        # TOC line, not a heading (real dotted headings never end
        # with a leader).
        if _TOC_LEADER_RE.search(stripped):
            continue

        for pattern_name,regex in PATTERNS:
            match = regex.match(stripped)
            if not match:
                continue

            # Code-context guard: a single-# candidate embedded in
            # an unfenced code stream is not a heading. Only
            # single # -- ##+ can never be a code comment.
            if (
                pattern_name == "markdown"
                and len(match.group(1)) == 1
                and _code_context_guard(lines, idx)
            ):
                break

            # ASCII art guard: after SGR color codes are sanitized
            # away, ASCII art '#' lines resurface as hash-prefixed
            # shapes. Symbol-only content containing # is not a
            # heading -- heading content must carry readable text.
            if (
                pattern_name == "markdown"
                and "#" in match.group(2)
                and _ASCII_ART_CONTENT_RE.fullmatch(match.group(2))
            ):
                break

            # All-caps byline guard: "J. Mogul" shape (capital
            # single letter + single surname word) is an RFC
            # author/signature line, not a heading. Multi-word
            # rests are unaffected.
            if (
                pattern_name == "alpha"
                and match.group(1).isupper()
                and _BYLINE_SURNAME_RE.fullmatch(match.group(2) or "")
            ):
                break

            # Lowercase alpha marker guard (ASCII separators only):
            # "a) You must ..." / "j. Note that ..." are license
            # clause lists, algorithm step letters, or wrapped
            # math variables -- real section headings never use
            # lowercase markers. The ideographic period is not
            # blocked: "a．子标题" is a legal Chinese GB subheading
# convention.
            if (
                pattern_name == "alpha"
                and match.group(1).islower()
                and stripped[1:2] != "\uff0e"
            ):
                break

            # Year/large-number guard: a single segment >= 3 digits
            # is a wrapped reference year ("1997. [jg640]"), not a
            # section heading -- true heading numbers rarely
            # reach hundreds.
            if pattern_name == "numeric":
                _num = match.group(3) or match.group(5) or ""
                if len(_num) >= 3:
                    break
                # List-wrap guard: a rest ending in ;/, is the
                # wrapped tail of a list item or reference --
                # headings never end with run-on punctuation.
                _rest = (
                    match.group(2)
                    or match.group(4)
                    or match.group(6)
                    or ""
                )
                if _rest.endswith((";", ",")):
                    break

            # Decoration content guard: content must carry readable
            # text (letter/digit/CJK) -- symbol-only content is a
            # horizontal rule. An '=' tail means asymmetric
            # markers ("== Title ==="), which is not MediaWiki
            # syntax: reject.
            if (
                pattern_name == "decoration"
                and (not _WORD_CHAR_RE.search(match.group(2))
                     or match.group(2).endswith("="))
            ):
                break

            # cn_chapter compound-word guard: 课时 is a whole
            # morpheme; a rest starting with 时 after 课 is a
            # prose line, not a section heading.
            if (
                pattern_name == "cn_chapter"
                and match.group(2) == "课"
                and (match.group(3) or "").startswith("时")
            ):
                break

            # Neighbor/shape guards (numeric/paren_numeric/alpha/
            # roman fallback families): wrapped prose-tail
            # evidence -- true headings have a blank/furniture/
            # block-boundary prev.
            if pattern_name in (
                "numeric", "paren_numeric", "alpha", "roman"
            ):
                _prev_nb = lines[idx - 1].strip() if idx > 0 else ""
                _next_nb = (
                    lines[idx + 1].strip() if idx + 1 < len(lines) else ""
                )
                if pattern_name == "numeric":
                    _rest_open = (
                        match.group(2)
                        or match.group(4)
                        or match.group(6)
                        or ""
                    )
                else:
                    _rest_open = match.group(2) or ""
                # L1 wrapped-tail guard: non-empty prev + rest
                # starting with an ASCII lowercase letter or a
                # non-alphanumeric symbol = mid-sentence wrap;
                # restricted to non-empty prev -- an isolated
                # lowercase rest between blank lines is a
                # separated list item. True heading rests
                # start with a capital letter/digit/CJK.
                _ch = _rest_open[:1]
                if _prev_nb and (
                    "a" <= _ch <= "z"
                    or (
                        _ch
                        and not _ch.isalnum()
                        and not ("\u4e00" <= _ch <= "\u9fff")
                        and _ch not in "。，、；：（）【】"
                    )
                ):
                    break
                # G1 TOC tail: an adjacent line with a dot leader
                # is a TOC-entry neighbor.
                if (_prev_nb and _TOC_LEADER_RE.search(_prev_nb)) or (
                    _next_nb and _TOC_LEADER_RE.search(_next_nb)
                ):
                    break
                # G2 reference wrap: non-empty prev ending with
                # section/chapter/... means this line is a
                # wrapped reference tail.
                if _prev_nb and _REF_WORD_TAIL_RE.search(_prev_nb):
                    break
                # G3 rest starting with ,/;: enumeration or
                # reference wrap tails.
                if _rest_open[:1] in ",;":
                    break
                # G4' prev ending with ,/; (paren/alpha only):
                # bibliography author wraps and algorithm
                # step lines.
                if (
                    pattern_name in ("paren_numeric", "alpha")
                    and _prev_nb
                    and _prev_nb[-1] in ",;"
                ):
                    break

            depth_hint = _resolve_depth_hint(pattern_name,match)

            candidates.append(
                HeadingCandidate(
                    raw=stripped,
                    pattern=pattern_name,
                    depth_hint=depth_hint,
                    start=line_starts[idx],
                    end=line_starts[idx] + len(line),
                    provenance=classify_heading_provenance(pattern_name),
                )
            )
            break

    # P1: setext underline heading recovery.
    _recover_setext(lines, fence_flags, line_starts, candidates)

    # P2: all-caps standalone heading recovery.
    _recover_caps_headings(lines, fence_flags, line_starts, candidates)

    # P3: hash-without-space markdown heading recovery.
    _recover_nospace_markdown(lines, fence_flags, line_starts, candidates)

    # P4: unnumbered functional-section heading recovery.
    _recover_word_headings(lines, fence_flags, line_starts, candidates)

    return _filter_prev_nonblank_neighbors(
        lines,
        _filter_candidate_runs(
            lines, _filter_ordered_list_items(lines, candidates)
        ),
    )


def _recover_setext(lines, fence_flags, line_starts, candidates):
    """Setext underline heading recovery: a title line followed by
    a full-line ``===`` / ``---``.

    Conservative conditions (any failure keeps the original
    verdict):
      1. title and underline lines are outside code fences;
      2. outside the frontmatter span (frontmatter entries + a
         closing ``---`` are the most common false shape);
      3. the title line carries readable text (letter/digit/CJK);
      4. the title line is not an underline itself (stacked
         underlines are separators);
      5. the title line is not an indented list item;
      6. ``---`` has >= 3 dashes (``--`` is a dash, not a rule);
      7. key: value shaped title lines are skipped under ``---``
         (structured-block residue; false-positive cost outweighs);
      8. no confirmed candidate at that position (main patterns
         win, no double booking).
    """
    fm_span = _frontmatter_span(lines)
    existing_starts = {c.start for c in candidates}
    n = len(lines)

    for idx in range(n - 1):
        title = lines[idx].strip()
        underline = lines[idx + 1].strip()
        if not title or not underline:
            continue
        if len(title) > MAX_HEADING_LEN:
            continue
        if fence_flags[idx] or (
            idx + 1 < len(fence_flags) and fence_flags[idx + 1]
        ):
            continue
        if fm_span and fm_span[0] <= idx <= fm_span[1]:
            continue
        if line_starts[idx] in existing_starts:
            continue

        if _SETEXT_EQ_RE.fullmatch(underline):
            level = 1
        elif _SETEXT_DASH_RE.fullmatch(underline):
            level = 2
        else:
            continue

        # An underline stacked on an underline: separator stack.
        if _SETEXT_EQ_RE.fullmatch(title) or _SETEXT_DASH_RE.fullmatch(title):
            continue
        # ASCII art: no readable text.
        if not _WORD_CHAR_RE.search(title):
            continue
        # Indented list item (frontmatter nested-list case).
        if _INDENTED_LIST_ITEM_RE.match(lines[idx]):
            continue
        # key: value shape + '---' underline: structured-block
        # residue.
        if level == 2 and _KEY_VALUE_LINE_RE.match(title):
            continue
        # Pipe-table row + underline: RFC box tables and state
        # diagrams carry '-----' row separators shaped like
        # setext headings. The test mirrors the contract-side
        # _is_table_line (stripped line starts and ends with
        # '|'), so exactly the lines the contract counts as
        # table rows are excluded here. A pipe row has no
        # typography basis to be a heading.
        if title.startswith("|") and title.endswith("|"):
            continue

        candidates.append(
            HeadingCandidate(
                raw=title,
                pattern="setext",
                depth_hint=level,
                start=line_starts[idx],
                end=line_starts[idx] + len(lines[idx]),
                # An underline is a typography convention, not an
                # explicit structural marker: inferred.
                provenance="inferred",
            )
        )

    candidates.sort(key=lambda c: c.start)


def _recover_caps_headings(lines, fence_flags, line_starts, candidates):
    """All-caps standalone heading recovery: the RFC / legal
    contract / technical-spec section convention.

    Conservative conditions:
      1. whole line of uppercase letters/digits/interpunctuation,
         >= 2 letters, length >= 4 (rejects "OK" / "N/A" /
         digits-only);
      2. outside code fences and frontmatter;
      3. blank prev or next line (standalone, not mid-sentence);
      4. no confirmed candidate at that position;
      5. no thousands-group digit token and fewer than two
         decimal tokens (data/total rows are table data, not
         headings).
    """
    fm_span = _frontmatter_span(lines)
    existing_starts = {c.start for c in candidates}
    n = len(lines)

    for idx, line in enumerate(lines):
        if fence_flags[idx]:
            continue
        if fm_span and fm_span[0] <= idx <= fm_span[1]:
            continue
        stripped = line.strip()
        if not (4 <= len(stripped) <= MAX_HEADING_LEN):
            continue
        if line_starts[idx] in existing_starts:
            continue
        if not _ALL_CAPS_RE.fullmatch(stripped):
            continue
        # TOC entry guard (same as the main loop).
        if _TOC_LEADER_RE.search(stripped):
            continue
        if len(_CAPS_LETTER_RE.findall(stripped)) < 2:
            continue
        if _CAPS_THOUSANDS_RE.search(stripped) or len(_CAPS_NUMERIC_TOKEN_RE.findall(stripped)) >= 2:
            continue

        prev_blank = idx == 0 or not lines[idx - 1].strip()
        next_blank = idx + 1 >= n or not lines[idx + 1].strip()

        # Tail-punct wrap guard: tail punctuation + non-empty prev
        # = wrapped tail (mid-sentence wraps, multi-line title
        # continuations, license clause tails, letter
        # signatures). True headings always start after a
        # blank line; neighbor shape is the only observable
        # evidence.
        if _CAPS_TAIL_PUNCT_RE.search(stripped) and not prev_blank:
            continue

        if not (prev_blank or next_blank):
            continue

        candidates.append(
            HeadingCandidate(
                raw=stripped,
                pattern="caps",
                depth_hint=1,
                start=line_starts[idx],
                end=line_starts[idx] + len(line),
                # All-caps standalone is a typography convention,
                # no structural marker: inferred.
                provenance="inferred",
            )
        )

    candidates.sort(key=lambda c: c.start)


def _recover_nospace_markdown(lines, fence_flags, line_starts, candidates):
    """Second recovery pass: markdown headings whose hash runs lack
    the trailing space ("###2.5分块器" style).

    The strict main regex stays untouched; promotion to candidate
    requires verifiable structural evidence:
      1. outside code fences;
      2. not a shebang or C preprocessor line;
      3. the hash count belongs to the document's confirmed
         markdown depth set;
      4. the content starts with a digit (continuing the level's
         numbering sequence).
    Without evidence the original verdict stands.
    """
    confirmed_depths = {
        c.depth_hint for c in candidates if c.pattern == "markdown"
    }
    if not confirmed_depths:
        return

    for idx, line in enumerate(lines):
        if fence_flags[idx]:
            continue
        stripped = line.strip()
        if not stripped or len(stripped) > MAX_HEADING_LEN:
            continue
        match = _HASH_NOSPACE_RE.match(stripped)
        if not match:
            continue
        hashes, content = match.group(1), match.group(2)
        if len(hashes) not in confirmed_depths:
            continue
        if _PSEUDO_HASH_RE.match(content):
            continue
        if not content[:1].isdigit():
            continue
        candidates.append(
            HeadingCandidate(
                raw=stripped,
                pattern="markdown",
                depth_hint=len(hashes),
                start=line_starts[idx],
                end=line_starts[idx] + len(line),
                # The explicit ``#`` marker exists (only the space
                # is missing, verified by numbering
                # continuity): declared.
                provenance="declared",
            )
        )
    candidates.sort(key=lambda c: c.start)

def _recover_word_headings(lines, fence_flags, line_starts, candidates):
    """Unnumbered functional-section heading recovery: a whole
    line that is exactly a functional-section word (contents /
    preface / references / acknowledgements -- paper, report and
    book convention).

    Conservative conditions (any failure keeps the original
    verdict):
      1. the stripped line is exactly a word-set word (case
         insensitive; all-caps shapes belong to the caps
         recovery);
      2. outside code fences and frontmatter;
      3. blank prev AND next line (standalone paragraph);
      4. no confirmed candidate at that position (no double
         booking);
      5. word length >= 2 (single characters are too noisy).
    """
    fm_span = _frontmatter_span(lines)
    existing_starts = {c.start for c in candidates}
    n = len(lines)

    for idx, line in enumerate(lines):
        if fence_flags[idx]:
            continue
        if fm_span and fm_span[0] <= idx <= fm_span[1]:
            continue
        stripped = line.strip()
        if not (2 <= len(stripped) <= 12):
            continue
        if line_starts[idx] in existing_starts:
            continue
        # Case-insensitive comparison (CJK unaffected by lower()).
        if stripped.lower() not in _WORD_HEADING_SET:
            continue

        prev_blank = idx == 0 or not lines[idx - 1].strip()
        next_blank = idx + 1 >= n or not lines[idx + 1].strip()
        if not (prev_blank and next_blank):
            # Standalone paragraph (blank on both sides) -- stricter
            # than caps: a prose paragraph line that happens
            # to equal a word-set word is excluded by the
            # double-blank test.
            continue

        candidates.append(
            HeadingCandidate(
                raw=stripped,
                pattern="word_section",
                depth_hint=1,
                start=line_starts[idx],
                end=line_starts[idx] + len(line),
                # No structural marker, pure typography convention:
                # inferred.
                provenance="inferred",
            )
        )

    candidates.sort(key=lambda c: c.start)


def _resolve_depth_hint(
        pattern_name,
        match
):
    if pattern_name == "markdown":
        return len(match.group(1))
    if pattern_name == "numeric":
        # Three alternatives in regex:
        #   A) group1 = dotted number (e.g. "1.1.1")
        #   B) group3 = number + 、 (e.g. "2")
        #   C) group5 = number + ./) (e.g. "1")
        num_str = match.group(1) or match.group(3) or match.group(5) or ""
        return num_str.count(".") + 1
    if pattern_name == "cn_lv1":
        return 1
    if pattern_name == "cn_lv2":
        return 2
    if pattern_name == "cn_chapter":
        # group(2) is the section suffix: 节 -> 2, 条 -> 3,
        # everything else (章/篇/部分/编/卷/回/讲/课/单元/幕) -> 1.
        if match.group(2) == "节":
            return 2
        if match.group(2) == "条":
            return 3
        return 1
    if pattern_name == "decoration":
        # MediaWiki semantics: marker count is the level.
        return len(match.group(1))
    if pattern_name == "paren_numeric":
        return 2
    if pattern_name == "alpha":
        return 3
    if pattern_name == "roman":
        return 1
    if pattern_name == "circled":
        return 3
    if pattern_name == "lettered_section":
        return 2
    if pattern_name == "bracket_cn":
        # Bracket sections sit at the cn_lv1 level (top level of
        # manuals/specs).
        return 1
    if pattern_name == "appendix_cn":
        return 1
    if pattern_name == "section_sign":
        # Dotted segments decide the level (as in numeric); without
        # dots, level 2.
        numbering = match.group(1) or ""
        return numbering.count(".") + 2 if "." in numbering else 2
    return 1
