# -*- coding: utf-8 -*-
"""Content completeness — loss-cause and coverage summary view.

Design (same "summarize, do not rebuild" approach as
ingestion_status):

- **Read-only over existing evidence keys** (ocr_pages /
  resource_guard / security_carriers / slide_order_method /
  ``{format}_unresolved`` / ``*_image_assets`` / pdf_annotations /
  config_entries), deriving ``metadata["content_completeness"]`` —
  additive, idempotent (overwrite rewrite), fail-silent,
  **non-empty writes only** (no loss and no coverage evidence → no
  key written, per the "no key when nothing unresolved" rule).
- **Cause-axis derivation**: of the six causes, render_recovered
  currently has no document-level evidence key (no rendered mapping
  in EVIDENCE_METHOD_MAP), so the derivation honestly produces
  nothing for it; ocr_recovered derives from the page-level
  ocr_pages evidence, inferred_content from
  slide_order_method=filename_fallback, resource_missing /
  security_blocked from the document-level resource_guard /
  security_carriers evidence, and object-level causes from
  ``{format}_unresolved`` records via loss_reason_of().
- **Envelope four fields** (frozen): loss_reason / loss_scope /
  raw_preserved / fallback_used. raw_preserved=True means the
  original evidence for the cause is still traceable (the audit
  chain is not dropped); fallback_used=True when the cause itself
  is the degraded-recovery channel (OCR / filename-order fallback).
- **Coverage dimensions**: each dimension is written only when
  evidence exists; a missing dimension key IS the fact (no null/0
  stand-ins).

Relations to existing modules (no duplication, no replacement):

- The ingestion_status three-state contract is untouched — this
  module never changes status and adds no fourth state; the cause
  axis is orthogonal to the status axis.
- ``{format}_unresolved`` records are unchanged — object-level
  attribution flows through the read-side
  vocab_registry.loss_reason_of() mapping, which this module only
  consumes.
- Parser stats keys are unchanged — the coverage view only reads
  them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from provendoc.core.parsers.vocab_registry import loss_reason_of
from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

CONTENT_COMPLETENESS_VERSION = "content_completeness_v1"


def _add_loss(
    losses: List[Dict[str, Any]],
    seen: set,
    reason: str,
    scope: str,
    fallback_used: bool,
) -> None:
    """Register a loss cause (first scope wins, deterministic dedup)."""
    if reason in seen:
        return
    seen.add(reason)
    losses.append(
        {
            "loss_reason": reason,
            "loss_scope": scope,
            "raw_preserved": True,
            "fallback_used": fallback_used,
        }
    )


def _derive_losses(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Evidence keys -> loss-cause envelopes (read-only; empty list without evidence)."""
    losses: List[Dict[str, Any]] = []
    seen: set = set()

    # page level: the OCR recovery channel (same evidence source as
    # ingestion_status)
    ocr_pages = metadata.get("ocr_pages")
    if isinstance(ocr_pages, list) and ocr_pages:
        _add_loss(losses, seen, "ocr_recovered", "page", True)

    # document level: inferred synthesis (filename-order fallback when
    # the pptx declared order is unavailable — the baseline shape)
    if metadata.get("slide_order_method") == "filename_fallback":
        _add_loss(losses, seen, "inferred_content", "document", True)

    # document level: resource guard (guards, not filters)
    resource_guard = metadata.get("resource_guard")
    if isinstance(resource_guard, dict) and resource_guard.get("reasons"):
        _add_loss(losses, seen, "resource_missing", "document", False)

    # document level: invisible-carrier census (carried-over key; a
    # non-zero census = carrier content with a registered security
    # stance left unextracted)
    census = metadata.get("security_carriers")
    if isinstance(census, dict) and any(isinstance(v, int) and v > 0 for v in census.values()):
        _add_loss(losses, seen, "security_blocked", "document", False)

    # object level: unresolved-marker records -> loss_reason_of()
    # attribution (None = cause axis not applicable, honest skip; the
    # four resolution=unsupported codes are already attributed to
    # policy_excluded in the registry).
    for key, value in metadata.items():
        if not key.endswith("_unresolved") or not isinstance(value, dict):
            continue
        records = value.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            reason_code = record.get("reason_code")
            if not isinstance(reason_code, str):
                continue
            mapped = loss_reason_of(reason_code)
            if mapped is not None:
                _add_loss(losses, seen, mapped, "object", False)

    return losses


def _derive_coverage(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence keys -> coverage dimension view (a dimension is written only with evidence; absence is the fact)."""
    coverage: Dict[str, Any] = {}

    # text: pdf page channels (pages_processed/pages_total, same
    # criteria as ingestion_status; OCR pages registered too — the
    # channel composition of page-level coverage).
    page_count = metadata.get("page_count")
    native_pages = metadata.get("native_text_pages")
    ocr_pages = metadata.get("ocr_pages")
    if isinstance(page_count, int) and (
        isinstance(native_pages, list) or isinstance(ocr_pages, list)
    ):
        processed = len(native_pages or []) + len(ocr_pages or [])
        text_view: Dict[str, int] = {
            "pages_total": page_count,
            "pages_processed": processed,
        }
        if isinstance(ocr_pages, list) and ocr_pages:
            text_view["ocr_pages"] = len(ocr_pages)
        coverage["text"] = text_view

    # asset: image_assets central module fact keys (docx/pptx/pdf/rtf
    # consumers share the shape: unique_assets / extracted /
    # not_extracted).
    for key, value in metadata.items():
        if (
            key.endswith("_image_assets")
            and isinstance(value, dict)
            and isinstance(value.get("extracted"), int)
        ):
            coverage["asset"] = {
                "assets_total": (
                    value.get("unique_assets")
                    if isinstance(value.get("unique_assets"), int)
                    else (value.get("extracted", 0) + value.get("not_extracted", 0))
                ),
                "assets_extracted": value["extracted"],
                "assets_not_extracted": value.get("not_extracted", 0),
            }

    # annotation: pdf /Annots review-state facts (document level).
    annotations = metadata.get("pdf_annotations")
    if isinstance(annotations, dict) and isinstance(annotations.get("total_annotation_count"), int):
        annotation_view: Dict[str, int] = {
            "annotation_count": annotations["total_annotation_count"],
        }
        if isinstance(annotations.get("scanned_pages"), int):
            annotation_view["scanned_pages"] = annotations["scanned_pages"]
        coverage["annotation"] = annotation_view

    # reference: resolution_status counts over config-contract
    # references (all references carry a status; the five-value range
    # of the contracts.py ref_dict).
    config_entries = metadata.get("config_entries")
    if isinstance(config_entries, dict):
        total = 0
        resolved = 0
        entries = config_entries.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                references = entry.get("references")
                if not isinstance(references, list):
                    continue
                for reference in references:
                    if not isinstance(reference, dict):
                        continue
                    total += 1
                    if reference.get("resolution_status") == "resolved":
                        resolved += 1
        if total:
            coverage["reference"] = {
                "references_total": total,
                "references_resolved": resolved,
            }

    return coverage


def summarize_content_completeness(
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Parser metadata -> loss-cause and coverage view (read-only derivation).

    metadata is not modified; the return value is serializable
    (writing it into ``metadata["content_completeness"]`` is done by
    the stamp_* layer).
    """
    losses = _derive_losses(metadata)
    coverage = _derive_coverage(metadata)
    view: Dict[str, Any] = {
        "completeness_version": CONTENT_COMPLETENESS_VERSION,
    }
    if losses:
        view["losses"] = losses
    if coverage:
        view["coverage"] = coverage
    return view


def stamp_content_completeness(document: Any) -> Optional[Dict[str, Any]]:
    """Derive and write ``document.metadata["content_completeness"]``.

    Idempotent (overwrite write; rerun on the same input yields the
    same view); **non-empty writes only** — no key is written when
    there is no loss and no coverage evidence (honest absence; most
    clean documents gain zero new keys); failures return None and
    never block the main flow (same discipline as provenance /
    ingestion_status / marker_contract — a missing completeness view
    must not block ingestion).
    """
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        view = summarize_content_completeness(metadata)
        if len(view) == 1:  # version field only = no evidence
            return None
        metadata["content_completeness"] = view
        return view
    except Exception as exc:
        _log.debug("Content completeness stamping failed", exc_info=True)
        record_degradation(document, "content_completeness", exc)
        return None


__all__ = [
    "CONTENT_COMPLETENESS_VERSION",
    "summarize_content_completeness",
    "stamp_content_completeness",
]
