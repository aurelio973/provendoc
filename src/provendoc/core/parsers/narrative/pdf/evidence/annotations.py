# ============================================================
# Narrative Domain — PDF evidence
# Annotation (/Annots) review-state evidence
# ============================================================
"""PDF annotation evidence (sticky notes, highlights, stamps, drawings).

Everything is *evidence*: annotation bodies (comment texts, FreeText
content) never enter the narrative text, and nothing is removed or
resolved.  The extractor records which annotation subtypes exist, how
many, on which pages, who authored them and whether any are flagged
hidden — the same visible-degradation discipline as DOCX review facts
(``docx_review``).

Fact boundaries (documented, not guessed):

- PyMuPDF's ``page.annots()`` iterates the page's /Annots array
  excluding Link annotations; links are navigation evidence and are
  covered by the navigation extractor, not double-counted here
- subtype names come verbatim from the annotation dictionary's
  /Subtype (MuPDF's normalized names: Text, FreeText, Highlight,
  Underline, StrikeOut, Squiggly, Stamp, Ink, Square, Circle, ...)
- the author is the annotation dictionary's /T entry (``annot.info``
  "title"); missing /T is an unauthored fact, not a guess
- the hidden flag is bit 2 (/F & 2); hidden annotations still count
- annotation content strings are sampled (bounded), never rendered
- a failed scan is a runtime note, never a fatal error
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

# Pathological-document caps (same spirit as watermark.py): totals are
# bounded by the page-scan cap; author lists and content samples stay
# small so metadata never scales with adversarial annotation counts.
_MAX_PAGES_SCANNED = 200
_MAX_AUTHORS = 20
_MAX_SAMPLE_CONTENTS = 5
_MAX_CONTENT_CHARS = 80


def _subtype_name(raw_type: object) -> str:
    """Normalize MuPDF's annot.type (tuple (number, name)) to a string."""
    if isinstance(raw_type, (tuple, list)) and len(raw_type) >= 2:
        return str(raw_type[1])
    return str(raw_type)


class AnnotationExtractor:
    """Collect /Annots review-state facts without altering narrative text."""

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract /Annots annotation evidence (body text untouched)."""
        document = fitz.open(file_path)
        try:
            metadata = {"pdf_annotations": self._annotation_facts(document)}
            return PDFExtractionResult(
                pages=[],
                extraction_method="annotations",
                metadata=metadata,
            )
        finally:
            document.close()

    # ------------------------------------------------------------
    # detection
    # ------------------------------------------------------------
    def _annotation_facts(self, document: fitz.Document) -> dict[str, Any]:
        pages_scanned = min(int(document.page_count), _MAX_PAGES_SCANNED)
        subtype_counts: dict[str, int] = {}
        page_counts: dict[int, int] = {}
        authors: set[str] = set()
        sample_contents: list[str] = []
        hidden_count = 0
        total = 0

        for index in range(pages_scanned):
            page_number = index + 1
            try:
                page = document[index]
                annots = list(page.annots())
            except Exception:  # Evidence gathering is never fatal.
                continue
            page_annotation_count = 0
            for annot in annots:
                total += 1
                page_annotation_count += 1
                subtype = _subtype_name(annot.type)
                subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
                try:
                    flags = int(annot.flags)
                except Exception:
                    flags = 0
                if flags & 2:  # /F bit 2 = Hidden
                    hidden_count += 1
                try:
                    info = annot.info or {}
                except Exception:
                    info = {}
                author = (info.get("title") or "").strip()
                if author:
                    authors.add(author)
                content = (info.get("content") or "").strip()
                if (
                    content
                    and len(sample_contents) < _MAX_SAMPLE_CONTENTS
                    and content[:_MAX_CONTENT_CHARS] not in sample_contents
                ):
                    sample_contents.append(content[:_MAX_CONTENT_CHARS])
            if page_annotation_count:
                page_counts[page_number] = page_annotation_count

        return {
            "scanned_pages": pages_scanned,
            "total_annotation_count": total,
            "present": bool(total),
            "subtype_counts": dict(sorted(subtype_counts.items())),
            "pages_with_annotations": dict(sorted(page_counts.items())),
            "hidden_annotation_count": hidden_count,
            "author_count": len(authors),
            "authors": sorted(authors)[:_MAX_AUTHORS],
            "sample_contents": sample_contents,
            "note": (
                "annotation evidence only: /Annots bodies (sticky notes, "
                "FreeText, markup) are never part of the narrative text; "
                "Link annotations are excluded (navigation evidence); "
                "subtype names are verbatim /Subtype values"
            ),
        }

    # ------------------------------------------------------------
    # page merge contract (annotation evidence is document-level)
    # ------------------------------------------------------------
    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        annotation_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Annotation evidence lives in metadata only; pages pass through."""
        return text_pages
