# -*- coding: utf-8 -*-
"""Log hygiene contract.

Boundary and positioning: the broadcast surface (warnings /
metadata JSON) is already disinfected by the char_policy +
normalization pipeline; the log surface (standard logging with lazy
%s interpolation) was previously not covered. This module adds the
structured escaping specific to the log surface — it is a second
SURFACE, not a second source of truth: this module only declares
the log-surface policy; the broadcast-surface policy remains
authoritative in char_policy; the two do not reference each other
and each is frozen separately.

Threat model: document-derived strings (file names, paths, parse
error texts, document metadata values) entering logs enable:

    - terminal injection: ANSI escape sequences altering terminal
      rendering;
    - log forgery: fake timestamp/level/field lines after a \\n /
      \\r, polluting downstream log collection and audit;
    - noise injection: overly long separator-free content squeezing
      log storage and collection quotas.

Frozen policy (``LOG_HYGIENE_POLICY``):

    - ansi_sequences_deleted: complete ANSI escape sequences (CSI /
      OSC / two-character ESC sequences) deleted as whole segments
      — executed before the control-character replacement so no
      truncated remnant sequences survive;
    - newline_escaped: \\n / \\r / \\t escaped to their literal
      two-character forms (guards forged log lines / column-align
      deception);
    - control_replaced: remaining C0 (0x00-0x1F) + DEL (0x7F) + C1
      (0x80-0x9F) each replaced by U+FFFD (including stray ESC and
      NEL 0x85);
    - length_truncated: truncated past max_len with a visible
      "…[+N chars]" marker appended (N = dropped characters,
      bounded and auditable).

Discipline: pure function, never raises (guards are non-fatal),
idempotent (sanitizing twice equals once). Non-string input is
str()-ed first; None renders explicitly as "<none>" — honest
absence, never faked as an empty string.
"""

from __future__ import annotations

import re
from typing import Any

LOG_HYGIENE_SCHEME = "log_hygiene_v1"

# Frozen policy table — changes require a scheme version bump.
LOG_HYGIENE_POLICY = {
    "ansi_sequences_deleted": True,
    "newline_escaped": True,
    "control_replaced": True,
    "length_truncated": True,
}

#: Max length of a single log-surface value (characters). Enough
#: for file paths / error summaries; overlong content is a
#: noise-injection face.
DEFAULT_LOG_VALUE_MAX_LEN = 512

# Complete ANSI escape sequences: CSI (ESC [ params intermediates
# terminator), OSC (ESC ] ... BEL / ST), and the remaining
# two-character ESC sequences (ESC @-Z / \\ / _).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[@-Z\\-_]"
    r")"
)


def _build_translation() -> dict[int, str]:
    """Build the log-surface character translation table (once at module load)."""
    table: dict[int, str] = {}
    for codepoint in range(0x00, 0x20):  # C0
        table[codepoint] = "\ufffd"
    for codepoint in range(0x7F, 0xA0):  # DEL + C1
        table[codepoint] = "\ufffd"
    # newline/CR/TAB: literal escape (guards log forgery), overriding
    # the default replacement above.
    table[0x0A] = "\\n"
    table[0x0D] = "\\r"
    table[0x09] = "\\t"
    return table


_LOG_TRANSLATION = _build_translation()


def sanitize_log_value(
    value: Any,
    max_len: int = DEFAULT_LOG_VALUE_MAX_LEN,
) -> str:
    """Structured escaping of document-derived strings before they reach the log surface (pure function; never raises).

    Args:
        value: any input (None -> ``"<none>"``; non-strings are
            str()-ed first).
        max_len: truncation limit (characters, including the prefix
            part before the appended marker).

    Returns:
        The escaped string: no raw newlines, no ANSI escapes, no
        other C0/C1/DEL control characters, bounded length (the
        truncation point carries a visible ``…[+N chars]`` marker).
    """
    try:
        if value is None:
            return "<none>"
        text = value if isinstance(value, str) else str(value)
        # 1. complete ANSI sequences deleted whole (before control
        #    replacement — fewer remnant-sequence artifacts)
        text = _ANSI_ESCAPE_RE.sub("", text)
        # 2. C0/DEL/C1 -> U+FFFD; \n \r \t -> literal escapes
        text = text.translate(_LOG_TRANSLATION)
        # 3. length truncation (visible marker, N = the real number
        #    of dropped characters; the marker itself counts toward
        #    max_len — the output length is exactly max_len,
        #    keeping the function idempotent)
        if len(text) > max_len:
            dropped = len(text) - max_len
            marker = f"…[+{dropped} chars]"
            keep = max_len - len(marker)
            # The dropped count and the marker's digit growth
            # converge (at most a couple of rounds).
            for _ in range(4):
                if keep <= 0:
                    break
                new_dropped = len(text) - keep
                if new_dropped == dropped:
                    break
                dropped = new_dropped
                marker = f"…[+{dropped} chars]"
                keep = max_len - len(marker)
            if keep > 0:
                text = text[:keep] + marker
            else:
                # max_len cannot even fit the marker: hard truncation
                # + a minimal ellipsis (still idempotent).
                text = text[:max_len] + "…"
        return text
    except Exception:  # pragma: no cover — guards are non-fatal
        return "<log-sanitize-failed>"


__all__ = [
    "DEFAULT_LOG_VALUE_MAX_LEN",
    "LOG_HYGIENE_POLICY",
    "LOG_HYGIENE_SCHEME",
    "sanitize_log_value",
]
