"""Extraction-level quality signals (not quality evaluation — raw extraction facts only)."""

from __future__ import annotations

from provendoc.core.parsers.narrative.pdf.contracts import PDFExtractionResult


class ExtractionQualityProbe:
    """Measure how much usable content the extraction runtime actually got."""

    def assess(self, extraction: PDFExtractionResult, narrative_text: str) -> dict:
        """Assess how much usable content the extraction run actually obtained."""
        page_count = len(extraction.pages)
        text_blocks_per_page = [
            sum(block.block_type == "text" for block in page.blocks)
            for page in extraction.pages
        ]
        image_blocks_per_page = [
            sum(block.block_type == "image" for block in page.blocks)
            for page in extraction.pages
        ]
        empty_text_page_numbers = [
            page.page_number
            for page in extraction.pages
            if not any(block.text.strip() for block in page.blocks if block.block_type == "text")
        ]
        empty_text_pages = len(empty_text_page_numbers)
        replacement_chars = narrative_text.count("\ufffd")

        total_text_chars = sum(
            len(block.text) for page in extraction.pages for block in page.blocks
        )
        confidence = self._confidence(
            page_count=page_count,
            empty_text_pages=empty_text_pages,
            replacement_chars=replacement_chars,
            total_text_chars=total_text_chars,
        )

        return {
            "extraction_method": extraction.extraction_method,
            "page_count": page_count,
            "empty_text_pages": empty_text_pages,
            "empty_text_page_numbers": empty_text_page_numbers,
            "text_blocks_per_page": text_blocks_per_page,
            "image_blocks_per_page": image_blocks_per_page,
            "total_text_chars": total_text_chars,
            "narrative_chars": len(narrative_text),
            "replacement_chars": replacement_chars,
            "extraction_confidence": confidence,
        }

    @staticmethod
    def _confidence(
        *,
        page_count: int,
        empty_text_pages: int,
        replacement_chars: int,
        total_text_chars: int,
    ) -> float:
        confidence = 1.0
        if page_count == 0:
            return 0.0
        if total_text_chars < 200:
            confidence *= 0.5
        if empty_text_pages:
            confidence *= 1 - (empty_text_pages / page_count) * 0.6
        if replacement_chars > 10:
            confidence *= 0.7
        return round(max(0.0, min(confidence, 1.0)), 3)
