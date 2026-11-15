"""Figure reconstruction with source-text chart descriptors and optional vision enrichment.

B11 (gap register §6) adds evidence-only figure-depth facts: captions are
searched on adjacent pages when none sits on the figure's own page (the
offset is recorded, the caption text is never moved), a caption shared by
several figures is reported as such, legends that live outside the figure
region are marked, and the traceability of chart data is stated honestly —
native numeric tokens are counted, but a chart whose pixels carry the data
still requires a visual backend.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

_CAPTION_RE = re.compile(r"^(?:figure|fig\.?|chart|图|表)\s*\d*[:.：\-]", re.IGNORECASE)
_LEGEND_RE = re.compile(r"^(?:legend|图例)\s*[:：]?\s*(.*)$", re.IGNORECASE)
_AXIS_RE = re.compile(r"^(?:x|y)[\s-]?axis\s*[:：]?\s*(.*)$|^[xy]轴\s*[:：]?\s*(.*)$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:%|[a-zA-Z]+)?")


class VisualFigureEnricher(Protocol):
    """Optional connector contract for a visual chart/layout model.

    A backend receives the complete source PDF and the immutable image-region
    evidence. It must return only an observation payload; this reconstructor
    binds that payload to its source image and caption evidence before exposing
    it to retrieval.
    """

    def availability(self) -> dict[str, object]:
        """Return visual-backend availability."""
        ...

    def enrich(self, file_path: str | Path, figure: dict[str, Any]) -> dict[str, Any]:
        """Return the observation payload for a single figure."""
        ...


class FigureReconstructor:
    """Create searchable, provenance-preserving figure and chart descriptors.

    Native text inside an image region is retained as source text, never treated
    as pixel interpretation. A configured visual backend may add a separately
    labelled enrichment that remains tied to the original image evidence ID.
    """

    def __init__(self, visual_enricher: VisualFigureEnricher | None = None):
        """Inject an optional visual-enhancement backend."""
        self.visual_enricher = visual_enricher

    def reconstruct(
        self,
        blocks: list[dict[str, Any]],
        *,
        file_path: str | Path | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Generate searchable figure/chart description blocks."""
        result: list[dict[str, Any]] = []
        figure_count = 0
        captioned_count = 0
        cross_page_caption_count = 0
        legend_outside_count = 0
        native_numeric_figure_count = 0
        visual_used_count = 0
        visual_unavailable_reasons: list[str] = []

        figures: list[dict[str, Any]] = []
        # Position of each figure in the original block order; figures are
        # re-inserted at their source position so the reconstruction never
        # reorders the document.
        pending: list[tuple[int, dict[str, Any]]] = []
        for index, block in enumerate(blocks):
            if block.get("type") != "image":
                result.append(block)
                continue
            figure = self._make_figure(block, blocks, index)
            figures.append(figure)
            pending.append((len(result), figure))

        self._annotate_shared_captions(figures)

        # Each earlier insert shifts every later snapshot position by one, so
        # compensate with the running insert count to keep figures at their
        # source position.
        for inserted, (position, figure) in enumerate(pending):
            visual = self._run_visual_enrichment(figure, file_path)
            figure = self._attach_visual_enrichment(figure, visual)
            figure_count += 1
            payload = figure.get("raw_payload") or {}
            captioned_count += int(bool(figure.get("caption")))
            cross_page_caption_count += int(
                bool(payload.get("caption_page_offset"))
            )
            description = payload.get("chart_textual_description") or {}
            legend_outside_count += int(
                bool(description.get("legend_outside_figure_region"))
            )
            traceability = payload.get("data_traceability") or {}
            native_numeric_figure_count += int(
                traceability.get("native_numeric_token_count", 0) > 0
            )
            visual_used_count += int(visual["status"] == "used")
            if visual["status"] == "unavailable" and visual.get("reason"):
                visual_unavailable_reasons.append(str(visual["reason"]))
            result.insert(position + inserted, figure)

        visual_status = "used" if visual_used_count else "not_run"
        if self.visual_enricher is not None and figure_count and not visual_used_count:
            visual_status = "unavailable"
        return result, {
            "figure_count": figure_count,
            "captioned_figure_count": captioned_count,
            "uncaptioned_figure_count": figure_count - captioned_count,
            "cross_page_caption_count": cross_page_caption_count,
            "shared_caption_figure_count": sum(
                1
                for figure in figures
                if (figure.get("raw_payload") or {}).get("shared_caption")
            ),
            "legend_outside_figure_count": legend_outside_count,
            "figures_with_native_numeric_tokens": native_numeric_figure_count,
            "visual_interpretation": visual_status,
            "visual_enriched_figure_count": visual_used_count,
            "visual_backend": self._visual_backend_metadata(
                status=visual_status,
                unavailable_reasons=visual_unavailable_reasons,
            ),
            "note": (
                "caption lookups extend to adjacent pages (the page offset is "
                "recorded, the caption never moves), shared captions, external "
                "legends and native numeric tokens are recorded as evidence; "
                "chart pixels that carry data still require a visual backend"
            ),
        }

    def _make_figure(
        self,
        image: dict[str, Any],
        blocks: list[dict[str, Any]],
        image_index: int,
    ) -> dict[str, Any]:
        caption_block, caption_page_offset = self._nearest_caption(
            image, blocks, image_index
        )
        caption = caption_block["text"] if caption_block else None
        textual_evidence = self._nearby_native_text(image, blocks, caption_block)
        page = image.get("page")
        descriptor = self._descriptor(
            page, caption, textual_evidence, caption_page_offset
        )

        raw_payload = dict(image.get("raw_payload") or {})
        raw_payload.update(
            {
                "caption": caption,
                "caption_evidence_ids": self._evidence_ids(caption_block),
                "caption_page": (
                    caption_block.get("page") if caption_block else None
                ),
                "caption_page_offset": caption_page_offset if caption_block else None,
                "textual_evidence": textual_evidence,
                "chart_textual_description": self._chart_textual_description(caption, textual_evidence),
                "data_traceability": self._data_traceability(textual_evidence),
                "visual_interpretation": "not_run",
                "requires_visual_backend_for_unread_chart_values": True,
            }
        )
        return {
            **image,
            "type": "figure",
            "text": descriptor,
            "raw_payload": raw_payload,
            "caption": caption,
        }

    @staticmethod
    def _descriptor(
        page: int | None,
        caption: str | None,
        textual_evidence: list[dict[str, Any]],
        caption_page_offset: int | None = None,
    ) -> str:
        descriptor = f"[Figure on page {page}]"
        if caption:
            descriptor += f" Caption: {caption}"
            if caption_page_offset and isinstance(page, int):
                # The caption lives on a neighbouring page; the offset is
                # stated where the figure is described, the caption itself
                # is never moved. (pageless image + offset never co-occur:
                # caption lookup bails on pageless images, guard is belt
                # and braces.)
                descriptor += f" (caption on page {page + caption_page_offset})"
        if textual_evidence:
            source_text = "; ".join(item["text"] for item in textual_evidence[:6])
            descriptor += f" Native chart text: {source_text}"
        if not caption and not textual_evidence:
            descriptor += " Image region recorded; visual interpretation is not available."
        return descriptor

    def _nearest_caption(
        self,
        image: dict[str, Any],
        blocks: list[dict[str, Any]],
        image_index: int,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """Find the closest caption block and report its page offset.

        Same-page captions keep the original geometric scoring.  Only when
        the figure's own page has no caption at all does the lookup extend to
        the bottom of the previous page and the top of the next one (B11) —
        the offset is recorded and the caption text stays where it is.
        """
        page = image.get("page")
        if not isinstance(page, int):
            # Figure blocks without a page number cannot do ±1-page association; give up honestly
            return None, None
        bbox = image.get("bbox") or [0, 0, 0, 0]
        candidates: list[tuple[float, dict[str, Any]]] = []
        for block in blocks:
            if block.get("page") != page or block.get("type") not in {"text", "heading"}:
                continue
            text = str(block.get("text", "")).strip()
            if not text or not _CAPTION_RE.match(text):
                continue
            candidate_bbox = block.get("bbox") or [0, 0, 0, 0]
            vertical_distance = min(
                abs(candidate_bbox[1] - bbox[3]),
                abs(bbox[1] - candidate_bbox[3]),
            )
            horizontal_distance = abs(
                ((candidate_bbox[0] + candidate_bbox[2]) / 2)
                - ((bbox[0] + bbox[2]) / 2)
            )
            candidates.append((vertical_distance + horizontal_distance * 0.25, block))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1], 0

        # Caption stranded at the bottom of the previous page.
        for candidate in reversed(blocks[max(0, image_index - 10):image_index]):
            if candidate.get("page") != page - 1:
                continue
            if candidate.get("type") not in {"text", "heading"}:
                continue
            text = str(candidate.get("text", "")).strip()
            if text and _CAPTION_RE.match(text):
                return candidate, -1

        # Caption pushed to the top of the next page (caption-below style).
        for candidate in blocks[image_index + 1 : image_index + 6]:
            if candidate.get("page") != page + 1:
                continue
            if candidate.get("type") not in {"text", "heading"}:
                continue
            text = str(candidate.get("text", "")).strip()
            if text and _CAPTION_RE.match(text):
                return candidate, 1
        return None, None

    @staticmethod
    def _annotate_shared_captions(figures: list[dict[str, Any]]) -> None:
        """Mark figures that were associated with one and the same caption.

        Two charts drawn under a single "Figure 1: ..." heading are a common
        authoring pattern; each figure keeps its own caption evidence while
        the shared fact is recorded — the parser never merges the figures.
        """
        by_caption: dict[str, list[dict[str, Any]]] = {}
        for figure in figures:
            payload = figure.get("raw_payload") or {}
            key = ",".join(payload.get("caption_evidence_ids") or [])
            if not key:
                continue
            by_caption.setdefault(key, []).append(figure)
        for group in by_caption.values():
            if len(group) < 2:
                continue
            for figure in group:
                payload = figure["raw_payload"]
                payload["shared_caption"] = {
                    "figure_count": len(group),
                    "other_figure_evidence_ids": [
                        evidence_id
                        for other in group
                        if other is not figure
                        for evidence_id in [other.get("evidence_id")]
                        if evidence_id
                    ],
                }

    @classmethod
    def _nearby_native_text(
        cls,
        image: dict[str, Any],
        blocks: list[dict[str, Any]],
        caption_block: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        image_bbox = image.get("bbox") or [0, 0, 0, 0]
        page = image.get("page")
        caption_ids = set(cls._evidence_ids(caption_block))
        evidence: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("page") != page or block.get("type") not in {"text", "heading"}:
                continue
            if set(cls._evidence_ids(block)) & caption_ids:
                continue
            text = str(block.get("text", "")).strip()
            bbox = block.get("bbox") or [0, 0, 0, 0]
            relation = cls._text_relation(image_bbox, bbox)
            if not text or relation is None:
                continue
            evidence.append(
                {
                    "text": text,
                    "bbox": list(bbox),
                    "evidence_ids": cls._evidence_ids(block),
                    "relation": relation,
                    "source_kind": block.get("source_kind"),
                    "numeric_tokens": _NUMBER_RE.findall(text),
                }
            )
        return evidence

    @staticmethod
    def _text_relation(image_bbox: list[float], text_bbox: list[float]) -> str | None:
        ix0, iy0, ix1, iy1 = (float(value) for value in image_bbox)
        tx0, ty0, tx1, ty1 = (float(value) for value in text_bbox)
        overlap_x = max(0.0, min(ix1, tx1) - max(ix0, tx0))
        overlap_y = max(0.0, min(iy1, ty1) - max(iy0, ty0))
        text_area = max(0.0, tx1 - tx0) * max(0.0, ty1 - ty0)
        if text_area > 0 and overlap_x * overlap_y / text_area >= 0.7:
            return "inside_figure_region"
        if tx1 >= ix0 - 36 and tx0 <= ix1 + 36 and ty1 >= iy0 - 36 and ty0 <= iy1 + 36:
            return "adjacent_to_figure_region"
        return None

    @staticmethod
    def _evidence_ids(block: dict[str, Any] | None) -> list[str]:
        if not block:
            return []
        values = block.get("evidence_ids") or [block.get("evidence_id")]
        return [str(value) for value in values if value]

    @classmethod
    def _chart_textual_description(
        cls,
        caption: str | None,
        textual_evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        legend_text: list[dict[str, Any]] = []
        axis_labels: list[dict[str, Any]] = []
        key_values: list[dict[str, Any]] = []
        for item in textual_evidence:
            text = item["text"]
            legend_match = _LEGEND_RE.match(text)
            axis_match = _AXIS_RE.match(text)
            if legend_match:
                legend_text.append({**item, "text": legend_match.group(1) or text})
            if axis_match:
                axis_labels.append({**item, "text": next(value for value in axis_match.groups() if value is not None) or text})
            if item["numeric_tokens"]:
                key_values.append(
                    {
                        "tokens": item["numeric_tokens"],
                        "text": text,
                        "evidence_ids": item["evidence_ids"],
                        "bbox": item["bbox"],
                        "source": "native_text_layer",
                    }
                )
        return {
            "title": caption,
            "title_evidence_ids": [],
            "legend_text": legend_text,
            "legend_outside_figure_region": any(
                item.get("relation") == "adjacent_to_figure_region"
                for item in legend_text
            ),
            "axis_labels": axis_labels,
            "key_values": key_values,
            "unread_visual_content_requires_backend": True,
        }

    @staticmethod
    def _data_traceability(textual_evidence: list[dict[str, Any]]) -> dict[str, Any]:
        """State honestly how far chart data can be traced back.

        Native numeric tokens inside or next to the figure region make the
        data partially traceable through the text layer; anything the pixels
        still carry (bar heights, point positions) requires a visual backend.
        The tokens are counted, never recalculated into series.
        """
        token_count = sum(
            len(item.get("numeric_tokens") or []) for item in textual_evidence
        )
        return {
            "native_numeric_token_count": token_count,
            "status": (
                "native_text_partial" if token_count else "visual_backend_required"
            ),
            "note": (
                "numeric tokens are counted as evidence; chart values drawn "
                "only as pixels remain unread without a visual backend"
            ),
        }

    def _run_visual_enrichment(
        self,
        figure: dict[str, Any],
        file_path: str | Path | None,
    ) -> dict[str, Any]:
        if self.visual_enricher is None:
            return {"status": "not_configured"}
        if file_path is None:
            return {"status": "unavailable", "reason": "source PDF path was not provided"}
        availability = self.visual_enricher.availability()
        if not availability.get("available", False):
            return {
                "status": "unavailable",
                "backend": availability,
                "reason": availability.get("reason", "visual backend is unavailable"),
            }
        try:
            output = self.visual_enricher.enrich(file_path, figure)
        except Exception as error:  # Visual enrichment must not make PDF parsing fail.
            return {"status": "unavailable", "backend": availability, "reason": str(error)}
        if not isinstance(output, dict):
            return {
                "status": "unavailable",
                "backend": availability,
                "reason": "visual backend returned a non-mapping result",
            }
        return {"status": "used", "backend": availability, "output": output}

    def _attach_visual_enrichment(self, figure: dict[str, Any], visual: dict[str, Any]) -> dict[str, Any]:
        raw_payload = dict(figure.get("raw_payload") or {})
        if visual["status"] != "used":
            raw_payload["visual_enrichment"] = visual
            return {**figure, "raw_payload": raw_payload}

        output = dict(visual["output"])
        visual_payload = {
            "status": "used",
            "backend": visual["backend"],
            "source_image_evidence_ids": self._evidence_ids(figure),
            "source_caption_evidence_ids": raw_payload.get("caption_evidence_ids", []),
            "source_image_bbox": figure.get("bbox"),
            "output": output,
        }
        raw_payload["visual_enrichment"] = visual_payload
        raw_payload["visual_interpretation"] = "backend_run"
        descriptor = figure.get("text", "")
        if output.get("description"):
            descriptor += f" Visual analysis: {output['description']}"
        return {**figure, "text": descriptor, "raw_payload": raw_payload}

    def _visual_backend_metadata(self, *, status: str, unavailable_reasons: list[str]) -> dict[str, Any]:
        if self.visual_enricher is None:
            return {"status": "not_configured"}
        availability = self.visual_enricher.availability()
        return {
            "status": status,
            "backend": availability,
            "unavailable_reasons": list(dict.fromkeys(unavailable_reasons)),
        }
