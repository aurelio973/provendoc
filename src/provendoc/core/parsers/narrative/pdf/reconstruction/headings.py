"""Heading recovery derived from native PDF evidence."""

from __future__ import annotations

import re
from collections import Counter
from typing import cast

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock

_NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*|[IVXLC]+|[一二三四五六七八九十]+)[.)、\s]+")


class HeadingReconstructor:
    """Recover conservative heading candidates from captured span evidence."""

    def classify(self, blocks: list[PDFBlock]) -> tuple[list[dict], dict]:
        """Recover conservative heading candidates from span evidence."""
        body_size = self._body_font_size(blocks)
        recovered: list[dict] = []
        heading_sizes: list[float] = []

        for block in blocks:
            font_size = self._block_font_size(block)
            text = block.text.strip()
            is_heading, confidence = self._is_heading(text, font_size, body_size)
            role = "heading" if is_heading else "text"
            if is_heading:
                heading_sizes.append(font_size)
            item = {
                "text": text,
                "type": role,
                "page": block.page_number,
                "bbox": list(block.bbox),
                "source_kind": block.source_kind,
                "confidence": round(confidence, 3),
                "font_size": font_size,
                "evidence_id": block.evidence_id,
                "raw_payload_ref": block.raw_payload_ref,
            }
            source_evidence_ids = (block.raw_payload or {}).get("source_evidence_ids")
            if isinstance(source_evidence_ids, list):
                item["source_evidence_ids"] = list(source_evidence_ids)
            recovered.append(item)

        levels = self._heading_levels(heading_sizes)
        for item in recovered:
            if item["type"] == "heading":
                item["heading_level"] = levels.get(
                    float(cast("float", item["font_size"])), 1
                )

        return recovered, {
            "body_font_size": body_size,
            "heading_candidate_count": sum(item["type"] == "heading" for item in recovered),
        }

    def _is_heading(self, text: str, font_size: float, body_size: float) -> tuple[bool, float]:
        if not text or len(text) > 180:
            return False, 0.0

        numbered = bool(_NUMBERED_HEADING_RE.match(text))
        no_terminal_punctuation = not text.endswith((".", "。", "?", "？", "!", "！", ";", "；"))
        short = len(text) <= 100
        size_lift = font_size >= body_size * 1.15 if body_size else False

        if numbered and short:
            return True, 0.9 if size_lift else 0.72
        if size_lift and short and no_terminal_punctuation:
            return True, 0.82
        return False, 0.0

    def _body_font_size(self, blocks: list[PDFBlock]) -> float:
        sizes = [self._block_font_size(block) for block in blocks if self._block_font_size(block) > 0]
        if not sizes:
            return 0.0
        counts = Counter(round(size, 1) for size in sizes)
        return max(counts, key=lambda size: counts[size])

    @staticmethod
    def _block_font_size(block: PDFBlock) -> float:
        spans = (block.raw_payload or {}).get("spans", [])
        sizes = [float(span["size"]) for span in spans if float(span.get("size", 0)) > 0]
        if not sizes:
            return 0.0
        return max(sizes)

    @staticmethod
    def _heading_levels(sizes: list[float]) -> dict[float, int]:
        distinct_sizes: list[float] = []
        for size in sorted(set(sizes), reverse=True):
            if not distinct_sizes or abs(distinct_sizes[-1] - size) >= 0.5:
                distinct_sizes.append(size)
        return {size: index + 1 for index, size in enumerate(distinct_sizes)}
