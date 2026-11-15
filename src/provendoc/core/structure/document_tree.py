"""DocumentTree — lightweight structural IR.

Not a full AST. Not a semantic graph.
Just enough structure to persist through the pipeline:
  - hierarchy (heading levels)
  - block types (body, code, table, list, quote)
  - position ranges (char offsets in document text)

Design principles:
  - Parser extracts observable signals
  - StructureAnalyzer organizes into this tree
  - MetadataEnricher queries this tree to enrich chunks
  - The tree persists as runtime artifact
  - Role taxonomy stays primitive: heading, body, code, table, list, quote
  - NO latent semantics, discourse reasoning, or semantic role parsing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class NodeType(str, Enum):
    """Primitive structural roles — kept intentionally simple."""
    HEADING = "heading"
    BODY = "body"
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    QUOTE = "quote"

    @property
    def is_heading(self) -> bool:
        """Whether this is a heading node."""
        return self == NodeType.HEADING

    @property
    def is_content(self) -> bool:
        """Non-heading nodes carry actual text content."""
        return self != NodeType.HEADING


class BoundaryType(str, Enum):
    """How a chunk boundary was determined — recording semantic compromise."""
    SEMANTIC = "semantic"        # boundary aligns with structural node boundary
    STRUCTURAL = "structural"    # boundary at section boundary, forced by token limit
    FORCED = "forced"            # boundary mid-block, forced by token limit


@dataclass
class DocumentNode:
    """A single node in the document structure tree."""
    node_id: str
    node_type: NodeType
    level: int = 0              # heading level (1-6), 0 for non-headings
    title: str = ""             # heading text or block preview (first ~60 chars)
    text_start: int = 0         # char offset in document text
    text_end: int = 0           # char offset (exclusive)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        """Length of text covered by the node (character count)."""
        return self.text_end - self.text_start

    @property
    def is_heading(self) -> bool:
        """Whether this is a heading node."""
        return self.node_type == NodeType.HEADING

    def __repr__(self) -> str:
        return (
            f"DocumentNode({self.node_id}, {self.node_type.value}, "
            f"level={self.level}, title={self.title!r}, "
            f"range=[{self.text_start}:{self.text_end}])"
        )


class DocumentTree:
    """Lightweight structural IR — a queryable tree of DocumentNodes.

    Built by StructureAnalyzer, queried by StructureMap and MetadataEnricher.
    """

    def __init__(self):
        """Initialize an empty tree (populated by StructureAnalyzer)."""
        self._root: Optional[DocumentNode] = None
        self._nodes: Dict[str, DocumentNode] = {}

    # ------------------------------------------------------------------
    # Construction (used by StructureAnalyzer)
    # ------------------------------------------------------------------

    def set_root(self, node: DocumentNode) -> None:
        """Set the root node and register it in the node table."""
        self._root = node
        self._nodes[node.node_id] = node

    def add_node(self, node: DocumentNode) -> None:
        """Add a node and link it to its parent."""
        self._nodes[node.node_id] = node
        if node.parent_id and node.parent_id in self._nodes:
            parent = self._nodes[node.parent_id]
            if node.node_id not in parent.children_ids:
                parent.children_ids.append(node.node_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def root(self) -> Optional[DocumentNode]:
        """Root node (None when not set)."""
        return self._root

    @property
    def nodes(self) -> Dict[str, DocumentNode]:
        """Full node_id → DocumentNode table."""
        return self._nodes

    def get_node(self, node_id: str) -> Optional[DocumentNode]:
        """Get a node by node_id; returns None when absent."""
        return self._nodes.get(node_id)

    def get_children(self, node_id: str) -> List[DocumentNode]:
        """Get direct children (in mount order)."""
        node = self.get_node(node_id)
        if not node:
            return []
        return [
            self._nodes[cid] for cid in node.children_ids
            if cid in self._nodes
        ]

    def get_ancestors(self, node_id: str) -> List[DocumentNode]:
        """Return ancestors from root to immediate parent (not including self)."""
        result: List[DocumentNode] = []
        node = self.get_node(node_id)
        seen: set = set()
        while node and node.parent_id:
            if node.parent_id in seen:
                break  # cycle guard
            seen.add(node.parent_id)
            parent = self.get_node(node.parent_id)
            if parent:
                result.append(parent)
                node = parent
            else:
                break
        return list(reversed(result))

    def get_heading_ancestors(self, node_id: str) -> List[DocumentNode]:
        """Return only heading ancestors (root to nearest heading)."""
        return [n for n in self.get_ancestors(node_id) if n.is_heading]

    def get_siblings(self, node_id: str) -> List[DocumentNode]:
        """Same-parent siblings (including self); empty when no parent."""
        node = self.get_node(node_id)
        if not node or not node.parent_id:
            return []
        return self.get_children(node.parent_id)

    def find_node_at_position(self, offset: int) -> Optional[DocumentNode]:
        """Find the deepest content node containing this character offset."""
        best: Optional[DocumentNode] = None
        best_size = float("inf")
        for node in self._nodes.values():
            if not node.node_type.is_content:
                continue
            if node.text_start <= offset < node.text_end:
                size = node.text_length
                if size < best_size:
                    best = node
                    best_size = size
        return best

    def find_nodes_in_range(
        self, start: int, end: int
    ) -> List[DocumentNode]:
        """Find all content nodes whose range overlaps [start, end)."""
        result = [
            n for n in self._nodes.values()
            if n.node_type.is_content
            and n.text_start < end
            and n.text_end > start
        ]
        return sorted(result, key=lambda n: n.text_start)

    def find_nearest_heading(self, offset: int) -> Optional[DocumentNode]:
        """Find the nearest heading at or before this position."""
        best: Optional[DocumentNode] = None
        for n in self._nodes.values():
            if not n.is_heading:
                continue
            if n.text_start <= offset:
                if (
                    best is None
                    or n.text_start > best.text_start
                    or (
                        n.text_start == best.text_start
                        and n.level > best.level
                    )
                ):
                    best = n
        return best

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def total_nodes(self) -> int:
        """Total node count."""
        return len(self._nodes)

    @property
    def heading_count(self) -> int:
        """Number of heading nodes."""
        return sum(1 for n in self._nodes.values() if n.is_heading)

    @property
    def content_node_count(self) -> int:
        """Number of content (non-heading) nodes."""
        return sum(1 for n in self._nodes.values() if n.node_type.is_content)

    def all_headings(self) -> List[DocumentNode]:
        """All heading nodes (unordered)."""
        return [n for n in self._nodes.values() if n.is_heading]

    def to_summary(self) -> dict:
        """Serializable summary for runtime artifacts."""
        return {
            "total_nodes": self.total_nodes,
            "heading_count": self.heading_count,
            "content_node_count": self.content_node_count,
            "headings": [
                {
                    "node_id": h.node_id,
                    "level": h.level,
                    "title": h.title,
                    "start": h.text_start,
                    "end": h.text_end,
                }
                for h in self.all_headings()
            ],
        }
