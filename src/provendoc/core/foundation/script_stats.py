"""Script and character-class profiling shared by PDF and DOCX parsers.

Single source of truth for "which scripts and character classes does
this text contain".
Both the PDF ``text_characters`` evidence extractor and the DOCX
``docx_text_characters`` summary call :func:`script_profile` so ligature,
PUA, RTL, CJK, and combining-mark counting can never drift between the
two parsers.  This module only observes — transformation policy stays in
``char_policy`` and normalization stays in ``normalization``.
"""

from __future__ import annotations

import unicodedata

# Ligature presentation forms: ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ (U+FB00..U+FB06)
LIGATURE_RANGE = range(0xFB00, 0xFB07)
# Private Use Area (BMP); plane-15/16 PUAs are covered by the explicit
# code-point comparison in ``_script_of``.
PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0x10FFFF))

BIDI_CONTROL_CHARS = {
    0x200E,  # LRM
    0x200F,  # RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # LRE/RLE/PDF/LRO/RLO
    0x2066, 0x2067, 0x2068, 0x2069,  # LRI/RLI/FSI/PDI
}

# (name, start, end) script ranges on the BMP, checked in order.
_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("hebrew", 0x0590, 0x05FF),
    ("arabic", 0x0600, 0x06FF),
    ("syriac", 0x0700, 0x074F),
    ("arabic_ext", 0x0750, 0x077F),
    ("arabic_ext_a", 0x08A0, 0x08FF),
    ("latin_ext", 0x00C0, 0x024F),
    ("greek", 0x0370, 0x03FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("devanagari", 0x0900, 0x097F),
    ("thai", 0x0E00, 0x0E7F),
    ("kana", 0x3040, 0x30FF),
    ("kana_ext", 0x31F0, 0x31FF),
    ("cjk_ext_a", 0x3400, 0x4DBF),
    ("cjk_unified", 0x4E00, 0x9FFF),
    ("hangul", 0xAC00, 0xD7AF),
    ("cjk_compat", 0xF900, 0xFAFF),
    ("arabic_present_a", 0xFB50, 0xFDFF),
    ("arabic_present_b", 0xFE70, 0xFEFF),
)

_LATIN_BASIC = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _script_of(char: str) -> str | None:
    """Classify one character into a script bucket, or ``None`` if neutral."""
    code = ord(char)
    if char in _LATIN_BASIC:
        return "latin"
    for name, start, end in _SCRIPT_RANGES:
        if start <= code <= end:
            return name
    if 0x20000 <= code <= 0x2FFFF or 0x30000 <= code <= 0x3FFFF:
        return "cjk_ext_b_plus"
    for start, end in PUA_RANGES:
        if start <= code <= end:
            return "private_use"
    return None


def detect_scripts(text: str) -> list[str]:
    """Return the sorted set of script names present in ``text``."""
    return sorted({name for char in text if (name := _script_of(char)) is not None})


def script_profile(text: str) -> dict[str, int]:
    """Count scripts and character classes relevant to B8 evidence.

    Returned keys are stable evidence fields: ``latin``, ``arabic`` (and
    friends), ``cjk``-family buckets, ``ligature_chars``, ``pua_chars``,
    ``combining_marks``, ``bidi_control_chars``, ``replacement_chars``.
    Neutral characters (spaces, digits, punctuation) are simply not
    counted anywhere.
    """
    counts: dict[str, int] = {}
    for char in text:
        code = ord(char)
        if code == 0xFFFD:
            counts["replacement_chars"] = counts.get("replacement_chars", 0) + 1
            continue
        if code in LIGATURE_RANGE:
            counts["ligature_chars"] = counts.get("ligature_chars", 0) + 1
            continue
        if code in BIDI_CONTROL_CHARS:
            counts["bidi_control_chars"] = counts.get("bidi_control_chars", 0) + 1
            continue
        if unicodedata.category(char) == "Mn":
            counts["combining_marks"] = counts.get("combining_marks", 0) + 1
            continue
        script = _script_of(char)
        if script is not None:
            counts[script] = counts.get(script, 0) + 1
    return counts


def is_rtl_script(script_name: str) -> bool:
    """Whether a script bucket from :func:`script_profile` renders RTL."""
    return script_name in (
        "arabic",
        "arabic_ext",
        "arabic_ext_a",
        "arabic_present_a",
        "arabic_present_b",
        "hebrew",
        "syriac",
    )
