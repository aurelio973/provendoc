"""Additive MinerU layout overlay over immutable PDF evidence.

MinerU runs only when routing recommends the layout runtime and the caller
explicitly injected a layout backend.  Its ordered semantic regions become a
reconstruction overlay: native/OCR/image/table evidence stays untouched, every
derived layout block records the immutable evidence IDs whose page geometry it
overlaps, and unmatched evidence remains visible in the final order instead of
being silently dropped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from provendoc.core.parsers.narrative.pdf.contracts import PDFBlock, PDFPage
from provendoc.core.parsers.narrative.pdf.enrichment import unconfigured_backend_metadata
from provendoc.core.parsers.narrative.pdf.mineru import (
    MinerUBackendError,
    MinerUBackendUnavailable,
    MinerUBreakerOpen,
    mineru_item_text,
    normalized_mineru_type,
)


class PDFLayoutBackend(Protocol):
    """Minimal contract a layout backend must satisfy for pipeline injection."""

    def availability(self) -> dict[str, object]:
        """Return the layout backend's availability."""
        ...

    def metadata(self) -> dict[str, object]:
        """Return the layout backend's identity metadata."""
        ...

    def extract(self, file_path: str | Path) -> dict[str, Any]:
        """Run the layout backend and return structured regions."""
        ...


# MinerU page-edge artifacts already carry their own semantic label; the
# overlay keeps them out of narrative reconstruction instead of guessing.
_PAGE_EDGE_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}

# Structural MinerU regions are described by the overlay, while their immutable
# image/table evidence continues through the normal reconstruction path.
_STRUCTURE_ONLY_TYPES = {"image", "chart"}


def apply_layout_overlay(
    *,
    pages: list[PDFPage],
    file_path: str | Path,
    layout_backend: "PDFLayoutBackend | None",
    enable_layout: bool,
    routing_recommends_layout: bool,
    runtime: dict[str, Any],
    runtime_notes: list[str],
) -> tuple[list[PDFBlock] | None, dict[str, Any]]:
    """Return MinerU-ordered blocks when possible, or a truthful fallback.

    The first return value is ``None`` unless a real layout overlay was
    produced; callers then keep their existing native/OCR block ordering.
    """
    backends = runtime.setdefault("enrichment_backends", unconfigured_backend_metadata())

    if layout_backend is None or not enable_layout:
        runtime["layout_status"] = "not_configured" if layout_backend is None else "disabled"
        if layout_backend is None:
            runtime_notes.append(
                "routing recommends 'layout' runtime; using native/OCR evidence because no layout backend is configured"
            )
        else:
            runtime_notes.append(
                "layout backend was configured but explicitly disabled; native/OCR evidence retained"
            )
        return None, {}
    if not routing_recommends_layout:
        # Respect the probe's routing decision; MinerU is never forced.
        runtime["layout_status"] = "not_routed"
        runtime_notes.append(
            "layout backend is configured but routing did not recommend the layout runtime; native/OCR evidence retained"
        )
        return None, {}

    availability = layout_backend.availability()
    runtime["layout_backend"] = availability
    runtime["enrichment_backends"] = _with_layout_backend(backends, layout_backend)
    if not availability.get("available", False):
        runtime["layout_status"] = "unavailable"
        runtime["layout_unavailable_reason"] = availability.get(
            "reason", "layout backend is unavailable"
        )
        runtime_notes.append(
            "routing recommends 'layout' runtime; native/OCR evidence retained because "
            f"{availability.get('reason', 'layout backend is unavailable')}"
        )
        return None, {}

    try:
        output = layout_backend.extract(file_path)
    except MinerUBreakerOpen as error:
        runtime["layout_status"] = "breaker_open"
        runtime["layout_failure_reason"] = str(error)
        runtime_notes.append(
            f"layout backend circuit breaker is open; native/OCR evidence retained: {error}"
        )
        return None, {}
    except (MinerUBackendUnavailable, MinerUBackendError) as error:
        runtime["layout_status"] = "failed"
        runtime["layout_failure_reason"] = str(error)
        runtime_notes.append(
            f"layout backend failed; native/OCR evidence retained: {error}"
        )
        return None, {}
    except Exception as error:  # Layout enrichment must never be fatal.
        runtime["layout_status"] = "failed"
        runtime["layout_failure_reason"] = str(error)
        runtime_notes.append(
            f"layout backend failed unexpectedly; native/OCR evidence retained: {error}"
        )
        return None, {}

    items = [item for item in output.get("items", []) if isinstance(item, dict)]
    if not items:
        runtime["layout_status"] = "empty_output"
        runtime_notes.append(
            "layout backend produced no structured regions; native/OCR evidence retained"
        )
        return None, {}

    overlay = _build_overlay(pages, items)
    if not overlay["blocks"]:
        runtime["layout_status"] = "empty_output"
        runtime_notes.append(
            "layout backend produced no geometry-bound regions; native/OCR evidence retained"
        )
        return None, {}

    runtime["layout_status"] = "used"
    runtime["layout_block_count"] = overlay["layout_block_count"]
    runtime["layout_bound_block_count"] = overlay["layout_bound_block_count"]
    runtime["layout_unmatched_evidence_ids"] = overlay["unmatched_evidence_ids"]
    runtime["layout_structural_region_count"] = overlay["structural_region_count"]
    runtime["layout_artifacts"] = output.get("artifacts", {})
    runtime["layout_command"] = output.get("command", {})
    runtime["layout_execution"] = output.get("execution", {})
    runtime["actual_runtime"] = (
        "hybrid_native_ocr_layout"
        if runtime.get("actual_runtime") == "hybrid_native_ocr"
        else "native_layout"
    )
    runtime_notes.append(
        "MinerU layout overlay applied over immutable native/OCR evidence"
    )
    return overlay["blocks"], {
        "reading_order": "mineru_layout_overlay",
        "layout_backend": output.get("backend", {}),
        "layout_block_count": overlay["layout_block_count"],
        "layout_bound_block_count": runtime["layout_bound_block_count"],
        "unmatched_evidence_ids": overlay["unmatched_evidence_ids"],
        "binding_rule": "page_number match and normalized-bbox coverage >= 0.6",
    }


def _with_layout_backend(
    backends: dict[str, Any],
    layout_backend: "PDFLayoutBackend",
) -> dict[str, Any]:
    updated = {key: dict(value) for key, value in backends.items()}
    layout_contract = dict(updated.get("layout", {}))
    layout_contract.update(layout_backend.metadata())
    layout_contract["status"] = "configured"
    updated["layout"] = layout_contract
    return updated


def _build_overlay(pages: list[PDFPage], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Translate ordered MinerU items into derived, evidence-bound blocks."""
    geometry = {page.page_number: (page.width, page.height) for page in pages}
    evidence = [block for page in pages for block in page.blocks]
    evidence_by_id = {
        block.evidence_id: block for block in evidence if block.evidence_id
    }

    derived: list[PDFBlock] = []
    consumed_ids: set[str] = set()
    structural_region_count = 0
    sequence = 0

    for item in items:
        raw_type = str(item.get("type", "text")).strip().casefold()
        if raw_type in _PAGE_EDGE_TYPES:
            continue
        structure = normalized_mineru_type(item)
        item_text = mineru_item_text(item)
        page_number, bbox = _denormalize_bbox(item, geometry)
        if structure in _STRUCTURE_ONLY_TYPES:
            structural_region_count += 1
            continue
        if structure in {"text", "formula"} and not item_text:
            # An empty region (text or formula) carries no representable fact.
            continue

        # Without a page number the region cannot be aligned to an evidence
        # page; same as a missing bbox, return an empty match.
        source_ids = (
            _match_evidence_ids(evidence, page_number, bbox)
            if bbox and page_number is not None
            else []
        )
        if structure == "text" and not source_ids:
            # A text region with no geometry-bound source evidence would claim
            # provenance we cannot audit; exclude it from reconstruction.
            continue
        if structure == "table" and not source_ids:
            structural_region_count += 1
            continue

        consumed_ids.update(source_ids)
        derived.append(
            PDFBlock(
                # MinerU formula regions keep their formula semantics; the
                # overlay never relabels them as narrative text.
                block_type=(
                    "text"
                    if structure in {"text", "title"}
                    else "formula"
                    if structure == "formula"
                    else "table"
                ),
                text=item_text,
                page_number=page_number or 0,
                bbox=(
                    (
                        round(bbox[0], 3),
                        round(bbox[1], 3),
                        round(bbox[2], 3),
                        round(bbox[3], 3),
                    )
                    if bbox
                    else (0.0, 0.0, 0.0, 0.0)
                ),
                source_kind="layout_overlay",
                confidence=0.9,
                raw_payload_ref=f"mineru:item:{sequence}",
                raw_payload={
                    "mineru_type": raw_type,
                    "mineru_text_level": item.get("text_level"),
                    "source_evidence_ids": source_ids,
                    "source_bboxes": [
                        list(evidence_by_id[evidence_id].bbox)
                        for evidence_id in source_ids
                        if evidence_id in evidence_by_id
                    ],
                },
                evidence_id=f"layout:p{page_number}:m{sequence}",
            )
        )
        sequence += 1

    residual = [block for block in evidence if block.evidence_id not in consumed_ids]
    ordered = _interleave_residual(derived, residual)
    return {
        "blocks": ordered,
        "layout_block_count": len(derived),
        "layout_bound_block_count": sum(
            1
            for block in derived
            if (block.raw_payload or {}).get("source_evidence_ids")
        ),
        "unmatched_evidence_ids": [
            block.evidence_id for block in residual if block.evidence_id
        ],
        "structural_region_count": structural_region_count,
    }


def _denormalize_bbox(
    item: dict[str, Any],
    geometry: dict[int, tuple[float, float]],
) -> tuple[int | None, tuple[float, float, float, float] | None]:
    page_idx = item.get("page_idx")
    bbox_normalized = item.get("bbox")
    if not isinstance(page_idx, int) or page_idx < 0:
        return None, None
    page_number = page_idx + 1
    if page_number not in geometry:
        return None, None
    if not isinstance(bbox_normalized, (list, tuple)) or len(bbox_normalized) != 4:
        return None, None
    page_width, page_height = geometry[page_number]
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox_normalized)
    except (TypeError, ValueError):
        return None, None
    return page_number, (
        x0 * page_width / 1000.0,
        y0 * page_height / 1000.0,
        x1 * page_width / 1000.0,
        y1 * page_height / 1000.0,
    )


def _match_evidence_ids(
    evidence: list[PDFBlock],
    page_number: int,
    bbox: tuple[float, float, float, float],
    *,
    coverage: float = 0.6,
) -> list[str]:
    """Return evidence IDs whose region is sufficiently covered by the layout box."""
    x0, y0, x1, y1 = bbox
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if area <= 0:
        return []

    matched: list[str] = []
    for block in evidence:
        if block.page_number != page_number or not block.evidence_id:
            continue
        bx0, by0, bx1, by1 = block.bbox
        overlap = max(0.0, min(x1, bx1) - max(x0, bx0)) * max(
            0.0, min(y1, by1) - max(y0, by0)
        )
        if overlap > 0 and (overlap / area) >= coverage:
            matched.append(block.evidence_id)
    return matched


def _interleave_residual(
    derived: list[PDFBlock],
    residual: list[PDFBlock],
) -> list[PDFBlock]:
    """Insert unmatched evidence into the layout order near its page position.

    Derived blocks keep MinerU's semantic order.  Each residual block is placed
    after the last derived block on the same page whose vertical centre is not
    below it, which preserves figure/table evidence visibility without
    inventing a new reading order for audited regions.
    """
    if not residual:
        return list(derived)
    result = list(derived)
    for block in sorted(
        residual,
        key=lambda item: (item.page_number, item.bbox[1], item.bbox[0]),
    ):
        insertion = len(result)
        for index in range(len(result) - 1, -1, -1):
            candidate = result[index]
            if candidate.page_number != block.page_number:
                continue
            if (candidate.bbox[1] + candidate.bbox[3]) / 2 <= (block.bbox[1] + block.bbox[3]) / 2:
                insertion = index + 1
                break
            insertion = index
        result.insert(insertion, block)
    return result


__all__ = [
    "apply_layout_overlay",
]
