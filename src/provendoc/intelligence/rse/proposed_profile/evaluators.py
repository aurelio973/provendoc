# -*- coding: utf-8 -*-
"""Deterministic evaluators for the proposed quality profile.

Evaluation order (events first, quality second): D1 -> D2 ->
D3 -> D4. All checks are deterministic rules; D0 emits raw
observation values only and produces no scoring events.

No double counting:
- table_split is booked by D1; that cut is excluded from
  D2's evaluatable-cut denominator.
- code_fence_broken is booked by D1 with
  diagnostic_views=["boundary_soundness"]; D2 reads that
  bypass view from the ledger instead of booking again.

diagnostic_views is a read-only cross-dimension bypass and
never changes event ownership.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from provendoc.intelligence.rse.proposed_profile.event_ledger import (
    DIM_BOUNDARY,
    DIM_CONTENT,
    DIM_CONTEXT,
    DIM_SEGMENTATION,
    DIM_STRUCTURAL,
    EventLedger,
    QualityEvent,
    policy_forced_for_merge,
    policy_forced_for_split,
)

# ----------------------------------
# ----------------------------------
# Text classification helpers (all deterministic)
# ----------------------------------

_FENCE_RE = re.compile(r"^\s{0,3}```")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s{0,3}(\|.+\|\s*|\+[-+]+\+\s*)$")
_TERMINAL_PUNCT = tuple(".!?:;。！？；：")
_LIST_MARKER_RE = re.compile(r"^\s{0,3}(?:[-*+]\s|\d+[.)]\s|o\s+\S)")
_CUE_START_RE = re.compile(
    # Explicit anaphora cues only: bare pronouns cannot be
    # told apart from prose deterministically, and false
    # positives pollute D3 scoring.
    r"^(?:上述|以上|如上所述|如前所述|前述|上文(?:提到|所述)?|上一[节章节步]|"
    r"as (?:mentioned|described|discussed|noted|stated|explained|shown) above|"
    r"as (?:previously|just) (?:mentioned|described|discussed|noted|stated)|"
    r"the aforementioned|the above(?:-|\s))",
    re.IGNORECASE,
)
_REF_CUE_RE = re.compile(
    r"(?:参见|参阅|见第?\s*\d|如图|如表|如下表|上图|下表|"
    r"see (?:section|figure|table|chapter)|refer to|as shown in)",
    re.IGNORECASE,
)
_HEADING_TARGET_RE = re.compile(r"\d+(?:\.\d+)*")

# Resource lines (markdown image / <img> / standalone link /
# bare URL / plain file name like "p007.jpg (150K)"): not
# prose, exempt from truncation checks.
_ASSET_LINE_RE = re.compile(
    r"^\s{0,3}(?:!\[[^\]]*\]\([^)]*\)|<img\b|\[[^\]]*\]\([^)]*\)\s*$|https?://"
    r"|[\w./\\-]+\.(?:jpe?g|png|gif|bmp|svg|webp|ico|pdf|zip|tar\w*)\b)",
    re.IGNORECASE,
)

# Prose-contiguous endings: letters / underscores / closing
# brackets / quotes / commas (excludes RFC header dates,
# bullet markers, heading markers; a trailing comma is the
# surest unfinished-sentence signal).
_PROSE_END_RE = re.compile(r"[A-Za-z_\)\]”’\",，、]$")

# Deterministic CJK truncation signal (the lowercase-
# continuation analogue for Chinese): comma/ideographic-
# comma ending plus a CJK start. Bare CJK endings are not
# judged: without letter case, a heading boundary cannot be
# told from a truncated sentence.
_ZH_COMMA_END_RE = re.compile(r"[，、,]$")
_CJK_START_RE = re.compile(r"^[一-鿿　-〿＀-￯]")

# Definition / grammar-block guards (ABNF, config,
# dictionary-style lines): a following `identifier =` line
# starts a new definition; a preceding `;` line is a
# grammar comment. Grammar boundaries are clean cuts, not
# truncation.
_DEFN_START_RE = re.compile(r"^\s{0,3}[A-Za-z0-9_-]+\s*:{0,2}=")
# ASN.1 definition lines ("... OBJECT IDENTIFIER ::= {...}"):
# the start of a new definition block, not prose
# continuation.
_ASN1_DEFN_RE = re.compile(
    r"^\s{0,3}[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)*\s*::="
)
# Grammar comment lines: ABNF ';' and ASN.1 '--' comments
# ('---' rules are excluded via lookahead; they are
# separators, not comments).
_GRAMMAR_COMMENT_RE = re.compile(r"^\s*(?:;|--(?!-))")
# Markdown link line endings ("](url)" tail): the resource
# line is closed; a lowercase paragraph after it is a
# paragraph boundary, not mid-sentence truncation.
_MD_LINK_TAIL_RE = re.compile(r"\]\([^)]*\)\s*$")
# Cross-document reference anchors ("of [HTTP]" / "RFC 1035
# [2]"): the target lives in another document and cannot be
# resolved in any chunk; exempted from
# unresolvable_reference.
_XDOC_REF_RE = re.compile(r"\b(?:RFC\s?\d+|of\s*\[[^\]]+\])")
# Code-shaped lines in unfenced sources (assignment / def /
# class / import / ASN.1 '::='): a '#' comment group whose
# next non-empty line is code-shaped is a code comment group,
# not headings.
_CODE_NEXT_RE = re.compile(
    r"^\s{0,3}(?:(?:def|class|return|import|from|raise|yield|lambda"
    r"|print)\b|[A-Za-z_][\w.]*\s*(?:=[^=]|\+=[^+=]|::=))"
)
# HTML comment delimiters (<!-- and -->): pairing is scanned
# outside fences, same as fence pairing.
_HTML_COMMENT_TOKEN_RE = re.compile(r"<!--|-->")
# Plain-text section heading shapes (no ATX markers):
# numbered English sections ("Section 3." / "Appendix A."
# / "Chapter 2.") and CJK volume/chapter markers. A
# heading-shaped line without terminal punctuation and
# length-bounded; full sentences rarely qualify (wrapped
# reference sentences expose themselves via terminal
# punctuation or lowercase continuation).
_PLAIN_SECTION_HEAD_RE = re.compile(
    r"^\s*(?:Section|SECTION|Chapter|CHAPTER|Appendix|APPENDIX)"
    r"\s+(?:\d+|[A-Z](?:\d+)?|[IVXLCDM]+)\b[.:\-]?\s*\S"
)
_CJK_VOLUME_HEAD_RE = re.compile(
    r"^\s*第[零一二三四五六七八九十百千万两]+[卷章節节回篇]"
    r"\s*[.。:：]?\s*$"
)
# Pure CJK chapter title lines: no punctuation, no Latin
# letters, 2-30 characters; only counted when paired with a
# volume marker line above.
_CJK_TITLE_LINE_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{2,30}$")
# Body-text start (uppercase letter or CJK): next-block
# guard for plain-text heading separation.
_UPPER_OR_CJK_START_RE = re.compile(r"^[A-Z\u3400-\u4dbf\u4e00-\u9fff]")
# Separator-only residue lines (lone "*" / "-" / em dash):
# typesetting separators, not semantic damage; exempted
# from degenerate checks.
_SEPARATOR_ONLY_RE = re.compile(r"^[\*\-—_=~·※]+$")

# Minimum combined size for a truncation split: short
# title-page or resource-list lines do not count. CJK
# carries 2-3x the information density of English, so it
# uses its own limit.
_MIN_SPLIT_COMBINED = 80
_MIN_SPLIT_COMBINED_CJK = 40


def _type_value(chunk) -> str:
    unit_type = getattr(chunk, "unit_type", None)
    return getattr(unit_type, "value", unit_type) or ""


def _chunk_id(chunk, index: int) -> str:
    sp = getattr(chunk, "semantic_primitives", None)
    identity = getattr(sp, "identity", None) if sp is not None else None
    return identity or f"chunk-{index}"


def _span_of(chunk) -> Tuple[Optional[int], Optional[int]]:
    ctx = getattr(chunk, "source_context", None)
    if ctx is None:
        return (None, None)
    return (
        getattr(ctx, "line_start", None),
        getattr(ctx, "line_end", None),
    )


def _fence_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if _FENCE_RE.match(line))


def _ends_inside_fence(text: str) -> bool:
    return _fence_lines(text) % 2 == 1


def _starts_inside_fence(text: str) -> bool:
    """Whether the chunk starts inside a fence (first
    non-empty line is not a fence marker but the text
    contains one)."""
    if "```" not in text:
        return False
    first = _first_line(text)
    return not _FENCE_RE.match(first)


def _comment_tokens(text: str) -> List[str]:
    """HTML comment delimiters (<!-- / -->) outside fences,
    in order.

    Delimiters inside fences are code examples, not
    document comments."""
    tokens: List[str] = []
    for line in _iter_lines_outside_fences(text):
        if "<!--" in line or "-->" in line:
            tokens.extend(
                m.group()
                for m in _HTML_COMMENT_TOKEN_RE.finditer(line)
            )
    return tokens


def _ends_inside_comment(text: str) -> bool:
    """Whether the chunk ends inside an unclosed HTML
    comment."""
    state = False
    for tok in _comment_tokens(text):
        if tok == "<!--" and not state:
            state = True
        elif tok == "-->" and state:
            state = False
    return state


def _starts_inside_comment(text: str) -> bool:
    """Whether the chunk starts inside an HTML comment (the first delimiter is the closing side)."""
    tokens = _comment_tokens(text)
    return bool(tokens) and tokens[0] == "-->"


def _plain_trailing_heading(cur_text: str) -> Optional[str]:
    """Whether the previous block's last line is a plain-text
    section heading (no ATX marker).

    Criteria (all deterministic): not an ATX heading,
    <= 80 chars, no terminal punctuation; then one of a
    numbered English section shape (Section/Chapter/
    Appendix + number), a standalone CJK volume/chapter
    marker, a CJK volume marker followed by a pure CJK
    title line, or a bare Title Case single-line title
    block. Full sentences rarely qualify; wrapped reference
    sentences are exposed by terminal punctuation or
    lowercase continuation.
    """
    last = _last_line(cur_text)
    if not last or _HEADING_RE.match(last):
        return None
    if last.endswith(_TERMINAL_PUNCT):
        return None
    lines = [
        ln.strip()
        for ln in cur_text.rstrip().splitlines()
        if ln.strip() and not _CONTEXT_LINE_RE.match(ln)
    ]
    # Setext heading pair: the last line is a pure -/= underline
    # and the previous non-empty line is a valid heading line, so
    # the whole setext heading stays in the trailing block while
    # the body falls into the next one. Figure rules and table
    # separators are excluded by the line-level checks.
    if _SETEXT_UNDERLINE_RE.match(last):
        if len(lines) >= 2 and (
            _title_case_line(lines[-2])
            or _PLAIN_SECTION_HEAD_RE.match(lines[-2])
        ):
            return lines[-2]
        return None
    if len(last) > 80:
        return None
    if _PLAIN_SECTION_HEAD_RE.match(last):
        return last
    if _CJK_VOLUME_HEAD_RE.match(last):
        return last
    if (
        len(lines) >= 2
        and _CJK_VOLUME_HEAD_RE.match(lines[-2])
        and _CJK_TITLE_LINE_RE.match(last)
    ):
        return f"{lines[-2]} {last}"
    return _bare_trailing_title(lines, last)


# Bare Title Case heading words: alphanumeric words, including
# apostrophe/hyphen compounds.
_TITLE_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*")
# Conventional lowercase stop-words in Title Case.
_TITLE_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "from",
    "in", "into", "nor", "of", "on", "onto", "or", "per",
    "the", "to", "up", "via", "with",
})
# Setext underline line (CommonMark second line: pure -/= runs of
# 3+, any length). An underline tail with a valid heading line
# above forms a setext heading block that stays in the trailing
# block, same as a single-line bare title.
_SETEXT_UNDERLINE_RE = re.compile(r"^(?:-{3,}|={3,})$")

# Metadata field lines ("Title: ..." / "Author: ...").
_FIELD_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z ]{2,30}:\s+\S")


def _title_case_line(line: str) -> bool:
    """Line-level bare Title Case heading test (no number,
    no ATX marker).

    Criteria (all deterministic): not an ATX/list/table/
    resource/definition marker line, no leading ``*``
    decoration; no double quotes; not a metadata field
    line; not a self-contained copyright notice; 12 <=
    length <= 80, no terminal punctuation, >= 3 words,
    each capitalized or a lowercased stop-word or
    digit-initial. Reused on both the cur side (single-line
    block) and the nxt side (heading-run guard).
    """
    if not line or _HEADING_RE.match(line):
        return False
    if (
        _LIST_MARKER_RE.match(line)
        or _TABLE_ROW_RE.match(line)
        or _ASSET_LINE_RE.match(line)
        or _DEFN_START_RE.match(line)
    ):
        return False
    if line.startswith("*"):
        return False
    # Running page headers (column-aligned RFC page headers):
    # four or more consecutive spaces mean a real heading would
    # not align itself that way.
    if "    " in line:
        return False
    if '"' in line or "\u201c" in line or "\u201d" in line:
        return False
    # Metadata field lines.
    if _FIELD_LINE_RE.match(line):
        return False
    # Copyright lines (including decorated variants) are
    # self-contained notices, not headings; leading decoration is
    # stripped before the test so real dedication lines survive.
    if line.lstrip("[_(\"'*- ").lower().startswith("copyright"):
        return False
    if len(line) < 12 or len(line) > 80 or line.endswith(_TERMINAL_PUNCT):
        return False
    words = _TITLE_WORD_RE.findall(line)
    if len(words) < 3:
        return False
    for w in words:
        if w.lower() in _TITLE_STOPWORDS:
            continue
        if w[0].isupper() or w[0].isdigit():
            continue
        return False
    return True


# Looser stop-word set for caption-style short lines, used only
# by _caption_block's next-block check.
_CAPTION_STOPWORDS = _TITLE_STOPWORDS | frozenset({
    "that", "this", "these", "those", "his", "her", "its", "their",
    "our", "your", "my", "one", "two", "three", "four", "five",
    "first", "second", "third", "last", "next", "after", "before",
    "when", "where", "while", "than", "then", "so", "not", "nor",
    "at", "but", "if", "or", "over", "under", "out", "off",
    "down", "up", "upon", "without", "within", "from",
})


def _caption_block(text: str) -> bool:
    """Whether the next block is a short caption/entry-list
    block.

    Criteria (all deterministic): 1-3 non-empty lines, each
    <= 40 chars with ASCII letters, no declarative terminal
    punctuation (caption ?/! endings are fine), every word
    capitalized or a loose stop-word or digit-initial.
    Cutting between caption entries is not heading-body
    separation; a real separation leaves a prose paragraph
    (long lines, lowercase words, end punctuation) that
    fails the test.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines or len(lines) > 3:
        return False
    for line in lines:
        if not line or len(line) > 40:
            return False
        # Declarative terminal punctuation -> a prose sentence.
        if line.rstrip().endswith((".", ":", ";")):
            return False
        if not any(ch.isascii() and ch.isalpha() for ch in line):
            return False
        words = _TITLE_WORD_RE.findall(line)
        if not words:
            return False
        for w in words:
            if w.lower() in _CAPTION_STOPWORDS or w[0].isupper() or w[0].isdigit():
                continue
            return False
    return True


def _bare_trailing_title(lines, last) -> Optional[str]:
    """Bare Title Case single-line title block (license
    titles, title pages; no numbering).

    The block has exactly one non-empty line and it passes
    _title_case_line. Next-block guards live in the
    caller's loop.
    """
    if len(lines) != 1:
        return None
    return lines[0] if _title_case_line(lines[0]) else None


# Context propagation prefix lines ("[Context: ...]"): injected
# metadata, not body text. _first_line/_last_line skip them so
# the real first/last lines are visible to D2/D3/D4.
_CONTEXT_LINE_RE = re.compile(r"^\s{0,3}\[Context\b[^\]]*\]")

# Front-matter/metadata block shapes (>= 80% of non-empty lines
# must match): YAML keys, '---' separators, standalone HTML
# tags, RFC page headers/footers, TOC dotted lines, resource
# listings.
_FRONTMATTER_LINE_RE = re.compile(r"^[A-Za-z0-9_.\-]+:(?:\s|$)")
_HTML_TAG_LINE_RE = re.compile(r"^</?[A-Za-z][^>]*>$")
_RFC_PAGE_LINE_RE = re.compile(r"^RFC\s?\d+\b|\[Page \d+\]\s*$|Internet-Draft")
_TOC_LINE_RE = re.compile(r"\.{4,}\s*\d+\s*$")


def _norm_heading(heading: str) -> str:
    """Normalize a heading: strip the leading '#' and
    whitespace.

    heading_path elements may keep the '#' prefix while
    source headings do not; comparing without normalization
    never matches and falls back to substring matching.
    """
    return heading.lstrip("#").strip()


def _iter_lines_outside_fences(text: str):
    """Yield lines outside code fences; fence markers and
    their contents are skipped.

    '#' lines inside fences are code comments, not headings;
    counting them would pollute both the source-side and
    representation-side heading scans with false events.
    """
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def _source_headings(document_text: str) -> List[str]:
    """Source document headings (fence-aware, code-adjacency
    guard).

    In unfenced sources '#' comment lines match the ATX
    shape; the deterministic signature of a comment group
    is that the next non-empty line after the group is
    code-shaped (assignment / def / class / import /
    '::='), while a real heading group is followed by
    prose, list or table. The whole group is dropped
    (single-line groups included).
    """
    lines = list(_iter_lines_outside_fences(document_text))
    headings: List[str] = []
    n = len(lines)
    i = 0
    while i < n:
        if not _HEADING_RE.match(lines[i]):
            i += 1
            continue
        # ATX group: consecutive heading lines (blank lines
        # between).
        group = [i]
        j = i + 1
        while j < n:
            if not lines[j].strip():
                j += 1
                continue
            if _HEADING_RE.match(lines[j]):
                group.append(j)
                j += 1
            else:
                break
        # Group followed by a code-shaped line -> comment group,
        # drop it.
        k = group[-1] + 1
        while k < n and not lines[k].strip():
            k += 1
        if not (k < n and _CODE_NEXT_RE.match(lines[k])):
            for g in group:
                heading_match = _HEADING_RE.match(lines[g])
                if heading_match is not None:
                    headings.append(heading_match.group(2).strip())
        i = j
    return headings


def _headings_in_text(text: str) -> List[Tuple[int, str]]:
    """Embedded heading lines in a chunk -> [(level, text)]
    (fence-aware).

    Comment-continuation guard: a lower-case "heading
    text" with adjacent '#' lines or a code-shaped
    follower is a comment, not a heading. Real headings
    start upper-case/numeric/CJK and are blank-line
    separated.
    """
    found: List[Tuple[int, str]] = []
    lines = list(_iter_lines_outside_fences(text))
    n = len(lines)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        heading_text = m.group(2).strip()
        if heading_text and heading_text[0].islower():
            adjacent_hash = (
                (i > 0 and _HEADING_RE.match(lines[i - 1]))
                or (i + 1 < n and _HEADING_RE.match(lines[i + 1]))
            )
            k = i + 1
            while k < n and not lines[k].strip():
                k += 1
            code_next = k < n and _CODE_NEXT_RE.match(lines[k])
            if adjacent_hash or code_next:
                continue
        found.append((len(m.group(1)), heading_text))
    return found


def _last_line(text: str) -> str:
    lines = [
        ln for ln in text.rstrip().splitlines()
        if ln.strip() and not _CONTEXT_LINE_RE.match(ln)
    ]
    return lines[-1].strip() if lines else ""


def _first_line(text: str) -> str:
    lines = [
        ln for ln in text.lstrip().splitlines()
        if ln.strip() and not _CONTEXT_LINE_RE.match(ln)
    ]
    return lines[0].strip() if lines else ""


def _is_metadata_block(text: str) -> bool:
    """Whether the whole block is front-matter/metadata (TOC,
    frontmatter, resource listing, RFC page furniture).
    Such blocks belong to no section; missing heading
    context is the document's structure, not an ingestion
    defect -- exempted from missing_heading_context.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = 0
    for ln in lines:
        if (
            _ASSET_LINE_RE.match(ln)
            or _FRONTMATTER_LINE_RE.match(ln)
            or _HTML_TAG_LINE_RE.match(ln)
            or _RFC_PAGE_LINE_RE.search(ln)
            or _TOC_LINE_RE.search(ln)
            or ln in ("---", "***", "<br/>", "<br>")
        ):
            hits += 1
    return hits >= 0.8 * len(lines)


def _has_leading_heading(text: str) -> bool:
    """Whether the chunk carries its own heading context: an
    ATX heading within the first 2 non-empty lines of the
    block (skipping [Context] lines, blank lines and the
    YAML frontmatter region).

    A leading heading means the context is in the chunk
    itself and missing_heading_context does not apply.
    Frontmatter is metadata, so a heading after it still
    counts as block-leading.
    """
    lines = list(_iter_lines_outside_fences(text))
    start = 0
    if lines and lines[0].strip() == "---":
        for k in range(1, len(lines)):
            if lines[k].strip() == "---":
                start = k + 1
                break
    seen = 0
    for ln in lines[start:]:
        if not ln.strip() or _CONTEXT_LINE_RE.match(ln):
            continue
        if _HEADING_RE.match(ln):
            return True
        seen += 1
        if seen >= 2:
            return False
    return False


# ==================================
# D1 structural_integrity
# ==================================

def evaluate_structural(
    chunks: List[Any],
    document_text: Optional[str],
    ledger: EventLedger,
) -> Dict[str, Any]:
    """D1: fence pairing / table integrity / orphan headings /
    heading survival.

    Denominator = total chunks. Events:
        code_fence_broken    unbalanced fence inside chunks
                             (critical; diagnostic_views=
                             [boundary_soundness])
        table_split          adjacent TABLE chunks with the
                             same heading path split apart
                             (error)
        orphan_heading       HEADING with no content after
                             it (error)
        heading_survival_gap source heading missing
                             entirely from the representation
                             (error)
    """
    total = len(chunks)
    observation: Dict[str, Any] = {
        "dimension": DIM_STRUCTURAL,
        "events": 0,
        "denominator": total,
        "detail": {},
    }
    if total == 0:
        observation["applicable"] = False
        return observation
    observation["applicable"] = True

    # --- code_fence_broken (cross-chunk scan, per-cut, deduped) ---
    # Scan the fence state chunk by chunk: still inside a fence
    # at a chunk end means the cut after it broke the fence. One
    # open fence is booked once; re-booking per chunk would
    # inflate one cut into dozens of events.
    fence_breaks = 0
    in_fence = False
    booked_for_current_fence = False
    for idx, chunk in enumerate(chunks):
        text = getattr(chunk, "text", "") or ""
        if "```" in text:
            for line in text.splitlines():
                if _FENCE_RE.match(line):
                    in_fence = not in_fence
        if in_fence:
            if not booked_for_current_fence:
                fence_breaks += 1
                booked_for_current_fence = True
                ledger.book(QualityEvent(
                    event_id="",
                    event_type="code_fence_broken",
                    primary_dimension=DIM_STRUCTURAL,
                    chunk_ids=[_chunk_id(chunk, idx)],
                    severity="critical",
                    source_spans=[_span_of(chunk)],
                    diagnostic_views=[DIM_BOUNDARY],
                    detail={"cut_after_chunk": idx},
                ))
        else:
            booked_for_current_fence = False
    observation["detail"]["code_fence_broken"] = fence_breaks

    # --- html_comment_split (same cross-chunk pattern as fences) ---
    # Delimiter pairing is scanned outside fences; an unclosed
    # comment at a chunk end means the cut split the comment
    # block. The booked flag prevents re-booking the same open
    # comment.
    comment_breaks = 0
    in_comment = False
    booked_for_current_comment = False
    for idx, chunk in enumerate(chunks):
        text = getattr(chunk, "text", "") or ""
        for tok in _comment_tokens(text):
            if tok == "<!--" and not in_comment:
                in_comment = True
            elif tok == "-->" and in_comment:
                in_comment = False
        if in_comment:
            if not booked_for_current_comment:
                comment_breaks += 1
                booked_for_current_comment = True
                ledger.book(QualityEvent(
                    event_id="",
                    event_type="html_comment_split",
                    primary_dimension=DIM_STRUCTURAL,
                    chunk_ids=[_chunk_id(chunk, idx)],
                    severity="critical",
                    source_spans=[_span_of(chunk)],
                    diagnostic_views=[DIM_BOUNDARY],
                    detail={"cut_after_chunk": idx},
                ))
        else:
            booked_for_current_comment = False
    observation["detail"]["html_comment_split"] = comment_breaks

    # --- table_split (adjacent TABLE, same heading path) ---
    table_splits = 0
    table_split_boundaries = set()  # D2 防双计：被拆表格的切点索引
    for idx in range(total - 1):
        cur, nxt = chunks[idx], chunks[idx + 1]
        if (
            _type_value(cur) == "table"
            and _type_value(nxt) == "table"
            and list(getattr(cur, "heading_path", None) or [])
            == list(getattr(nxt, "heading_path", None) or [])
        ):
            table_splits += 1
            table_split_boundaries.add(idx)
            ledger.book(QualityEvent(
                event_id="",
                event_type="table_split",
                primary_dimension=DIM_STRUCTURAL,
                chunk_ids=[_chunk_id(cur, idx), _chunk_id(nxt, idx + 1)],
                severity="error",
                source_spans=[_span_of(cur), _span_of(nxt)],
                detail={"heading_path": list(cur.heading_path or [])},
            ))
    observation["detail"]["table_split"] = table_splits
    observation["detail"]["_table_split_boundaries"] = sorted(
        table_split_boundaries
    )

    # --- orphan_heading ---
    orphans = 0
    for idx, chunk in enumerate(chunks):
        if _type_value(chunk) != "heading":
            continue
        is_last = idx == total - 1
        next_is_heading = (
            not is_last and _type_value(chunks[idx + 1]) == "heading"
        )
        if is_last or next_is_heading:
            orphans += 1
            ledger.book(QualityEvent(
                event_id="",
                event_type="orphan_heading",
                primary_dimension=DIM_STRUCTURAL,
                chunk_ids=[_chunk_id(chunk, idx)],
                severity="error",
                source_spans=[_span_of(chunk)],
                detail={
                    "position": "sequence_end" if is_last
                    else "followed_by_heading"
                },
            ))
    observation["detail"]["orphan_heading"] = orphans

    # --- heading_survival_gap (needs source text) ---
    gaps = 0
    if document_text:
        source_headings = _source_headings(document_text)
        if source_headings:
            # Fence-aware: '#' comment lines inside fences do not
            # count as heading survival; path elements are
            # normalized before joining.
            corpus = "\n".join(
                line
                for c in chunks
                for line in _iter_lines_outside_fences(
                    getattr(c, "text", "") or ""
                )
            )
            paths = "\n".join(
                _norm_heading(p)
                for c in chunks
                for p in (getattr(c, "heading_path", None) or [])
            )
            for heading in source_headings:
                norm = _norm_heading(heading)
                if norm not in corpus and norm not in paths:
                    gaps += 1
                    ledger.book(QualityEvent(
                        event_id="",
                        event_type="heading_survival_gap",
                        primary_dimension=DIM_STRUCTURAL,
                        chunk_ids=[],
                        severity="error",
                        detail={"heading": heading},
                    ))
    observation["detail"]["heading_survival_gap"] = gaps
    observation["events"] = (
        fence_breaks + comment_breaks + table_splits + orphans + gaps
    )
    return observation


# ==================================
# D2 boundary_soundness
# ==================================

def evaluate_boundary(
    chunks: List[Any],
    document_text: Optional[str],
    ledger: EventLedger,
    structural_observation: Dict[str, Any],
) -> Dict[str, Any]:
    """D2: boundary legality rates.

        invalid_boundary_rate = illegal cuts / evaluatable
                                cuts
        missed_protected_boundary_rate = missed boundaries /
            required boundaries

    Evaluatable cuts are adjacent-chunk cuts excluding those
    already booked as table_split by D1. Fence-class illegal
    cuts are read back from D1's diagnostic_views, not
    re-booked.
    """
    total = len(chunks)
    observation: Dict[str, Any] = {
        "dimension": DIM_BOUNDARY,
        "invalid_boundary_rate": None,
        "missed_protected_boundary_rate": None,
        "denominator": max(total - 1, 0),
        "detail": {},
    }
    if not chunks:
        observation["applicable"] = False
        return observation
    observation["applicable"] = True

    skipped = set(
        structural_observation.get("detail", {}).get(
            "_table_split_boundaries", []
        )
    )

    # --- illegal cuts (fence/comment class: read from D1 views) ---
    fence_cuts = 0
    comment_cuts = 0
    for event in ledger.events:
        if (
            event.event_type == "code_fence_broken"
            and DIM_BOUNDARY in (event.diagnostic_views or [])
        ):
            fence_cuts += 1
        elif (
            event.event_type == "html_comment_split"
            and DIM_BOUNDARY in (event.diagnostic_views or [])
        ):
            comment_cuts += 1

    # --- illegal cuts (table-row continuation class) ---
    table_continuation_cuts = 0
    for idx in range(max(total - 1, 0)):
        if idx in skipped:
            continue
        prev_last = _last_line(getattr(chunks[idx], "text", "") or "")
        next_first = _first_line(getattr(chunks[idx + 1], "text", "") or "")
        if (
            prev_last
            and next_first
            and _TABLE_ROW_RE.match(prev_last)
            and _TABLE_ROW_RE.match(next_first)
        ):
            table_continuation_cuts += 1
            ledger.book(QualityEvent(
                event_id="",
                event_type="illegal_boundary",
                primary_dimension=DIM_BOUNDARY,
                chunk_ids=[
                    _chunk_id(chunks[idx], idx),
                    _chunk_id(chunks[idx + 1], idx + 1),
                ],
                severity="error",
                source_spans=[
                    _span_of(chunks[idx]), _span_of(chunks[idx + 1])
                ],
                detail={"kind": "table_row_continuation"},
            ))

    # --- illegal cuts (plain-text heading separation class) ---
    # The previous block's last line is a plain-text section
    # heading and its body falls into the next block: the
    # heading-body boundary was cut. The next-block guard
    # requires a body first line (upper-case/CJK start, not a
    # heading/list/table/resource/definition line).
    plain_heading_cuts = 0
    for idx in range(max(total - 1, 0)):
        if idx in skipped:
            continue
        cur_t, nxt_t = chunks[idx], chunks[idx + 1]
        if list(getattr(cur_t, "heading_path", None) or []) != list(
            getattr(nxt_t, "heading_path", None) or []
        ):
            continue
        cur_text = getattr(cur_t, "text", "") or ""
        plain_heading = _plain_trailing_heading(cur_text)
        if plain_heading is None:
            continue
        # Source-existence guard: volume marker and chapter title
        # are separate lines in the source, checked individually.
        if document_text and not all(
            part in document_text for part in plain_heading.split()
        ):
            continue
        nxt_text = getattr(nxt_t, "text", "") or ""
        nxt_first = _first_line(nxt_text)
        # TOC guard: the next block also starts with a section
        # heading shape -> a run of TOC entries; the cut sits
        # between whole entries and is not heading-body
        # separation.
        if (
            _PLAIN_SECTION_HEAD_RE.match(nxt_first or "")
            or _CJK_VOLUME_HEAD_RE.match(nxt_first or "")
        ):
            continue
        # Unit-boundary guard: a short all-caps heading first line
        # in the next block means a legal boundary between two
        # structural units, not heading-body separation.
        if (
            nxt_first
            and any(ch.isascii() and ch.isalpha() for ch in nxt_first)
            and nxt_first == nxt_first.upper()
            and len(nxt_first.strip()) <= 40
            and not nxt_first.rstrip().endswith(_TERMINAL_PUNCT)
        ):
            continue
        # Heading-run guard: the next block's first line is also a
        # bare Title Case heading -> the cut sits between
        # headings, both blocks start with headings, not
        # separation.
        if _title_case_line(nxt_first or ""):
            continue
        # Caption guard: the next block is a short caption/entry
        # list -> the cut sits between whole entries, not
        # separation.
        if _caption_block(nxt_text):
            continue
        if (
            not nxt_first
            or _HEADING_RE.match(nxt_first)
            or not _UPPER_OR_CJK_START_RE.match(nxt_first)
            or _LIST_MARKER_RE.match(nxt_first)
            or _TABLE_ROW_RE.match(nxt_first)
            or _ASSET_LINE_RE.match(nxt_first)
            or _DEFN_START_RE.match(nxt_first)
        ):
            continue
        # The next block repeats the heading as a whole line ->
        # the heading travels with it, not detached. Whole-line
        # equality only: substring containment would swallow
        # true positives where link text happens to contain the
        # title.
        nxt_repeat_lines = [
            ln.strip()
            for ln in _iter_lines_outside_fences(nxt_text)
            if ln.strip() and not _CONTEXT_LINE_RE.match(ln)
        ]
        if any(ln == plain_heading for ln in nxt_repeat_lines):
            continue
        plain_heading_cuts += 1
        ledger.book(QualityEvent(
            event_id="",
            event_type="illegal_boundary",
            primary_dimension=DIM_BOUNDARY,
            chunk_ids=[
                _chunk_id(cur_t, idx),
                _chunk_id(nxt_t, idx + 1),
            ],
            severity="warning",
            source_spans=[_span_of(cur_t), _span_of(nxt_t)],
            detail={
                "kind": "trailing_plain_heading_detached",
                "heading": plain_heading,
            },
        ))

    illegal = fence_cuts + comment_cuts + table_continuation_cuts + plain_heading_cuts
    evaluatable = max(total - 1 - len(skipped), 0)
    observation["invalid_boundary_rate"] = (
        round(illegal / evaluatable, 4) if evaluatable else None
    )
    observation["detail"]["illegal_cuts"] = illegal
    observation["detail"]["fence_cuts_via_views"] = fence_cuts
    observation["detail"]["comment_cuts_via_views"] = comment_cuts
    observation["detail"]["table_continuation_cuts"] = (
        table_continuation_cuts
    )
    observation["detail"]["plain_heading_cuts"] = plain_heading_cuts
    observation["detail"]["evaluatable_cuts"] = evaluatable
    observation["detail"]["skipped_table_split_boundaries"] = sorted(
        skipped
    )

    # --- required boundaries (source heading edges) kept? ---
    if document_text:
        source_headings = _source_headings(document_text)
        if source_headings:
            # Per-chunk precomputation of the scan surface and the
            # normalized heading paths; identical results, but
            # avoids rebuilding the scan per (heading, chunk)
            # pair.
            chunk_scans = [
                "\n".join(
                    ln for ln in _iter_lines_outside_fences(
                        getattr(chunk, "text", "") or ""
                    )
                    if not _CONTEXT_LINE_RE.match(ln)
                )
                for chunk in chunks
            ]
            chunk_path_norms = [
                [
                    _norm_heading(p)
                    for p in (getattr(chunk, "heading_path", None) or [])
                ]
                for chunk in chunks
            ]
            missed = 0
            for heading in source_headings:
                norm = _norm_heading(heading)
                carrier = None
                for idx, chunk in enumerate(chunks):
                    # Scan surface: lines outside fences with
                    # [Context] lines removed.
                    in_text = norm in chunk_scans[idx]
                    in_path = norm in chunk_path_norms[idx]
                    if in_path:
                        carrier = None  # 标题路径保留了——边界语义在
                        break
                    if in_text and carrier is None:
                        carrier = (idx, chunk)
                if carrier is not None:
                    idx, chunk = carrier
                    # The heading sits mid-chunk: its leading
                    # boundary was swallowed. Only when it is not
                    # the chunk's first heading line.
                    chunk_headings = _headings_in_text(
                        getattr(chunk, "text", "") or ""
                    )
                    if (
                        not chunk_headings
                        or _norm_heading(chunk_headings[0][1]) != norm
                    ):
                        missed += 1
                        ledger.book(QualityEvent(
                            event_id="",
                            event_type="missed_protected_boundary",
                            primary_dimension=DIM_BOUNDARY,
                            chunk_ids=[_chunk_id(chunk, idx)],
                            severity="warning",
                            source_spans=[_span_of(chunk)],
                            detail={"heading": heading},
                        ))
            # --- trailing_heading_detached ---
            # The heading line stays at the previous chunk's tail
            # while its body falls into the next chunk, and the
            # next chunk's delivered text (including injected
            # [Context] lines) does not contain the heading: the
            # protected heading-body boundary was cut. Judged on
            # delivered text, not the heading_path field, which
            # can be out of sync with the injected lines.
            # No double counting: a block-leading heading is
            # covered by the swallow check above; non-leading
            # headings by this one. A next block starting with a
            # heading owns it, so skip.
            trailing = 0
            for tidx in range(max(total - 1, 0)):
                cur_t, nxt_t = chunks[tidx], chunks[tidx + 1]
                if list(getattr(cur_t, "heading_path", None) or []) != list(
                    getattr(nxt_t, "heading_path", None) or []
                ):
                    continue
                cur_text = getattr(cur_t, "text", "") or ""
                tmatch = _HEADING_RE.match(_last_line(cur_text))
                if not tmatch:
                    continue
                tnorm = _norm_heading(tmatch.group(2))
                cur_heads = _headings_in_text(cur_text)
                if not cur_heads or (
                    _norm_heading(cur_heads[0][1]) != tnorm
                ):
                    continue
                if _HEADING_RE.match(
                    _first_line(getattr(nxt_t, "text", "") or "")
                ):
                    continue
                nxt_scan = "\n".join(
                    _iter_lines_outside_fences(
                        getattr(nxt_t, "text", "") or ""
                    )
                )
                if tnorm in nxt_scan:
                    continue
                trailing += 1
                missed += 1
                ledger.book(QualityEvent(
                    event_id="",
                    event_type="missed_protected_boundary",
                    primary_dimension=DIM_BOUNDARY,
                    chunk_ids=[
                        _chunk_id(cur_t, tidx),
                        _chunk_id(nxt_t, tidx + 1),
                    ],
                    severity="warning",
                    source_spans=[
                        _span_of(cur_t), _span_of(nxt_t)
                    ],
                    detail={
                        "heading": tmatch.group(2),
                        "kind": "trailing_heading_detached",
                    },
                ))
            observation["detail"]["trailing_heading_detached"] = trailing
            observation["missed_protected_boundary_rate"] = round(
                missed / len(source_headings), 4
            )
            observation["detail"]["missed_protected_boundaries"] = missed
            observation["detail"]["source_headings"] = len(source_headings)
    return observation


# ==================================
# D3 contextual_sufficiency
# ==================================

def evaluate_context(
    chunks: List[Any],
    document_text: Optional[str],
    ledger: EventLedger,
) -> Dict[str, Any]:
    """D3: every chunk must be self-sufficient. Four contract
    checks (all deterministic):

    dangling_opening        chunk without a heading path
                            opens with an explicit anaphora
                            cue (warning; bare pronouns not
                            judged)
    missing_heading_context document has headings but the
                            chunk has no heading path
                            (warning)
    unresolvable_reference  explicit cross-reference cue
                            whose numbered target is not in
                            the chunk (warning)
    context_overload        overlap repetition exceeds half
                            the chunk text (warning)
    """
    total = len(chunks)
    observation: Dict[str, Any] = {
        "dimension": DIM_CONTEXT,
        "events": 0,
        "denominator": total,
        "detail": {
            "dangling_opening": 0,
            "missing_heading_context": 0,
            "unresolvable_reference": 0,
            "context_overload": 0,
        },
    }
    if total == 0:
        observation["applicable"] = False
        return observation
    observation["applicable"] = True

    doc_has_headings = bool(document_text and _source_headings(document_text))

    # Preface boundary: the index of the first chunk carrying a
    # structural heading; blocks before it are document front
    # matter (cover badges, intro, TOC) and belong to no
    # section, so they are exempt from missing_heading_context.
    # Structural anchor = non-empty heading path or an embedded
    # level >= 2 heading; a level-1 heading is document-level
    # and does not open section structure. With no level >= 2
    # anchor anywhere, fall back to the first embedded heading
    # of any level, else 0 (no exemption: losing all headings is
    # exactly the event to record).
    first_structure_idx = None
    fallback_idx = None
    for i, c in enumerate(chunks):
        if (getattr(c, "heading_path", None) or []):
            first_structure_idx = i
            break
        heads = _headings_in_text(getattr(c, "text", "") or "")
        if any(lv >= 2 for lv, _ in heads):
            first_structure_idx = i
            break
        if heads and fallback_idx is None:
            fallback_idx = i
    if first_structure_idx is None:
        first_structure_idx = (
            fallback_idx if fallback_idx is not None else 0
        )

    for idx, chunk in enumerate(chunks):
        text = getattr(chunk, "text", "") or ""
        heading_path = list(getattr(chunk, "heading_path", None) or [])
        cid = _chunk_id(chunk, idx)

        # 1. dangling_opening: no heading context but opens with
        #    an anaphora cue ([Context] lines skipped).
        first = _first_line(text)
        if first and not heading_path and _CUE_START_RE.match(first):
            observation["detail"]["dangling_opening"] += 1
            ledger.book(QualityEvent(
                event_id="",
                event_type="dangling_opening",
                primary_dimension=DIM_CONTEXT,
                chunk_ids=[cid],
                severity="warning",
                source_spans=[_span_of(chunk)],
                detail={"opening": first[:40]},
            ))

        # 2. missing_heading_context: document has headings but
        #    the chunk has none. Exemptions: preface blocks
        #    before the first structural anchor, whole-block
        #    metadata shapes, and blocks with a leading ATX
        #    heading.
        if (
            doc_has_headings
            and not heading_path
            and idx >= first_structure_idx
            and not _is_metadata_block(text)
            and not _has_leading_heading(text)
        ):
            observation["detail"]["missing_heading_context"] += 1
            ledger.book(QualityEvent(
                event_id="",
                event_type="missing_heading_context",
                primary_dimension=DIM_CONTEXT,
                chunk_ids=[cid],
                severity="warning",
                source_spans=[_span_of(chunk)],
                detail={"document_has_headings": True},
            ))

        # 3. unresolvable_reference: the numbered target is not
        #    in this chunk. Precision first: take the nearest
        #    numbered target after the cue; other numbers in
        #    the cue window (years, RFC numbers) are not
        #    targets. The target is resolvable if it appears
        #    elsewhere in the chunk or in the heading path;
        #    unnumbered cues are not recorded.
        for m in _REF_CUE_RE.finditer(text):
            after = text[m.end(): m.end() + 30]
            tm = _HEADING_TARGET_RE.search(after)
            if not tm:
                continue
            # Cross-document references ("RFC 1035 [2]" /
            # "of [HTTP]"): the target is in another document
            # and cannot be resolved in any chunk; exempt.
            if _XDOC_REF_RE.search(text[m.start(): m.end() + 60]):
                continue
            target = tm.group(0)
            resolvable = (
                text.count(target) >= 2
                or any(target in p for p in heading_path)
            )
            if not resolvable:
                observation["detail"]["unresolvable_reference"] += 1
                ledger.book(QualityEvent(
                    event_id="",
                    event_type="unresolvable_reference",
                    primary_dimension=DIM_CONTEXT,
                    chunk_ids=[cid],
                    severity="warning",
                    source_spans=[_span_of(chunk)],
                    detail={
                        "cue_window": text[
                            m.start(): m.end() + 30
                        ].strip()[:80],
                        "target": target,
                    },
                ))
                break  # 每 chunk 记一次，避免 cue 密集时淹没账本

        # 4. context_overload: overlap repetition over half.
        overlap = getattr(chunk, "overlap_context", "") or ""
        if overlap and len(text) > 0 and len(overlap) > 0.5 * len(text):
            observation["detail"]["context_overload"] += 1
            ledger.book(QualityEvent(
                event_id="",
                event_type="context_overload",
                primary_dimension=DIM_CONTEXT,
                chunk_ids=[cid],
                severity="warning",
                source_spans=[_span_of(chunk)],
                detail={
                    "overlap_chars": len(overlap),
                    "chunk_chars": len(text),
                },
            ))

    observation["events"] = sum(
        v for k, v in observation["detail"].items()
        if not k.startswith("_")
    )
    return observation


# ==================================
# D4 segmentation_fitness
# ==================================

def evaluate_segmentation(
    chunks: List[Any],
    policy,
    ledger: EventLedger,
) -> Dict[str, Any]:
    """D4: two-stage -- policy_forced verdicts first, only
    unforced events count toward over-fragmentation.

        source_unit_split   source unit split under the
                            same heading path (list
                            continuation / sentence
                            interruption); combined >
                            hard_max_chars -> policy_forced
        cross_section_merge one chunk swallows several
                            same-level sections;
                            merge_short_sibling_sections=
                            True -> policy_forced

    Provisional score: 1 - unforced / total_chunks.
    """
    total = len(chunks)
    observation: Dict[str, Any] = {
        "dimension": DIM_SEGMENTATION,
        "events": 0,
        "unforced_events": 0,
        "policy_forced_events": 0,
        "denominator": total,
        "detail": {},
    }
    if total == 0:
        observation["applicable"] = False
        return observation
    observation["applicable"] = True

    # --- source_unit_split ---
    split_count = 0
    # Fragment-run flag: consecutive degenerate cuts booked once.
    degenerate_run_open = False
    fragments_in_run = 0
    for idx in range(total - 1):
        cur, nxt = chunks[idx], chunks[idx + 1]
        cur_path = list(getattr(cur, "heading_path", None) or [])
        nxt_path = list(getattr(nxt, "heading_path", None) or [])
        cur_text = getattr(cur, "text", "") or ""
        nxt_text = getattr(nxt, "text", "") or ""
        # Degenerate bypass: a <= 2-char residual block is itself
        # evidence of fragmenting cuts -- the cross-section path
        # guard, size threshold and prose cues all fail on it.
        # Hit unconditionally, except for separator-only residue
        # (lone '*'/'-' lines are typesetting, not damage).
        degenerate_text = (cur_text or "").strip()
        degenerate = len(degenerate_text) <= 2
        separator_residue = bool(
            degenerate_text
            and degenerate
            and _SEPARATOR_ONLY_RE.match(degenerate_text)
        )
        if cur_path != nxt_path and not degenerate:
            continue
        cur_last, nxt_first = _last_line(cur_text), _first_line(nxt_text)
        cur_type, nxt_type = _type_value(cur), _type_value(nxt)

        # Fence/comment cuts are already booked by D1; skip here
        # to avoid double counting.
        if (
            _ends_inside_fence(cur_text)
            or _starts_inside_fence(nxt_text)
            or _ends_inside_comment(cur_text)
            or _starts_inside_comment(nxt_text)
        ):
            continue

        hit = None
        combined = len(cur_text) + len(nxt_text)
        en_continuation = bool(re.match(r"^[a-z]", nxt_first))
        zh_continuation = bool(
            _ZH_COMMA_END_RE.search(cur_last)
            and _CJK_START_RE.match(nxt_first)
        )
        threshold = (
            _MIN_SPLIT_COMBINED_CJK if zh_continuation
            else _MIN_SPLIT_COMBINED
        )
        # (a) list/procedure continuation: same path, same type,
        #     next block starts with a list marker.
        if (
            cur_type == nxt_type
            and cur_type in ("list", "procedure")
            and _LIST_MARKER_RE.match(nxt_text)
        ):
            hit = "list_continuation"
        # (a2) degenerate residual block: <= 2 chars of content is
        #      itself deterministic evidence of a fragmenting
        #      cut. Separator-only residue is exempt.
        elif degenerate and not separator_residue:
            hit = "degenerate_residual"
        # (b) sentence interruption: previous block lacks terminal
        #     punctuation and the next block starts with a
        #     continuation signal. Precision guards: the last
        #     line must end prose-contiguously; the next line
        #     must not be a resource line; combined size must
        #     reach the threshold (own limit for CJK). EN signal
        #     = lowercase start; ZH = comma ending + CJK start.
        elif (
            cur_type not in ("code", "mixed", "table")
            and cur_last
            and not cur_last.endswith(_TERMINAL_PUNCT)
            and _PROSE_END_RE.search(cur_last)
            and not _FENCE_RE.match(cur_last)
            and not _MD_LINK_TAIL_RE.search(cur_last)
            and nxt_first
            and not _TABLE_ROW_RE.match(nxt_first)
            and not _LIST_MARKER_RE.match(nxt_first)
            and not _ASSET_LINE_RE.match(nxt_first)
            and not _DEFN_START_RE.match(nxt_first)
            and not _ASN1_DEFN_RE.match(nxt_first)
            and not _GRAMMAR_COMMENT_RE.match(cur_last)
            and (en_continuation or zh_continuation)
            and (combined >= threshold or degenerate)
        ):
            # Degenerate residue and sentence interruption are two
            # damage shapes; kind tells them apart.
            hit = (
                "degenerate_residual" if degenerate
                else "sentence_truncation"
            )

        if hit is None:
            degenerate_run_open = False
            continue
        if degenerate:
            if degenerate_run_open:
                # Continuation of the same fragment run: the run
                # head was booked, do not re-book.
                continue
            degenerate_run_open = True
            # Pre-scan the fragments covered by this run.
            j = idx
            while j + 1 < total and len(
                ((getattr(chunks[j + 1], "text", "") or "").strip())
            ) <= 2:
                j += 1
            fragments_in_run = j - idx + 2
        else:
            degenerate_run_open = False
        split_count += 1
        forced = policy_forced_for_split(combined, policy)
        detail = {"kind": hit, "combined_chars": combined}
        if degenerate:
            detail["kind"] = "degenerate_residual"
            detail["fragments_in_run"] = fragments_in_run
        ledger.book(QualityEvent(
            event_id="",
            event_type="source_unit_split",
            primary_dimension=DIM_SEGMENTATION,
            chunk_ids=[_chunk_id(cur, idx), _chunk_id(nxt, idx + 1)],
            severity="warning" if forced else "error",
            policy_forced=forced,
            source_spans=[_span_of(cur), _span_of(nxt)],
            detail=detail,
        ))
    observation["detail"]["source_unit_split"] = split_count

    # --- cross_section_merge: one chunk swallows same-level
    # sections ---
    merge_count = 0
    merge_forced = policy_forced_for_merge(policy)
    for idx, chunk in enumerate(chunks):
        headings = _headings_in_text(getattr(chunk, "text", "") or "")
        if len(headings) < 2:
            continue
        # Same level and different text = sections merged.
        same_level_siblings = any(
            headings[i][0] == headings[j][0]
            and headings[i][1] != headings[j][1]
            for i in range(len(headings))
            for j in range(i + 1, len(headings))
        )
        if not same_level_siblings:
            continue
        merge_count += 1
        ledger.book(QualityEvent(
            event_id="",
            event_type="cross_section_merge",
            primary_dimension=DIM_SEGMENTATION,
            chunk_ids=[_chunk_id(chunk, idx)],
            severity="warning" if merge_forced else "error",
            policy_forced=merge_forced,
            source_spans=[_span_of(chunk)],
            detail={
                "merged_headings": [t for _, t in headings][:8],
                "policy": (
                    "merge_short_sibling_sections" if merge_forced
                    else None
                ),
            },
        ))
    observation["detail"]["cross_section_merge"] = merge_count

    all_seg_events = ledger.events_for(
        DIM_SEGMENTATION, scoring_only=False
    )
    observation["events"] = len(all_seg_events)
    observation["unforced_events"] = len(ledger.events_for(DIM_SEGMENTATION))
    observation["policy_forced_events"] = len([
        e for e in all_seg_events if e.policy_forced
    ])
    return observation


# ==================================
# D0 content_preservation (observation only, not scored)
# ==================================

def evaluate_content_observation(
    chunks: List[Any],
    document_text: Optional[str],
) -> Dict[str, Any]:
    """D0 raw observation: total chunk text / source length
    (uncapped, not deduplicated).

    Known limit: overlap inflates the numerator, so this is
    a soft signal, not a guarantee; it is not scored and
    carries no threshold -- the raw numbers ride along for
    audit only.
    """
    observation: Dict[str, Any] = {
        "dimension": DIM_CONTENT,
        "role": "observation_only_weight_0",
        "raw_coverage": None,
        "note": (
            "soft signal (recoverability Layer 1 analogue); not "
            "capped, not deduplicated for overlap; v1 does not "
            "score this dimension (DDR §1.1)"
        ),
    }
    if not chunks or not document_text:
        observation["applicable"] = False
        return observation
    total_chars = sum(len(getattr(c, "text", "") or "") for c in chunks)
    source_len = len(document_text)
    observation["applicable"] = source_len > 0
    if source_len > 0:
        observation["raw_coverage"] = round(total_chars / source_len, 4)
    observation["detail"] = {
        "chunk_chars_total": total_chars,
        "source_chars": source_len,
    }
    return observation


# ==================================
# Composition entry point (fixed order)
# ==================================

def evaluate_profile(
    chunks: List[Any],
    document_text: Optional[str],
    policy=None,
) -> Tuple[EventLedger, Dict[str, Dict[str, Any]]]:
    """Evaluate in D1->D2->D3->D4 order, returning (ledger,
    per-dimension observations).

    A single-dimension failure does not sink the whole run,
    but must surface explicitly in the observation.
    """
    ledger = EventLedger()
    observations: Dict[str, Dict[str, Any]] = {}

    structural = evaluate_structural(chunks, document_text, ledger)
    observations[DIM_STRUCTURAL] = structural

    boundary = evaluate_boundary(
        chunks, document_text, ledger, structural
    )
    observations[DIM_BOUNDARY] = boundary

    observations[DIM_CONTEXT] = evaluate_context(
        chunks, document_text, ledger
    )
    observations[DIM_SEGMENTATION] = evaluate_segmentation(
        chunks, policy, ledger
    )
    # D0: observation only, raw coverage numbers ride along.
    observations[DIM_CONTENT] = evaluate_content_observation(
        chunks, document_text
    )
    return ledger, observations
