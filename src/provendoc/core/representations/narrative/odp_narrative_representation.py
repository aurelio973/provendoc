# ============================================================
# ODP NARRATIVE REPRESENTATION
# ============================================================
"""ODP-specific narrative representation — slide container awareness.

A slide is odp's (OpenDocument Presentation) native semantic
boundary: base sections are hard-clipped to the parser's
odp_slides spans (one section per page, speaker notes
belong to their page), coarse merging never crosses pages
thanks to the container_locality barrier, and every unit
carries slide_index/slide_title provenance. Mechanism see
the container_narrative_representation.py module docstring
(epub/pptx use the same container-aware machinery; thin
subclasses).
"""

from provendoc.core.representations.narrative.container_narrative_representation import (
    ContainerNarrativeRepresentation,
)


class OdpNarrativeRepresentation(ContainerNarrativeRepresentation):
    """Slide-container-aware odp narrative representation."""

    container_kind = "slide"
    container_spans_key = "odp_slides"
    span_index_field = "slide_index"
    container_index_meta_key = "slide_index"
    span_label_field = "title"
    container_label_meta_key = "slide_title"
