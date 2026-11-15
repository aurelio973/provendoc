# -*- coding: utf-8 -*-
"""Security-carrier census (observation layer).

Positioning: **carrier detection, not attack detection** — deterministic
identification and counting of invisible/disguised character-level
carriers, with zero false-positive room (unless the scope itself counts
a legitimate character). Semantic-level injection is out of scope.

Layering contract: this module only observes, never modifies — input is
pre-sanitization text (same view as build_diagnostics), output is a
per-family count dict; no text transformation is produced.

Family enumeration (order = presentation order):
    zero_width / zwj / bidi_control / ansi_escape / c0_control /
    tag_chars / variation_selector / line_sep / homoglyph
(The homoglyph count unit = number of hit runs (token level), unlike
the character counts of the other eight families; detection and the H-set
derivation share the single foundation.homoglyph_policy implementation,
observation-only with no sanitization action.)

Scope details:
    - First-tier families derive their character sets from char_policy
      stat keys (single source of truth, no duplicated character tables);
      \x0c/t/n/r are naturally excluded (different stat key / not in the
      table).
    - ansi_escape counts ANSI CSI sequences (ANSI_ESCAPE_PATTERN);
      a bare \x1b outside any sequence goes to c0_control (each CSI
      sequence consumes exactly one \x1b, so the c0 side uses total
      \x1b count minus sequence count — no double counting).
    - line_sep: U+2028/U+2029 derived from the char_policy stat key
      line_sep (8th family).
    - tag_chars: count of **malformed instances** — well-formed emoji
      subdivision-flag sequences (base + tag payload + U+E007F
      terminator) are exempt; detection shares
      foundation.emoji_legality (legitimate forms must not be counted
      as security facts once sanitization handles them).
    - variation_selector: only instances of U+FE00-FE0F that are neither
      adjacent to an emoji base nor part of a keycap (guards against
      VS16 false positives).
    - zwj: count of **illegally-adjacent instances** — in a legitimate
      emoji ZWJ sequence both sides are always emoji sequence
      components, which are exempt; wholesale counting would produce
      guaranteed false positives in emoji documents. Detection shares
      foundation.emoji_legality.
"""

from __future__ import annotations

from typing import Dict, List

from provendoc.core.foundation.char_policy import (
    ANSI_ESCAPE_PATTERN,
    STATS_CHAR_SETS,
)
from provendoc.core.foundation.emoji_legality import (
    scan_sequence_violations,
)
from provendoc.core.foundation.homoglyph_policy import (
    count_homoglyph_runs,
)

# ---- first tier: derived from policy-table stat keys (guards against set drift) ----
_ZERO_WIDTH_CHARS = (
    STATS_CHAR_SETS["zero_width_space"]
    | STATS_CHAR_SETS["zero_width"]
    | STATS_CHAR_SETS["mid_file_bom"]
)
_BIDI_CHARS = STATS_CHAR_SETS["bidi_control"]
# C0+DEL minus \t\n\r (not in the table) minus \x0c (stats_key=form_feed,
# excluded from the census); \x1b attribution follows the ansi_escape
# rule and is handled separately during the scan.
_C0_CHARS = (
    STATS_CHAR_SETS["control_char"]
    | STATS_CHAR_SETS["control_char_preserved"]
) - {"\x1b"}

# ---- 8th family: line/paragraph separators, derived from policy-table stat keys ----
_LINE_SEP_CHARS = STATS_CHAR_SETS["line_sep"]

# ---- second tier: tag / VS / ZWJ malformed-instance counting ----
# Detection shares the single foundation.emoji_legality implementation,
# same source as the transform layer's sanitization — no dual-scope drift.

# Family enumeration (presentation order)
CARRIER_FAMILIES = (
    "zero_width",
    "zwj",
    "bidi_control",
    "ansi_escape",
    "c0_control",
    "tag_chars",
    "variation_selector",
    "line_sep",
    "homoglyph",
)


def detect_security_carriers(text: str) -> Dict[str, int]:
    """Census security carriers in pre-sanitization text; return
    {family: count} (non-zero families only).

    Deterministic pure function: same input, same output; predominantly a
    single O(n) pass (str.count is C-level; the tag/VS/ZWJ violation scan
    has a C-level pre-check hint and skips the per-character loop when
    nothing hits).
    """
    census: Dict[str, int] = {}

    zero_width = sum(text.count(ch) for ch in _ZERO_WIDTH_CHARS)
    if zero_width:
        census["zero_width"] = zero_width

    bidi = sum(text.count(ch) for ch in _BIDI_CHARS)
    if bidi:
        census["bidi_control"] = bidi

    ansi = len(ANSI_ESCAPE_PATTERN.findall(text))
    if ansi:
        census["ansi_escape"] = ansi

    # C0+DEL: count table characters directly; a bare \x1b (not part of
    # any CSI sequence) goes to this family
    c0 = sum(text.count(ch) for ch in _C0_CHARS)
    bare_esc = text.count("\x1b") - ansi
    if bare_esc > 0:
        c0 += bare_esc
    if c0:
        census["c0_control"] = c0

    # tag / VS / ZWJ: one violation scan yields all three family counts
    bad_tag, bad_vs, bad_zwj = scan_sequence_violations(text)
    if bad_zwj:
        census["zwj"] = len(bad_zwj)
    if bad_tag:
        census["tag_chars"] = len(bad_tag)
    if bad_vs:
        census["variation_selector"] = len(bad_vs)

    line_sep = sum(text.count(ch) for ch in _LINE_SEP_CHARS)
    if line_sep:
        census["line_sep"] = line_sep

    # homoglyph family: hit-run counting; the C-level pre-check
    # short-circuits when H characters are sparse. Observation-only,
    # no sanitization action.
    homoglyph = count_homoglyph_runs(text)
    if homoglyph:
        census["homoglyph"] = homoglyph

    return census


def build_carrier_facts(census: Dict[str, int]) -> List[str]:
    """Render the census as a list of fact codes.

    ``security_carrier:<family>:<count>`` — attached to
    metadata["warnings"], aggregated per family after tiering
    normalization (digits placeholder-ized to N).
    """
    return [
        f"security_carrier:{family}:{census[family]}"
        for family in CARRIER_FAMILIES
        if census.get(family)
    ]
