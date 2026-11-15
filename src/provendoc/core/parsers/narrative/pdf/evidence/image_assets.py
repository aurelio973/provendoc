"""PDF image asset extraction: original bytes plus figure-bound evidence.

Reads the original image bytes for every figure the reconstruction layer
identified (``type == "figure"`` blocks carry their xref, bbox, page and
caption evidence).  ``fitz.Document.extract_image(xref)`` returns the
stored image stream — bit-exact, never re-encoded.

Honesty rules: an inline image (no xref) is reported as
``not_extracted_inline`` instead of being silently dropped; an image with
an SMask keeps its main stream as the original while the mask xref is
recorded (alpha composition belongs to the derived pipeline); CMYK or
exotic colourspaces are recorded as-is — colour conversion also belongs
to the derived pipeline, never to the original evidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.image_assets.dimensions import dpi_from_points
from provendoc.core.image_assets.schema import (
    PAYLOAD_KEY,
    asset_id_for,
    image_format_for,
)

_MAX_ASSET_PLACEMENTS = 300

# G11: not-extracted reason when the per-document payload byte budget
# (max_payload_bytes) is exhausted. Guards, not filters: the placements
# are still recorded, the document survives, and the truncation is
# honest in metadata (payloads_truncated / budget_bytes).
_BUDGET_EXHAUSTED = "budget_exhausted"


class PDFImageAssetExtractor:
    """Extract original image bytes for reconstructed figure blocks."""

    def __init__(self, max_payload_bytes: int | None = None):
        """``max_payload_bytes`` caps the cumulative extracted payload
        bytes per document (G11).  ``None`` = no byte budget (historical
        behavior). Figures beyond the budget are still registered
        (not_extracted_reason="budget_exhausted"); already-extracted
        assets are unaffected — the guard never loses a document.
        """
        self.max_payload_bytes = max_payload_bytes

    def extract(
        self,
        file_path: str | Path,
        figure_blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Collect per-placement image asset records.

        ``figure_blocks`` are the pipeline's reconstructed blocks; only
        ``type == "figure"`` entries are considered, and the caption
        evidence bound by ``FigureReconstructor`` is reused as-is.
        """
        records: list[dict[str, Any]] = []
        # xref -> extracted image dict, so a repeated placement of the
        # same object extracts once and deduplicates by content hash.
        by_xref: dict[int, dict[str, Any] | None] = {}
        asset_ref_counts: dict[str, int] = {}
        # G11: cumulative payload bytes and truncation facts.
        payload_bytes = 0
        budget_exhausted = False
        skipped_by_budget = 0
        # xrefs rejected by the byte budget (distinct from extraction
        # failures so repeated placements report the honest reason).
        budget_skipped_xrefs: set[int] = set()
        # xrefs whose bytes are already charged to the budget (repeat
        # placements of the same xref share the cached bytes, so only
        # the first extraction is charged).
        budget_accounted_xrefs: set[int] = set()

        document = fitz.open(file_path)
        try:
            for block in figure_blocks:
                if len(records) >= _MAX_ASSET_PLACEMENTS:
                    break
                if block.get("type") != "figure":
                    continue
                payload = block.get("raw_payload") or {}
                xref = int(payload.get("xref") or 0)
                placement = {
                    "page": block.get("page"),
                    "bbox": [
                        round(float(v), 1) for v in (block.get("bbox") or [])
                    ],
                    "caption": payload.get("caption"),
                    "caption_evidence_ids": payload.get(
                        "caption_evidence_ids"
                    ),
                    "caption_page_offset": payload.get("caption_page_offset"),
                    "evidence_id": block.get("evidence_id"),
                }

                if xref <= 0:
                    records.append(
                        {
                            "asset_id": None,
                            "extracted": False,
                            "not_extracted_reason": "not_extracted_inline",
                            "xref": xref,
                            **placement,
                        }
                    )
                    continue

                # G11: once the payload byte budget is exhausted, the
                # remaining placements are recorded without extraction —
                # the same honest-degradation contract as the placement
                # count cap above.  A repeated placement of an image
                # already extracted before exhaustion still counts as
                # extracted (its bytes are already accounted).
                if (
                    self.max_payload_bytes is not None
                    and budget_exhausted
                    and xref not in by_xref
                ):
                    skipped_by_budget += 1
                    budget_skipped_xrefs.add(xref)
                    records.append(
                        {
                            "asset_id": None,
                            "extracted": False,
                            "not_extracted_reason": _BUDGET_EXHAUSTED,
                            "xref": xref,
                            **placement,
                        }
                    )
                    continue
                if xref in budget_skipped_xrefs:
                    # Repeated placement of an over-budget asset: same
                    # honest reason, no double-count of skipped_by_budget
                    # (the placement itself is still recorded).
                    records.append(
                        {
                            "asset_id": None,
                            "extracted": False,
                            "not_extracted_reason": _BUDGET_EXHAUSTED,
                            "xref": xref,
                            **placement,
                        }
                    )
                    continue

                if xref not in by_xref:
                    by_xref[xref] = self._extract_xref(document, xref)
                is_new_extraction = xref not in budget_accounted_xrefs
                image = by_xref[xref]

                if image is None or not image.get("image"):
                    records.append(
                        {
                            "asset_id": None,
                            "extracted": False,
                            "not_extracted_reason": (
                                image.get("not_extracted_reason")
                                if image
                                else "xref_not_readable"
                            ),
                            "xref": xref,
                            **placement,
                        }
                    )
                    continue

                data = image["image"]

                # G11: first-fit budget, accounted per unique asset (a
                # repeated placement of the same xref shares the cached
                # bytes and the deduplicated payload map, so only the
                # first extraction is charged to the budget — matching
                # the bytes actually retained in metadata).
                if (
                    self.max_payload_bytes is not None
                    and is_new_extraction
                    and payload_bytes + len(data) > self.max_payload_bytes
                ):
                    budget_exhausted = True
                    budget_skipped_xrefs.add(xref)
                    skipped_by_budget += 1
                    records.append(
                        {
                            "asset_id": None,
                            "extracted": False,
                            "not_extracted_reason": _BUDGET_EXHAUSTED,
                            "xref": xref,
                            **placement,
                        }
                    )
                    continue
                if is_new_extraction:
                    payload_bytes += len(data)
                    budget_accounted_xrefs.add(xref)
                asset_id = asset_id_for(data)
                asset_ref_counts[asset_id] = (
                    asset_ref_counts.get(asset_id, 0) + 1
                )
                # Rendering resolution at the placement: plain arithmetic
                # over the recorded pixel size and bbox width (points).
                # A None here means a fact gap (no bbox / no header size),
                # never a guess.
                bbox_width = None
                bbox = placement.get("bbox")
                if bbox is not None and len(bbox) >= 4:
                    bbox_width = float(bbox[2]) - float(bbox[0])
                records.append(
                    {
                        "asset_id": asset_id,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "extracted": True,
                        "format": image_format_for(None, image.get("ext")),
                        "byte_size": len(data),
                        "xref": xref,
                        "pixel_width": image.get("width"),
                        "pixel_height": image.get("height"),
                        "effective_dpi": dpi_from_points(
                            image.get("width"), bbox_width
                        ),
                        "colorspace": image.get("colorspace"),
                        "bits_per_component": image.get("bpc"),
                        "smask_xref": image.get("smask") or None,
                        "has_transparency_mask": bool(image.get("smask")),
                        "ref_count": asset_ref_counts[asset_id],
                        **placement,
                        PAYLOAD_KEY: data,
                    }
                )
        finally:
            document.close()

        result: dict[str, Any] = {
            "extractor": "pdf_image_assets_v1",
            "records": records,
        }
        # G11: honest truncation facts — always present when a budget is
        # configured so consumers can distinguish "no images" from
        # "budget ran out" without string-matching reason codes.
        if self.max_payload_bytes is not None:
            result["payload_budget_bytes"] = self.max_payload_bytes
            result["payload_bytes"] = payload_bytes
            result["payloads_truncated"] = budget_exhausted
            result["budget_skipped_placements"] = skipped_by_budget
        return result

    @staticmethod
    def _extract_xref(document, xref: int) -> dict[str, Any] | None:
        """Extract one image stream, recording failure reasons honestly."""
        try:
            info = document.extract_image(xref)
        except Exception:
            return None
        if not info or not info.get("image"):
            return None
        return info


__all__ = ["PDFImageAssetExtractor"]
