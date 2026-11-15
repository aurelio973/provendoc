"""MetadataEnricher — chunks + tree -> enriched chunk metadata.

Post-chunking enrichment: adds structural context to each chunk WITHOUT
changing how chunks are created.  This is the "semantic persistence" step --
structural information from the DocumentTree flows into chunk metadata,
so downstream (retrieval, quality evaluation, visualization) doesn't have to guess.

Enriched metadata fields per chunk:
  - section_path: ["Chapter 1", "Section 1.2"] (scoped semantic address)
  - structural_role: "body" | "code" | "table" | "list" | "quote"
  - heading_context: "Section 1.2" (nearest enclosing heading)
  - sibling_index: 0, 1, 2... (position among chunks in same section)
  - section_chunk_count: total chunks in this section
  - boundary_type: "semantic" | "structural" | "forced"

Design:
  - Pure post-processing -- does NOT modify chunk text or boundaries
  - Falls back gracefully when position lookup fails (merged chunks etc.)
  - Preserves any existing metadata (e.g., heading_path from representation)
"""

from __future__ import annotations

from typing import Any, List, Tuple

from provendoc.core.structure.document_tree import (
    BoundaryType,
)
from provendoc.core.structure.structure_map import StructureMap


class ChunkMetadataEnricher:
    """Enriches chunks with structural metadata from a StructureMap.

    Usage:
        analyzer = StructureAnalyzer()
        tree = analyzer.analyze(document)
        smap = StructureMap(tree)
        enricher = ChunkMetadataEnricher(smap)
        enricher.enrich(chunks, document.text)
    """

    # Number of chars to search for position lookup
    _SEARCH_PREFIX_LEN = 40

    def __init__(self, structure_map: StructureMap):
        """Initialize the enricher with a structure map."""
        self._smap = structure_map

    def enrich(
        self,
        chunks: List[Any],
        document_text: str,
    ) -> List[Any]:
        """Enrich each chunk's metadata with structural context.

        Args:
            chunks: list of Chunk objects (.text, .metadata)
            document_text: the full document text for position lookup

        Returns:
            The same list of chunks (mutated in-place) with enriched metadata.
        """
        # Step 1: find each chunk's position in the document
        chunk_ranges = self._find_chunk_positions(chunks, document_text)

        # Step 1.5: precompute section paths ONCE per chunk.
        # get_chunk_section_info historically recomputed every chunk's
        # section path for every query (O(n^2) path lookups, each a
        # linear tree scan) — pathological once paragraph-level chunking
        # yields hundreds of units. Grouping here keeps semantics identical.
        range_dicts = [
            {"start": cr[0], "end": cr[1]} for cr in chunk_ranges
        ]
        section_paths = [
            self._smap.get_section_path_for_position(cr["start"])
            for cr in range_dicts
        ]
        groups: dict[tuple, List[dict]] = {}
        for cr, sp in zip(range_dicts, section_paths):
            groups.setdefault(tuple(sp), []).append(cr)
        for crs in groups.values():
            crs.sort(key=lambda c: c["start"])

        # Step 2: enrich each chunk
        for i, chunk in enumerate(chunks):
            start, end = chunk_ranges[i]

            # Structural context (precomputed; same semantics as
            # get_chunk_section_info)
            section_path = section_paths[i]
            same_section = groups[tuple(section_path)]
            sibling_index = 0
            for k, cr in enumerate(same_section):
                if cr["start"] == start:
                    sibling_index = k
                    break
            section_info = {
                "section_path": section_path,
                "sibling_index": sibling_index,
                "section_chunk_count": len(same_section),
                "heading_context": (
                    section_path[-1] if section_path else None
                ),
            }

            dominant_role = self._smap.get_dominant_role(start, end)

            boundary_type = self._determine_boundary_type(
                start, end, i, len(chunks),
            )

            # Enrich metadata (preserve existing fields)
            # RepresentationUnit carries metadata dict for third-party extensions
            if hasattr(chunk, "metadata") and isinstance(chunk.metadata, dict):
                md = chunk.metadata
            else:
                md = {}
                chunk.metadata = md
            md.setdefault("section_path", section_info["section_path"])
            md.setdefault("structural_role", dominant_role)
            md.setdefault("heading_context", section_info["heading_context"])
            md.setdefault("sibling_index", section_info["sibling_index"])
            md.setdefault(
                "section_chunk_count", section_info["section_chunk_count"]
            )
            md.setdefault("boundary_type", boundary_type)

        return chunks

    # ------------------------------------------------------------------
    # Position finding
    # ------------------------------------------------------------------

    def _find_chunk_positions(
        self,
        chunks: List[Any],
        document_text: str,
    ) -> List[Tuple[int, int]]:
        """Find each chunk's [start, end] in the document text.

        Searches for the first _SEARCH_PREFIX_LEN chars of each chunk.
        Falls back to sequential offset estimation if search fails.
        """
        ranges: List[Tuple[int, int]] = []
        search_from = 0

        for chunk in chunks:
            text = chunk.text or ""
            # Use first non-whitespace chars for search
            prefix = self._extract_search_prefix(text)

            start = -1
            if prefix:
                start = document_text.find(prefix, search_from)

            if start < 0:
                # Fallback: try searching from beginning
                if prefix:
                    start = document_text.find(prefix)

            if start < 0:
                # Last resort: estimate from previous position
                start = search_from

            # End is start + chunk length (approximate)
            end = start + len(text)
            if end > len(document_text):
                end = len(document_text)

            ranges.append((start, end))
            # Next search starts after this chunk
            search_from = end

        return ranges

    def _extract_search_prefix(self, text: str) -> str:
        """Extract the first meaningful characters for position search."""
        # Strip leading whitespace
        stripped = text.lstrip()
        if not stripped:
            return ""
        # Take first N chars
        prefix = stripped[:self._SEARCH_PREFIX_LEN]
        # Trim to last word boundary to avoid partial matches
        if len(stripped) > self._SEARCH_PREFIX_LEN:
            last_space = prefix.rfind(" ")
            if last_space > 10:
                prefix = prefix[:last_space]
        return prefix

    # ------------------------------------------------------------------
    # Boundary type determination
    # ------------------------------------------------------------------

    def _determine_boundary_type(
        self,
        start: int,
        end: int,
        chunk_index: int,
        total_chunks: int,
    ) -> str:
        """Determine how this chunk's boundaries were determined.

        - SEMANTIC: boundary aligns with a structural node boundary
        - STRUCTURAL: boundary at section/heading boundary
        - FORCED: boundary mid-block, forced by token limit

        Document edges are natural boundaries, never forced cuts: the
        first chunk's start coincides with the document start and the
        last chunk's end with the document end, so those sides count
        as aligned regardless of tree layout.
        """
        tree = self._smap.tree

        # Check if chunk start aligns with a node boundary
        start_nodes = tree.find_nodes_in_range(start, start + 1)
        start_aligned = any(
            n.text_start == start for n in start_nodes
        )

        # Check if chunk end aligns with a node boundary
        end_nodes = tree.find_nodes_in_range(max(end - 1, start), end)
        end_aligned = any(
            n.text_end == end for n in end_nodes
        )

        # Document-edge sides are natural boundaries (see docstring)
        if chunk_index == 0:
            start_aligned = True
        if chunk_index == total_chunks - 1:
            end_aligned = True

        # Check if boundaries align with heading positions
        start_heading = tree.find_nearest_heading(start)
        start_at_heading = (
            start_heading and abs(start_heading.text_start - start) <= 1
        )
        end_heading = tree.find_nearest_heading(end)
        end_at_heading = (
            end_heading and abs(end_heading.text_end - end) <= 1
        )

        if start_aligned and end_aligned:
            return BoundaryType.SEMANTIC.value
        if start_at_heading or end_at_heading:
            return BoundaryType.STRUCTURAL.value
        return BoundaryType.FORCED.value
