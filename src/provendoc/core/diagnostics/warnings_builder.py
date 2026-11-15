def build_warnings(diagnostics):
    """Translate diagnostics metrics into human-readable warning strings
    by threshold.

    Read-only: a warning is emitted only when a metric crosses its
    threshold (e.g. replacement_chars > 10 suggests mixed encoding);
    returns an empty list when nothing is off.

    Args:
        diagnostics: the metric dict produced by build_diagnostics.

    Returns:
        list[str]: warning strings (possibly empty).
    """
    warnings = []
    if diagnostics["replacement_chars"] > 0:
        warnings.append(
            (
                f'{diagnostics["replacement_chars"]} '
                'replacement chars detected'
            )
        )

    if diagnostics["replacement_chars"] > 10:
        warnings.append("mixed encoding suspected")

    if diagnostics["suspicious_chars"] > 20:
        warnings.append(
            (
                "suspicious control "
                "characters detected"
            )
        )

    if diagnostics["printable_ratio"] < 0.8:
        warnings.append(
            (
                "low printable ratio "
                "detected"
            )
        )

    if diagnostics["hidden_unicode"]:
        warnings.append(
            (
                "hidden unicode "
                "characters detected"
            )
        )

    # Blank-line warning base = excess ratio of consecutive blank lines
    # (rows past the first of each blank run), threshold 0.3 aligned with
    # the scoring layer's content_density penalty. A plain empty_line_ratio
    # threshold would misreport markdown's paragraph-separator convention
    # as "high blank ratio" — a single blank line is layout, not padding.
    # Missing key (legacy/event-domain callers) is treated as silence.
    if diagnostics.get("blank_run_excess_ratio", 0.0) > 0.3:
        warnings.append(
            (
                "excessive consecutive blank "
                "lines detected"
            )
        )

    if diagnostics["symbol_ratio"] > 0.5:
        warnings.append(
            (
                "high symbol ratio "
                "detected"
            )
        )

    # emoji_ratio is independent of symbol_ratio: early-degradation signal.
    # Its 0.2 threshold fires well before symbol_ratio's 0.5 blanket line —
    # a "semantic content may be sparse" hint aimed at RAG scenarios.
    if diagnostics.get("emoji_ratio", 0.0) > 0.2:
        warnings.append(
            (
                "high emoji ratio detected "
                "(semantic content may be sparse)"
            )
        )

    if diagnostics["contains_ansi_escape"]:
        warnings.append(
            (
                "ansi escape sequences "
                "detected"
            )
        )

    for finding in diagnostics.get("repetition", []):
        if finding["type"] == "consecutive_repeated_lines":
            warnings.append(
                f'{finding["count"]} consecutive repeated lines '
                f'(sample: "{finding["sample"]}")'
            )
        elif finding["type"] == "repeated_line_ratio":
            warnings.append(
                f'{finding["sample"]} repeated '
                f'({finding["count"]:.0%} of non-empty lines)'
            )
        elif finding["type"] == "consecutive_repeated_chars":
            warnings.append(
                f'run of {finding["count"]} repeated characters '
                f'(sample: {finding["sample"]})'
            )

    return warnings
