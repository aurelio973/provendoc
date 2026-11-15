"""External PDF input-reliability context for narrative evaluation consumers.

This module deliberately does not calculate, adjust, or reinterpret any
narrative evaluation score.  A PDF is still represented as a narrative stream
and therefore uses the same invariant-based NarrativeQuality as TXT,
Markdown, HTML, and DOCX.

PDF extraction and layout facts are a separate concern: they explain how much
confidence a reader should place in the *input representation* that the
evaluation saw.  Keeping this payload outside the evaluation prevents
source-extraction failures from becoming an extra invariant or silently
changing ``invariant_composite``.
"""

from __future__ import annotations

from typing import Any

_QUALITY_FIELDS_UNCHANGED = (
    "invariant_version",
    "invariants",
    "invariant_composite",
    "issues",
    "metadata",
)


def build_pdf_quality_for_evaluation(
    *,
    quality: dict[str, Any],
    runtime: dict[str, Any],
    routing: dict[str, Any],
    reconstruction: dict[str, Any],
) -> dict[str, Any]:
    """Return PDF facts that contextualize, but never alter, the narrative evaluation.

    The returned payload contains no evaluation dimension, score, or
    optimization objective.  ``narrative_input_reliability`` is intentionally
    a qualitative status rather than a competing numerical metric: it reports
    whether the text representation that entered the narrative evaluation
    had extraction or fallback limitations requiring review.
    """
    extraction = dict(quality.get("extraction") or {})
    layout = dict(quality.get("layout") or {})
    requested_pages = _page_numbers(runtime.get("ocr_requested_pages"))
    used_pages = _page_numbers(runtime.get("ocr_used_pages"))
    unavailable_pages = _page_numbers(runtime.get("ocr_unavailable_pages"))
    reasons = _reliability_reasons(
        extraction=extraction,
        runtime=runtime,
        routing=routing,
        requested_pages=requested_pages,
        used_pages=used_pages,
        unavailable_pages=unavailable_pages,
    )

    return {
        "contract_version": "1.1",
        "scope": "external_pdf_narrative_input_context",
        "does_not_modify_narrative_evaluation": True,
        "quality_fields_unchanged": list(_QUALITY_FIELDS_UNCHANGED),
        "interpretation": (
            "Use this only to interpret the reliability of the PDF-derived "
            "narrative input. It is not part of the narrative evaluation, "
            "does not change invariant scores or invariant_composite, and "
            "must not be optimized as a chunking objective."
        ),
        "narrative_input_reliability": {
            "status": _reliability_status(
                extraction=extraction,
                requested_pages=requested_pages,
                used_pages=used_pages,
                unavailable_pages=unavailable_pages,
                runtime=runtime,
                routing=routing,
            ),
            "reasons": reasons,
        },
        "source_facts": {
            "extraction_method": extraction.get("extraction_method"),
            "page_count": extraction.get("page_count", 0),
            "empty_text_pages": extraction.get("empty_text_pages", 0),
            "empty_text_page_numbers": list(
                extraction.get("empty_text_page_numbers") or []
            ),
            "replacement_chars": extraction.get("replacement_chars", 0),
            "extraction_confidence": extraction.get("extraction_confidence"),
            "text_blocks_per_page": list(extraction.get("text_blocks_per_page") or []),
            "image_blocks_per_page": list(extraction.get("image_blocks_per_page") or []),
        },
        "layout_and_fallback": {
            "recommended_runtime": routing.get("recommended_runtime"),
            "actual_runtime": runtime.get("actual_runtime"),
            "reading_order_mode": layout.get("reading_order_mode"),
            "multicolumn_suspected": bool(layout.get("multicolumn_suspected")),
            "layout_status": runtime.get("layout_status"),
            "ocr_status": runtime.get("ocr_status"),
            "ocr_requested_pages": requested_pages,
            "ocr_attempted_pages": _page_numbers(runtime.get("ocr_attempted_pages")),
            "ocr_used_pages": used_pages,
            "ocr_unavailable_pages": unavailable_pages,
            # This is factual page-level provenance, not a quality score. It
            # makes mixed native/OCR and fallback decisions inspectable
            # without mixing extraction reliability into the narrative
            # evaluation.
            "ocr_page_routes": _page_routes(runtime.get("ocr_page_routes")),
        },
        "provenance_contract": {
            "immutable_source_evidence": True,
            "page_coordinates_preserved": True,
            "source_evidence_ids_preserved": True,
            "layout_overlay_is_derived": True,
        },
    }


def _page_numbers(value: object) -> list[int]:
    """Coerce runtime page lists to a stable, JSON-safe representation."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and item >= 1]


def _page_routes(value: object) -> list[dict[str, Any]]:
    """Expose only stable, JSON-safe per-page source-routing facts."""
    if not isinstance(value, list):
        return []
    routes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("page_number"), int):
            continue
        page_number = item["page_number"]
        if page_number < 1:
            continue
        routes.append(
            {
                "page_number": page_number,
                "native_text_chars": item.get("native_text_chars", 0),
                "native_text_usable": bool(item.get("native_text_usable")),
                "ocr_requested": bool(item.get("ocr_requested")),
                "ocr_attempted": bool(item.get("ocr_attempted")),
                "final_route": item.get("final_route"),
                "fallback_reason": item.get("fallback_reason"),
            }
        )
    return sorted(routes, key=lambda item: item["page_number"])


def _reliability_status(
    *,
    extraction: dict[str, Any],
    requested_pages: list[int],
    used_pages: list[int],
    unavailable_pages: list[int],
    runtime: dict[str, Any],
    routing: dict[str, Any],
) -> str:
    """Classify representation reliability without assigning an evaluation score."""
    if routing.get("recommended_runtime") == "guarded":
        # Encrypted or over-limit documents keep native evidence only; the
        # representation is knowingly degraded and deserves review. The guard
        # is the primary, deliberate degradation, so it outranks downstream
        # symptoms such as OCR-disabled pages caused by the guard itself.
        return "review_recommended"
    if extraction.get("empty_text_pages", 0) or unavailable_pages:
        return "limited"
    if runtime.get("ocr_empty_output_pages"):
        return "limited"
    if (
        used_pages
        or requested_pages
        or routing.get("recommended_runtime") == "layout"
        or runtime.get("layout_status") in {"unavailable", "failed", "empty_output"}
    ):
        return "review_recommended"
    if extraction.get("replacement_chars", 0):
        return "review_recommended"
    return "high"


def _reliability_reasons(
    *,
    extraction: dict[str, Any],
    runtime: dict[str, Any],
    routing: dict[str, Any],
    requested_pages: list[int],
    used_pages: list[int],
    unavailable_pages: list[int],
) -> list[str]:
    """Provide deterministic, human-readable context for the qualitative state."""
    reasons: list[str] = []
    if extraction.get("empty_text_pages", 0):
        reasons.append(
            "one or more pages have no recovered text evidence in the final representation"
        )
    if unavailable_pages:
        reasons.append(
            "OCR was required but unavailable or disabled for pages "
            + ", ".join(str(page) for page in unavailable_pages)
        )
    if runtime.get("ocr_empty_output_pages"):
        reasons.append(
            "OCR returned no usable text evidence for pages "
            + ", ".join(
                str(page) for page in _page_numbers(runtime.get("ocr_empty_output_pages"))
            )
        )
    elif used_pages:
        reasons.append(
            "OCR supplied final text evidence for pages "
            + ", ".join(str(page) for page in used_pages)
        )
    elif requested_pages:
        reasons.append(
            "OCR was requested for pages "
            + ", ".join(str(page) for page in requested_pages)
        )

    layout_status = runtime.get("layout_status")
    if routing.get("recommended_runtime") == "guarded":
        reasons.append(
            "resource guard engaged (encrypted or over-limit document); "
            "only native evidence was used"
        )
    if routing.get("recommended_runtime") == "layout" and layout_status != "used":
        reasons.append(
            "layout routing was recommended but the final representation uses the native/OCR fallback"
        )
    elif layout_status == "used":
        reasons.append("a provenance-bound layout overlay determined reading order")

    if extraction.get("replacement_chars", 0):
        reasons.append("replacement characters remain in reconstructed narrative text")
    if not reasons:
        reasons.append("native evidence produced a complete text representation without recorded fallback")
    return reasons
