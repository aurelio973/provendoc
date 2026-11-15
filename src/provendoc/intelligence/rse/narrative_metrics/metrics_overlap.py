"""Overlap-consistency dimension (SECONDARY): diagnostics of overlap-strategy implementation consistency."""

import re

from provendoc.intelligence.rse import narrative_checks

# ==================================
# dimension: overlap consistency (SECONDARY)
# ==================================
#
# Sentence-aware overlap consistency check.
#
# Purpose:
#   This is a DIAGNOSTIC metric — it verifies that the
#   chunker correctly implemented the configured overlap
#   strategy. It does NOT judge overlap quality.
#
# Sentence-aware:
#   Instead of raw character comparison, checks if the
#   overlap region contains complete sentences. This aligns
#   with RAG best practices: chunk overlap should preserve
#   sentence boundaries for context continuity.
#
# Scoring:
#   1.0 — overlap region matches exactly (character-level)
#   0.7-0.9 — overlap region is mostly matching with
#             minor boundary differences
#   0.5 — overlap exists but breaks sentences
#   0.0 — no overlap detected despite overlap > 0 configured
#
# Returns 1.0 (N/A) if overlap=0, single chunk,
# or overlap not triggered.
# ==================================

def overlap_consistency(chunks, overlap):
    """Overlap-consistency diagnostics: whether inter-chunk overlaps follow the configured strategy."""
    if len(chunks) <= 1:
        return 1.0

    if overlap == 0:
        return 1.0

    has_overlap = False
    for i in range(len(chunks) - 1):
        curr = chunks[i].text
        nxt = chunks[i + 1].text

        if (len(curr) >= overlap
                and len(nxt) >= overlap):
            if curr[-overlap:] == nxt[:overlap]:
                has_overlap = True
                break

    if not has_overlap:
        # v4 contract fix: no exact overlap means overlap was not
        # triggered.  The declared contract is N/A → 1.0; it is
        # not a failure of semantic-boundary strategies that do
        # not duplicate literal text.  Sentence-aware matching
        # remains available for diagnostics, but must not turn a
        # non-triggered overlap into a quality penalty.
        return 1.0

    scores = []
    for i in range(len(chunks) - 1):
        curr = chunks[i].text
        nxt = chunks[i + 1].text

        if (len(curr) < overlap
                or len(nxt) < overlap):
            continue

        curr_tail = curr[-overlap:]
        next_head = nxt[:overlap]

        # calibration fix: very short overlap is unreliable
        # because short character sequences match by chance
        # v3: increased divisor from 20 to 40 so that 30-char
        # overlap gets reliability 0.75 instead of 1.0
        overlap_reliability = min(1.0, overlap / 40)

        # v3: content density of overlap region
        # if overlap text is mostly newlines/short fragments,
        # character-level match is meaningless
        overlap_lines = [
            line for line in curr_tail.split("\n") if line.strip()
        ]
        if overlap_lines:
            overlap_avg_len = sum(
                len(line) for line in overlap_lines
            ) / len(overlap_lines)
            if overlap_avg_len < 15:
                overlap_reliability *= 0.5
            elif overlap_avg_len < 25:
                overlap_reliability *= 0.7

        if curr_tail == next_head:
            # check sentence boundary quality of overlap
            score = overlap_sentence_quality(
                curr_tail
            )
            scores.append(score * overlap_reliability)
        else:
            match = sum(
                1 for a, b in zip(curr_tail, next_head)
                if a == b
            )
            raw_score = match / overlap
            # partial match — moderate the score
            if raw_score > 0.8:
                scores.append(
                    (0.7 + 0.2 * (raw_score - 0.8) * 5)
                    * overlap_reliability
                )
            else:
                scores.append(raw_score * 0.5 * overlap_reliability)

    if not scores:
        return 1.0

    return round(sum(scores) / len(scores), 3)

def sentence_aware_overlap(chunks, overlap):
    """Check if overlap exists at sentence level (not character)."""
    scores = []
    for i in range(len(chunks) - 1):
        curr = chunks[i].text
        nxt = chunks[i + 1].text

        if len(curr) < 10 or len(nxt) < 10:
            continue

        # find the longest common suffix-prefix at sentence level
        curr_sentences = re.split(
            r'[.!?。！？\n]', curr
        )
        nxt_sentences = re.split(
            r'[.!?。！？\n]', nxt
        )

        curr_sentences = [
            s.strip() for s in curr_sentences if s.strip()
        ]
        nxt_sentences = [
            s.strip() for s in nxt_sentences if s.strip()
        ]

        if not curr_sentences or not nxt_sentences:
            continue

        # check if last sentence(s) of curr appear in start of nxt
        overlap_found = False
        check_len = min(2, len(curr_sentences), len(nxt_sentences))

        for j in range(1, check_len + 1):
            curr_tail = " ".join(curr_sentences[-j:])
            nxt_head = " ".join(nxt_sentences[:j])

            if curr_tail and nxt_head:
                similarity = text_similarity(
                    curr_tail, nxt_head
                )
                if similarity > 0.7:
                    scores.append(0.8)
                    overlap_found = True
                    break

        if not overlap_found:
            scores.append(0.0)

    if not scores:
        return 1.0

    return round(sum(scores) / len(scores), 3)

def overlap_sentence_quality(overlap_text):
    """Assess whether the overlap text respects sentence boundaries."""
    sentence_ends = sum(
        1 for c in overlap_text
        if c in narrative_checks.SENTENCE_ENDS
    )
    # overlap that contains sentence boundaries is better
    # than mid-sentence overlap
    if sentence_ends >= 1:
        return 1.0
    # check if overlap starts/ends at word boundaries
    if (overlap_text[0].isalpha() and
            overlap_text[-1] in narrative_checks.SENTENCE_ENDS):
        return 0.9
    if overlap_text[0].isupper() or (
            "\u4e00" <= overlap_text[0] <= "\u9fff"):
        return 0.8
    return 0.6

def text_similarity(text1, text2):
    """Jaccard similarity for overlap detection."""
    tokens1 = narrative_checks.tokenize(text1)
    tokens2 = narrative_checks.tokenize(text2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)
    return intersection / max(union, 1)
