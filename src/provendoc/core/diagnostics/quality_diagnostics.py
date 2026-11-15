import unicodedata
from collections import Counter


def printable_ratio(text: str) -> float:
    """Fraction of characters that are printable (including \\n \\t \\r).

    1.0 = fully normal, 0.5 = heavily abnormal, 0.1 = nearly binary.

    Performance-equivalent rewrite: a per-character loop became Counter
    aggregation (C-level counting + per-unique-character class checks),
    collapsing pathological inputs dense with repeated characters
    (e.g. 100k consecutive ``{``) from an O(N) Python loop to O(unique);
    adversarial bit-equivalence probes confirm zero drift.
    """
    if not text:
        return 1.0
    counts = Counter(text)
    printable = sum(
        n
        for char, n in counts.items()
        if char.isprintable() or char in ("\n", "\t", "\r")
    )
    return printable / len(text)


def symbol_ratio(text: str) -> float:
    """Fraction of Unicode symbol-category characters (category starting
    with S) among visible characters.

    Performance-equivalent rewrite: same as printable_ratio — Counter
    aggregation over unique characters, category checked per unique char;
    bit-equivalent, zero drift.
    """
    if not text:
        return 0.0

    counts = Counter(text)
    symbol_count = 0
    visible_count = 0

    for char, n in counts.items():
        if char in ("\n", "\t", "\r"):
            continue
        visible_count += n
        if unicodedata.category(char).startswith("S"):
            symbol_count += n

    if visible_count == 0:
        return 0.0
    return symbol_count / visible_count


# Conservative heuristic set of emoji code-point ranges (no third-party
# dependency). Covers the major emoji blocks; plain symbols like math or
# currency signs are not included.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # Miscellaneous Symbols, Emoticons, Supplemental Symbols and Pictographs, etc.
    (0x2600, 0x27BF),    # Misc Symbols + Dingbats (☀☎✈✂)
    (0x2B00, 0x2BFF),    # Misc Symbols and Arrows (⭐⬛)
    (0x1F1E6, 0x1F1FF),  # Regional indicators (flags; already inside the first range, declared explicitly for clarity)
)


def _is_emoji(char: str) -> bool:
    cp = ord(char)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def emoji_ratio(text: str) -> float:
    """Fraction of emoji characters among visible characters.

    Kept separate from symbol_ratio: symbol_ratio is the blanket metric for
    all S-category symbols (math/currency included), while emoji_ratio counts
    only emoji — an early-degradation signal for "emoji spam thinning out
    semantics" (threshold in warnings_builder).

    Performance-equivalent rewrite: same as printable_ratio — Counter
    aggregation over unique characters with _is_emoji per unique char;
    bit-equivalent, zero drift (adversarial probes measured pathological
    inputs at 0.072s → 0.003s).
    """
    if not text:
        return 0.0

    counts = Counter(text)
    emoji_count = 0
    visible_count = 0

    for char, n in counts.items():
        if char in ("\n", "\t", "\r"):
            continue
        visible_count += n
        if _is_emoji(char):
            emoji_count += n

    if visible_count == 0:
        return 0.0
    return emoji_count / visible_count


def empty_line_ratio(text: str) -> float:
    """Fraction of empty lines (blank after strip) among all lines."""
    lines = text.splitlines()
    if not lines:
        return 0.0
    empty = sum(
        1
        for line in lines
        if not line.strip()
    )
    return empty / len(lines)


def blank_run_excess_ratio(text: str) -> float:
    """Excess ratio of consecutive blank lines: rows past the first of each
    blank run, over total line count.

    Shared measure backing the blank-line measurement (the common base for
    the content_density penalty and the warnings broadcast): a single blank
    line is the paragraph-separator convention of markdown/rst (and common
    plain-text layout), not padding; rows past the first of a blank run are
    what dilute density. Line splitting uses ``str.splitlines()`` — same
    scope as empty_line_ratio (the parser normalizes newlines before
    diagnostics).
    """
    lines = text.splitlines()
    if not lines:
        return 0.0
    excess = 0
    run = 0
    for line in lines:
        if not line.strip():
            run += 1
            if run >= 2:
                excess += 1
        else:
            run = 0
    return excess / len(lines)
