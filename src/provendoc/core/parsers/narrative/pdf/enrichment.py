"""Contracts and registry for optional PDF layout and visual enrichment.

This module intentionally contains no MinerU or vision-model dependency.  It
makes the integration boundary explicit so future backends can be enabled only
when they preserve page coordinates and source-evidence links required by the
PDF representation contract.
"""

from __future__ import annotations

from provendoc.core.parsers.narrative.pdf.contracts import PDFEnrichmentBackend

LAYOUT_BACKEND_CONTRACT = PDFEnrichmentBackend(
    name="layout_backend",
    version="unconfigured",
    kind="layout",
    supported_block_types=("text", "table", "figure", "formula"),
)

VISUAL_BACKEND_CONTRACT = PDFEnrichmentBackend(
    name="visual_backend",
    version="unconfigured",
    kind="visual",
    supported_block_types=("figure", "chart", "table"),
)


def unconfigured_backend_metadata() -> dict[str, dict]:
    """Expose integration requirements without implying a backend was invoked."""
    return {
        "layout": {
            **LAYOUT_BACKEND_CONTRACT.as_dict(),
            "status": "not_configured",
            "required_outputs": [
                "page_number",
                "bbox",
                "block_type",
                "text_or_structured_content",
                "confidence",
                "source_evidence_ids",
            ],
            "intended_uses": [
                "multi-column reading order",
                "image-text layout",
                "scanned and borderless tables",
                "formula and caption regions",
            ],
        },
        "visual": {
            **VISUAL_BACKEND_CONTRACT.as_dict(),
            "status": "not_configured",
            "required_outputs": [
                "page_number",
                "image_bbox",
                "caption_evidence_ids",
                "legend_text",
                "axis_labels",
                "key_values",
                "confidence",
            ],
            "intended_uses": [
                "chart legend extraction",
                "axis and key-value recognition",
                "chart searchable description",
            ],
        },
    }
