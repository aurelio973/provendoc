"""Stable contracts for the PDF ingestion runtime.

The evidence contract is intentionally small.  Extraction runtimes may differ
(native PDF, OCR, or a future visual backend), but every runtime must preserve
where a piece of text came from before reconstruction derives reading order,
paragraphs, headings, or higher-level structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

BBox = tuple[float, float, float, float]


LIMITS_VERSION = "1.2"
"""Version of the PDF runtime resource-limit contract.

Bump this whenever limit semantics change so cached fingerprints and run
summaries can be compared across versions honestly.
"""


@dataclass(frozen=True)
class PDFRuntimeLimits:
    """Resource ceilings applied at the PDF pipeline entrance.

    Limits are guards, not filters: exceeding them never drops a document.
    The pipeline keeps running the cheap native path, records the violated
    limit as a fact in ``runtime["guard"]``, and skips the expensive OCR and
    layout enrichment paths so one pathological file cannot monopolize
    workers. All values default to ``None`` meaning "no ceiling configured".
    """

    max_pages: Optional[int] = None
    max_file_bytes: Optional[int] = None
    # OCR render budget: cap on total pixels per rendered page, used to
    # derive an effective DPI for oversized pages instead of always 300.
    max_page_pixels: Optional[int] = None
    # Document-level OCR time budget; remaining pages are skipped with a
    # recorded reason once the budget is exhausted.
    ocr_time_budget_seconds: Optional[float] = None
    # Upper bound for a single MinerU layout run (default tightened from the
    # historical 1800s so a stuck model run cannot hold a worker for 30 min).
    mineru_timeout_seconds: Optional[float] = None
    # Page-level OCR process parallelism for scanned documents. None/1 keeps
    # the serial path; values > 1 OCR pages across a process pool (MuPDF is
    # not thread-safe, hence processes) while preserving per-page failure
    # isolation and the document time budget semantics.
    ocr_parallel_workers: Optional[int] = None
    # G11: per-document cumulative byte budget for image asset payloads
    # (the raw image bytes retained in metadata["image_asset_payloads"]).
    # Placements beyond the budget are recorded as
    # not_extracted_reason="budget_exhausted" — the document survives
    # and the truncation is a fact in metadata (payloads_truncated).
    # None = no byte budget (historical behaviour).
    max_image_asset_bytes: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "version": LIMITS_VERSION,
            "max_pages": self.max_pages,
            "max_file_bytes": self.max_file_bytes,
            "max_page_pixels": self.max_page_pixels,
            "ocr_time_budget_seconds": self.ocr_time_budget_seconds,
            "mineru_timeout_seconds": self.mineru_timeout_seconds,
            "ocr_parallel_workers": self.ocr_parallel_workers,
            "max_image_asset_bytes": self.max_image_asset_bytes,
        }

    def violated_by(self, *, page_count: int, file_size_bytes: int) -> list[str]:
        """Return human-readable reasons for every violated ceiling."""
        reasons: list[str] = []
        if self.max_pages is not None and page_count > self.max_pages:
            reasons.append(
                f"page count {page_count} exceeds max_pages {self.max_pages}"
            )
        if self.max_file_bytes is not None and file_size_bytes > self.max_file_bytes:
            reasons.append(
                f"file size {file_size_bytes} bytes exceeds "
                f"max_file_bytes {self.max_file_bytes}"
            )
        return reasons


@dataclass
class PDFBlock:
    """An immutable page-level piece of extraction evidence.

    ``text`` and ``bbox`` are extracted facts.  Derived classifications such as
    ``heading`` and ``paragraph`` belong to the reconstruction result, not this
    evidence object.  New optional fields may be added without changing the
    meaning of the initial contract fields.
    """

    block_type: str
    text: str
    page_number: int
    bbox: BBox
    source_kind: str = "native_text"
    confidence: float = 1.0
    raw_payload_ref: Optional[str] = None
    raw_payload: Optional[dict[str, Any]] = None
    evidence_id: Optional[str] = None


@dataclass
class PDFPage:
    """Evidence and immutable page geometry for one PDF page."""

    page_number: int
    width: float
    height: float
    blocks: list[PDFBlock] = field(default_factory=list)
    rotation: int = 0


@dataclass
class PDFExtractionResult:
    """Output of one extraction runtime before reading reconstruction."""

    pages: list[PDFPage]
    metadata: dict[str, Any]
    extraction_method: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PDFRoutingDecision:
    """Explainable runtime selection based on inexpensive PDF probe signals."""

    recommended_runtime: str
    candidate_runtimes: tuple[str, ...]
    signals: dict[str, Any]
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "recommended_runtime": self.recommended_runtime,
            "candidate_runtimes": list(self.candidate_runtimes),
            "signals": self.signals,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PDFEnrichmentBackend:
    """Declarative contract for a future layout or visual enrichment backend.

    The runtime may expose a backend as ``not_configured`` before it exists, but
    it must never label an extraction as layout/visual output without declaring
    the backend identity, version, supported evidence, and provenance behavior.
    """

    name: str
    version: str
    kind: str
    supported_block_types: tuple[str, ...]
    preserves_coordinates: bool = True
    preserves_evidence_links: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "supported_block_types": list(self.supported_block_types),
            "preserves_coordinates": self.preserves_coordinates,
            "preserves_evidence_links": self.preserves_evidence_links,
        }
