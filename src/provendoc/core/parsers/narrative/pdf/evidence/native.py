"""Native PDF page-evidence extraction using PyMuPDF.

The extractor deliberately does not infer reading order, headings, paragraphs,
or page noise.  It records the raw block and span evidence required for those
replaceable reconstruction stages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFPage,
)


class NativePDFExtractor:
    """Extract immutable text/span evidence from native-text PDF pages."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract per-page evidence from the native text layer (per-page failure isolation)."""
        document = fitz.open(file_path)
        try:
            # B9: page-level failure isolation.  A single broken page must
            # never destroy the whole document's native evidence; the failure
            # is recorded per page (``pdf_native_page_errors``) and the page
            # keeps its geometry with an empty block list instead of being
            # silently dropped from the sequence.
            pages: list[PDFPage] = []
            page_errors: list[dict[str, Any]] = []
            for page_index in range(int(document.page_count)):
                page = document[page_index]
                page_number = page_index + 1
                try:
                    pages.append(self._extract_page(page, page_number))
                except Exception as error:
                    page_errors.append(
                        {
                            "page_number": page_number,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    try:
                        pages.append(
                            PDFPage(
                                page_number=page_number,
                                width=float(page.rect.width),
                                height=float(page.rect.height),
                                rotation=int(page.rotation),
                                blocks=[],
                            )
                        )
                    # Page-metadata read failures fall back to empty blocks (native text absence != a corrupt file).
                    except Exception:
                        pass
            metadata: dict[str, Any] = {
                "page_count": len(pages),
                "source_kind": "native_text",
                "pdf_document_metadata": self._document_metadata(document),
            }
            if page_errors:
                metadata["pdf_native_page_errors"] = page_errors
            return PDFExtractionResult(
                pages=pages,
                extraction_method="native_text",
                metadata=metadata,
            )
        finally:
            document.close()

    @staticmethod
    def _document_metadata(document: fitz.Document) -> dict[str, str]:
        """Preserve useful embedded document context without inventing values."""
        return {
            key: str(value)
            for key, value in document.metadata.items()
            if value not in (None, "")
        }

    def _extract_page(self, page: fitz.Page, page_number: int) -> PDFPage:
        raw_page = page.get_text("dict", sort=False)
        blocks: list[PDFBlock] = []

        for block_index, block in enumerate(raw_page.get("blocks", [])):
            if block.get("type") != 0:
                continue

            text, spans = self._block_text_and_spans(block)
            if not text:
                continue

            raw_bbox = tuple(float(value) for value in block["bbox"])
            bbox: tuple[float, float, float, float] = (
                raw_bbox[0], raw_bbox[1], raw_bbox[2], raw_bbox[3],
            )
            raw_payload = {
                "block_index": block_index,
                "line_count": len(block.get("lines", [])),
                "spans": spans,
            }
            blocks.append(
                PDFBlock(
                    block_type="text",
                    text=text,
                    page_number=page_number,
                    bbox=bbox,
                    source_kind="native_text",
                    confidence=1.0,
                    raw_payload_ref=f"page:{page_number}:block:{block_index}",
                    raw_payload=raw_payload,
                    evidence_id=f"native:p{page_number}:b{block_index}",
                )
            )

        return PDFPage(
            page_number=page_number,
            width=float(page.rect.width),
            height=float(page.rect.height),
            rotation=int(page.rotation),
            blocks=blocks,
        )

    @staticmethod
    def _block_text_and_spans(block: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """Normalize a PyMuPDF text-dict block into reusable text/span evidence.

        OCR-backed ``TextPage`` objects expose the same block shape, so keeping
        this normalizer runtime-neutral ensures native and OCR evidence share
        identical geometry and span contracts.
        """
        line_texts: list[str] = []
        spans: list[dict[str, Any]] = []

        for line_index, line in enumerate(block.get("lines", [])):
            line_spans = line.get("spans", [])
            line_text = "".join(str(span.get("text", "")) for span in line_spans).strip()
            if not line_text:
                continue
            line_texts.append(line_text)

            # B8: writing-mode evidence.  ``wmode`` 1 = vertical writing
            # (CJK vertical columns);  ``dir`` is the writing direction vector —
            # (1, 0) is normal LTR, negative x means an RTL line, any
            # other vector is rotated text (axis-tilted / sideways text).
            line_direction = line.get("dir", (1.0, 0.0))
            line_wmode = int(line.get("wmode", 0))

            for span_index, span in enumerate(line_spans):
                span_text = str(span.get("text", ""))
                if not span_text:
                    continue
                spans.append(
                    {
                        "line_index": line_index,
                        "span_index": span_index,
                        "text": span_text,
                        "bbox": [float(value) for value in span["bbox"]],
                        "font": str(span.get("font", "")),
                        "size": float(span.get("size", 0.0)),
                        "flags": int(span.get("flags", 0)),
                        "color": int(span.get("color", 0)),
                        "line_dir": [float(line_direction[0]), float(line_direction[1])],
                        "line_wmode": line_wmode,
                    }
                )

        return "\n".join(line_texts).strip(), spans
