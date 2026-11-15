# ============================================================
# PPTX NARRATIVE REPRESENTATION
# ============================================================
"""PPTX-specific narrative representation — slide container awareness.

A slide is pptx's native semantic boundary: base sections
are hard-clipped to the parser's pptx_slides spans (one
section per page, speaker notes belong to their page),
coarse merging never crosses pages thanks to the
container_locality barrier, and every unit carries
slide_index/slide_title provenance. Mechanism see the
container_narrative_representation.py module docstring.
"""

from provendoc.core.representations.narrative.container_narrative_representation import (
    ContainerNarrativeRepresentation,
)


class PptxNarrativeRepresentation(ContainerNarrativeRepresentation):
    """Slide-container-aware pptx narrative representation."""

    container_kind = "slide"
    container_spans_key = "pptx_slides"
    span_index_field = "slide_index"
    container_index_meta_key = "slide_index"
    span_label_field = "title"
    container_label_meta_key = "slide_title"

    # Whole-page aggregation: a slide holds one topic per page
    # (the page title is often the only text), and the span
    # slice is the whole page text (title line, body, notes
    # all inside the span) — one atomic base unit per page,
    # no more shattering into text-frame paragraphs. Over
    # size_hard_limit, degrade to line-boundary splitting
    # under the same contract as paragraphs.
    compact_container = True
