"""PDF Document Ingestion Runtime.

The active runtime is ``probe → page evidence → reconstruction →
representation``.  The legacy ``recovery`` prototype was removed
(2026-09-14): it sat off the production path and its per-page
``heading_path`` reset / "simplified" heading level semantics diverged
from the reconstruction layer.  Use ``reconstruction/`` for structure
rebuilding.
"""

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFRoutingDecision,
)
from provendoc.core.parsers.narrative.pdf.pipeline import PDFPipeline, PDFPipelineResult

__all__ = [
    "PDFBlock",
    "PDFExtractionResult",
    "PDFRoutingDecision",
    "PDFPipeline",
    "PDFPipelineResult",
]
