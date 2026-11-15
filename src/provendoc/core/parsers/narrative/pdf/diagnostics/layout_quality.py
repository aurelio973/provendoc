"""Layout-level quality signals derived from reconstruction results."""

from __future__ import annotations

from collections import Counter


class LayoutQualityProbe:
    """Summarize reconstruction outcomes as plain layout signals."""

    def assess(
        self,
        *,
        blocks: list[dict],
        reading_order_info: dict,
        repetitive_info: dict,
        heading_info: dict,
        table_info: dict | None = None,
        figure_info: dict | None = None,
    ) -> dict:
        """Summarize reconstruction output into layout-level quality signals."""
        heading_levels = Counter(
            block.get("heading_level")
            for block in blocks
            if block.get("type") == "heading"
        )
        merged_paragraphs = sum(block.get("merged_from", 1) - 1 for block in blocks)
        table_info = table_info or {}
        figure_info = figure_info or {}

        return {
            "reading_order_mode": reading_order_info.get("reading_order"),
            "multicolumn_suspected": reading_order_info.get("multicolumn_suspected", False),
            "multicolumn_suspected_pages": reading_order_info.get(
                "multicolumn_suspected_pages", []
            ),
            "suppressed_artifact_count": repetitive_info.get("suppressed_block_count", 0),
            "heading_count_by_level": {
                str(level): count
                for level, count in sorted(
                    heading_levels.items(), key=lambda item: (item[0] is None, item[0])
                )
            },
            "heading_candidate_count": heading_info.get("heading_candidate_count", 0),
            "paragraph_merge_count": merged_paragraphs,
            "body_font_size": heading_info.get("body_font_size"),
            "table_count": table_info.get("table_count", 0),
            "continued_table_count": table_info.get("continued_table_count", 0),
            "figure_count": figure_info.get("figure_count", 0),
            "captioned_figure_count": figure_info.get("captioned_figure_count", 0),
            "visual_interpretation": figure_info.get("visual_interpretation", "not_run"),
        }
