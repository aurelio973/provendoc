"""Shared sentence-boundary detection.

Sentence-end-priority cut-point detection for hard fallback splitting
(``_split_at_hard_fallback`` / ``CoarseGrainedSplitter.
_split_at_hard_max``) — zero-dependency pure functions shared by the
txt and granularity machines (granularity must not import txt back:
circular dependency).

Contract (wide set, prefer recall over precision):
- Sentence-end punctuation: ASCII ``. ! ?`` plus CJK ``。！？；…``;
- Closing quotes/brackets after the punctuation (``”’)]`` and the
  like) are tolerated before the boundary;
- Boundary = whitespace / end of string / non-ASCII successor
  (CJK convention omits spaces after ``。``, so an adjacent CJK
  character still counts as a boundary);
- Cut point (return value) = position after punctuation (+ closers),
  guaranteeing 0 < cut <= limit (fragment length <= limit, aligned
  with the space-rfind convention);
- Returns -1 when the window has no sentence-end boundary; the
  caller then falls back to space/hard split (existing behavior).
"""

import re

# Sentence-end punctuation: . ! ? 。！？；…  (character class, listed
# verbatim to avoid ambiguity)
_SENTENCE_END_CHARS = ".!?。！？；…"
# Closing quotes/brackets tolerated after the punctuation, any count
_CLOSING_CHARS = "”’)\"\\]》〉】〕"

# Sentence boundary: end punctuation + any closers + boundary lookahead
_SENTENCE_BOUNDARY_RE = re.compile(
    "[" + re.escape(_SENTENCE_END_CHARS) + "]"
    "[" + re.escape(_CLOSING_CHARS) + "]*"
    r"(?=\s|$|[^\x00-\x7F])"
)


def find_sentence_boundary(text: str, limit: int) -> int:
    """Find the last sentence-end cut point within the first limit+1 chars.

    Returns the cut index (text[:cut] ends with sentence-end
    punctuation/closers, i.e. a prefix of complete sentences), or -1
    when the window has no sentence-end boundary. The cut is always
    <= limit — fragment = remaining[:cut] stays within the limit,
    aligned with the space-rfind convention (a match whose
    punctuation sits exactly at index limit is rejected, falling
    back to space/hard split).
    """
    best = -1
    # endpos=limit+1: the lookahead treats that position as end of
    # string, but a match is only adopted when end <= limit, so
    # end-of-string false positives (a real non-space successor)
    # are filtered out naturally.
    for match in _SENTENCE_BOUNDARY_RE.finditer(text, 0, limit + 1):
        end = match.end()
        if 0 < end <= limit:
            best = end
    return best
