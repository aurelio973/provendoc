"""Page-geometry evidence for B5 (cross-page & layout geometry).

Everything here is evidence, never a rewrite: page boxes are recorded and
compared, but the parser never re-crops, re-rotates, or re-orders content.
Facts that geometry makes *invisible* (text clipped outside the CropBox)
are reported as exactly that — a clipped region the extraction cannot see —
instead of being silently ignored.

Scope (gap register B5):

* MediaBox / CropBox / TrimBox / BleedBox mismatches (PDF §2);
* extreme margins / content overflow / text crossing the crop box (§2);
* rotation other than 90/180/270 — skewed line directions (§2);
* mixed landscape/portrait pages (§2);
* sidebars / margin notes (§2);
* background text overlapped by foreground text (§2);
* cross-page paragraph continuation candidates (batch title);
* tables rotated 90°, tables spanning columns, tables interleaved with
  body text (§5);
* figures nested between columns (§2 / §6).
"""

from __future__ import annotations

import math
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

_MAX_PAGES = 2000
"""Bounded page scan: geometry evidence degrades honestly beyond this."""

_MAX_SAMPLES = 20
"""Cap per list-of-samples field so metadata stays reviewable."""

_MAX_TEXT_CHARS = 120
"""Sample-text truncation for evidence entries."""

_OVERLAP_MIN_AREA = 4.0
"""Minimum intersection area (pt²) before two text blocks count as overlapping."""

_SIDEBAR_MAX_WIDTH_RATIO = 0.3
"""A margin-note cluster wider than this share of the page is body text."""

_SIDEBAR_EDGE_RATIO = 0.25
"""Margin notes live within this share of the page width from either edge."""

_TERMINAL_PUNCTUATION = (".", "?", "!", "。", "！", "？", ";", "；", ":")

# Line-direction angle (degrees) tolerance around an axis before a line is
# called axis-aligned.  90/180/270 multiples are normal rotated text; other
# angles are skewed text — the honest B5 signal for "rotation
# other than 90/180/270".
_AXIS_ANGLE_TOLERANCE = 2.0


def _block_text(block: dict[str, Any]) -> str:
    """Concatenate span text of a ``get_text("dict")`` block.

    Dict-mode blocks carry no top-level ``text`` key; the words live in
    ``lines[i]["spans"][j]["text"]``.
    """
    return "".join(
        str(span.get("text", ""))
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    )


def _rect_tuple(rect: Any) -> list[float]:
    try:
        return [round(float(value), 2) for value in rect]
    except (TypeError, ValueError):
        return []


def _rects_equal(left: Any, right: Any, tolerance: float = 0.5) -> bool:
    try:
        return all(
            abs(float(a) - float(b)) <= tolerance
            for a, b in zip(tuple(left), tuple(right))
        )
    except (TypeError, ValueError):
        return False


class PageGeometryExtractor:
    """Extract document-level page-geometry evidence (metadata only).

    ``merge_into_pages`` is a passthrough: like the B8 character evidence,
    geometry evidence lives under the namespaced metadata key
    ``pdf_page_geometry`` and never mutates page text evidence.
    """

    # Capability flag: the pipeline may inject a SharedTableFinder so table
    # detection runs once per page across all evidence consumers.
    accepts_shared_table_finder = True

    def extract(
        self,
        file_path: str | Path,
        *,
        table_finder: Any | None = None,
    ) -> PDFExtractionResult:
        """Extract document-level page geometry evidence (metadata only)."""
        document = fitz.open(file_path)
        try:
            page_facts: list[dict[str, Any]] = []
            for page_index in range(int(document.page_count)):
                page_number = page_index + 1
                if page_number > _MAX_PAGES:
                    break
                page_facts.append(
                    self._page_facts(document[page_index], page_number, table_finder)
                )
            return PDFExtractionResult(
                pages=[],
                extraction_method="page_geometry",
                metadata={
                    "pdf_page_geometry": self._document_summary(page_facts),
                },
            )
        finally:
            document.close()

    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        geometry_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Passthrough: geometry evidence lives in metadata, never in pages."""
        return text_pages

    # ------------------------------------------------------------
    # per-page facts
    # ------------------------------------------------------------

    def _page_facts(
        self,
        page: fitz.Page,
        page_number: int,
        table_finder: Any | None = None,
    ) -> dict[str, Any]:
        rect = page.rect
        page_width, page_height = float(rect.width), float(rect.height)
        boxes = self._page_boxes(page)
        facts: dict[str, Any] = {
            "page_number": page_number,
            "rotation": int(page.rotation),
            "orientation": (
                "landscape" if page_width > page_height else "portrait"
            ),
            "width": round(page_width, 2),
            "height": round(page_height, 2),
            "boxes": boxes,
        }

        # Box mismatches: the CropBox is what the reader (and this parser)
        # sees; anything outside it is invisible to extraction.
        mismatches = self._box_mismatches(boxes)
        if mismatches:
            facts["box_mismatches"] = mismatches
            facts["box_mismatch_note"] = (
                "the visible page differs from the declared MediaBox; content "
                "outside the CropBox is invisible to text extraction"
            )

        try:
            raw = page.get_text("dict", sort=False)
        except Exception:
            raw = {}
        text_blocks = [
            block
            for block in raw.get("blocks", [])
            if block.get("type") == 0 and _block_text(block).strip()
        ]
        block_bboxes: list[tuple[float, float, float, float]] = []
        for block in text_blocks:
            b = block["bbox"]
            block_bboxes.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))

        facts["overflow_text"] = self._overflow_blocks(
            block_bboxes, page_width, page_height
        )
        facts["skewed_lines"] = self._skewed_lines(raw)
        facts["overlapping_text"] = self._overlapping_text_pairs(text_blocks)
        facts["margin_note_clusters"] = self._margin_note_clusters(
            block_bboxes, page_width
        )
        # Bounded text samples for cross-page paragraph candidates: the
        # first/last block of every page, never the full body text.
        facts["text_blocks"] = [
            {
                "bbox": [round(v, 2) for v in block["bbox"]],
                "text": _block_text(block)[:_MAX_TEXT_CHARS],
            }
            for block in (text_blocks[:1] + text_blocks[-1:])
            if text_blocks
        ]
        gutters = self._text_gutters(block_bboxes, page_width)

        try:
            if table_finder is not None:
                tables = table_finder.tables_for(page_number - 1)
            else:
                tables = list(page.find_tables().tables)
        except Exception:
            tables = []
        facts["table_geometry"] = self._table_geometry(
            tables, block_bboxes, page_width, gutters
        )

        try:
            image_rects = self._image_rects(page)
        except Exception:
            image_rects = []
        facts["figure_between_columns"] = self._figures_vs_columns(
            image_rects, gutters
        )
        return facts

    @staticmethod
    def _page_boxes(page: fitz.Page) -> dict[str, list[float]]:
        boxes: dict[str, list[float]] = {}
        for name in ("mediabox", "cropbox", "trimbox", "bleedbox", "artbox"):
            rect = getattr(page, name, None)
            if rect is not None:
                boxes[name] = _rect_tuple(rect)
        return boxes

    @staticmethod
    def _box_mismatches(boxes: dict[str, list[float]]) -> list[str]:
        mismatches: list[str] = []
        media = boxes.get("mediabox")
        crop = boxes.get("cropbox")
        if media and crop and not _rects_equal(media, crop):
            mismatches.append("cropbox_differs_from_mediabox")
        crop = crop or media
        if crop:
            for name in ("trimbox", "bleedbox", "artbox"):
                box = boxes.get(name)
                if box and not _rects_equal(box, crop):
                    mismatches.append(f"{name}_differs_from_cropbox")
        return mismatches

    @staticmethod
    def _overflow_blocks(
        block_bboxes: list[tuple[float, float, float, float]],
        page_width: float,
        page_height: float,
    ) -> dict[str, Any]:
        """Text blocks whose bbox crosses the visible page rectangle.

        Text fully outside the CropBox is already invisible to extraction;
        only boundary-crossing blocks can be seen, and those are the honest
        overflow evidence.
        """
        overflow = [
            [round(value, 2) for value in bbox]
            for bbox in block_bboxes
            if bbox[0] < -0.5
            or bbox[1] < -0.5
            or bbox[2] > page_width + 0.5
            or bbox[3] > page_height + 0.5
        ]
        return {"count": len(overflow), "bboxes": overflow[:_MAX_SAMPLES]}

    @staticmethod
    def _skewed_lines(raw: dict[str, Any]) -> dict[str, Any]:
        """Lines whose writing direction is not axis-aligned (skewed text)."""
        angles: list[float] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                direction = line.get("dir", (1.0, 0.0))
                try:
                    dx, dy = float(direction[0]), float(direction[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if dx == 0.0 and dy == 0.0:
                    continue
                angle = math.degrees(math.atan2(dy, dx)) % 180.0
                remainder = min(angle % 90.0, 90.0 - (angle % 90.0))
                if remainder > _AXIS_ANGLE_TOLERANCE:
                    angles.append(round(angle, 1))
        return {
            "count": len(angles),
            "angles_sample": sorted(set(angles))[:_MAX_SAMPLES],
        }

    @staticmethod
    def _overlapping_text_pairs(
        text_blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Text blocks whose areas substantially intersect on the same page.

        Overlap is the geometry signature of a foreground/background text
        layer pair (watermark under body text, stamped text); which layer is
        visually on top is a rendering fact the parser cannot confirm.
        """
        pairs: list[dict[str, Any]] = []
        bboxes = [
            (index, tuple(float(v) for v in block["bbox"]), block)
            for index, block in enumerate(text_blocks)
        ]
        for i in range(len(bboxes)):
            for j in range(i + 1, len(bboxes)):
                _, (ax0, ay0, ax1, ay1), _ = bboxes[i]
                _, (bx0, by0, bx1, by1), _ = bboxes[j]
                overlap = (
                    max(0.0, min(ax1, bx1) - max(ax0, bx0))
                    * max(0.0, min(ay1, by1) - max(ay0, by0))
                )
                if overlap < _OVERLAP_MIN_AREA:
                    continue
                pairs.append(
                    {
                        "bbox_a": [round(v, 2) for v in (ax0, ay0, ax1, ay1)],
                        "bbox_b": [round(v, 2) for v in (bx0, by0, bx1, by1)],
                        "overlap_area": round(overlap, 2),
                        "text_a": _block_text(bboxes[i][2])[:_MAX_TEXT_CHARS],
                        "text_b": _block_text(bboxes[j][2])[:_MAX_TEXT_CHARS],
                    }
                )
                if len(pairs) >= _MAX_SAMPLES:
                    return {"count": len(pairs), "pairs": pairs}
        return {"count": len(pairs), "pairs": pairs}

    @staticmethod
    def _margin_note_clusters(
        block_bboxes: list[tuple[float, float, float, float]],
        page_width: float,
    ) -> list[dict[str, Any]]:
        """Narrow text clusters hugging a page edge (sidebars / margin notes).

        A cluster qualifies when at least two blocks sit within the outer
        quarter of the page and their combined width stays under 30% of the
        page — wider groups are ordinary body text at the margin.
        """
        if page_width <= 0:
            return []
        clusters: list[dict[str, Any]] = []
        for side, edge, sign in (
            ("left", 0.0, 1.0),
            ("right", page_width, -1.0),
        ):
            near = [
                bbox
                for bbox in block_bboxes
                if (bbox[0] if sign > 0 else page_width - bbox[2])
                < page_width * _SIDEBAR_EDGE_RATIO
            ]
            if len(near) < 2:
                continue
            width = max(bbox[2] for bbox in near) - min(bbox[0] for bbox in near)
            if width > page_width * _SIDEBAR_MAX_WIDTH_RATIO:
                continue
            clusters.append(
                {
                    "side": side,
                    "block_count": len(near),
                    "bbox": [
                        round(min(b[0] for b in near), 2),
                        round(min(b[1] for b in near), 2),
                        round(max(b[2] for b in near), 2),
                        round(max(b[3] for b in near), 2),
                    ],
                }
            )
        return clusters

    @staticmethod
    def _table_geometry(
        tables: list[Any],
        block_bboxes: list[tuple[float, float, float, float]],
        page_width: float,
        gutters: list[tuple[float, float]],
    ) -> dict[str, Any]:
        """Rotated / column-spanning / interleaved table geometry evidence."""
        rotated: list[dict[str, Any]] = []
        spanning: list[dict[str, Any]] = []
        interleaved: list[dict[str, Any]] = []

        for table in tables:
            bbox = tuple(float(v) for v in table.bbox)
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            # A table whose bbox is far taller than wide, with text inside,
            # is the geometry signature of a table rotated 90° on the page.
            if height > width * 2.5 and width < page_width * 0.6:
                rotated.append({"bbox": [round(v, 2) for v in bbox]})

            # Column spanning: the table box crosses a durable vertical
            # whitespace gutter that the surrounding text columns leave open.
            if any(
                bbox[0] < gutter_end - 1.0 and bbox[2] > gutter_start + 1.0
                for gutter_start, gutter_end in gutters
            ):
                spanning.append(
                    {
                        "bbox": [round(v, 2) for v in bbox],
                        "gutters_crossed": len(gutters),
                    }
                )

            # Interleaving: text blocks vertically inside the table band but
            # horizontally outside the table box (body text flows around it).
            beside = [
                bbox_text
                for bbox_text in block_bboxes
                if bbox_text[1] >= bbox[1]
                and bbox_text[3] <= bbox[3]
                and (bbox_text[2] < bbox[0] - 1.0 or bbox_text[0] > bbox[2] + 1.0)
            ]
            if len(beside) >= 2:
                interleaved.append(
                    {
                        "bbox": [round(v, 2) for v in bbox],
                        "beside_text_block_count": len(beside),
                    }
                )

        return {
            "table_count": len(tables),
            "rotated_candidates": rotated[:_MAX_SAMPLES],
            "rotated_candidate_count": len(rotated),
            "interleaved_tables": interleaved[:_MAX_SAMPLES],
            "interleaved_table_count": len(interleaved),
            "spanning_tables": spanning[:_MAX_SAMPLES],
            "spanning_table_count": len(spanning),
        }

    @staticmethod
    def _image_rects(page: fitz.Page) -> list[tuple[float, float, float, float]]:
        rects: list[tuple[float, float, float, float]] = []
        for xref, *_ in page.get_images(full=True):
            try:
                for rect in page.get_image_rects(xref):
                    rects.append((
                        float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]),
                    ))
            except Exception:
                continue
        return rects

    @staticmethod
    def _text_gutters(
        block_bboxes: list[tuple[float, float, float, float]],
        page_width: float,
    ) -> list[tuple[float, float]]:
        """Durable vertical whitespace bands no text block covers.

        These gutters separate text columns; figures or tables crossing one
        are the geometry signature of column-spanning layout objects.
        """
        if not block_bboxes or page_width <= 0:
            return []
        covered = sorted(
            [bbox[0], bbox[2]] for bbox in block_bboxes
        )
        merged: list[list[float]] = []
        for start, end in covered:
            if merged and start <= merged[-1][1] + 1.0:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [
            (merged[i][1], merged[i + 1][0])
            for i in range(len(merged) - 1)
            if merged[i + 1][0] - merged[i][1] > page_width * 0.04
        ]

    @staticmethod
    def _figures_vs_columns(
        image_rects: list[tuple[float, float, float, float]],
        gutters: list[tuple[float, float]],
    ) -> dict[str, Any]:
        """Images whose bbox crosses a durable vertical whitespace gutter.

        Figures nested between columns sit in (or across) the gutter between
        text columns.  Evidence only — the figure's true column membership is
        not inferred.
        """
        if not image_rects or not gutters:
            return {"count": 0, "bboxes": []}

        crossing = [
            [round(v, 2) for v in rect]
            for rect in image_rects
            if any(
                rect[0] < gutter_end - 1.0 and rect[2] > gutter_start + 1.0
                for gutter_start, gutter_end in gutters
            )
        ]
        return {
            "count": len(crossing),
            "bboxes": crossing[:_MAX_SAMPLES],
            "gutter_ranges": [
                [round(start, 2), round(end, 2)] for start, end in gutters
            ][:_MAX_SAMPLES],
        }

    # ------------------------------------------------------------
    # document summary
    # ------------------------------------------------------------

    def _document_summary(self, page_facts: list[dict[str, Any]]) -> dict[str, Any]:
        orientation_counts: dict[str, int] = {}
        for facts in page_facts:
            orientation_counts[facts["orientation"]] = (
                orientation_counts.get(facts["orientation"], 0) + 1
            )
        mixed_orientation = len(orientation_counts) > 1
        orientations = sorted(orientation_counts)

        box_mismatch_pages = [
            facts["page_number"] for facts in page_facts if facts.get("box_mismatches")
        ]
        overflow_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["overflow_text"]["count"] > 0
        ]
        skewed_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["skewed_lines"]["count"] > 0
        ]
        nonstandard_rotation_pages = [
            facts["page_number"]
            for facts in page_facts
            if int(facts.get("rotation", 0)) % 90 != 0
        ]
        overlapping_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["overlapping_text"]["count"] > 0
        ]
        margin_note_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["margin_note_clusters"]
        ]
        rotated_table_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["table_geometry"]["rotated_candidate_count"] > 0
        ]
        interleaved_table_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["table_geometry"]["interleaved_table_count"] > 0
        ]
        figure_between_column_pages = [
            facts["page_number"]
            for facts in page_facts
            if facts["figure_between_columns"]["count"] > 0
        ]

        summary: dict[str, Any] = {
            "page_count": len(page_facts),
            "orientations": orientations,
            "mixed_orientation": mixed_orientation,
            "landscape_pages": [
                facts["page_number"]
                for facts in page_facts
                if facts["orientation"] == "landscape"
            ],
            "box_mismatch_pages": box_mismatch_pages,
            "overflow_text_pages": overflow_pages,
            "skewed_text_pages": skewed_pages,
            "nonstandard_rotation_pages": nonstandard_rotation_pages,
            "overlapping_text_pages": overlapping_pages,
            "margin_note_pages": margin_note_pages,
            "rotated_table_pages": rotated_table_pages,
            "interleaved_table_pages": interleaved_table_pages,
            "figure_between_columns_pages": figure_between_column_pages,
            "pages": page_facts,
        }

        summary["cross_page_paragraph_candidates"] = (
            self._cross_page_paragraph_candidates(page_facts)
        )
        return summary

    @staticmethod
    def _cross_page_paragraph_candidates(
        page_facts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Report — never merge — paragraphs that likely continue across pages.

        A candidate is the last text block of page N whose text does not end
        in terminal punctuation, followed by a first block on page N+1 whose
        left edge is within a small tolerance of the previous block's left
        edge (same column position).  Reconstruction keeps the blocks
        separate; this evidence lets consumers know a merge was plausible.
        """
        candidates: list[dict[str, Any]] = []
        for current, following in zip(page_facts, page_facts[1:]):
            current_blocks = current.get("text_blocks") or []
            following_blocks = following.get("text_blocks") or []
            if not current_blocks or not following_blocks:
                continue
            last = current_blocks[-1]
            first = following_blocks[0]
            last_text = str(last.get("text", "")).strip()
            first_text = str(first.get("text", "")).strip()
            if not last_text or not first_text:
                continue
            if last_text[-1] in _TERMINAL_PUNCTUATION:
                continue
            last_x = float(last["bbox"][0])
            first_x = float(first["bbox"][0])
            if abs(last_x - first_x) > 12.0:
                continue
            if not first_text[0].islower() and not _CJK_TEXT_MATCH(first_text):
                continue
            candidates.append(
                {
                    "from_page": current["page_number"],
                    "to_page": following["page_number"],
                    "last_text": last_text[-_MAX_TEXT_CHARS:],
                    "first_text": first_text[:_MAX_TEXT_CHARS],
                    "left_edge_delta": round(abs(last_x - first_x), 2),
                }
            )
        return candidates


def _CJK_TEXT_MATCH(text: str) -> bool:
    """CJK continuation heuristic: CJK paragraphs rarely start with a capital."""
    return bool(text) and any("\u4e00" <= char <= "\u9fff" for char in text[:4])


__all__ = ["PageGeometryExtractor"]
