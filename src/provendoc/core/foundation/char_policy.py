"""Character-classification policy table — single source of truth.

The transform layer (core.text.normalization) and the observation layer
(core.diagnostics.unicode_diagnostics,
intelligence.diagnostics.document_quality) used to each keep their own
copy of the character tables, kept consistent only by convention, and
scope drift already happened once (document_quality missed the
U+2066-2069 isolates).

This module consolidates every character-classification decision;
each character is declared exactly once:

  - report_name: observation-layer report name (detect_hidden_unicode's
    type; None = not reported individually)
  - transform:   transform-layer action ("space" / "replacement" / "preserve")
  - stats_key:   category key for transform-layer sanitization stats
    (None = not counted)

The layering contract is unchanged: this module only declares policy
and performs no text transformation or diagnostics.
"""

from __future__ import annotations

import re
from typing import NamedTuple

REPLACEMENT_CHAR = "\ufffd"

# ANSI escape sequences (\x1B[...m): a sequence-level policy, not part of
# the per-character table. The transform layer (normalization) and the
# observation layer (unicode_diagnostics / document_quality) share this
# pattern — no local copies.
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# ANSI SGR pure color codes (\x1b[0m / \x1b[31;1m): color/style control
# carries no information, deleted outright (processed before
# ANSI_ESCAPE_PATTERN so nothing noisy remains); other ANSI control
# sequences (cursor moves / screen clears) are still replaced with
# U+FFFD for traceability.
ANSI_SGR_PATTERN = re.compile(r"\x1B\[[0-;]*m")

# ---- transform actions ----
SPACE = "space"              # replace with a plain space (keeps word boundaries)
REPLACEMENT = "replacement"  # replace with U+FFFD
STRIP = "strip"              # delete outright (pure display control: removing the character restores logically-ordered text)
PRESERVE = "preserve"        # exceptional preservation, counting only
NEWLINE_BREAK = "newline_break"  # whole character becomes a blank line (page boundary -> paragraph boundary, structural semantics preserved)


class CharPolicy(NamedTuple):
    """Classification policy for a single character (observation report
    name / transform action / stats key)."""

    report_name: str | None
    transform: str
    stats_key: str | None


def _c0_controls() -> frozenset:
    """C0 control characters + DEL, minus \\t \\n \\r (legitimate text
    structure)."""
    return frozenset(
        chr(cp) for cp in range(32) if cp not in (0x09, 0x0A, 0x0D)
    ) | {"\x7f"}


# ---- per-character policy table ----
# Observation-layer names and transform-layer actions are aligned here;
# adding a new character class means touching exactly this one place.

_CHAR_POLICY: dict[str, CharPolicy] = {
    # form feed (U+000C): the page separator of old plain-text documents
    # (page-boundary semantics of txt). Whole character becomes a blank
    # line (page boundary -> paragraph boundary) preserving structural
    # semantics, no U+FFFD injected; stats key form_feed. The observation
    # layer's suspicious_chars still counts it pre-sanitization (an honest
    # exposure of the control-character facts in the source file). The
    # two-character replacement is carried out by a dedicated transform-
    # layer stage (the translate table only supports single characters);
    # the transform field here only declares the action.
    "\x0c": CharPolicy("form_feed", NEWLINE_BREAK, "form_feed"),
    # zero-width characters
    "\u200b": CharPolicy("zero_width_space", SPACE, "zero_width_space"),
    "\u200c": CharPolicy("zero_width_non_joiner", REPLACEMENT, "zero_width"),
    # ZWJ: emoji ZWJ-sequence joiner, exceptionally preserved.
    # PRESERVE is only the static fallback for legitimate shapes — a ZWJ
    # without emoji neighbors (steganographic carrier) is stripped by
    # the transform layer's context guard (zwj_stripped), whose detection
    # shares the single foundation.emoji_legality implementation with the
    # observation layer. The stats_key semantics are accordingly narrowed
    # to "legitimate ZWJ surviving the guard".
    "\u200d": CharPolicy("zero_width_joiner", PRESERVE, "zwj_preserved"),
    "\u2060": CharPolicy("word_joiner", REPLACEMENT, "zero_width"),
    # U+FEFF refers only to occurrences mid-body (the file-head BOM is
    # handled by strip_bom). A mid-body BOM / zero-width no-break space
    # carries no information, deleted outright — injecting U+FFFD would
    # only create noise at the start of each line ("�Another BOM
    # here"), same principle as the SGR color codes; the count goes to the
    # dedicated stats key mid_file_bom (no U+FFFD injected, excluded from
    # the replacement_injected reconciliation), raw-layer originals stay
    # traceable.
    "\ufeff": CharPolicy("zero_width_no_break_space", STRIP, "mid_file_bom"),
    # directional marks / BiDi embedding overrides / Unicode 6.3 isolates
    # (Trojan Source attack carriers)
    # Pure display-direction control: parsed by sanitize_invisible_chars
    # stage 0 by pairing structure — paired markers wrapping pure LTR
    # content → deleting the markers restores logically-ordered semantics;
    # not reliably parseable (unpaired / nested / RTL content) → falls
    # back to U+FFFD replacement. The STRIP entries in the translate
    # table are only a fallback (stage 0 normally consumes all BiDi).
    "\u200e": CharPolicy("left_to_right_mark", STRIP, "bidi_control"),
    "\u200f": CharPolicy("right_to_left_mark", STRIP, "bidi_control"),
    "\u202a": CharPolicy("left_to_right_embedding", STRIP, "bidi_control"),
    "\u202b": CharPolicy("right_to_left_embedding", STRIP, "bidi_control"),
    "\u202c": CharPolicy("pop_directional_formatting", STRIP, "bidi_control"),
    "\u202d": CharPolicy("left_to_right_override", STRIP, "bidi_control"),
    "\u202e": CharPolicy("right_to_left_override", STRIP, "bidi_control"),
    "\u2066": CharPolicy("left_to_right_isolate", STRIP, "bidi_control"),
    "\u2067": CharPolicy("right_to_left_isolate", STRIP, "bidi_control"),
    "\u2068": CharPolicy("first_strong_isolate", STRIP, "bidi_control"),
    "\u2069": CharPolicy("pop_directional_isolate", STRIP, "bidi_control"),
    # Line separator (U+2028) / paragraph separator (U+2029): Unicode-defined
    # line/paragraph boundaries, but interpreted inconsistently across
    # renderers and toolchains (a smuggling channel). Policy follows the
    # form-feed precedent (NEWLINE_BREAK): line boundary -> \n, paragraph
    # boundary -> blank line, structural semantics preserved, no U+FFFD
    # injected; the two-character replacement is carried out by a
    # dedicated transform-layer stage (the translate table only supports
    # single characters). Corpus evidence (612 txt files): zero hits in
    # the real corpus, only attack samples — so counting them into the
    # census line_sep family carries no false-positive risk.
    "\u2028": CharPolicy("line_separator", NEWLINE_BREAK, "line_sep"),
    "\u2029": CharPolicy("paragraph_separator", NEWLINE_BREAK, "line_sep"),
}

# C0 control characters + DEL: lazy byte noise (no display or semantic
# effect, cannot hide characters or reorder) — preserved verbatim with
# counting only (control_char_preserved), no visible U+FFFD noise
# injected; the sole exception is \x1b (ANSI escape introducer, terminal
# injection carrier): still replaced with U+FFFD (control_char). Neither
# is reported individually in hidden_unicode (aggregated by
# count_suspicious_chars).
for _ch in _c0_controls():
    if _ch == "\x1b":
        _CHAR_POLICY.setdefault(_ch, CharPolicy(None, REPLACEMENT, "control_char"))
    else:
        _CHAR_POLICY.setdefault(
            _ch, CharPolicy(None, PRESERVE, "control_char_preserved")
        )


# ---- derived views (consumers take what they need) ----

# Observation layer: detect_hidden_unicode's {character: report name}
HIDDEN_UNICODE_MAP: dict[str, str] = {
    ch: p.report_name
    for ch, p in _CHAR_POLICY.items()
    if p.report_name is not None
}

# Transform layer: character sets per sanitization stats category
STATS_CHAR_SETS: dict[str, frozenset] = {
    key: frozenset(ch for ch, p in _CHAR_POLICY.items() if p.stats_key == key)
    for key in {
        p.stats_key for p in _CHAR_POLICY.values() if p.stats_key is not None
    }
}

# Transform layer: single-character replacement table for str.translate
# {ord: replacement} (a None value makes str.translate delete the
# character — the STRIP action)
TRANSLATE_TABLE: dict[int, str | None] = {}
for _ch, _p in _CHAR_POLICY.items():
    if _p.transform == REPLACEMENT:
        TRANSLATE_TABLE[ord(_ch)] = REPLACEMENT_CHAR
    elif _p.transform == SPACE:
        TRANSLATE_TABLE[ord(_ch)] = " "
    elif _p.transform == STRIP:
        TRANSLATE_TABLE[ord(_ch)] = None

# Exceptionally preserved characters (e.g. ZWJ): kept out of the
# translate table, counting only
PRESERVED_CHARS: frozenset = frozenset(
    ch for ch, p in _CHAR_POLICY.items() if p.transform == PRESERVE
)

# Full set of BiDi controls (for the transform layer's stage-0 pairing)
BIDI_CONTROLS: frozenset = frozenset(
    ch for ch, p in _CHAR_POLICY.items() if p.stats_key == "bidi_control"
)
# Pairing: opening marker -> closing marker (embedding/override family
# closes with PDF; isolate family closes with PDI)
BIDI_PAIRS: dict[str, str] = {
    "\u202a": "\u202c",
    "\u202b": "\u202c",
    "\u202d": "\u202c",
    "\u202e": "\u202c",
    "\u2066": "\u2069",
    "\u2067": "\u2069",
    "\u2068": "\u2069",
}
# Strongly-RTL script regions (Arabic/Hebrew etc.): skip pairing
# analysis when such text is present
RTL_SCRIPT_RE = re.compile(
    "[\u0590-\u05ff\u0600-\u06ff\u0700-\u074f\u07c0-\u08ff"
    "\ufb50-\ufdfff\ufe70-\ufeff]"
)


def char_policy(char: str) -> CharPolicy | None:
    """Query the policy for one character; returns None when it is not
    in the table (i.e. no special handling)."""
    return _CHAR_POLICY.get(char)
