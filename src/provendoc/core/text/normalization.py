"""Transform layer: text normalization and invisible-character handling.

Layering contract:
    - the transform layer (core/text) only changes text, no warnings;
    - the observation layer (core/diagnostics) only observes, owns
      warnings;
    - the orchestration layer (base/parser) diagnoses first, then
      normalizes, and records handling stats in metadata.

Character-set contract: which characters are handled, how, and how
they are reported is declared solely by the policy table in
core.foundation.char_policy (single source of truth); this module only
executes it.
"""

from provendoc.core.foundation.char_policy import (
    ANSI_ESCAPE_PATTERN,
    ANSI_SGR_PATTERN,
    BIDI_CONTROLS,
    BIDI_PAIRS,
    REPLACEMENT_CHAR,
    RTL_SCRIPT_RE,
    STATS_CHAR_SETS,
    TRANSLATE_TABLE,
)
from provendoc.core.foundation.emoji_legality import (
    scan_sequence_violations,
)


def _resolve_bidi(text: str) -> tuple[str, int, int]:
    """Pair-resolve BiDi control characters (phase 0, before other
    sanitization).

    Resolvable → semantics restored: an opening marker plus its
    paired closing marker wrapping non-empty content that contains
    no nested BiDi or strong RTL text — dropping both markers yields
    the logical-order text; counted as resolved. RLO (U+202E)
    visual reversal is a rendering-layer effect and is never
    materialized into the text — after marker stripping the
    logical order stays ("mirrored" reads "mirrored"). Weak
    directional marks (LRM/RLM) have no pairing semantics and are
    simply deleted, also counted as resolved.

    Unresolvable → degraded replacement: an unpaired opening marker,
    a stray closing marker, or nested / strong-RTL content gets its
    markers replaced with U+FFFD for traceability, counted as
    unresolved. The body text (including RTL text itself) is kept
    as-is; no forced resolution.
    """
    if not any(ch in text for ch in BIDI_CONTROLS):
        return text, 0, 0

    openers = BIDI_PAIRS
    closers = set(BIDI_PAIRS.values())
    marks = ("\u200e", "\u200f")

    out: list[str] = []
    resolved = 0
    unresolved = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in openers:
            closer = openers[ch]
            j = text.find(closer, i + 1)
            inner = text[i + 1 : j] if j != -1 else ""
            if (
                j != -1
                and inner
                and not any(c in BIDI_CONTROLS for c in inner)
                and not RTL_SCRIPT_RE.search(inner)
            ):
                # Paired with determinably pure-LTR content: drop the
                # markers to restore semantics. When both sides of the
                # markers are spaces ("Normal <RLO> mirrored <PDF> back")
                # the seam would leave a double space — collapse to one.
                if out and out[-1] == " " and inner.startswith(" "):
                    inner = inner[1:]
                if (
                    inner.endswith(" ")
                    and j + 1 < n
                    and text[j + 1] == " "
                ):
                    inner = inner[:-1]
                # All paired markers (RLO/LRO/RLE/LRE included) drop the
                # markers and keep the logical order: RLO's visual
                # reversal is a BiDi renderer presentation effect and is
                # not materialized into the text — exported products
                # (raw/markdown) display normally after stripping.
                out.append(inner)
                resolved += 2
                i = j + 1
                continue
            # Unpaired / nested / RTL content: degrade-replace the
            # opening marker; the content flows on to later stages
            out.append(REPLACEMENT_CHAR)
            unresolved += 1
            i += 1
            continue
        if ch in closers:
            # Stray closing marker (its opener was consumed or absent)
            out.append(REPLACEMENT_CHAR)
            unresolved += 1
            i += 1
            continue
        if ch in marks:
            # Weak directional mark: no pairing semantics, just delete
            resolved += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), resolved, unresolved


def normalize_newlines(text: str) -> str:
    """Unify every newline style to \\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_bom(text: str) -> str:
    """Remove the BOM (U+FEFF)."""
    return text.removeprefix("\ufeff")


def _sanitize_sequence_chars(text: str) -> tuple[str, dict]:
    """Emoji-sequence carrier guard (phase 0.6).

    Illegal instances of three context-sensitive dual-state character
    classes are sanitized here; legal forms are preserved byte-for-byte
    (the legality rules are shared with the observation layer via
    foundation.emoji_legality):
        - tag characters: instances outside well-formed subdivision-flag
          sequences → replaced with U+FFFD (visible marker);
        - variation selectors: not adjacent to an emoji base and not a
          keycap → deleted (deleting a modifier never harms the base
          character, same STRIP principle as mid-file BOM);
        - ZWJ: not emoji-adjacent (a steganography carrier) → deleted.
    Stats keys: tag_replaced / vs_stripped / zwj_stripped (non-zero
    entries only).
    """
    bad_tag, bad_vs, bad_zwj = scan_sequence_violations(text)
    if not (bad_tag or bad_vs or bad_zwj):
        return text, {}

    stats: dict = {}
    if bad_tag:
        stats["tag_replaced"] = len(bad_tag)
    if bad_vs:
        stats["vs_stripped"] = len(bad_vs)
    if bad_zwj:
        stats["zwj_stripped"] = len(bad_zwj)

    # 0 = keep, 1 = replace with U+FFFD, 2 = delete
    action = bytearray(len(text))
    for p in bad_tag:
        action[p] = 1
    for p in bad_vs:
        action[p] = 2
    for p in bad_zwj:
        action[p] = 2

    out: list[str] = []
    for idx, ch in enumerate(text):
        m = action[idx]
        if m == 1:
            out.append(REPLACEMENT_CHAR)
        elif m == 2:
            continue
        else:
            out.append(ch)
    return "".join(out), stats


def sanitize_invisible_chars(text: str) -> tuple[str, dict]:
    """Normalize invisible/control characters; returns (new_text,
    per-class stats).

    Normalization actions are declared by the policy table in
    core.foundation.char_policy:
        - BiDi controls (phase 0 pair resolution): a pair wrapping
          pure-LTR content → markers dropped, logical order restored
          (bidi_resolved); unresolvable → degraded to U+FFFD
          (bidi_unresolved);
        - U+200B (zero-width space): converted to a normal space to
          preserve word boundaries;
        - other zero-width characters: replaced with U+FFFD;
        - C0 controls + DEL (except \\x1b): kept verbatim, counted only
          (lazy byte noise — injecting visible U+FFFD noise would be
          worse; control_char_preserved);
        - \\x1b (ANSI escape introducer, a terminal-injection carrier):
          replaced with U+FFFD (control_char);
        - form feed (U+000C): converted to a blank line (page boundary
          → paragraph boundary; form_feed);
        - line/paragraph separators (U+2028/U+2029): line boundary → \n,
          paragraph boundary → blank line (line_sep);
        - tag characters / variation selectors / ZWJ (phase 0.6 context
          guard): tag outside well-formed flag sequences → U+FFFD
          (tag_replaced); illegal VS → deleted (vs_stripped);
          non-emoji-adjacent ZWJ → deleted (zwj_stripped); legal emoji
          forms are preserved byte-for-byte;
        - ANSI SGR pure color codes (\\x1b[31m): deleted outright
          (ansi_sgr_stripped);
        - other ANSI escape sequences: whole sequence replaced with a
          single U+FFFD (ansi_escape);
        - U+200D (ZWJ): a legal emoji-sequence joiner is kept and only
          counted; illegal instances were already stripped in phase 0.6.

    Stats are returned keyed by the policy table's stats_key (non-zero
    entries only):
        bidi_resolved / bidi_unresolved / zero_width_space /
        zero_width / ansi_sgr_stripped / ansi_escape /
        control_char / control_char_preserved / zwj_preserved /
        line_sep / tag_replaced / vs_stripped / zwj_stripped
    """
    if not text:
        return text, {}

    stats = {}

    # 0. BiDi pair resolution: resolvable pairs drop their markers and
    #    restore semantics; unresolvable ones are degraded (phase 0
    #    runs first — all BiDi is consumed here)
    text, bidi_resolved, bidi_unresolved = _resolve_bidi(text)
    if bidi_resolved:
        stats["bidi_resolved"] = bidi_resolved
    if bidi_unresolved:
        stats["bidi_unresolved"] = bidi_unresolved

    # 0.5 form feed (U+000C): the page separator of old plain-text
    #     documents. The whole character becomes a blank line (page
    #     boundary → paragraph boundary), preserving structural
    #     semantics; counted as form_feed, no U+FFFD injected (nothing
    #     is left for the later C0 counting / translate stages).
    #     The observation layer's suspicious_chars already counted it
    #     pre-sanitization, so the source-file fact is preserved.
    n_form_feed = text.count("\x0c")
    if n_form_feed:
        text = text.replace("\x0c", "\n\n")
        stats["form_feed"] = n_form_feed

    # 0.55 line/paragraph separators (U+2028/U+2029): a smuggling
    #     channel that renderers interpret inconsistently — treated
    #     like form feed: line boundary → \n, paragraph boundary →
    #     blank line, structural semantics preserved. In real corpora
    #     these never occur; only attack samples hit them.
    n_line_sep = text.count("\u2028") + text.count("\u2029")
    if n_line_sep:
        text = text.replace("\u2028", "\n").replace("\u2029", "\n\n")
        stats["line_sep"] = n_line_sep

    # 0.6 emoji-sequence carrier guard: tag outside well-formed flag
    #     sequences → U+FFFD; illegal VS / non-emoji-adjacent ZWJ →
    #     deleted; legal forms preserved byte-for-byte.
    text, seq_stats = _sanitize_sequence_chars(text)
    stats.update(seq_stats)

    # 1. ANSI escape sequences:
    #    - SGR pure color codes (\x1b[31m / \x1b[0m): carry no
    #      information — deleted outright (otherwise every color code
    #      would become a U+FFFD and exports fill with noise);
    #    - other control sequences (cursor movement etc.): the whole
    #      sequence becomes a single U+FFFD for traceability;
    #    patterns are declared by the char_policy table
    sgr_matches = ANSI_SGR_PATTERN.findall(text)
    if sgr_matches:
        text = ANSI_SGR_PATTERN.sub("", text)
        stats["ansi_sgr_stripped"] = len(sgr_matches)
    ansi_matches = ANSI_ESCAPE_PATTERN.findall(text)
    if ansi_matches:
        text = ANSI_ESCAPE_PATTERN.sub(REPLACEMENT_CHAR, text)
        stats["ansi_escape"] = len(ansi_matches)

    # 2. Count each policy class (on the post-ANSI-replacement text so
    #    control characters inside sequences are not double-counted)
    n_any = False
    for stats_key, char_set in STATS_CHAR_SETS.items():
        n = sum(text.count(ch) for ch in char_set)
        if n:
            stats[stats_key] = n
            n_any = True

    # 3. Single-character replacement (space / replacement actions all
    #    go through the translate table)
    if n_any:
        text = text.translate(TRANSLATE_TABLE)

    # Exceptional preserved characters (ZWJ / lazy C0) are not in the
    # translate table; they were already counted under their own
    # stats_key in phase 2 — no double counting here.
    return text, stats
