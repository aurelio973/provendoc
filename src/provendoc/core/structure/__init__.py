"""Provendoc Structure — lightweight structural IR.

DocumentTree as persistent semantic object, not "signal used during
chunking then discarded."

Pipeline:
    raw document
        -> parser (extract observable signals)
        -> StructureAnalyzer (organize into DocumentTree)
        -> Representation packaging (chunks)
        -> ChunkMetadataEnricher (enrich with structural context)
        -> runtime artifacts

The tree persists through the pipeline.  Downstream (retrieval, quality
evaluation, visualization) can query structural context instead of guessing.

Public API:
    NodeType           -- primitive structural roles (heading/body/code/...)
    BoundaryType       -- semantic / structural / forced
    DocumentNode       -- single node in the tree
    DocumentTree       -- queryable tree of nodes
    StructureMap       -- read-only query layer (section_path, siblings, ...)
    StructureAnalyzer  -- document -> DocumentTree
    ChunkMetadataEnricher -- chunks + tree -> enriched chunk metadata
"""

from provendoc.core.structure.document_tree import (
    BoundaryType,
    DocumentNode,
    DocumentTree,
    NodeType,
)
from provendoc.core.structure.metadata_enricher import ChunkMetadataEnricher
from provendoc.core.structure.structure_analyzer import StructureAnalyzer
from provendoc.core.structure.structure_map import StructureMap

__all__ = [
    "NodeType",
    "BoundaryType",
    "DocumentNode",
    "DocumentTree",
    "StructureMap",
    "StructureAnalyzer",
    "ChunkMetadataEnricher",
]
