"""Footnote separation from body text for native and OCR evidence.

Scanned books frequently OCR page-bottom footnotes straight into the body
stream (e.g. circled markers misread as ``O``). This reconstructor tags
bottom-zone blocks that start with a footnote marker, plus continuation
blocks on the same page, as ``type="footnote"``. Evidence is preserved:
only the block role changes, so footnote text stays auditable in
``blocks`` metadata while leaving the narrative body stream.
"""

from __future__ import annotations

import re
from collections import Counter

# Marker patterns expected at the start of a footnote line.  Each entry is
# (kind, compiled regex).  A block only needs one of these to be a marker
# candidate; geometry gates keep reference lists mid-page out of scope.
_FOOTNOTE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("circled", re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")),
    ("bracket", re.compile(r"^\[\d{1,3}\]")),
    ("paren", re.compile(r"^[（(]\s*\d{1,3}\s*[)）]")),
    ("note_label", re.compile(r"^[注註]\s*[：:]")),
    ("asterisk", re.compile(r"^[*＊]{1,3}\s*\S")),
)

# OCR frequently misreads circled numbers as a bare Latin letter.  Accept the
# variant only when enough real content follows, and only inside the bottom
# zone (enforced by the caller-side geometric gate).
_OCR_CIRCLED_MARKER = re.compile(r"^[O0o〇○]\s+\S{4,}")

# Continued footnote lines (wrapped second/third lines) start without any
# marker at all; they inherit the footnote role from the preceding block.


class FootnoteSeparator:
    """Conservative page-bottom footnote tagging over classified blocks."""

    def __init__(
        self,
        bottom_ratio: float = 0.78,
        body_font_ratio: float = 0.85,
    ):
        """Configure the page-bottom area ratio and the font-size threshold."""
        self.bottom_ratio = bottom_ratio
        self.body_font_ratio = body_font_ratio

    def separate(
        self,
        blocks: list[dict],
        page_heights: dict[int, float] | None = None,
    ) -> tuple[list[dict], dict]:
        """Tag footnote blocks in place and report detection statistics.

        Only ``type == "text"`` blocks are considered; headings, tables, and
        figures are never reclassified.  A block becomes a footnote when it
        sits in the page-bottom zone and starts with a footnote marker, or
        when it continues an already-tagged footnote on the same page.
        """
        page_heights = page_heights or {}
        marker_counts: Counter[str] = Counter()
        by_source_kind: Counter[str] = Counter()
        continuation_count = 0
        bottom_zone_candidates = 0

        # Track the most recent text block per page so wrapped footnote
        # lines without markers can inherit the footnote role.
        last_footnote_by_page: dict[int, bool] = {}

        for block in blocks:
            page = block.get("page")
            if not isinstance(page, int):
                # Blocks without a page number do not join bottom-area detection or footnote inheritance
                continue
            if block.get("type") != "text" or not block.get("text"):
                last_footnote_by_page[page] = False
                continue

            page_height = page_heights.get(page)
            bbox = block.get("bbox") or []
            in_bottom_zone = bool(
                page_height
                and len(bbox) >= 4
                and bbox[1] >= self.bottom_ratio * page_height
            )
            if in_bottom_zone:
                bottom_zone_candidates += 1

            text = str(block["text"]).strip()
            marker_kind = self._marker_kind(text, in_bottom_zone)

            if in_bottom_zone and marker_kind:
                self._tag(block, marker_kind, confidence=0.9)
                marker_counts[marker_kind] += 1
                by_source_kind[block.get("source_kind") or "unknown"] += 1
                last_footnote_by_page[page] = True
                continue

            # Continuation: a wrapped footnote line inside the bottom zone
            # on a page whose previous text block was already a footnote.
            if in_bottom_zone and last_footnote_by_page.get(page):
                self._tag(block, "continuation", confidence=0.65)
                continuation_count += 1
                by_source_kind[block.get("source_kind") or "unknown"] += 1
                last_footnote_by_page[page] = True
                continue

            last_footnote_by_page[page] = False

        footnote_count = sum(marker_counts.values()) + continuation_count
        info = {
            "footnote_count": footnote_count,
            "marker_counts": dict(marker_counts),
            "continuation_count": continuation_count,
            "bottom_zone_candidate_count": bottom_zone_candidates,
            "by_source_kind": dict(by_source_kind),
        }
        return blocks, info

    def _marker_kind(self, text: str, in_bottom_zone: bool) -> str | None:
        for kind, pattern in _FOOTNOTE_MARKERS:
            if pattern.match(text):
                return kind
        if in_bottom_zone and _OCR_CIRCLED_MARKER.match(text):
            return "circled_ocr"
        return None

    @staticmethod
    def _tag(block: dict, marker_kind: str, *, confidence: float) -> None:
        block["type"] = "footnote"
        block["footnote_marker"] = marker_kind
        block["confidence"] = round(
            max(float(block.get("confidence", 0.0)), confidence), 3
        )
