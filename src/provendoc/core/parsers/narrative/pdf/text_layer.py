"""Per-page text-layer facts: status taxonomy, layer conflicts, quality panel.

Engineering principles (spec §Engineering Principles):

1. When native / OCR / hidden text layers coexist, compare first, then
   register (``TEXT_LAYER_CONFLICT``) — never silently overwrite. The
   native evidence of replaced pages is preserved by the pipeline in
   ``pdf_replaced_native_pages``; this module only produces the fact of
   "the comparison result".
2. Every page is classified as ``text_layer_absent`` / ``valid`` /
   ``corrupted`` / ``duplicate`` / ``low_quality`` (the spec's five states);
   this module additionally registers ``conflict`` as a sixth evidential
   state — when native text exists, OCR was adopted, and the two layers
   disagree, none of the five states can honestly describe the page;
   the disagreement itself is the fact.
3. Recovery triggering must be explainable: ``native_text_quality`` lays
   out the input metrics of every routing/replacement decision per page
   (character counts, replacement ratio, usability, hidden layer, conflict
   classification), each status carries its reasons, so consumers can
   replay every downgrade decision.

The comparison method is the token multiset intersection divided by the
smaller layer's token count (``duplicate_share``). It is a geometric
measure, not a semantic judgement: a high share means the two layers
coincide on the surface; a low share means they disagree; which layer is
closer to ground truth is not concluded here.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# The spec's five states + the evidential state conflict.
TEXT_LAYER_STATUSES = (
    "absent",
    "valid",
    "low_quality",
    "corrupted",
    "duplicate",
    "conflict",
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# duplicate_share thresholds: >= 0.8 means the layers are duplicates
# (OCR may safely stand in for the native layer); 0.3-0.8 is partial
# overlap (keep both layers); < 0.3 is disagreement (register a
# conflict, do not merge).
_DUPLICATE_SHARE = 0.8
_PARTIAL_SHARE = 0.3

# Same threshold as classifier's text_quality_degraded: native text
# with a replacement-char ratio of 0.3 is corrupted soup, not low-quality text.
_CORRUPTED_REPLACEMENT_RATIO = 0.3


def _token_counts(text: str) -> Counter[str]:
    return Counter(_WORD_RE.findall((text or "").casefold()))


def compare_layers(native_text: str, ocr_text: str) -> dict[str, Any]:
    """Compare two text layers and classify their relationship.

    Returns a fact record only: which classification applies and the share
    that produced it.  The caller decides what to keep; this function never
    recommends dropping either layer.
    """
    native_tokens = _token_counts(native_text)
    ocr_tokens = _token_counts(ocr_text)
    native_total = sum(native_tokens.values())
    ocr_total = sum(ocr_tokens.values())
    if not native_total or not ocr_total:
        return {
            "classification": "not_comparable",
            "duplicate_share": None,
            "native_token_count": native_total,
            "ocr_token_count": ocr_total,
            "shared_token_count": 0,
            "comparison": "word_multiset_intersection_over_smaller_layer",
        }
    shared = sum((native_tokens & ocr_tokens).values())
    denominator = min(native_total, ocr_total)
    share = round(shared / denominator, 3)
    if share >= _DUPLICATE_SHARE:
        classification = "duplicate"
    elif share >= _PARTIAL_SHARE:
        classification = "partial_overlap"
    else:
        classification = "conflict"
    return {
        "classification": classification,
        "duplicate_share": share,
        "native_token_count": native_total,
        "ocr_token_count": ocr_total,
        "shared_token_count": shared,
        "comparison": "word_multiset_intersection_over_smaller_layer",
    }


class TextLayerAssessor:
    """Derive per-page text-layer status from already-recorded facts.

    Every input is an existing pipeline fact (probe page signals, OCR page
    routes, native page errors, invisible-text pages, layer conflicts); this
    assessor adds the classification layer on top without re-reading the
    file or re-running any extractor.
    """

    def assess(
        self,
        *,
        page_signals: list[dict[str, Any]],
        ocr_page_routes: list[dict[str, Any]],
        native_page_errors: list[dict[str, Any]] | None = None,
        invisible_text_pages: list[int] | None = None,
        layer_conflicts: list[dict[str, Any]] | None = None,
        hidden_layer_comparisons: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Derive per-page text_layer_status and the native_text_quality panel."""
        routes_by_page = {
            route.get("page_number"): route
            for route in ocr_page_routes or []
            if isinstance(route, dict)
        }
        conflict_by_page = {
            conflict.get("page"): conflict
            for conflict in layer_conflicts or []
            if isinstance(conflict, dict)
        }
        error_pages = {
            entry.get("page_number")
            for entry in native_page_errors or []
            if isinstance(entry, dict)
        }
        invisible = set(invisible_text_pages or [])

        pages: list[dict[str, Any]] = []
        for signal in page_signals or []:
            page_number = signal.get("page_number")
            if not isinstance(page_number, int):
                continue
            text_chars = int(signal.get("text_chars", 0) or 0)
            replacement_chars = int(signal.get("replacement_chars", 0) or 0)
            usable = bool(signal.get("native_text", False))
            replacement_ratio = (
                round(replacement_chars / text_chars, 4) if text_chars else 0.0
            )
            route = routes_by_page.get(page_number, {})
            conflict = conflict_by_page.get(page_number)
            conflict_class = (conflict or {}).get("classification")

            status, reasons = self._classify(
                page_number=page_number,
                text_chars=text_chars,
                replacement_ratio=replacement_ratio,
                usable=usable,
                in_error_pages=page_number in error_pages,
                conflict_class=conflict_class,
            )
            entry: dict[str, Any] = {
                "page_number": page_number,
                "text_layer_status": status,
                "final_route": route.get("final_route"),
                "fallback_reason": route.get("fallback_reason"),
                "native_text_chars": text_chars,
                "replacement_char_ratio": replacement_ratio,
                "native_text_usable": usable,
                "hidden_text_layer": page_number in invisible,
                "layer_conflict": conflict_class,
                "reasons": reasons,
            }
            if conflict_class:
                entry["duplicate_share"] = (conflict or {}).get("duplicate_share")
            pages.append(entry)

        return self._summarize(
            pages,
            conflict_by_page,
            invisible,
            error_pages,
            hidden_layer_comparisons or [],
        )

    def _classify(
        self,
        *,
        page_number: int,
        text_chars: int,
        replacement_ratio: float,
        usable: bool,
        in_error_pages: bool,
        conflict_class: str | None,
    ) -> tuple[str, list[str]]:
        """One deterministic status per page, each decision with its reason."""
        reasons: list[str] = []
        if in_error_pages:
            reasons.append("native page extraction failed (pdf_native_page_errors)")
            return "corrupted", reasons
        if text_chars == 0:
            reasons.append("no native text characters on this page")
            return "absent", reasons
        if replacement_ratio >= _CORRUPTED_REPLACEMENT_RATIO:
            reasons.append(
                "replacement_char_ratio="
                f"{replacement_ratio}>={_CORRUPTED_REPLACEMENT_RATIO}"
            )
            return "corrupted", reasons
        if conflict_class == "duplicate":
            reasons.append(
                "native layer duplicated the kept OCR text "
                "(duplicate_share>=0.8); OCR kept, native preserved in metadata"
            )
            return "duplicate", reasons
        if conflict_class in {"partial_overlap", "conflict"}:
            reasons.append(
                f"native layer disagrees with the kept OCR text ({conflict_class}); "
                "both layers registered, neither silently dropped"
            )
            return "conflict", reasons
        if usable:
            reasons.append("native text above usable threshold")
            return "valid", reasons
        reasons.append(
            "native text present but below the usable threshold "
            "(routed to OCR or native fallback)"
        )
        return "low_quality", reasons

    @staticmethod
    def _summarize(
        pages: list[dict[str, Any]],
        conflict_by_page: dict[Any, dict[str, Any]],
        invisible: set[int],
        error_pages: set[Any],
        hidden_layer_comparisons: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status_counts: Counter[str] = Counter(
            page["text_layer_status"] for page in pages
        )
        conflict_counts: Counter[str] = Counter(
            str(classification)
            for classification in (
                (conflict or {}).get("classification")
                for conflict in conflict_by_page.values()
            )
            if classification
        )
        hidden_counts: Counter[str] = Counter(
            str(record.get("classification"))
            for record in hidden_layer_comparisons
            if record.get("classification")
        )
        return {
            "extractor": "pdf_text_layer_v1",
            "pages": pages,
            "status_counts": dict(status_counts),
            "layer_conflict_counts": dict(conflict_counts),
            "hidden_layer_comparisons": list(hidden_layer_comparisons),
            "hidden_layer_comparison_counts": dict(hidden_counts),
            "hidden_text_layer_pages": sorted(
                page for page in invisible if isinstance(page, int)
            ),
            "native_page_error_pages": sorted(
                page for page in error_pages if isinstance(page, int)
            ),
            "taxonomy": {
                "absent": "no native text characters on the page",
                "valid": "usable native text (above the classifier threshold)",
                "low_quality": "native text present but below the usable threshold",
                "corrupted": "replacement-character soup or failed page extraction",
                "duplicate": "native layer duplicates the kept OCR text",
                "conflict": "native layer disagrees with the kept OCR text",
            },
            "conflict_registration": "TEXT_LAYER_CONFLICT",
            "note": (
                "text-layer status explains every routing and replacement "
                "decision with its input metrics; a conflicting or duplicate "
                "layer is registered, never silently overwritten"
            ),
        }


__all__ = ["TextLayerAssessor", "compare_layers", "TEXT_LAYER_STATUSES"]
