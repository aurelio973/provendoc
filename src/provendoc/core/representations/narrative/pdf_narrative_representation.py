"""Compatibility entry point for PDF narrative representation.

The implementation lives with the PDF ingestion runtime so that PDF-specific
page, coordinate and evidence provenance never leaks into text-format
representations.  This import path remains stable for UniversalParser and
existing callers.
"""

from provendoc.core.parsers.narrative.pdf.representation.pdf_representation import (
    PDFRepresentationRuntime,
)


class PdfNarrativeRepresentation(PDFRepresentationRuntime):
    """Backward-compatible public name for the PDF representation runtime."""
