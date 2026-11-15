"""Figure-region evidence extraction (B11, gap register §6).

Records where vector figures (non-bitmap drawings) live on each page.
Clusters of page drawings are grouped into candidate vector-figure regions
and compared against image placements and detected tables — a region that
overlaps an image is decoration on top of a bitmap, a region that overlaps a
table is (mostly) the table's own rules, and only a region clear of both is a
standalone vector figure candidate.

Everything is evidence: the cluster geometry is recorded, never re-drawn or
promoted into the narrative body.
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

# A drawing cluster qualifies as a vector-figure-region candidate when it
# aggregates enough drawings and covers a non-trivial area.  The thresholds
# are intentionally conservative: page borders, single underlines and stray
# decoration lines stay below them.
_MIN_CLUSTER_DRAWINGS = 4
_MIN_CLUSTER_AREA_PT2 = 900.0
_CLUSTER_INFLATE_PT = 5.0

_MAX_PAGES = 500
_MAX_REGION_SAMPLES = 20


def _round1(value: float) -> float:
    return round(float(value), 1)


def _rects_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _inflate(
    rect: tuple[float, float, float, float], amount: float
) -> tuple[float, float, float, float]:
    return (rect[0] - amount, rect[1] - amount, rect[2] + amount, rect[3] + amount)


def _union(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


class FigureEvidenceExtractor:
    """Extract vector-figure-region evidence without interpreting content.

    ``merge_into_pages`` is a passthrough: like the B5 geometry evidence and
    the B8 character evidence, figure evidence lives under the namespaced
    metadata key ``pdf_figures`` and never mutates page blocks.
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
        """Extract vector-graphics region evidence."""
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
                extraction_method="figure_evidence",
                metadata={"pdf_figures": self._document_summary(page_facts)},
            )
        finally:
            document.close()

    def _page_facts(
        self,
        page: fitz.Page,
        page_number: int,
        table_finder: Any | None = None,
    ) -> dict[str, Any]:
        drawings = self._drawings(page)
        images = self._image_bboxes(page)
        tables = self._table_bboxes(page, page_number - 1, table_finder)
        regions = self._cluster_regions(drawings)
        vector_regions: list[dict[str, Any]] = []
        unclassified_cluster_count = 0
        for region in regions:
            if region["drawing_count"] < _MIN_CLUSTER_DRAWINGS:
                unclassified_cluster_count += 1
                continue
            if _area(region["bbox"]) < _MIN_CLUSTER_AREA_PT2:
                unclassified_cluster_count += 1
                continue
            overlaps_image = any(
                _rects_intersect(image, region["bbox"]) for image in images
            )
            overlaps_table = any(
                _rects_intersect(table, region["bbox"]) for table in tables
            )
            vector_regions.append(
                {
                    "page": page_number,
                    "bbox": [_round1(value) for value in region["bbox"]],
                    "drawing_count": region["drawing_count"],
                    "dashed_drawing_count": region["dashed_count"],
                    "overlaps_image": overlaps_image,
                    "overlaps_table": overlaps_table,
                    "standalone_vector_figure_candidate": not overlaps_image
                    and not overlaps_table,
                }
            )
        return {
            "page": page_number,
            "image_count": len(images),
            "drawing_count": len(drawings),
            "vector_regions": vector_regions,
            # Spec engineering principle: unknown_visual_content is never
            # force-classified — drawing clusters below the vector-figure
            # candidate threshold (underlines, page frames, stray ornaments)
            # are registered by count; content is not guessed.
            "unclassified_drawing_cluster_count": unclassified_cluster_count,
        }

    @staticmethod
    def _drawings(page: fitz.Page) -> list[dict[str, Any]]:
        try:
            facts = []
            for drawing in page.get_drawings():
                rect = drawing.get("rect")
                if rect is None:
                    continue
                dashes = str(drawing.get("dashes") or "")
                facts.append(
                    {
                        "rect": (
                            float(rect.x0),
                            float(rect.y0),
                            float(rect.x1),
                            float(rect.y1),
                        ),
                        "dashed": bool(dashes) and not dashes.startswith("[]"),
                    }
                )
            return facts
        except Exception:
            # Figure evidence is additive and never fatal.
            return []

    @staticmethod
    def _image_bboxes(page: fitz.Page) -> list[tuple[float, float, float, float]]:
        try:
            bboxes: list[tuple[float, float, float, float]] = []
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if bbox:
                    raw = tuple(float(value) for value in bbox)
                    bboxes.append((raw[0], raw[1], raw[2], raw[3]))
            return bboxes
        except Exception:
            return []

    @staticmethod
    def _table_bboxes(
        page: fitz.Page,
        page_index: int | None = None,
        table_finder: Any | None = None,
    ) -> list[tuple[float, float, float, float]]:
        try:
            if table_finder is not None and page_index is not None:
                finder = table_finder.finder_for(page_index)
            else:
                finder = page.find_tables()
            return [
                (
                    float(table.bbox[0]),
                    float(table.bbox[1]),
                    float(table.bbox[2]),
                    float(table.bbox[3]),
                )
                for table in finder.tables
            ]
        except Exception:
            return []

    @staticmethod
    def _cluster_regions(
        drawings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Union-find clustering of drawing rects into candidate regions.

        Two drawings belong to one cluster when their inflated bounding boxes
        intersect; the cluster bbox is the union of its members.  This is a
        geometric fact only — nothing is asserted about what the cluster
        depicts.
        """
        rects = [_inflate(item["rect"], _CLUSTER_INFLATE_PT) for item in drawings]
        parent = list(range(len(rects)))

        def find(node: int) -> int:
            """Union-find lookup with path compression."""
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if _rects_intersect(rects[i], rects[j]):
                    root_i, root_j = find(i), find(j)
                    if root_i != root_j:
                        parent[root_j] = root_i

        clusters: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(drawings):
            root = find(index)
            cluster = clusters.setdefault(
                root,
                {
                    "bbox": tuple(item["rect"]),
                    "drawing_count": 0,
                    "dashed_count": 0,
                },
            )
            cluster["bbox"] = _union(cluster["bbox"], tuple(item["rect"]))
            cluster["drawing_count"] += 1
            cluster["dashed_count"] += int(item["dashed"])
        return sorted(clusters.values(), key=lambda c: _area(c["bbox"]), reverse=True)

    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        figure_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Passthrough: figure evidence lives in metadata, never in pages."""
        return text_pages

    @staticmethod
    def _document_summary(page_facts: list[dict[str, Any]]) -> dict[str, Any]:
        regions: list[dict[str, Any]] = []
        image_count = 0
        drawing_count = 0
        unclassified_cluster_count = 0
        unclassified_pages: list[int] = []
        for facts in page_facts:
            image_count += facts["image_count"]
            drawing_count += facts["drawing_count"]
            regions.extend(facts["vector_regions"])
            page_unclassified = int(facts.get("unclassified_drawing_cluster_count", 0))
            unclassified_cluster_count += page_unclassified
            if page_unclassified:
                unclassified_pages.append(int(facts["page"]))
        return {
            "image_count": image_count,
            "drawing_count": drawing_count,
            "vector_region_count": len(regions),
            "standalone_vector_figure_count": sum(
                1 for region in regions if region["standalone_vector_figure_candidate"]
            ),
            "regions_overlapping_image_count": sum(
                1 for region in regions if region["overlaps_image"]
            ),
            "regions_overlapping_table_count": sum(
                1 for region in regions if region["overlaps_table"]
            ),
            "samples": regions[:_MAX_REGION_SAMPLES],
            # Spec engineering principle: unknown_visual_content is never force-classified.
            "unknown_visual_content": {
                "unclassified_drawing_cluster_count": unclassified_cluster_count,
                "pages": unclassified_pages[:_MAX_REGION_SAMPLES],
                "note": (
                    "drawing clusters below the vector-figure candidate "
                    "thresholds (underlines, rules, sparse decoration) are "
                    "counted, never guessed into a semantic category"
                ),
            },
            "note": (
                "vector-figure regions are drawing clusters recorded as "
                "evidence; overlap with images or tables is reported, never "
                "resolved, and vector content is never re-drawn or promoted "
                "into the narrative body"
            ),
        }


__all__ = ["FigureEvidenceExtractor"]
