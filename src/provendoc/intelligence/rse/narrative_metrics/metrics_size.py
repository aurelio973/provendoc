"""Size-compliance dimension (PRIMARY): intrinsic granularity + operational target alignment."""

import math

# ==================================
# dimension: size compliance (PRIMARY)
# ==================================
#
# Composite metric with two layers:
#
# 1. Intrinsic granularity quality (document-adaptive)
#    - "Do chunks form reasonable semantic units?"
#    - Compares chunk size to the document's own
#      paragraph/section structure.
#    - A 200-char paragraph document should produce
#      ~200-char chunks. A 500-char paragraph document
#      should produce ~500-char chunks.
#    - This is NOT target-based — it is intrinsic to
#      the document's natural granularity.
#
# 2. Operational target alignment
#    - "Do chunks fit the configured target size?"
#    - Gaussian distance from target_chunk_size.
#    - This IS target-based — it reflects the
#      operational preference of the RAG system.
#
# Combining:
#   intrinsic carries 0.6 (canonical quality)
#   operational carries 0.4 (operational preference)
#
# Short-document mode:
#   When total document < target * 0.5, the document
#   is intrinsically too small for meaningful chunking.
#   In this mode, size_compliance measures coverage
#   (did the chunker capture all content?) instead.
# ==================================

def size_compliance(
    chunks, target_size, orphan_threshold,
    document, total_text_length, short_doc_mode,
):
    """Size-compliance composite metric: document-adaptive granularity x0.6 + target alignment x0.4."""
    if not chunks:
        return 0.0

    # short-document mode: coverage-based
    if short_doc_mode:
        return size_compliance_short(
            chunks, total_text_length
        )

    # normal mode: composite intrinsic + operational
    intrinsic = intrinsic_granularity(
        chunks, document
    )
    operational = operational_alignment(
        chunks, target_size, orphan_threshold
    )

    return round(
        0.6 * intrinsic + 0.4 * operational, 3
    )

def intrinsic_granularity(chunks, document):
    """Document-adaptive granularity quality.

    Measures whether chunk sizes align with the document's
    natural semantic unit sizes (paragraphs/sections).

    Academic basis:
        Manning & Schütze (1999) — text segmentation theory:
        optimal segments should reflect the document's natural
        structure, not an arbitrary external target.

    Algorithm:
        1. Extract paragraph lengths from the document text.
        2. Compute the document's median paragraph length
           as the "natural unit size".
        3. For each chunk, measure how close its length is
           to a multiple of the natural unit size.
        4. This allows chunks that contain 1, 2, or 3
          paragraphs to all score well, as long as the
          chunk size is proportional to the document's
          natural granularity.

    v3 calibration fix:
        - Extract paragraphs directly from document.text using
          \\n\\n splitting (the Document object may not have a
          paragraphs attribute). Previously, the fallback used
          chunk lengths as proxy, which creates a circular
          measurement (chunks scored against their own sizes).
        - If median paragraph length is very short (<30 chars),
          the document lacks meaningful semantic units.
          Apply a penalty to prevent pathological docs from
          gaming this metric via tiny fragments.
        - If chunk length is very short (<30 chars), apply
          additional penalty — tiny chunks are not meaningful
          semantic units regardless of alignment.
    """
    # extract paragraph lengths from document text
    # v3: try document.paragraphs first, then fall back to
    # splitting document.text by double newlines
    paragraphs = getattr(document, "paragraphs", None)
    if paragraphs:
        para_lengths = [len(p) for p in paragraphs if p.strip()]
    else:
        # v3: extract from document.text
        doc_text = getattr(document, "text", "")
        if doc_text:
            raw_paras = doc_text.split("\n\n")
            para_lengths = [
                len(p.strip()) for p in raw_paras
                if p.strip()
            ]
        else:
            # final fallback: use chunks as proxy
            lengths = [len(c.text) for c in chunks]
            if not lengths:
                return 0.5
            para_lengths = lengths

    if not para_lengths:
        return 0.5

    median_len = sorted(para_lengths)[len(para_lengths) // 2]

    if median_len == 0:
        return 0.5

    # calibration fix: very short median indicates pathological
    # document structure (e.g., random line breaks)
    # v3: lowered threshold from 30 to 50 and added proportion
    # penalty for docs where many paragraphs are short
    median_penalty = 1.0
    if median_len < 50:
        median_penalty = 0.3 + 0.7 * (median_len / 50)

    # v3: if more than half the paragraphs are <50 chars,
    # the document is fragmented — apply additional penalty
    short_para_count = sum(1 for pl in para_lengths if pl < 50)
    if short_para_count > len(para_lengths) * 0.5:
        fragmentation_ratio = short_para_count / len(para_lengths)
        median_penalty *= max(0.2, 1.0 - 0.6 * fragmentation_ratio)

    scores = []
    for chunk in chunks:
        length = len(chunk.text)
        if length == 0:
            scores.append(0.0)
            continue

        # calibration fix: tiny chunks are not semantic units
        chunk_penalty = 1.0
        if length < 30:
            chunk_penalty = 0.5 + 0.5 * (length / 30)

        # how many "natural units" does this chunk contain?
        units = length / median_len
        # closest integer number of units
        nearest_units = max(round(units), 1)
        # ratio: how close to a clean multiple?
        ratio = abs(units - nearest_units) / max(nearest_units, 1)
        # gaussian: perfect alignment at integer multiples
        score = math.exp(-2.0 * ratio * ratio)
        score *= chunk_penalty
        scores.append(score)

    raw_avg = sum(scores) / len(scores)
    return round(raw_avg * median_penalty, 3)

def operational_alignment(
    chunks, target_size, orphan_threshold
):
    """Target-based size alignment (Gaussian model).

    Measures how close chunk sizes are to the configured
    target_chunk_size. This is an OPERATIONAL preference,
    not a canonical quality metric.

    Academic basis:
        LangChain/LlamaIndex chunking docs recommend 256-512
        tokens as the embedding sweet spot. The Gaussian
        model provides smooth degradation from target.

    Calibration fix:
        - Orphan penalty increased from quadratic to cubic
          for very small chunks (<50 chars). Tiny fragments
          are not meaningful semantic units even if they
          happen to align with target size.
    """
    sigma = max(target_size * 0.4, 30)
    scores = []

    for chunk in chunks:
        length = len(chunk.text)

        if length == 0:
            scores.append(0.0)
            continue

        ratio = (length - target_size) / sigma
        gaussian = math.exp(-0.5 * ratio * ratio)

        if length < orphan_threshold:
            orphan_ratio = length / max(orphan_threshold, 1)
            # calibration fix: cubic penalty for very small
            if length < 50:
                orphan_penalty = orphan_ratio ** 3
            else:
                orphan_penalty = orphan_ratio ** 2
            gaussian *= orphan_penalty

        scores.append(gaussian)

    return round(sum(scores) / len(scores), 3)

def size_compliance_short(chunks, total_text_length):
    """Size compliance for short documents.

    When the entire document is shorter than the
    target chunk size, the chunk cannot possibly
    reach target — this is a document scale
    constraint, not a chunker failure.

    In this mode, we measure "did the chunker
    capture all available content?" — if the
    single (or few) chunks collectively cover
    the full document, that's the best achievable.
    """
    if not chunks:
        return 0.0

    captured = sum(len(c.text) for c in chunks)
    coverage = captured / max(total_text_length, 1)

    empty_ratio = sum(
        1 for c in chunks if not c.text.strip()
    ) / len(chunks)

    score = coverage * (1.0 - 0.3 * empty_ratio)
    return round(max(score, 0.0), 3)
