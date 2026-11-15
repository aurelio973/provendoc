"""Zero-width character runs: detection, measurement, and utilities.

A *zero-width run* is a maximal sequence of adjacent characters that
contribute no visible glyph width: combining marks (Mn/Mc/Me), zero-width
joiners and non-joiners, word joiners, variation selectors, soft hyphens,
and BOM/ZWSP/BOM-style control characters.

Why this matters for PDF parsing: a font with a broken /ToUnicode CMap
can yield a single span containing thousands of consecutive combining
marks — an *overlong grapheme cluster*. Visually the line is short, but
``len()`` counts every zero-width codepoint, so:

* heading heuristics (``len(text) >= N``) misfire,
* chunking (size limits counted in characters) collapses,
* text gets padded/inflated without any visible content.

The run detection is a proxy for overlong grapheme clusters: a run of
zero-width characters longer than a small threshold is, in practice,
always pathological for narrative text (scripts like Devanagari, Arabic,
or Korean compose clusters of at most a handful of marks).

Public helpers
--------------
``count_zero_width_runs(text, min_run)`` — count runs of zero-width
characters whose length meets ``min_run``; returns ``(total, longest)``
so callers can measure both prevalence and worst-case severity.

``zero_width_runs(text, min_run)`` — yield ``(start, length)`` for each
qualifying run; the low-level scanner used by the counter.

Everything here is pure-standard-library (``re`` only), so any
parser runtime can adopt the detector without new dependencies.
"""

from __future__ import annotations

import re

# Characters that contribute no visible glyph advance. This covers:
#   * combining marks (categories Mn, Mc, Me) — most common pathological
#     source in PDFs with broken /ToUnicode CMaps,
#   * zero-width joiner / non-joiner / word joiner / soft hyphen,
#   * variation selectors (FE00–FE0F) and their mongolian/math variants,
#   * ZWSP, BOM, and a few directional/embedding controls that render
#     without consuming width.
# NOTE: \r \n \t are NOT zero-width: they are layout characters that
# matter to line structure and are handled by the line splitting itself.
_ZERO_WIDTH_RE = re.compile(
    "["
    "\u0300-\u036f"  # Combining Diacritical Marks
    "\u0483-\u0489"  # Cyrillic combining marks
    "\u0591-\u05bd"  # Hebrew accents (subset; remaining Hebrew marks are Mn too)
    "\u0610-\u061a"  # Arabic honorifics / marks
    "\u064b-\u065f"  # Arabic diacritics
    "\u0670"         # Arabic superscript alef
    "\u06d6-\u06dc"  # Arabic Quranic marks
    "\u06df-\u06e4"
    "\u06e7-\u06e8"
    "\u06ea-\u06ed"
    "\u0711"         # Syriac combining mark
    "\u0730-\u074a"  # Syriac marks
    "\u07a6-\u07b0"  # Thaana marks
    "\u0816-\u0819"
    "\u081b-\u0823"
    "\u0825-\u0827"
    "\u0829-\u082d"
    "\u0859-\u085b"
    "\u08d3-\u08e1"  # Arabic extended marks
    "\u08e3-\u0902"
    "\u093a"         # Devanagari vowel signs (subset)
    "\u093c"
    "\u0941-\u0948"
    "\u094d"
    "\u0951-\u0957"
    "\u0962-\u0963"
    "\u0981"         # Bengali marks (subset)
    "\u09bc"
    "\u09be-\u09c4"
    "\u09c7-\u09c8"
    "\u09cb-\u09cd"
    "\u09d7"
    "\u09e2-\u09e3"
    "\u0a01-\u0a02"  # Gurmukhi marks
    "\u0a3c"
    "\u0a41-\u0a42"
    "\u0a47-\u0a48"
    "\u0a4b-\u0a4d"
    "\u0a51"
    "\u0a70-\u0a71"
    "\u0a75"
    "\u0a81-\u0a82"  # Gujarati marks
    "\u0abc"
    "\u0ac1-\u0ac5"
    "\u0ac7-\u0ac8"
    "\u0acd"
    "\u0ae2-\u0ae3"
    "\u0b01"         # Oriya marks
    "\u0b3c"
    "\u0b3e-\u0b44"
    "\u0b47-\u0b48"
    "\u0b4b-\u0b4d"
    "\u0b56-\u0b57"
    "\u0b62-\u0b63"
    "\u0b82"         # Tamil marks
    "\u0bbe-\u0bc2"
    "\u0bc6-\u0bc8"
    "\u0bca-\u0bcd"
    "\u0bd7"
    "\u0c00"         # Telugu marks
    "\u0c3e-\u0c40"
    "\u0c46-\u0c48"
    "\u0c4a-\u0c4d"
    "\u0c55-\u0c56"
    "\u0c62-\u0c63"
    "\u0c81"         # Kannada marks
    "\u0cbc"
    "\u0cbf-\u0cc4"
    "\u0cc6-\u0cc8"
    "\u0cca-\u0ccd"
    "\u0cd5-\u0cd6"
    "\u0ce2-\u0ce3"
    "\u0d01-\u0d02"  # Malayalam marks
    "\u0d3c"
    "\u0d3e-\u0d44"
    "\u0d46-\u0d48"
    "\u0d4a-\u0d4d"
    "\u0d57"
    "\u0d62-\u0d63"
    "\u0d82-\u0d83"  # Sinhala marks
    "\u0dca"
    "\u0dcf-\u0dd4"
    "\u0dd6"
    "\u0dd8-\u0ddf"
    "\u0df2-\u0df3"
    "\u0e31"         # Thai marks
    "\u0e34-\u0e3a"
    "\u0e47-\u0e4e"
    "\u0eb1"         # Lao marks
    "\u0eb4-\u0eb9"
    "\u0ebb-\u0ebc"
    "\u0ec8-\u0ecd"
    "\u0f18-\u0f19"  # Tibetan marks
    "\u0f35"
    "\u0f37"
    "\u0f39"
    "\u0f71-\u0f7e"
    "\u0f80-\u0f84"
    "\u0f86-\u0f87"
    "\u0f8d-\u0f97"
    "\u0f99-\u0fbc"
    "\u0fc6"
    "\u102d-\u1030"  # Myanmar marks
    "\u1032-\u1037"
    "\u1039-\u103a"
    "\u103d-\u103e"
    "\u1058-\u1059"
    "\u105e-\u1060"
    "\u1071-\u1074"
    "\u1082"
    "\u1085-\u1086"
    "\u108d"
    "\u109d"
    "\u135d-\u135f"  # Egyptian marks
    "\u1712-\u1714"  # Tagalog marks
    "\u1732-\u1734"
    "\u1752-\u1753"
    "\u1772-\u1773"
    "\u17b4-\u17b5"  # Khmer marks
    "\u17b7-\u17bd"
    "\u17c6"
    "\u17c9-\u17d3"
    "\u17dd"
    "\u180b-\u180d"  # Mongolian FVS
    "\u180f"
    "\u18a9"
    "\u1920-\u1922"  # Limbu marks
    "\u1927-\u1928"
    "\u1932"
    "\u1939-\u193b"
    "\u1a17-\u1a18"  # Buginese marks
    "\u1a1b"
    "\u1a56"
    "\u1a58-\u1a5e"
    "\u1a60"
    "\u1a62"
    "\u1a65-\u1a6c"
    "\u1a73-\u1a7c"
    "\u1a7f"
    "\u1ab0-\u1abe"  # Combining Diacritical Marks Extended
    "\u1b00-\u1b03"  # Balinese marks
    "\u1b34"
    "\u1b36-\u1b3a"
    "\u1b3c"
    "\u1b42"
    "\u1b6b-\u1b73"
    "\u1b80-\u1b81"  # Sundanese marks
    "\u1ba2-\u1ba5"
    "\u1ba8-\u1ba9"
    "\u1bab-\u1bad"
    "\u1be6"         # Batak marks
    "\u1be8-\u1be9"
    "\u1bed"
    "\u1bef-\u1bf1"
    "\u1c2c-\u1c33"  # Lepcha marks
    "\u1c36-\u1c37"
    "\u1cd0-\u1cd2"  # Vedic marks
    "\u1cd4-\u1ce0"
    "\u1ce2-\u1ce8"
    "\u1ced"
    "\u1cf4"
    "\u1cf8-\u1cf9"
    "\u1dc0-\u1df5"  # Combining Diacritical Marks Supplement
    "\u1dfc-\u1dff"
    "\u200b"         # ZERO WIDTH SPACE
    "\u200c"         # ZERO WIDTH NON-JOINER
    "\u200d"         # ZERO WIDTH JOINER
    "\u200e-\u200f"  # LRM/RLM
    "\u202a-\u202e"  # Embedding controls
    "\u2060-\u2064"  # WORD JOINER and friends
    "\u206a-\u206f"  # Deprecated controls
    "\u20d0-\u20f0"  # Combining marks for symbols
    "\u2cef-\u2cf1"  # Coptic marks
    "\u2d7f"         # Tifinagh joiner
    "\u2de0-\u2dff"  # Cyrillic-A marks
    "\u302a-\u302d"  # CJK compatibility marks
    "\u3099-\u309a"  # Kana voicing marks
    "\ua66f"         # Cyrillic-B marks (subset)
    "\ua674-\ua67d"
    "\ua69e-\ua69f"
    "\ua6f0-\ua6f1"
    "\ua802"         # Syloti Nagri marks
    "\ua806"
    "\ua80b"
    "\ua825-\ua826"
    "\ua8c4-\ua8c5"
    "\ua8e0-\ua8f1"  # Devanagari Extended marks
    "\ua926-\ua92d"  # Kayah Li marks
    "\ua947-\ua951"
    "\ua980-\ua982"  # Javanese marks
    "\ua9b3"
    "\ua9b6-\ua9b9"
    "\ua9bc-\ua9bd"
    "\ua9e5"
    "\uaa29-\uaa2e"  # Cham marks
    "\uaa31-\uaa32"
    "\uaa35-\uaa36"
    "\uaa43"
    "\uaa4c"
    "\uaa7b-\uaa7d"
    "\uaab0"         # Tai Viet marks
    "\uaab2-\uaab4"
    "\uaab7-\uaab8"
    "\uaabe-\uaabf"
    "\uaac1"
    "\uaaeb-\uaaef"  # Meetei Mayek marks
    "\uaaf5-\uaaf6"
    "\uabe3-\uabe4"  # Meetei Mayek Extensions
    "\uabe6-\uabe7"
    "\uabe9-\uabea"
    "\uabec"
    "\ufe00-\ufe0f"  # VARIATION SELECTORS 1-16
    "\ufe20-\ufe2f"  # Combining half marks
    "\ufe70"         # Arabic presentation forms (subset)
    "\ufe72"
    "\ufe74"
    "\ufe76"
    "\ufe78"
    "\ufe7a"
    "\ufe7c"
    "\ufe7e"
    "\ufeef"         # ZERO WIDTH NO-BREAK SPACE (BOM)
    "\ufff0-\ufff8"  # Invalid/reserved characters (pathological input signal)
    "\ufff9-\ufffb"  # Interlinear annotation characters (never render width)
    "]+"
)

# ZWNBS/BOM is sometimes useful (it keeps words together); still zero-width.
# Some marks (Mc: spacing marks) do consume a little width in some fonts,
# but they cluster with base glyphs, so treating them as zero-width for
# run-detection purposes is a safe over-approximation for pathological
# detection: a legitimate text rarely produces long runs even with Mc
# included.


def zero_width_runs(text: str, min_run: int = 8) -> list[tuple[int, int]]:
    """Return ``[(start, length), ...]`` for zero-width runs >= ``min_run``.

    The scan is a single pass over the compiled character class, so the
    cost is O(len(text)) with a small constant. Runs shorter than
    ``min_run`` are ignored — normal scripts (Devanagari, Arabic, Hebrew
    vowelization, Korean) compose clusters of at most a handful of marks,
    so runs of 8+ are, in narrative text, always pathological.
    """
    if min_run <= 0:
        raise ValueError("min_run must be positive")
    runs: list[tuple[int, int]] = []
    for match in _ZERO_WIDTH_RE.finditer(text):
        length = match.end() - match.start()
        if length >= min_run:
            runs.append((match.start(), length))
    return runs


def count_zero_width_runs(text: str, min_run: int = 8) -> tuple[int, int]:
    """Return ``(number_of_runs, longest_run_length)`` for ``text``.

    Convenience wrapper for callers that only need aggregate severity:
    ``count`` for prevalence and ``longest`` for worst-case. Callers that
    need positions should use :func:`zero_width_runs` directly.
    """
    runs = zero_width_runs(text, min_run)
    longest = max((length for _, length in runs), default=0)
    return len(runs), longest


def is_likely_pathological_cluster(
    text: str,
    min_run: int = 8,
    max_cluster_len: int = 64,
) -> bool:
    """Heuristic: does ``text`` contain pathological grapheme clusters?

    A run of ``max_cluster_len`` or more zero-width characters is almost
    certainly a broken-font artifact (thousands of combining marks in a
    single span) rather than legitimate orthography, so we can flag a
    page/block as *likely pathological* without further analysis.
    """
    for _, length in zero_width_runs(text, min_run):
        if length >= max_cluster_len:
            return True
    return False


__all__ = [
    "zero_width_runs",
    "count_zero_width_runs",
    "is_likely_pathological_cluster",
]
