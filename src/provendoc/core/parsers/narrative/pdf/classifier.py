"""Low-cost PDF probing and explainable runtime routing.

This module never performs OCR or layout reconstruction.  It only gathers the
cheap signals needed to decide which extraction runtime is appropriate.  The
Phase 1 pipeline always falls back to native extraction while recording when a
future OCR or layout runtime would be preferable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import PDFRoutingDecision, PDFRuntimeLimits


class PDFClassifier:
    """Classify a PDF using probe-level page evidence only."""

    MIN_NATIVE_TEXT_CHARS_PER_PAGE = 40
    SCANNED_TEXT_PAGE_RATIO = 0.15
    IMAGE_HEAVY_PAGE_RATIO = 0.50
    MULTI_COLUMN_MIN_BLOCKS = 12
    MULTI_COLUMN_GUTTER_RATIO = 0.12
    # B5: N-column clustering.  Left-edge anchors are split where the gap
    # exceeds this share of the page width; a cluster only counts as a
    # column with at least this many supporting blocks, and a cluster
    # wider than this share of the page is body text, not a column.
    COLUMN_CLUSTER_GAP_RATIO = 0.06
    COLUMN_CLUSTER_MIN_SUPPORT = 4
    COLUMN_CLUSTER_MAX_WIDTH_RATIO = 0.6
    # Registry #175 hole 1 (2026-11-05): full-page scan image + embedded text
    # layer = the product of some old OCR engine (an OCR sandwich). Pages
    # whose image-area share exceeds this threshold are marked scan_backed
    # — facts registered only; routing behaviour is decided by the pipeline.
    SCAN_BACKED_MIN_IMAGE_AREA_RATIO = 0.9

    def classify(
        self, file_path: str | Path, *, limits: "PDFRuntimeLimits | None" = None
    ) -> PDFRoutingDecision:
        """Return a route recommendation with the evidence behind it.

        The probe records encryption and file-size facts without judging them;
        resource-limit evaluation only happens when ``limits`` is provided.
        """
        path = Path(file_path)
        document = fitz.open(file_path)
        try:
            encrypted = bool(document.needs_pass)
            if encrypted:
                # An unauthenticated encrypted document refuses page loading
                # entirely; the entrance guard reports that fact instead of
                # probing pages we cannot read.
                page_signals: list[dict[str, Any]] = []
            else:
                page_signals = [
                    self._probe_page_safe(document[page_index], page_index + 1, encrypted)
                    for page_index in range(int(document.page_count))
                ]
        finally:
            document.close()

        try:
            file_size_bytes = path.stat().st_size
        except OSError:
            file_size_bytes = 0
        return self._build_decision(
            page_signals,
            encrypted=encrypted,
            file_size_bytes=file_size_bytes,
            limits=limits,
        )

    def _probe_page_safe(
        self, page: fitz.Page, page_number: int, encrypted: bool
    ) -> dict[str, Any]:
        """Probe one page, degrading honestly when content is inaccessible."""
        try:
            return self._probe_page(page, page_number)
        except Exception as error:
            # An unauthenticated encrypted document (or a damaged page) may
            # refuse text probing; record the refusal instead of guessing.
            return {
                "page_number": page_number,
                "text_chars": 0,
                "replacement_chars": 0,
                "block_count": 0,
                "image_count": 0,
                "native_text": False,
                "likely_multicolumn": False,
                "rotation": int(getattr(page, "rotation", 0) or 0),
                "width": float(getattr(page.rect, "width", 0.0) or 0.0),
                "height": float(getattr(page.rect, "height", 0.0) or 0.0),
                "probe_failed": str(error),
                "encrypted": encrypted,
                "image_area_ratio": 0.0,
                "scan_backed": False,
            }

    def _probe_page(self, page: fitz.Page, page_number: int) -> dict[str, Any]:
        text = (page.get_text("text") or "").strip()
        blocks = [block for block in page.get_text("blocks") if str(block[4]).strip()]
        image_count = len(page.get_images(full=True))
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        x_positions = [float(block[0]) for block in blocks]
        block_right_edges = [float(block[2]) for block in blocks]
        text_chars = len(text)
        # B8 follow-up: replacement characters (U+FFFD) mean the native
        # text layer carries glyph-to-Unicode failures (broken/missing
        # CMap).  "Zero native chars" is not the only unusable-text shape —
        # text that extracts as replacement soup routes to native_text
        # today; the ratio is recorded so the degradation is visible
        # instead of silently mislabelled as healthy text.
        replacement_chars = text.count("\ufffd")
        columns = self._detect_columns(
            x_positions=x_positions,
            right_edges=block_right_edges,
            page_width=page_width,
            block_count=len(blocks),
        )
        likely_multicolumn = columns["column_count"] >= 2

        # Registry #175 hole 1: full-page image coverage + extractable text
        # = an OCR sandwich. image_area_ratio records the area fact only
        # (bboxes clipped to the page rect), with no text condition folded
        # in — the semantics of "scan_backed plus a separate text layer"
        # are left to the pipeline's routing. Pure scan pages (no text) are
        # also marked True; they already take the native_text=False OCR
        # route, so nothing changes. Pages without images skip
        # get_image_info entirely — probe cost stays flat.
        image_area_ratio = 0.0
        if image_count > 0 and page_width > 0.0 and page_height > 0.0:
            page_area = page_width * page_height
            covered = 0.0
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if not bbox:
                    continue
                try:
                    x0, y0, x1, y1 = (float(value) for value in bbox)
                except (TypeError, ValueError):
                    continue
                if x1 > x0 and y1 > y0:
                    covered += max(0.0, min(x1, page_width) - max(x0, 0.0)) * max(
                        0.0, min(y1, page_height) - max(y0, 0.0)
                    )
            image_area_ratio = min(covered / page_area, 1.0)
        scan_backed = image_area_ratio >= self.SCAN_BACKED_MIN_IMAGE_AREA_RATIO

        return {
            "page_number": page_number,
            "text_chars": text_chars,
            "replacement_chars": replacement_chars,
            "block_count": len(blocks),
            "image_count": image_count,
            "native_text": text_chars >= self.MIN_NATIVE_TEXT_CHARS_PER_PAGE,
            "likely_multicolumn": likely_multicolumn,
            # B5: richer column structure evidence.  ``column_count`` is the
            # number of durable left-anchor clusters (1 = single column);
            # ``column_boundaries`` are the x split points between adjacent
            # clusters; ``irregular_columns`` flags strongly unequal widths.
            "column_count": columns["column_count"],
            "column_boundaries": columns["boundaries"],
            "column_widths": columns["widths"],
            "irregular_columns": columns["irregular"],
            "rotation": int(page.rotation),
            "width": page_width,
            "height": float(page.rect.height),
            # Registry #175 hole 1: image-coverage fact + scan_backed flag.
            # Routing unchanged (pipeline's call); this only provides evidence.
            "image_area_ratio": round(image_area_ratio, 4),
            "scan_backed": scan_backed,
        }

    def derive_column_signals(
        self,
        *,
        x_positions: list[float],
        right_edges: list[float],
        page_width: float,
        block_count: int,
    ) -> dict[str, Any]:
        """Registry #175 hole 3: derive the column structure from arbitrary block geometry.

        The probe's column evidence only reflects the native layer; probe
        signals on OCR-replaced pages come from an empty/broken layer with
        no column information. The pipeline calls this with the finally
        adopted OCR block geometry to re-derive the column structure, so
        ordering can be column-first on those pages too. The return value
        has the same shape as the probe signal's column_* keys
        (column_count/boundaries/widths/irregular).
        """
        return self._detect_columns(
            x_positions=x_positions,
            right_edges=right_edges,
            page_width=page_width,
            block_count=block_count,
        )

    def _build_decision(
        self,
        pages: list[dict[str, Any]],
        *,
        encrypted: bool = False,
        file_size_bytes: int = 0,
        limits: "PDFRuntimeLimits | None" = None,
    ) -> PDFRoutingDecision:
        page_count = len(pages)

        guard_reasons: list[str] = []
        if encrypted:
            guard_reasons.append(
                "document is password protected; text evidence cannot be recovered"
            )
        if limits is not None:
            guard_reasons.extend(
                limits.violated_by(
                    page_count=page_count,
                    file_size_bytes=file_size_bytes,
                )
            )
        if guard_reasons:
            # Guards outrank every routing heuristic (including the empty
            # document check): an encrypted or over-limit file must never be
            # labelled "empty" or "ocr" when the truthful state is "guarded".
            return PDFRoutingDecision(
                recommended_runtime="guarded",
                candidate_runtimes=("native_text",),
                signals={
                    "page_count": page_count,
                    "pages": pages,
                    "encrypted": encrypted,
                    "file_size_bytes": file_size_bytes,
                    "guard_reasons": guard_reasons,
                },
                confidence=1.0,
            )

        if not page_count:
            return PDFRoutingDecision(
                recommended_runtime="empty",
                candidate_runtimes=("native_text",),
                signals={"page_count": 0, "pages": []},
                confidence=1.0,
            )

        native_pages = sum(page["native_text"] for page in pages)
        image_heavy_pages = sum(page["image_count"] > 3 for page in pages)
        multicolumn_pages = sum(page["likely_multicolumn"] for page in pages)
        total_text_chars = sum(page["text_chars"] for page in pages)
        total_replacement_chars = sum(page.get("replacement_chars", 0) for page in pages)
        native_text_ratio = native_pages / page_count
        image_heavy_ratio = image_heavy_pages / page_count
        multicolumn_ratio = multicolumn_pages / page_count
        replacement_char_ratio = (
            total_replacement_chars / total_text_chars if total_text_chars else 0.0
        )
        # Text-quality degradation flag: routing stays unchanged (no
        # ground-truth corpus yet for garbled-native → OCR rerouting);
        # the honest signal is exposed for consumers and future routing
        # calibration.
        text_quality_degraded = replacement_char_ratio >= 0.3

        signals: dict[str, Any] = {
            "page_count": page_count,
            "native_text_pages": native_pages,
            "native_text_ratio": round(native_text_ratio, 3),
            "total_text_chars": total_text_chars,
            "avg_text_chars_per_page": round(total_text_chars / page_count, 2),
            "total_replacement_chars": total_replacement_chars,
            "replacement_char_ratio": round(replacement_char_ratio, 4),
            "text_quality_degraded": text_quality_degraded,
            "image_heavy_pages": image_heavy_pages,
            "image_heavy_ratio": round(image_heavy_ratio, 3),
            "multicolumn_pages": multicolumn_pages,
            "multicolumn_ratio": round(multicolumn_ratio, 3),
            "pages": pages,
            "encrypted": encrypted,
            "file_size_bytes": file_size_bytes,
        }

        if native_text_ratio <= self.SCANNED_TEXT_PAGE_RATIO:
            return PDFRoutingDecision(
                recommended_runtime="ocr",
                candidate_runtimes=("ocr", "native_text"),
                signals=signals,
                confidence=round(1.0 - native_text_ratio, 3),
            )

        if image_heavy_ratio >= self.IMAGE_HEAVY_PAGE_RATIO or multicolumn_pages:
            return PDFRoutingDecision(
                recommended_runtime="layout",
                candidate_runtimes=("layout", "native_text", "ocr"),
                signals=signals,
                confidence=round(max(image_heavy_ratio, multicolumn_ratio, 0.6), 3),
            )

        return PDFRoutingDecision(
            recommended_runtime="native_text",
            candidate_runtimes=("native_text", "layout", "ocr"),
            signals=signals,
            confidence=round(min(0.99, 0.65 + native_text_ratio * 0.3), 3),
        )

    def _likely_multicolumn(
        self,
        *,
        x_positions: list[float],
        page_width: float,
        block_count: int,
    ) -> bool:
        """Detect two durable x-position clusters separated by a central gutter.

        Retained for backward compatibility with earlier probe consumers; the
        richer :meth:`_detect_columns` drives the same flag today.
        """
        if block_count < self.MULTI_COLUMN_MIN_BLOCKS or page_width <= 0:
            return False

        middle = page_width / 2
        gutter = page_width * self.MULTI_COLUMN_GUTTER_RATIO
        left = sum(x < middle - gutter for x in x_positions)
        right = sum(x >= middle for x in x_positions)
        return left >= 4 and right >= 4

    def _detect_columns(
        self,
        *,
        x_positions: list[float],
        right_edges: list[float],
        page_width: float,
        block_count: int,
    ) -> dict[str, Any]:
        """Cluster text-block left anchors into durable columns.

        Returns the column count, the x split points between adjacent
        columns, the per-column widths, and whether the widths are strongly
        unequal (irregular layout).  Clusters need durable support
        (>= COLUMN_CLUSTER_MIN_BLOCKS anchors), a real gap to their
        neighbour (>= COLUMN_CLUSTER_GAP_RATIO of the page width), and a
        sane column width (<= COLUMN_CLUSTER_MAX_WIDTH_RATIO of the page).
        Single-column pages degrade to ``column_count == 1`` with no
        boundaries, never a fabricated structure.
        """
        if block_count < self.MULTI_COLUMN_MIN_BLOCKS or page_width <= 0:
            return {"column_count": 1, "boundaries": [], "widths": [], "irregular": False}

        gap_threshold = page_width * self.COLUMN_CLUSTER_GAP_RATIO
        max_width = page_width * self.COLUMN_CLUSTER_MAX_WIDTH_RATIO
        anchors = sorted(x_positions)

        clusters: list[list[float]] = [[anchors[0]]]
        for anchor in anchors[1:]:
            if anchor - clusters[-1][-1] > gap_threshold:
                clusters.append([anchor])
            else:
                clusters[-1].append(anchor)

        columns: list[tuple[float, float]] = []
        for cluster in clusters:
            if len(cluster) < self.COLUMN_CLUSTER_MIN_SUPPORT:
                continue
            left = min(cluster)
            right = max(
                (
                    edge
                    for anchor, edge in zip(x_positions, right_edges)
                    if anchor in cluster
                ),
                default=left,
            )
            if right - left > max_width:
                # A cluster spanning most of the page is body text whose
                # anchors merely vary; it is not a narrow column.
                continue
            columns.append((left, right))

        if len(columns) <= 1:
            return {"column_count": 1, "boundaries": [], "widths": [], "irregular": False}

        columns.sort()
        boundaries = [
            round((left_edge + previous_right) / 2.0, 2)
            for (previous_right, _), (_, left_edge) in zip(columns, columns[1:])
        ]
        widths = [round(right - left, 2) for left, right in columns]
        irregular = len(widths) >= 2 and (max(widths) / max(min(widths), 1e-6)) > 1.8
        return {
            "column_count": len(columns),
            "boundaries": boundaries,
            "widths": widths,
            "irregular": irregular,
        }
