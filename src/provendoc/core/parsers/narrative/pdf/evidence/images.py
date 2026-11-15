"""Image-region evidence extraction.

Records where images live on each page.  Phase 1 does not interpret image
content (captions, charts, figures) — that is Phase 2 work — but the geometry
is cheap, immutable evidence that routing and diagnostics already need.
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFPage,
)


class ImageRegionExtractor:
    """Extract image placement evidence without interpreting content."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract image placement evidence (content not interpreted)."""
        document = fitz.open(file_path)
        try:
            pages = [
                self._extract_page(document[page_index], page_index + 1)
                for page_index in range(int(document.page_count))
            ]
            image_block_count = sum(len(page.blocks) for page in pages)
            return PDFExtractionResult(
                pages=pages,
                extraction_method="image_regions",
                metadata={
                    "page_count": len(pages),
                    "source_kind": "image_region",
                    "image_block_count": image_block_count,
                },
            )
        finally:
            document.close()

    def _extract_page(self, page: fitz.Page, page_number: int) -> PDFPage:
        blocks: list[PDFBlock] = []

        for image_index, info in enumerate(page.get_image_info(xrefs=True)):
            bbox = info.get("bbox")
            if not bbox:
                continue
            xref = info.get("xref", 0)
            raw_bbox = tuple(float(value) for value in bbox)
            blocks.append(
                PDFBlock(
                    block_type="image",
                    text="",
                    page_number=page_number,
                    bbox=(raw_bbox[0], raw_bbox[1], raw_bbox[2], raw_bbox[3]),
                    source_kind="image_region",
                    confidence=1.0,
                    raw_payload_ref=f"page:{page_number}:image:{image_index}",
                    raw_payload={
                        "xref": int(xref),
                        "width": int(info.get("width", 0)),
                        "height": int(info.get("height", 0)),
                    },
                    evidence_id=f"image:p{page_number}:i{image_index}",
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
    def merge_into_pages(
        text_pages: list[PDFPage],
        image_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Attach image evidence blocks to the text evidence pages."""
        images_by_page: dict[int, list[PDFBlock]] = {
            page.page_number: page.blocks for page in image_pages
        }
        merged: list[PDFPage] = []
        for page in text_pages:
            extra = images_by_page.get(page.page_number, [])
            merged.append(
                PDFPage(
                    page_number=page.page_number,
                    width=page.width,
                    height=page.height,
                    rotation=page.rotation,
                    blocks=list(page.blocks) + list(extra),
                )
            )
        return merged
