"""Shared narrative boundary/cohesion primitive checks.

Single source of truth for the low-level structural checks consumed
by the narrative scoring track:

    Invariant track — narrative_invariants registry (4 semantic
                       invariants, the sole scoring authority since
                       rse_naming_spec_v1 batch 3)

==================================
Why this module exists (drift record)
==================================

The invariant track was originally bootstrapped by COPYING these
primitives from the retired narrative_cqi.py ("to avoid importing
the old class"). The copies then drifted: the legacy track received the
v3 calibration fix and two v4 contract fixes, but the copied
primitives did not:

    v3 fix (ending)      — text ending shortly after a paragraph
                           boundary (\n\n) scores 0.5, not 0.1
    v4 fix (ending)      — terminal newlines are boundary evidence:
                           rstrip(" \t") instead of bare rstrip()
    v4 fix (ending)      — a closing code fence is a valid break
                           for ANY block type (mixed chunks), not
                           only CODE chunks
    v4 fix (starting)    — a context-propagation prefix
                           ("[Context: parent > heading]") is an
                           inherited heading boundary → 1.0

Unifying here was a BUGFIX for the invariant track (it inherits
the calibrated v3/v4 behavior) and a NO-OP for the then-legacy
track (it already used this code). After this module, drift on
these primitives is structurally impossible.

==================================
Shared contents
==================================

Character sets:
    SENTENCE_ENDS / CLAUSE_ENDS / STRUCTURAL_ENDS / CODE_ENDS
    FENCE_MARKS

Structural patterns:
    HEADING_PATTERNS / LIST_PATTERNS

Lexical:
    STOPWORDS

Functions:
    tokenize(text)         — ASCII words + CJK bi-grams + numbers
    find_split_point(text) — boundary nearest the midpoint
    check_ending(chunk)    — chunk ends at structural break (v3/v4)
    check_starting(chunk)  — chunk starts at structural break (v4)
"""

import re

from provendoc.base.models.representation_unit import UnitType

# ==================================
# boundary character sets
# ==================================

SENTENCE_ENDS = set(".!?。！？")
CLAUSE_ENDS = set(";；:：")
STRUCTURAL_ENDS = set(")]}）」』】|")
CODE_ENDS = set("};")
FENCE_MARKS = ("```", "~~~")

# ==================================
# structural patterns
# ==================================

HEADING_PATTERNS = [
    re.compile(r"^#{1,6}\s"),
    re.compile(r"^【.+】"),
    re.compile(r"^第[一二三四五六七八九十百千万\d]+条"),
    re.compile(r"^[一二三四五六七八九十]+[、.]"),
]

LIST_PATTERNS = [
    re.compile(r"^[-*•]\s"),
    re.compile(r"^\d+[.)、]\s"),
]

# ==================================
# stopwords for cohesion (minimal)
# ==================================

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "to", "of", "in", "on", "at", "by", "for", "with", "from",
    "and", "or", "but", "not", "no", "if", "then", "else",
    "this", "that", "these", "those", "it", "its",
    "as", "so", "do", "does", "did", "has", "have", "had",
    "will", "would", "can", "could", "should", "may", "might",
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
    "一", "个", "上", "也", "很", "到", "说", "要", "去", "你",
    "会", "着", "没", "看", "好", "自", "己", "这", "那",
}


# ==================================
# context-prefix primitive
# ==================================
# Runtime packaging prepends an inherited-context line to chunk
# text: "[Context: parent > heading]". It is navigation metadata,
# NOT chunk content. Scoring primitives that measure content
# (lexical cohesion, orphan-heading detection) must strip it
# first, or the boilerplate masks real signals.

CONTEXT_PREFIX_LINE = re.compile(
    r"^\s*\[(?:context|上下文|语境)\s*[:：][^\]]{0,200}\]\s*$",
    re.IGNORECASE,
)


def strip_context_prefix(text: str) -> str:
    """Drop a leading context-propagation line ("[Context: p > h]").

    Only a FIRST non-empty line matching the prefix shape is
    stripped — a mid-text bracketed line is ordinary prose and is
    kept. Returns the body text unchanged when no prefix exists.
    """
    lines = (text or "").split("\n")
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if CONTEXT_PREFIX_LINE.match(line):
            return "\n".join(lines[idx + 1:]).lstrip("\n")
        break
    return text or ""


# ==================================
# lexical primitives
# ==================================

def tokenize(text: str) -> set:
    """Tokenize for cohesion measurement.

    ASCII: words (2+ chars) as tokens.
    CJK: bi-grams (consecutive char pairs) as tokens.
          Chen & Chen (2010) — CJK n-gram processing.
          "人工智能" produces {"人工", "工智", "智能"}
          instead of single chars which inflate overlap.
    Numbers: digit sequences (2+ digits) as tokens.
    """
    tokens = set()
    for word in re.findall(r"[a-zA-Z]{2,}", text.lower()):
        if word not in STOPWORDS:
            tokens.add(word)
    cjk_chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(cjk_chars) - 1):
        tokens.add(cjk_chars[i] + cjk_chars[i + 1])
    for num in re.findall(r"\d{2,}", text):
        tokens.add(num)
    return tokens


def find_split_point(text: str) -> int:
    """Find the sentence boundary closest to the text midpoint.

    Searches for sentence-ending characters within ±30% of text
    length around the midpoint. Falls back to the character
    midpoint if no boundary is found.
    """
    mid = len(text) // 2
    window = max(int(len(text) * 0.3), 20)

    best_pos = mid
    best_dist = window + 1
    boundary_chars = SENTENCE_ENDS | CLAUSE_ENDS | {"\n"}

    search_start = max(0, mid - window)
    search_end = min(len(text), mid + window)
    for i in range(search_start, search_end):
        if text[i] in boundary_chars:
            dist = abs(i - mid)
            if dist < best_dist:
                best_dist = dist
                best_pos = i + 1  # split AFTER boundary
    return best_pos


# ==================================
# boundary classification primitives (calibrated v3/v4)
# ==================================

def check_ending(chunk) -> float:
    """Check if chunk ends at a structural break point.

    Scoring (structural boundary classification):
        1.0 — sentence end (.!?。！？) or closing code fence
        0.8 — paragraph boundary (\\n\\n)
        0.7 — clause end (;；:：) or structural end ()]}）
        0.6 — single newline (\\n)
        0.5 — just after paragraph boundary / code trailing newline
        0.3 — comma (,，) — weak boundary
        0.1 — no structural marker

    v3 calibration fix:
        - If the chunk ends shortly after a paragraph boundary
          (\\n\\n), score 0.5 instead of 0.1. The paragraph boundary
          IS present in the chunk, just not at the end. This
          prevents penalizing chunks that contain paragraph
          structure but happen to be cut mid-sentence in the new
          paragraph.

    v4 contract fixes:
        - Terminal newlines are boundary evidence: strip only
          spaces/tabs, never the trailing newline itself.
        - A closing code fence is a valid structural break for ANY
          block type. Mixed chunks that end with a fence previously
          escaped this check because it only ran inside the code
          branch.
    """
    # Preserve terminal newlines: they are boundary evidence.
    text = chunk.text.rstrip(" \t")
    if not text:
        return 0.0

    # v4: closing fence is valid for ANY block type (incl. mixed)
    for fence in FENCE_MARKS:
        if text.endswith(fence):
            return 1.0

    if chunk.unit_type == UnitType.CODE:
        if text[-1] in CODE_ENDS:
            return 0.7
        if text.endswith("\n"):
            return 0.5
        return 0.3

    # check paragraph boundary (\n\n) before single \n
    if text.endswith("\n\n"):
        return 0.8

    last_char = text[-1]
    if last_char in SENTENCE_ENDS:
        return 1.0
    if last_char in CLAUSE_ENDS:
        return 0.7
    if last_char in STRUCTURAL_ENDS:
        return 0.7
    if last_char == "\n":
        return 0.6
    if last_char in ",，":
        return 0.3

    # v3: partial credit when ending shortly after a paragraph
    # boundary — the chunk contains \n\n but continues into a new
    # paragraph that gets cut
    last_100 = text[-100:] if len(text) > 100 else text
    if "\n\n" in last_100:
        after_break = last_100.rsplit("\n\n", 1)[-1]
        if len(after_break) < 50:
            return 0.5
    return 0.1


def check_starting(chunk) -> float:
    """Check if chunk starts at a structural break point.

    Scoring (structural boundary classification):
        1.0 — heading, list item, code fence, or inherited
              context prefix at start
        0.7 — capital letter or CJK character (sentence start)
        0.5 — digit (possible list/numbering)
        0.3 — lowercase or punctuation (mid-sentence)

    v4 contract fix:
        - A context-propagation prefix ("[Context: parent > heading]")
          is a deliberate inherited heading boundary. It is not
          ordinary bracketed prose and must receive the same credit
          as a heading, including when the following body begins
          with a lowercase word.
    """
    text = chunk.text.lstrip()
    if not text:
        return 0.0

    # v4: inherited context prefix counts as heading boundary
    if text.startswith("[Context:"):
        return 1.0

    if chunk.unit_type == UnitType.CODE:
        for fence in FENCE_MARKS:
            if text.startswith(fence):
                return 1.0
        return 0.3

    for pattern in HEADING_PATTERNS:
        if pattern.match(text):
            return 1.0

    for pattern in LIST_PATTERNS:
        if pattern.match(text):
            return 1.0

    for fence in FENCE_MARKS:
        if text.startswith(fence):
            return 1.0

    first = text[0]
    if first.isupper():
        return 0.7
    if "\u4e00" <= first <= "\u9fff":
        return 0.7
    if first.isdigit():
        return 0.5
    return 0.3
