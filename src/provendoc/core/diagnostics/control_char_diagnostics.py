from provendoc.core.foundation.char_policy import (
    REPLACEMENT_CHAR,
    STATS_CHAR_SETS,
)


def count_replacement_chars(text: str) -> int:
    """Count of U+FFFD replacement characters."""
    return text.count(REPLACEMENT_CHAR)


# The control-char set is declared by the char_policy table (C0 minus
# \t \n \r, plus DEL 0x7F); this module only aggregates counts and holds
# no local definition. Observation-scope = \x1b (control_char, still
# replaced with U+FFFD) ∪ lazily preserved verbatim C0
# (control_char_preserved): an honest count of source-file facts,
# independent of what the transform layer preserves.
_SUSPICIOUS_CHARS = frozenset(
    STATS_CHAR_SETS["control_char"] | STATS_CHAR_SETS["control_char_preserved"]
)


def count_suspicious_chars(text: str) -> int:
    """Count of control characters (C0 minus \\n \\t \\r; plus DEL 0x7F).

    The set is derived from the "control_char" ∪ "control_char_preserved"
    stat categories of core.foundation.char_policy, sharing the same
    single source of truth as the transform layer's sanitization stats.
    """
    return sum(text.count(ch) for ch in _SUSPICIOUS_CHARS)


def escape_control_chars_for_display(text: str) -> str:
    """Display-layer escaping: render control characters as visible
    ``\\xNN`` forms, pass everything else through verbatim.

    Defense-in-depth tool (read-only, never rewrites stored text):
    lazy C0 control characters are preserved verbatim in chunk text
    (``\\x1b`` is still replaced with U+FFFD). Before printing raw
    chunk text to a terminal / log / messaging system, run it through
    this function so control characters cannot trigger terminal
    control sequences or pollute log lines. It is independent of the
    transform layer and performs no modification of chunk content.
    """
    return "".join(
        f"\\x{ord(ch):02x}" if ch in _SUSPICIOUS_CHARS else ch for ch in text
    )
