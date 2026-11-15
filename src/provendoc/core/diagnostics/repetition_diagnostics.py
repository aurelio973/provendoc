"""Repetition diagnostics — detect meaningless repetition (line-level /
character-level).

Design stance:
    - Repetition is a content-quality problem, not an encoding problem →
      report only (write diagnostics + warning), never replace
      (replacing would delete content);
    - Sits at the same level as encoding-type diagnostics
      (replacement_chars etc.), uniformly consumed by build_warnings.

Detection rules:
    1. Consecutive repeated lines: the same line (after strip) appearing
       >= 3 times in a row triggers
    2. Repeated-line ratio: repeated lines (appearing >= 2 times anywhere)
       making up >= 40% of non-empty lines triggers
    3. Consecutive repeated characters: the same character repeated
       >= 6 times triggers (works for any script, e.g. "aaaaaa",
       "。。。。。。", meaningless filler)
"""


def detect_repetition(text: str) -> list[dict]:
    """Detect meaningless repetition, return [{type, count, sample}, ...].

    Script-agnostic: the criterion is "repetition itself", not any
    specific character — no alphabet or tokenization involved.
    """
    if not text:
        return []

    findings: list[dict] = []
    lines = text.splitlines()

    # ---- 1. consecutive repeated lines (>= 3 identical lines in a row) ----
    max_run = 1
    current_run = 1
    current_line = None
    sample_line = None
    for line in lines:
        stripped = line.strip()
        if stripped and stripped == current_line:
            current_run += 1
            if current_run > max_run:
                max_run = current_run
                sample_line = stripped
        else:
            current_line = stripped
            current_run = 1
    if max_run >= 3:
        findings.append({
            "type": "consecutive_repeated_lines",
            "count": max_run,
            "sample": (sample_line or "")[:40],
        })

    # ---- 2. repeated-line ratio (lines appearing >= 2 times over non-empty) ----
    non_empty = [line.strip() for line in lines if line.strip()]
    if len(non_empty) >= 5:
        counts: dict[str, int] = {}
        for line in non_empty:
            counts[line] = counts.get(line, 0) + 1
        repeated = sum(c for c in counts.values() if c >= 2)
        ratio = repeated / len(non_empty)
        if ratio >= 0.4:
            findings.append({
                "type": "repeated_line_ratio",
                "count": round(ratio, 3),
                "sample": f"{repeated}/{len(non_empty)} lines",
            })

    # ---- 3. consecutive repeated characters (same char >= 6 times, script-agnostic) ----
    char_run_max = 1
    char_run = 1
    run_char = ""
    for ch in text:
        if ch == run_char and not ch.isspace():
            char_run += 1
            if char_run > char_run_max:
                char_run_max = char_run
                max_run_char = ch
        else:
            run_char = ch
            char_run = 1
    if char_run_max >= 6:
        findings.append({
            "type": "consecutive_repeated_chars",
            "count": char_run_max,
            "sample": ("" if not char_run_max else max_run_char) * 5 + "...",
        })

    return findings
