"""Standalone export functions — optional persistence utilities.

Design philosophy:
    SDK interfaces (parse/chunk/evaluate/auto_pipeline) return
    IngestionResult objects only. They never create files.

    These functions exist for users who *choose* to persist
    results to disk. They are not called by any SDK interface.

    The exporters are shared with the runtime (single source — no
    second render/export implementation):
        - markdown -> core/exporters/markdown_exporter.py (pure presentation)
        - raw      -> core/exporters/raw_document_exporter.py (raw snapshot)
        - invoice  -> core/exporters/invoice_exporter.py (invoice
                      scenario field framework)
    Contract: JSON is the runtime truth, markdown is only a render
    artifact; when you need chunk metadata (unit id / source span /
    heading path) use export_json — no machine annotations are
    stuffed into markdown.

Usage:
    from provendoc.sdk.export import (
        export_markdown, export_json, export_raw_document, export_invoice,
    )

    result = oi.chunk("file.txt")

    # Only if you want to save:
    export_markdown(result, "output.md")
    export_json(result, "output.json")
    export_raw_document(result.document, "raw.json")

    # Invoice scenario field framework (Chinese keys, human-readable;
    # reads document.metadata only, never re-parses):
    export_invoice(result, "invoice.json")

Accepts:
    - IngestionResult (uses .representations["semantic"].content)
    - List[RepresentationUnit] (direct chunk list)
    - RepresentationResult (uses .content)
    - Document (for export_raw_document only)
"""

from typing import List

from provendoc.base.models.document import Document
from provendoc.base.models.ingestion_result import IngestionResult
from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import RepresentationUnit
from provendoc.core.exporters.atomic_write import atomic_write_json
from provendoc.core.exporters.invoice_exporter import InvoiceExporter
from provendoc.core.exporters.markdown_exporter import MarkdownExporter
from provendoc.core.exporters.raw_document_exporter import RawDocumentExporter
from provendoc.core.foundation.serialization import safe_serialize


def _extract_units(data) -> List[RepresentationUnit]:
    """Extract RepresentationUnit list from various input types."""
    if isinstance(data, IngestionResult):
        semantic = data.representations.get("semantic")
        if semantic is None:
            return []
        return _extract_units(semantic)
    if isinstance(data, RepresentationResult):
        return _extract_units(data.content)
    if isinstance(data, list):
        return data
    if hasattr(data, "base"):
        return data.base
    if hasattr(data, "all_units"):
        return data.all_units()
    return []


def export_markdown(data, path: str) -> None:
    """Export RepresentationUnit list or IngestionResult to a markdown file.

    Reuses the runtime MarkdownExporter: headings render at each
    unit's own heading level; no "# Chunk N" sequence headings, no
    META machine annotations. Markdown is presentation only —
    structured facts are authoritative via export_json.

    Args:
        data: IngestionResult, RepresentationResult, or List[RepresentationUnit]
        path: output file path
    """
    units = _extract_units(data)
    MarkdownExporter().export(units, path)


def export_json(data, path: str) -> None:
    """Export any serializable object to a JSON file.

    Uses safe_serialize to handle dataclasses, Enums, datetimes,
    circular references, etc.

    Args:
        data: any object (IngestionResult, RepresentationResult,
              List[RepresentationUnit], Document, dict, list, etc.)
        path: output file path
    """
    serialized = safe_serialize(data)
    # atomic write: temp + os.replace — an interrupted process never
    # leaves a truncated JSON file.
    atomic_write_json(path, serialized)


def export_raw_document(document: Document, path: str) -> None:
    """Export a Document to a raw JSON file.

    Reuses the runtime RawDocumentExporter (the exact implementation
    behind the runs/{run_id}/raw dumps): file info, encoding verdict
    fields, sha256, original_text (pre-cleaning snapshot),
    text_statistics, diagnostics, semantic_signals.

    Args:
        document: Document object
        path: output file path
    """
    RawDocumentExporter().export(document, path)


def export_invoice(data, path: str) -> None:
    """Export the invoice scenario field framework to a JSON file.

    Reuses core/exporters/invoice_exporter.py's InvoiceExporter:
    a Chinese-keyed human-readable framework (scenario / invoice /
    verification / unextracted fields / notes), one file per
    invoice.

    Pure presentation: reads only existing document.metadata keys
    (invoice_fields / scenario / ocr_unresolved) — no recomputation,
    no backfilling, no second-guessing; null fields only record
    their reason (honest boundary). When metadata carries no
    invoice_fields, an honest stub is written (scenario recorded,
    all fields null, note explains the absence) instead of raising.

    Args:
        data: IngestionResult, Document, or metadata dict
        path: output file path (atomic write)
    """
    InvoiceExporter().export(data, path)
