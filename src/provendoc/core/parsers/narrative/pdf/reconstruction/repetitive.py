"""Conservative repetitive header/footer detection for PDF evidence."""

from __future__ import annotations

import re
from collections import defaultdict

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock, PDFPage

_PAGE_NUMBER_RE = re.compile(
    r"^(?:page\s*)?\d+(?:\s*(?:/|of)\s*\d+)?$|^第\s*\d+\s*页$",
    re.IGNORECASE,
)


class RepetitiveArtifactSuppressor:
    """Detect repeated text edge artifacts without deleting raw evidence.

    Image and table evidence is never considered header/footer noise.  Their
    empty ``text`` values must not accidentally form a repeated pattern.
    """

    def __init__(self, edge_ratio: float = 0.14, min_repeated_pages: int = 3):
        """Configure the margin-area ratio and the minimum repeat page count."""
        self.edge_ratio = edge_ratio
        self.min_repeated_pages = min_repeated_pages

    def suppress(self, pages: list[PDFPage]) -> tuple[list[PDFBlock], dict]:
        """Suppress repeated headers/footers (original evidence never deleted)."""
        candidates: list[PDFBlock] = []
        for page in pages:
            candidates.extend(self._edge_candidates(page))

        page_sets: dict[str, set[int]] = defaultdict(set)
        for block in candidates:
            page_sets[self._normalize(block.text)].add(block.page_number)

        suppressed_ids: set[str] = set()
        suppressed_samples: list[str] = []
        # Printed page numbers (spec: double page numbering).  One visible number per
        # page, recorded verbatim as the reader sees it; a page with several
        # different number-like edge blocks is flagged ambiguous instead of
        # guessed.
        visible_page_numbers: dict[int, str] = {}
        ambiguous_pages: set[int] = set()
        for block in candidates:
            normalized = self._normalize(block.text)
            repeated = len(page_sets[normalized]) >= self.min_repeated_pages
            page_number = bool(_PAGE_NUMBER_RE.match(normalized))
            if repeated or page_number:
                if block.evidence_id:
                    suppressed_ids.add(block.evidence_id)
                if normalized and normalized not in suppressed_samples:
                    suppressed_samples.append(normalized)
            if page_number:
                printed = " ".join(block.text.split())
                if block.page_number in visible_page_numbers:
                    if visible_page_numbers[block.page_number] != printed:
                        ambiguous_pages.add(block.page_number)
                else:
                    visible_page_numbers[block.page_number] = printed

        content = [
            block
            for page in pages
            for block in page.blocks
            if block.evidence_id not in suppressed_ids
        ]
        return content, {
            "suppressed_block_count": len(suppressed_ids),
            "suppressed_repetitive_samples": suppressed_samples[:10],
            "edge_candidate_count": len(candidates),
            # Spec engineering principle: header/footer artifacts are classified, not deleted — the evidence IDs of
            # suppressed blocks are listed here; the full evidence stays in pdf_evidence, auditable.
            "suppressed_evidence_ids": sorted(suppressed_ids),
            "suppressed_content_role": "repetitive_edge_artifact",
            # Spec engineering principle: double page numbering — printed page numbers are registered facts, never a renumbering basis.
            "visible_page_numbers": {
                str(page): number
                for page, number in sorted(visible_page_numbers.items())
            },
            "visible_page_number_ambiguous_pages": sorted(ambiguous_pages),
        }

    def _edge_candidates(self, page: PDFPage) -> list[PDFBlock]:
        header_boundary = page.height * self.edge_ratio
        footer_boundary = page.height * (1 - self.edge_ratio)
        return [
            block
            for block in page.blocks
            if (
                block.block_type == "text"
                and block.text.strip()
                and (block.bbox[1] <= header_boundary or block.bbox[3] >= footer_boundary)
            )
        ]

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.casefold().split())
