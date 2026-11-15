# -*- coding: utf-8 -*-
"""Emoji-sequence legality detection — single source of truth shared by
the transform and observation layers.

Why this lives in the foundation layer: the policies for tag characters,
variation selectors (VS) and ZWJ are **context-sensitive dual-state**
(legal keep / illegal sanitize), which a static char_policy table (a
str.translate single-character table) cannot express. The detection
logic must be **shared as one implementation** by the transform layer
(core.text.normalization stage-0.6 sanitization dispatch) and the
observation layer (core.diagnostics.security_carrier malformed-instance
counting), preventing dual-scope drift (the historical lesson from
before char_policy consolidation: document_quality missed the
U+2066-2069 isolates).

Detection scope:

    - VS (U+FE00-FE0F) legal = preceded by an emoji base (presentation
      form, e.g. ❤️), or a keycap sequence ([0-9#*] + VS + U+20E3);
      everything else illegal.
    - ZWJ (U+200D) legal = both sides are emoji sequence components —
      emoji base / VS modifier / keycap combining mark (adjacency
      guard; in a legal emoji ZWJ sequence the ZWJ is always an
      emoji↔emoji connector — no other shape is worth preserving).
    - tag characters (U+E0000-U+E007F) well-formed = an emoji base
      followed by consecutive tag payload terminated by U+E007F
      (CANCEL TAG) (the only legal shape in practice: subdivision-flag
      sequences, e.g. 🏴+gb+eng+TERM); a well-formed sequence is legal
      as a whole, all other tag characters are illegal — language
      tags / mid-sequence strays / missing terminator / orphan
      terminator.

Layering contract: this module only **decides** (pure functions,
observe-only), performs no text transformation (dispatch lives in
normalization) and produces no diagnostics (counting lives in
security_carrier).
"""

from __future__ import annotations

import re

# ---- character ranges and constants (single declaration point,
# char_policy style) ----
TAG_MIN, TAG_MAX = 0xE0000, 0xE007F
TAG_CANCEL = "\U000E007F"     # CANCEL TAG — terminator of a well-formed tag sequence
VS_MIN, VS_MAX = 0xFE00, 0xFE0F
ZWJ = "\u200d"
KEYCAP_COMBINING = "\u20e3"   # enclosing combining mark of a keycap sequence
KEYCAP_BASES = "#*0123456789"

# Emoji base ranges (shared by VS/ZWJ legal-adjacency and the tag
# well-formedness base; conservatively the commonly used ranges):
#   U+231A-231F  watch and misc
#   U+2600-27BF  misc symbols + dingbats (❤ ⚠ ☀ etc.)
#   U+2B00-2BFF  arrows/stars (⭐ etc.)
#   U+1F000-1FAFF  main SMP emoji block (incl. regional indicators
#                  1F1E6-1F1FF)
_EMOJI_BASE_RANGES = (
    (0x231A, 0x231F),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x1F000, 0x1FAFF),
)

# Hot-path pre-check (C-level scan): the three classes are extremely
# sparse in real corpora (of 612 files only ~12 hit), so the Python-level
# per-character scan is skipped when nothing hits.
_SEQUENCE_HINT = re.compile(
    "[\u200d\ufe00-\ufe0f\U000e0000-\U000e007f]"
)


def is_emoji_base(ch: str) -> bool:
    """Whether a single character is an emoji base (basis of the
    legal-adjacency rules)."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_BASE_RANGES)


def is_tag_char(ch: str) -> bool:
    """Whether a single character is a Unicode Tag character
    (U+E0000-E007F carrier segment)."""
    return TAG_MIN <= ord(ch) <= TAG_MAX


def is_variation_selector(ch: str) -> bool:
    """Whether a single character is a Variation Selector modifier
    (U+FE00-FE0F)."""
    return VS_MIN <= ord(ch) <= VS_MAX


def is_vs_legal(prev: str, nxt: str) -> bool:
    """VS legal-adjacency rule: preceding emoji base, or a keycap
    sequence."""
    if not prev:
        return False
    if is_emoji_base(prev):
        return True
    return prev in KEYCAP_BASES and nxt == KEYCAP_COMBINING


def _is_sequence_component(ch: str) -> bool:
    """Legal ZWJ neighbor: emoji base / VS modifier / keycap combining
    mark."""
    if not ch:
        return False
    if is_variation_selector(ch) or ch == KEYCAP_COMBINING:
        return True
    return is_emoji_base(ch)


def is_zwj_legal(prev: str, nxt: str) -> bool:
    """ZWJ adjacency guard: kept only when both sides are emoji
    sequence components."""
    return _is_sequence_component(prev) and _is_sequence_component(nxt)


def scan_sequence_violations(
    text: str,
) -> tuple[list[int], list[int], list[int]]:
    """One-pass scan of the illegal-instance positions of the three
    carrier classes.

    Returns ``(illegal tag positions, illegal VS positions, illegal
    ZWJ positions)`` — three ascending lists. Legal shapes
    (well-formed flag sequences / emoji presentation VS / keycap /
    emoji ZWJ sequences) never appear in any of the returns.

    Deterministic pure function: same input, same output, one O(n)
    pass.
    """
    if not _SEQUENCE_HINT.search(text):
        return [], [], []

    n = len(text)
    bad_tag: list[int] = []
    bad_vs: list[int] = []
    bad_zwj: list[int] = []

    i = 0
    while i < n:
        ch = text[i]
        cp = ord(ch)
        if TAG_MIN <= cp <= TAG_MAX:
            # Maximal consecutive tag run: split into sequences inside
            # the run by the "base + payload + terminator" rule. A legal
            # sequence must start at the head of the run (the character
            # before the run is an emoji base) and end with CANCEL TAG;
            # everything after inside the run (restarting past a
            # terminator / payload without a base) is illegal — with two
            # flags back to back there is always a base character
            # between the runs, so runs split naturally.
            j = i + 1
            while j < n and TAG_MIN <= ord(text[j]) <= TAG_MAX:
                j += 1
            base_ok = i > 0 and is_emoji_base(text[i - 1])
            ptr = 0
            run_len = j - i
            while ptr < run_len:
                if text[i + ptr] == TAG_CANCEL:
                    # orphan terminator: cannot start a payload
                    bad_tag.append(i + ptr)
                    ptr += 1
                    continue
                k = text.find(TAG_CANCEL, i + ptr, j)
                if k == -1:
                    # payload without a terminator: whole remainder illegal
                    bad_tag.extend(range(i + ptr, j))
                    ptr = run_len
                else:
                    if not (ptr == 0 and base_ok):
                        # payload without a base (or not at run head):
                        # the payload + terminator is illegal as a whole
                        bad_tag.extend(range(i + ptr, k + 1))
                    ptr = k + 1 - i
            i = j
        elif VS_MIN <= cp <= VS_MAX:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if not is_vs_legal(prev, nxt):
                bad_vs.append(i)
            i += 1
        elif ch == ZWJ:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if not is_zwj_legal(prev, nxt):
                bad_zwj.append(i)
            i += 1
        else:
            i += 1

    return bad_tag, bad_vs, bad_zwj
