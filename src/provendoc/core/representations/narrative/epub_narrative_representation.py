# ============================================================
# EPUB NARRATIVE REPRESENTATION
# ============================================================
"""EPUB-specific narrative representation — spine chapter container
awareness.

spine chapters are the epub's native semantic boundary: base sections
are hard-cut along the parser's epub_chapter_spans, untitled chapters
own their content (it no longer flows into the previous chapter's
last section), coarse merging never crosses a chapter thanks to the
container_locality barrier, and every unit carries chapter_index/
chapter_href for tracing. See the container_narrative_representation
module docstring for the mechanism.

Section aggregation: epub is a long-document container (a whole
novel can reach hundreds of thousands of characters); laying
paragraph atoms out flat would produce thousands of fragments — the
aggregation atom is the in-chapter heading section (including the
preamble), and the hard size cap degrades to splitting on line
boundaries (same contract as paragraphs).
"""

from provendoc.core.representations.narrative.container_narrative_representation import (
    ContainerNarrativeRepresentation,
)


class EpubNarrativeRepresentation(ContainerNarrativeRepresentation):
    """Spine-chapter-aware epub narrative representation."""

    container_kind = "chapter"
    container_spans_key = "epub_chapter_spans"
    span_index_field = "chapter_index"
    container_index_meta_key = "chapter_index"
    span_label_field = "href"
    container_label_meta_key = "chapter_href"

    # Section aggregation: one atomic base unit per in-chapter
    # heading section (including the preamble) instead of splitting
    # on blank-line paragraphs — epub chapters can reach hundreds of
    # thousands of characters; the aggregation atom is the section,
    # not the whole chapter (epub counterpart of the pptx whole-page
    # mode). The hard size cap degrades to line-boundary splitting,
    # same contract as paragraphs.
    compact_sections = True
