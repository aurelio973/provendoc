"""Watermark evidence (text-repetition + diagonal/large rendering).

Everything here is *evidence*: watermark glyphs are never removed from
the narrative text.  The extractor records where high-confidence
watermark text lives — repeated across pages, and either rendered
diagonally or in a font size far above the page's body text — so
downstream consumers can decide policy (keep, strip, or annotate).

Detection discipline (mirrors the shared heading layer's
recovery-scan discipline: no pattern relaxation, only verifiable
structural evidence promotes a candidate):

* the same normalized text must repeat on >= 50% of scanned pages
  (>= 2 pages), so one-off repeated boilerplate never qualifies;
* the candidate must carry at least one watermark feature:
  - a non-axis-aligned text direction (diagonal rendering), or
  - an average font size >= 1.6x the page median body size;
* headers/footers repeat too, but they are axis-aligned and
  body-sized — the feature requirement is what separates them.

Annotation-based watermarks (PDF 2.0 /Subtype /Watermark) are rare and
not enumerated by PyMuPDF's annotation types; the text-repetition
channel is the factual surface this extractor covers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFExtractionResult,
    PDFPage,
)

# Pathological-document caps (same spirit as security.py): totals are
# bounded by the page-scan cap, samples stay small.
_MAX_PAGES_SCANNED = 200
_MAX_CANDIDATES = 10
_MAX_SAMPLE_PAGES = 20
_MAX_WATERMARK_TEXT_CHARS = 80

# A candidate's average font size must exceed the page's median body
# size by this factor to qualify via the "large type" feature.
_BODY_SIZE_RATIO = 1.6

# Repeated text must cover at least this share of scanned pages.
_PAGE_COVERAGE_RATIO = 0.5

# Axis-aligned text directions (horizontal / vertical writing).  Any
# other direction is diagonal rendering — the classic watermark shape.
_AXIS_ALIGNED_DIRS = {
    (1.0, 0.0),
    (-1.0, 0.0),
    (0.0, 1.0),
    (0.0, -1.0),
}


class WatermarkExtractor:
    """Collect repeated diagonal/large text evidence without altering text."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract repeated diagonal / large-glyph watermark evidence."""
        document = fitz.open(file_path)
        try:
            metadata = self._watermark_facts(document)
            return PDFExtractionResult(
                pages=[],
                extraction_method="watermark",
                metadata=metadata,
            )
        finally:
            document.close()

    # ------------------------------------------------------------
    # detection
    # ------------------------------------------------------------
    def _watermark_facts(self, document: fitz.Document) -> dict[str, Any]:
        pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)

        # occurrence index: normalized text -> per-candidate facts
        occurrences: dict[str, dict[str, Any]] = {}
        # per-page median body font size (median over all text spans)
        page_body_sizes: dict[int, float] = {}

        for index in range(pages_scanned):
            page_number = index + 1
            try:
                page = document[index]
                raw = page.get_text("dict", sort=False)
            except Exception:  # Evidence gathering is never fatal.
                continue

            span_sizes: list[float] = []
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    direction = (
                        round(float(line.get("dir", (1.0, 0.0))[0]), 3),
                        round(float(line.get("dir", (1.0, 0.0))[1]), 3),
                    )
                    for span in line.get("spans", []):
                        text = str(span.get("text", "")).strip()
                        size = float(span.get("size", 0.0) or 0.0)
                        if not text or size <= 0:
                            continue
                        span_sizes.append(size)
                        if len(text) > _MAX_WATERMARK_TEXT_CHARS:
                            # Long repeated text is boilerplate, not a
                            # watermark mark; never indexed.
                            continue
                        entry = occurrences.setdefault(
                            text,
                            {
                                "pages": [],
                                "sizes": [],
                                "directions": set(),
                                "sample_bbox": None,
                            },
                        )
                        if page_number not in entry["pages"]:
                            entry["pages"].append(page_number)
                        entry["sizes"].append(size)
                        entry["directions"].add(direction)
                        if entry["sample_bbox"] is None:
                            try:
                                entry["sample_bbox"] = [
                                    round(float(v), 2) for v in span.get("bbox", ())
                                ]
                            except Exception:
                                entry["sample_bbox"] = None

            if span_sizes:
                span_sizes.sort()
                page_body_sizes[page_number] = span_sizes[len(span_sizes) // 2]

        candidates: list[dict[str, Any]] = []
        if pages_scanned >= 2:
            for text, entry in occurrences.items():
                page_count = len(entry["pages"])
                coverage = page_count / pages_scanned
                if coverage < _PAGE_COVERAGE_RATIO:
                    continue

                rotated = any(
                    direction not in _AXIS_ALIGNED_DIRS
                    for direction in entry["directions"]
                )
                avg_size = sum(entry["sizes"]) / len(entry["sizes"])
                body_sizes = [
                    page_body_sizes.get(number, 0.0)
                    for number in entry["pages"]
                    if page_body_sizes.get(number, 0.0) > 0
                ]
                body_reference = (
                    sum(body_sizes) / len(body_sizes) if body_sizes else 0.0
                )
                size_ratio = (
                    round(avg_size / body_reference, 2) if body_reference else None
                )
                large_type = size_ratio is not None and size_ratio >= _BODY_SIZE_RATIO

                if not (rotated or large_type):
                    continue

                candidates.append(
                    {
                        "text": text,
                        "page_numbers": entry["pages"][:_MAX_SAMPLE_PAGES],
                        "page_count": page_count,
                        "page_coverage": round(coverage, 3),
                        "rotated": rotated,
                        "directions": [
                            list(direction)
                            for direction in sorted(entry["directions"])
                        ],
                        "avg_font_size": round(avg_size, 2),
                        "size_vs_body_ratio": size_ratio,
                        "features": (
                            ["repeated"]
                            + (["rotated"] if rotated else [])
                            + (["large_type"] if large_type else [])
                        ),
                        "sample_bbox": entry["sample_bbox"],
                    }
                )
                if len(candidates) >= _MAX_CANDIDATES:
                    break

        candidates.sort(
            key=lambda candidate: (
                -candidate["page_count"],
                candidate["text"],
            )
        )
        return {
            "pdf_watermarks": {
                "scanned_pages": pages_scanned,
                "present": bool(candidates),
                "candidates": candidates,
                "note": (
                    "watermark evidence only: the watermark glyphs remain "
                    "in the extracted narrative text; removal is a "
                    "downstream policy decision, never a parser rewrite"
                ),
            }
        }

    # ------------------------------------------------------------
    # page merge contract (watermark evidence is document-level)
    # ------------------------------------------------------------
    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        watermark_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Watermark evidence lives in metadata only; pages pass through."""
        return text_pages
