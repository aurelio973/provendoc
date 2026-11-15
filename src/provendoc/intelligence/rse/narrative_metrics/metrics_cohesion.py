"""Semantic-cohesion dimension (SECONDARY): lexical continuity proxy (overlap coefficient)."""

from provendoc.base.models.representation_unit import UnitType
from provendoc.intelligence.rse import narrative_checks

# ==================================
# dimension: semantic cohesion (SECONDARY)
# ==================================
#
# Academic basis:
#   Halliday & Hasan (1976) — lexical cohesion theory.
#   Hearst (1997) TextTiling — word frequency similarity.
#
# This is a LEXICAL cohesion proxy, NOT semantic
# understanding. It measures token overlap between adjacent
# textual units using overlap coefficients. Low scores on
# diverse-vocabulary technical docs do NOT mean poor cohesion.
#
# Responsibility boundary:
#   boundary_quality  → structural surface (sentence/heading/fence)
#   semantic_cohesion → lexical continuity (overlap coefficient)
#
# These two dimensions are deliberately non-overlapping:
#   boundary_quality checks surface structure markers,
#   semantic_cohesion checks lexical token distributions.
# ==================================

# Baseline floor for the relative dip: the boundary sides' own internal continuity (head-three vs tail-three
# jaccard); below this there is no "credible continuity" left to plunge, so dip is recorded as 0.
# 2026-10-04 calibration-set sensitivity scan: 0.10/0.15/0.20 all flip
# bad<good (+0.025/+0.033/+0.035); take the plateau midpoint 0.15 to avoid
# argmax overfitting to the 34 calibration documents.
_BASELINE_FLOOR = 0.15


def semantic_cohesion(chunks):
    """Lexical cohesion: within-chunk + cross-chunk boundary dip.

    Combines two signals:
      within  — chunk internal vocabulary continuity
                (half-split overlap coefficient with penalties
                judged INTO the score, see within_chunk_cohesion
                v7)
      cross   — boundary cohesion dip RELATIVE to each flank's
                own internal continuity (TextTiling depth
                semantics, see cross_boundary_scores v8),
                evidence-weighted by the number of valid
                non-structural cut points

    v8 redesign (2026-10-04, 34-doc x 7-strategy calibration):
      v6 cross used ABSOLUTE dip (1 - jaccard(tail, head)): on
      random text every window pair is dissimilar, so every
      boundary scored a "perfect transition" (dip ~ 1.0) — bad
      band docs (pathological / repetitive / dense) scored ABOVE
      good docs and the band means INVERTED (bad 0.473 vs good
      0.280). The dip is now normalized against local baseline
      continuity, and within penalties moved from weights into
      the score, with a doc-level TTR repetition discount added.
      Post-redesign band means: bad 0.112 < acceptable 0.134 <
      good 0.144 (monotone, direction restored; pure-within
      control is +0.026, cross evidence adds real signal).

    v6 composition fix retained (2026-10-02):
      cross share scales with evidence volume (full 0.4 share
      needs >= 3 valid non-structural boundaries); when there is
      nothing to judge, its weight is redistributed to `within`.

    Metric-gaming note:
      Repetitive text ("AI AI AI") inflates raw within — correct,
      the chunk IS internally coherent — so the doc-level TTR
      discount (doc_ttr_discount) scales that inflation back,
      and cross catches segmentation gaming: a mid-topic cut has
      boundary similarity equal to its own flanks' continuity,
      earning dip ~ 0.
    """
    if len(chunks) <= 1:
        return 1.0

    within = within_chunk_cohesion(chunks) * doc_ttr_discount(chunks)
    boundary_scores = cross_boundary_scores(chunks)

    if not boundary_scores:
        # all cut points structural → nothing to judge for cross; within alone carries the cohesion verdict
        return round(within, 3)

    cross = sum(boundary_scores) / len(boundary_scores)
    # evidence-quantity weighting: with < 3 valid non-structural cut points, shrink the cross share proportionally
    cross_w = 0.4 * min(1.0, len(boundary_scores) / 3.0)
    return round(
        (0.6 * within + cross_w * cross) / (0.6 + cross_w), 3
    )


def _starts_at_structural_break(chunk) -> bool:
    """Whether the chunk starts at a structural (heading) boundary.

    v5 contract fix:
        A chunk with a [Context: ...] inheritance prefix, starting with a heading line, or being a
        HEADING unit itself is a by-design section-level cut point.
        The TextTiling dip of these cut points is a "guaranteed good" boundary; counting them into the
        cross average measures the document's topic-switch frequency (a document property), not
        the strategy's cut-point quality — structural cut points dilute and mask
        the true signal of mid-section cuts. Only non-structural cuts carry strategy
        information and are this metric's measurement target.
    """
    text = (getattr(chunk, "text", "") or "").lstrip()
    if narrative_checks.CONTEXT_PREFIX_LINE.match(text.split("\n", 1)[0]):
        return True
    if getattr(chunk, "unit_type", None) == UnitType.HEADING:
        return True
    body = narrative_checks.strip_context_prefix(
        getattr(chunk, "text", "") or ""
    ).lstrip()
    if not body:
        return True
    return any(
        p.match(body) for p in narrative_checks.HEADING_PATTERNS
    )


def _density_factor(text: str) -> float:
    """Content density of a text window (avg non-empty line length).

    Prose lines should be >30 chars; fragmented lines <15.
    Scales from 1.0 (avg>=40) down to 0.3 (avg<10); 0.5 when
    there are no lines at all.
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return 0.5
    avg_line_len = sum(len(line) for line in lines) / len(lines)
    if avg_line_len < 10:
        return 0.3
    if avg_line_len < 20:
        return 0.3 + 0.7 * (avg_line_len - 10) / 10
    if avg_line_len < 40:
        return 0.6 + 0.4 * (avg_line_len - 20) / 20
    return 1.0


def _jaccard(t1, t2) -> float:
    return len(t1 & t2) / max(len(t1 | t2), 1)


def within_chunk_cohesion(chunks):
    """Vocabulary continuity: half-split overlap coefficient.

    v7 redesign (2026-10-04, 34-doc x 7-strategy calibration):
        v6 carried density_factor / token_penalty as WEIGHTS
        (averaged alongside, not judged) — a fragmented chunk
        kept scoring its full overlap coefficient while merely
        sitting beside real prose at reduced weight, so
        pathological docs averaged high. Both penalties now
        multiply INTO the score: a fragmented or token-starved
        chunk is judged low, not sidelined. Length reliability
        stays a weight: a short chunk is legitimately less
        certain, not less coherent.

    v6 core retained (2026-10-02, 8-sample LLM audit +
    discrimination experiment):

    Jaccard → OVERLAP COEFFICIENT (|A∩B| / min(|A|,|B|)).
        Jaccard saturates at the floor for diverse-vocabulary
        literary text (0.03-0.06 for GOOD chunkings of novels),
        leaving no usable dynamic range. The overlap coefficient
        asks the right question — "what fraction of the front
        half's established vocabulary carries into the back
        half" — and measured ~2x the range on the same corpora
        (0.08-0.21 for good chunkings) while keeping the
        mixed-chunk discrimination (good vs deliberately
        topic-mixed chunks: 14-29% relative drop, same as
        Jaccard; an adjacent-sentence chaining variant was
        tested and REJECTED — 0-4% drop, no signal).

    CV penalty retired: chunk-size spread is
    distribution_uniformity's responsibility (granularity
    invariant); multiplying it into cohesion double-counted
    granularity.
    """
    cohesion_scores = []
    weights = []

    for chunk in chunks:
        text = narrative_checks.strip_context_prefix(chunk.text)
        length = len(text)
        if length < 20:
            continue

        split = narrative_checks.find_split_point(text)
        tokens_first = narrative_checks.tokenize(text[:split])
        tokens_second = narrative_checks.tokenize(text[split:])

        if not tokens_first or not tokens_second:
            continue

        intersection = len(tokens_first & tokens_second)
        # v6 core: overlap coefficient — the rate at which first-half lexical items continue into the second half
        cohesion = intersection / min(
            len(tokens_first), len(tokens_second)
        )

        # reliability weight from chunk length
        reliability_weight = min(1.0, length / 80)

        # content density factor — fragmented text is judged low
        density_factor = _density_factor(text)

        # token set size penalty — tiny token set overlap is noise
        total_tokens = len(tokens_first | tokens_second)
        if total_tokens < 8:
            token_penalty = 0.3 + 0.7 * (total_tokens / 8)
        else:
            token_penalty = 1.0

        # v7: penalties judge the score, not the weight
        cohesion_scores.append(cohesion * density_factor * token_penalty)
        weights.append(reliability_weight)

    if not cohesion_scores:
        return 0.5

    # weighted average
    weighted_sum = sum(c * w for c, w in zip(cohesion_scores, weights))
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.5
    return round(weighted_sum / total_weight, 3)


def doc_ttr_discount(chunks) -> float:
    """v7b: document-level repetition discount on within.

    TTR (type-token ratio) >= 0.5 → no discount; scales linearly
    to 0 at TTR = 0. Repetition-driven vocabulary overlap ("AI
    AI AI") is a DOCUMENT property — such chunks are internally
    coherent by construction — not chunking quality. Never fires
    on natural corpora (2026-10-04 calibration: all 34 docs TTR
    ~ 1.0); pure anti-gaming guard.
    """
    tokens: list[str] = []
    for chunk in chunks:
        tokens.extend(narrative_checks.tokenize(
            narrative_checks.strip_context_prefix(chunk.text)
        ))
    if not tokens:
        return 1.0
    ttr = len(set(tokens)) / len(tokens)
    return min(1.0, ttr / 0.5)


def cross_boundary_scores(chunks):
    """Per-boundary RELATIVE dip scores, structural cuts excluded.

    v8 (2026-10-04, 34-doc x 7-strategy calibration):
        The dip is no longer absolute (1 - jaccard). Absolute
        dissimilarity is a guaranteed false positive on random
        text: any two lorem windows share nothing, so every
        boundary scored a "perfect transition" (dip = 1.0) and
        bad-band docs scored ABOVE good docs. A boundary now
        earns dip credit only RELATIVE to each flank's own
        internal continuity (TextTiling's depth-against-local-
        context semantics):

            baseline = mean(jaccard(head3, tail3) of left chunk,
                            jaccard(head3, tail3) of right chunk)
            dip = clip((baseline - boundary_sim)
                       / max(baseline, _BASELINE_FLOOR))

        A mid-topic cut has boundary_sim ~= baseline → dip ~ 0.
        A fragmented (incoherent) flank has baseline ~ 0 → no
        credible continuity to drop from → dip ~ 0.
        A true transition between coherent chunks → dip → 1.

    Evidence validity guards — pairs failing these are NOT
    counted as evidence (mirroring within's noise guards):
      - flank third line density >= 0.5: random fragments
        cannot evidence a "perfect topical transition"
      - >= 8 tokens on both boundary windows: token-starved
        windows are noise (cf. within's token_penalty)
      - non-empty tokenizable thirds on all four windows

    Retained v5/v6 contract:
        - heading-aligned (structural) boundary pairs are
          EXCLUDED (see _starts_at_structural_break): they are
          guaranteed-good cuts and would only dilute the dip
          signal with document properties.
        - Context prefixes are stripped on both sides before
          tokenization.
        - Exposed as a LIST (not an average): callers weight the
          composite by evidence volume (see semantic_cohesion).
    """
    if len(chunks) <= 1:
        return []

    boundary_scores = []

    for i in range(len(chunks) - 1):
        if _starts_at_structural_break(chunks[i + 1]):
            continue

        curr_text = narrative_checks.strip_context_prefix(chunks[i].text)
        next_text = narrative_checks.strip_context_prefix(
            chunks[i + 1].text
        )

        tail_len = max(len(curr_text) // 3, 20)
        head_len = max(len(next_text) // 3, 20)

        tail = curr_text[-tail_len:]
        head = next_text[:head_len]

        # evidence-validity guard: fragmented boundary windows do not count as evidence
        if _density_factor(tail) < 0.5 or _density_factor(head) < 0.5:
            continue

        tail_tokens = narrative_checks.tokenize(tail)
        head_tokens = narrative_checks.tokenize(head)

        if not tail_tokens or not head_tokens:
            continue

        # evidence-validity guard: lexically starved boundaries are noise
        if min(len(tail_tokens), len(head_tokens)) < 8:
            continue

        # local baseline: each side's own internal lexical continuity (head-three vs tail-three)
        curr_head = narrative_checks.tokenize(curr_text[:tail_len])
        curr_tail = narrative_checks.tokenize(curr_text[-tail_len:])
        next_head = narrative_checks.tokenize(next_text[:head_len])
        next_tail = narrative_checks.tokenize(next_text[-head_len:])
        if not (curr_head and curr_tail and next_head and next_tail):
            continue

        baseline = (
            _jaccard(curr_head, curr_tail)
            + _jaccard(next_head, next_tail)
        ) / 2.0

        boundary_sim = _jaccard(tail_tokens, head_tokens)
        dip = max(0.0, baseline - boundary_sim) / max(
            baseline, _BASELINE_FLOOR
        )

        boundary_scores.append(min(1.0, dip))

    return boundary_scores


def cross_chunk_cohesion(chunks):
    """Boundary relative-dip average (Hearst TextTiling inspired).

    Thin average view over ``cross_boundary_scores``; returns 1.0
    (perfect) when there are no adjacent pairs, no non-structural
    pairs, or no valid evidence at boundaries — the v5
    "nothing to judge is not a penalty" contract.

    NOTE (v6): the flat 1.0 made this a CONSTANT term for
    heading-aligned documents when composed as 0.6*within +
    0.4*cross. Use ``cross_boundary_scores`` directly to weight
    by evidence volume (see semantic_cohesion).
    """
    boundary_scores = cross_boundary_scores(chunks)
    if not boundary_scores:
        return 1.0

    return round(
        sum(boundary_scores) / len(boundary_scores), 3
    )
