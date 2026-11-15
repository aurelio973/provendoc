"""StructureMap — query layer over DocumentTree.

Provides high-level queries for the metadata enrichment step:
  - section_path: heading ancestry as list of titles
  - heading_context: nearest enclosing heading
  - sibling lookup
  - position-based node lookup
  - structural_role for a text position

This is the "read interface" to the structural IR.
The DocumentTree itself is the "write interface" (built by StructureAnalyzer).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.structure.document_tree import (
    DocumentNode,
    DocumentTree,
    NodeType,
)


class StructureMap:
    """Read-only query layer over a DocumentTree."""

    def __init__(self, tree: DocumentTree):
        """Initialize the query layer with a structure tree."""
        self._tree = tree

    @property
    def tree(self) -> DocumentTree:
        """Underlying structure tree."""
        return self._tree

    # ------------------------------------------------------------------
    # Section path queries
    # ------------------------------------------------------------------

    def get_section_path(self, node_id: str) -> List[str]:
        """Return heading titles from root to the node's section.

        E.g., ["Chapter 1", "Section 1.2", "Subsection 1.2.3"]
        """
        ancestors = self._tree.get_heading_ancestors(node_id)
        return [a.title for a in ancestors if a.title]

    def get_section_path_for_position(self, offset: int) -> List[str]:
        """Get section path for a character position in the document."""
        node = self._tree.find_node_at_position(offset)
        if node:
            return self.get_section_path(node.node_id)
        heading = self._tree.find_nearest_heading(offset)
        if heading:
            return self.get_section_path(heading.node_id) + [heading.title]
        return []

    def get_heading_context(self, offset: int) -> Optional[str]:
        """Return the title of the nearest enclosing heading."""
        path = self.get_section_path_for_position(offset)
        return path[-1] if path else None

    # ------------------------------------------------------------------
    # Structural role queries
    # ------------------------------------------------------------------

    def get_structural_role(self, offset: int) -> str:
        """Determine the structural role of content at this position."""
        node = self._tree.find_node_at_position(offset)
        if node:
            return node.node_type.value
        return NodeType.BODY.value

    def get_nodes_in_range(
        self, start: int, end: int
    ) -> List[DocumentNode]:
        """Return content nodes overlapping [start, end)."""
        return self._tree.find_nodes_in_range(start, end)

    def get_siblings(self, node_id: str) -> List[DocumentNode]:
        """Same-parent siblings (including self)."""
        return self._tree.get_siblings(node_id)

    # ------------------------------------------------------------------
    # Chunk-level queries
    # ------------------------------------------------------------------

    def get_chunk_section_info(
        self,
        chunk_start: int,
        chunk_end: int,
        all_chunk_ranges: List[Dict[str, int]],
    ) -> Dict[str, Any]:
        """Compute sibling_index and section_chunk_count for a chunk.

        Args:
            chunk_start: char offset of this chunk's start
            chunk_end: char offset of this chunk's end
            all_chunk_ranges: list of {"start": int, "end": int} for all chunks

        Returns:
            {"sibling_index": int, "section_chunk_count": int,
             "section_path": list, "heading_context": str|None}
        """
        section_path = self.get_section_path_for_position(chunk_start)

        same_section: List[Dict[str, int]] = []
        for cr in all_chunk_ranges:
            cr_path = self.get_section_path_for_position(cr["start"])
            if cr_path == section_path:
                same_section.append(cr)

        same_section.sort(key=lambda c: c["start"])

        sibling_index = 0
        for i, cr in enumerate(same_section):
            if cr["start"] == chunk_start:
                sibling_index = i
                break

        heading_context = section_path[-1] if section_path else None

        return {
            "sibling_index": sibling_index,
            "section_chunk_count": len(same_section),
            "section_path": section_path,
            "heading_context": heading_context,
        }

    def get_dominant_role(
        self, chunk_start: int, chunk_end: int
    ) -> str:
        """Determine the dominant structural role in a chunk's range."""
        nodes = self.get_nodes_in_range(chunk_start, chunk_end)
        if not nodes:
            return NodeType.BODY.value

        role_coverage: Dict[str, int] = {}
        for n in nodes:
            overlap_start = max(n.text_start, chunk_start)
            overlap_end = min(n.text_end, chunk_end)
            overlap = max(0, overlap_end - overlap_start)
            role_coverage[n.node_type.value] = (
                role_coverage.get(n.node_type.value, 0) + overlap
            )

        if not role_coverage:
            return NodeType.BODY.value
        return max(role_coverage, key=lambda node_type: role_coverage[node_type])
