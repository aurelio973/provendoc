"""Distribution-evenness dimension (SECONDARY): normalized Shannon entropy of the chunk-length distribution."""

import math

# ==================================
# dimension: distribution uniformity (SECONDARY)
# ==================================
#
# Uses normalized Shannon entropy instead of CV.
#
# Academic basis:
#   Shannon (1948) — entropy measures distribution uncertainty.
#   Manning & Schütze (1999) — entropy in NLP applications.
#
# Normalized entropy = H / log(n), where H is Shannon entropy
# of the chunk size distribution and n is the number of chunks.
#
# Advantages over CV:
#   1. Bounded [0, 1] without ad-hoc mapping functions.
#   2. More principled for small samples (CV is unstable
#      with n<5; entropy is well-defined for any n>=2).
#   3. No arbitrary coefficient (the exp(-2*cv) factor 2
#      was an empirical choice with no theoretical basis).
#
# normalized_entropy = 1.0 means perfectly uniform.
# normalized_entropy = 0.0 means all but one chunk has
#   zero size (maximally concentrated).
# ==================================

def distribution_uniformity(chunks):
    """Chunk-length distribution evenness: normalized Shannon entropy."""
    if len(chunks) <= 1:
        return 1.0

    lengths = [len(c.text) for c in chunks]
    total = sum(lengths)

    if total == 0:
        return 0.0

    # compute Shannon entropy of the size distribution
    n = len(lengths)
    probs = [length / total for length in lengths if length > 0]

    if not probs:
        return 0.0

    entropy = -sum(p * math.log2(p) for p in probs)

    # normalize by maximum possible entropy (log2(n))
    max_entropy = math.log2(n)
    if max_entropy == 0:
        return 1.0

    normalized = entropy / max_entropy

    # apply mild penalty for very low entropy (concentrated)
    # using a soft threshold: below 0.3 is concerning
    if normalized < 0.3:
        normalized *= 0.8

    return round(normalized, 3)
