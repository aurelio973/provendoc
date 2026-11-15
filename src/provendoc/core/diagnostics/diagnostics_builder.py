def build_diagnostics(
        replacement_chars,
        suspicious_chars,
        printable_ratio,
        symbol_ratio,
        empty_line_ratio,
        hidden_unicode,
        contains_ansi_escape,
        emoji_ratio=0.0,
        repetition=None,
        blank_run_excess_ratio=0.0
):
    """Assemble character-level observation metrics into a diagnostics dict.

    Pure assembly: each metric is computed by its detector (unicode_diagnostics
    et al.) and passed in; this function unifies precision (ratios rounded to
    3 decimals) and guarantees a complete fixed set of keys for
    build_warnings and result-metadata consumers.

    Args:
        replacement_chars: count of U+FFFD replacement characters.
        suspicious_chars: count of suspicious control characters.
        printable_ratio: fraction of printable characters (0-1).
        symbol_ratio: fraction of symbol characters (0-1).
        empty_line_ratio: fraction of empty lines (0-1).
        hidden_unicode: list of hidden-unicode detection results.
        contains_ansi_escape: whether ANSI escape sequences are present.
        emoji_ratio: fraction of emoji characters (0-1).
        repetition: repetition detection result; None is normalized to [].
        blank_run_excess_ratio: excess ratio of consecutive blank lines (0-1);
            the shared base for the blank-line measurement (event-domain and
            legacy callers take the default 0.0 and never trigger a warning).

    Returns:
        dict: the diagnostic-metric table with a fixed key set.
    """
    return {
        "replacement_chars": replacement_chars,
        "suspicious_chars": suspicious_chars,
        "printable_ratio": round(printable_ratio, 3),
        "symbol_ratio": round(symbol_ratio, 3),
        "empty_line_ratio": round(empty_line_ratio, 3),
        "hidden_unicode": hidden_unicode,
        "contains_ansi_escape": contains_ansi_escape,
        "emoji_ratio": round(emoji_ratio, 3),
        "repetition": repetition or [],
        "blank_run_excess_ratio": round(blank_run_excess_ratio, 3)
    }
