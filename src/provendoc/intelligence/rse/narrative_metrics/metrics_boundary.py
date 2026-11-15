"""Boundary-quality dimension (SECONDARY): how well cuts align with structural breakpoints.

Low-level decision primitives are all delegated to narrative_checks (same source as the invariant track).
"""

from provendoc.intelligence.rse import narrative_checks


# ==================================
# dimension: boundary quality (SECONDARY)
# ==================================
#
# Structural boundary quality.
#
# Academic basis:
#   Hearst (1997) TextTiling — boundaries should align
#   with transitions. This dimension checks STRUCTURAL
#   alignment only (sentence, paragraph, heading, fence).
#
# This is deliberately a STRUCTURAL metric. It does NOT
# use lexical analysis — that is semantic_cohesion's job.
#
# Responsibility boundary:
#   boundary_quality  → "did the cut happen at a structural
#                         break point?" (surface structure)
#   semantic_cohesion → "did the cut happen at a lexical
#                         transition?" (lexical continuity)
#
# Checks:
#   - sentence completeness (ends with .!?。！？)
#   - paragraph boundary (ends with \n\n)
#   - heading boundary (next chunk starts with heading)
#   - code fence boundary (ends with fence mark)
#   - list item boundary (next chunk starts with list item)
# ==================================
def boundary_quality(chunks):
    """Structural boundary quality: whether chunk cuts land on structural breakpoints."""
    if len(chunks) == 0:
        return 0.0
    if len(chunks) == 1:
        return narrative_checks.check_ending(chunks[0])

    boundary_scores = []

    for i in range(len(chunks) - 1):
        ending = narrative_checks.check_ending(chunks[i])
        starting = narrative_checks.check_starting(chunks[i + 1])
        boundary_scores.append((ending + starting) / 2)

    boundary_scores.append(
        narrative_checks.check_ending(chunks[-1])
    )

    return round(
        sum(boundary_scores) / len(boundary_scores), 3
    )
