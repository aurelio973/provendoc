"""Shared text normalization helpers for matching structural titles.

Both the PDF navigation layer (bookmark trees, page labels) and the DOCX
parser need to answer the same question: *do these two strings refer to the
same title even though they were written slightly differently?*  Bookmarks
truncate, drop numbering, or append page numbers ("2.3.1" vs "2.3.1 System
Design"); style names carry locale prefixes ("heading 1" vs "标题 1").  The
helpers here normalize that noise once, at the shared-structure level, so the
two runtimes do not drift apart.

Nothing in this module infers document structure on its own; it only compares
and normalizes text that other layers already consider a candidate.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from provendoc.core.structure.heading_patterns import match_heading_line

_WHITESPACE_RE = re.compile(r"\s+")

# Decorative punctuation that frequently wraps short bookmark titles
# ("(a) Overview", "【摘要】", '"Conclusion"').  Stripping it from both ends
# keeps prefix/whole-string matching honest without altering inner content.
_EDGE_PUNCT = "。，、；：·…—–-()（）[]【】{}<>《》«»\"'‘’“”.,:;!?|/"

# Trailing artifacts bookmark authors copy from printed TOCs.
_TRAILING_PAGE_RE = re.compile(r"\s*(?:[.·•]\s*){2,}\s*\d+\s*$")
_TRAILING_PLAIN_PAGE_RE = re.compile(r"\s+\d{1,4}\s*$")

# ASCII/Arabic digits are normalized forms; CJK digits sometimes appear in
# hand-typed bookmarks.  The structural forms ("第三章", "（一）", "2.3.1")
# are recognized by heading_patterns; this module only strips what those
# patterns already matched.


@dataclass(frozen=True)
class NumberingMatch:
    """Result of stripping leading numbering from a single line of text."""

    text: str
    numbering: str
    rest: str
    pattern_name: str | None
    depth_hint: int


def normalize_title_text(text: str | None) -> str:
    """Collapse whitespace and strip decorative edge punctuation.

    Uses NFKC normalization so full-width digits/brackets typed by different
    authors compare equal, then removes the kind of edge punctuation that
    printed TOCs and PDF bookmark panes decorate titles with.
    """

    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.strip(_EDGE_PUNCT)


def strip_trailing_page_number(text: str | None) -> str:
    """Remove the trailing page number a bookmark/TOC line often carries.

    Handles both dot-leader style ("Introduction ......... 12") and the plain
    form ("Introduction 12").  The plain form only fires for 1-4 digits at the
    very end, which is the shape seen in PDF bookmark titles.
    """

    if not text:
        return ""
    text = normalize_title_text(text)
    text = _TRAILING_PAGE_RE.sub("", text)
    # A dot-leader pattern already consumed; only strip plain trailing digits
    # when the remaining text still has at least two word characters so that
    # pure numbers ("2023") survive as titles.
    stripped = _TRAILING_PLAIN_PAGE_RE.sub("", text).strip()
    if len(normalize_title_text(stripped)) < 2:
        return text
    return stripped


def strip_leading_numbering(text: str | None) -> NumberingMatch:
    """Strip a leading structural-numbering prefix from a single line.

    Delegates to :func:`heading_patterns.match_heading_line` so that
    "2.3.1 System Design", "第三章 系统设计", "（一）概述" and "# 结论"
    all reduce to their title part with the numbering and a depth hint
    preserved.  When no pattern matches, the input is returned unchanged with
    ``pattern_name=None``.
    """

    raw = (text or "").strip()
    if not raw:
        return NumberingMatch(text=raw, numbering="", rest="", pattern_name=None, depth_hint=0)

    match = match_heading_line(raw)
    if match is None or not match.rest.strip():
        return NumberingMatch(text=raw, numbering="", rest=raw, pattern_name=None, depth_hint=0)

    return NumberingMatch(
        text=raw,
        numbering=match.numbering,
        rest=match.rest.strip(),
        pattern_name=match.pattern,
        depth_hint=match.depth_hint,
    )


def title_similarity(a: str | None, b: str | None) -> float:
    """Similarity of two titles after shared normalization, in [0, 1].

    Uses ``difflib.SequenceMatcher`` on the normalized strings.  Deliberately
    conservative: callers pair this with structural signals (page anchors,
    style levels) rather than trusting a fuzzy score alone.
    """

    left = normalize_title_text(a)
    right = normalize_title_text(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def titles_equivalent(a: str | None, b: str | None, *, threshold: float = 0.85) -> bool:
    """Whether two strings refer to the same title after normalization.

    Equivalent when normalized forms are equal, when one is a prefix of the
    other after stripping page numbers and leading numbering (the classic
    bookmark "2.3.1" vs body "2.3.1 System Design" case), or when the fuzzy
    similarity clears ``threshold``.
    """

    left = normalize_title_text(strip_trailing_page_number(a))
    right = normalize_title_text(strip_trailing_page_number(b))
    if not left or not right:
        return False
    if left == right:
        return True

    left_body = strip_leading_numbering(left).rest or left
    right_body = strip_leading_numbering(right).rest or right
    if left_body and right_body and (left_body == right_body):
        return True
    if left_body and right_body and (left_body in right_body or right_body in left_body):
        # Guard trivial prefixes: "a" should not match "analysis pipeline".
        shorter = min(len(left_body), len(right_body))
        if shorter >= 2:
            return True

    return title_similarity(left, right) >= threshold
