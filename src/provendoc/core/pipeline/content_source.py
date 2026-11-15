# -*- coding: utf-8 -*-
"""Content-source marking contract — incremental updates, hidden objects, historical content.

Formats with incremental saves, revision records, or hidden
layers/sheets can parse inconsistently across versions, with hidden
content accidentally extracted or missed. Defense is a per-category
extract/ignore policy, with the source marked in the results
(content_source = visible_text / annotation / hidden_sheet / ...), so
parser behavior changes cannot blur the data boundary.

Implementation in this library:

- ``metadata["content_sources"]`` is a **per-document** content
  boundary declaration: primary source (where the narrative text
  payload comes from) + categories (per-category verdicts, present
  only with evidence) + policy (the library-global frozen per-class
  strategy).
- Verdicts all come from **existing evidence keys** (docx_review /
  pdf_annotations / spreadsheet sheet visibility / pptx
  notes_slide_count); this module only summarizes and declares — it
  opens no new extraction path and changes no parser behavior.
- Honesty rule: categories without an evidence key get no invented
  counts (they never appear in categories); "this library has no
  such extractor" is a structural fact and lands in policy, not in
  categories.
- Idempotent stamping: ``stamp_content_sources`` may be called
  repeatedly; unexpected document shapes return silently (guards are
  never fatal).

Category semantics (frozen; changes require a scheme bump):

- ``visible_text``: the narrative text payload itself — always
  present; on parse_failed the decision is ``absent`` (honest
  absence rather than empty text).
- ``tracked_changes``: DOCX revisions (w:ins/w:del) — python-docx
  text traversal never enters revision-marked content, hence
  ``evidence_only``.
- ``annotations``: DOCX comments + PDF /Annots — annotation bodies
  never enter the narrative text, hence ``evidence_only``.
- ``hidden_sheets``: Excel hidden sheets — the openpyxl backend
  extracts all sheets (including hidden ones) and records
  per-sheet visibility, hence ``extracted`` (extracted but carrying
  visibility facts; downstream may filter).
- ``speaker_notes``: PPTX speaker notes — included in the narrative
  text by default (include_notes=True), hence ``extracted``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

CONTENT_SOURCE_SCHEME = "content_source_v1"

# Global frozen policy (per-category extract/ignore; structural facts
# of this library). Per-document verdicts in categories must agree
# with it — disagreement is implementation drift, and
# test_security_content_source.py freezes the cross-check.
CONTENT_SOURCE_POLICY: Dict[str, str] = {
    "visible_text": "extracted",
    "tracked_changes": "evidence_only",
    "annotations": "evidence_only",
    "hidden_sheets": "extracted_with_visibility_facts",
    "speaker_notes": "extracted",
    # The categories below have no extractor in this library —
    # structurally "not extracted", a frozen fact rather than an
    # omission; adding an extraction path later requires updating
    # this table and bumping the scheme.
    "attachments": "not_extracted",
    "form_fields": "not_extracted",
    "pdf_layers": "not_extracted",
    "deleted_content_history": "not_extracted",
}


def _visible_text_category(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Verdict for the narrative text payload origin (always present)."""
    if metadata.get("parse_failed"):
        return {
            "decision": "absent",
            "reason": "parse_failed",
        }
    return {"decision": "extracted"}


def _tracked_changes_category(
    metadata: Dict[str, Any],
) -> Dict[str, Any] | None:
    review = metadata.get("docx_review") or {}
    tracked = review.get("tracked_changes")
    if tracked is None:
        return None
    return {
        "decision": "evidence_only",
        "insertion_count": tracked.get("insertion_count"),
        "deletion_count": tracked.get("deletion_count"),
        "has_tracked_changes": tracked.get("has_tracked_changes"),
    }


def _annotations_category(metadata: Dict[str, Any]) -> Dict[str, Any] | None:
    review = metadata.get("docx_review") or {}
    comments = review.get("comments")
    pdf_annotations = metadata.get("pdf_annotations")
    if comments is None and pdf_annotations is None:
        return None
    category: Dict[str, Any] = {"decision": "evidence_only"}
    if comments is not None:
        category["docx_comment_count"] = comments.get("comment_count")
    if pdf_annotations is not None:
        category["pdf_annotation_count"] = pdf_annotations.get(
            "total_annotation_count"
        )
    return category


def _hidden_sheets_category(metadata: Dict[str, Any]) -> Dict[str, Any] | None:
    spreadsheet = metadata.get("spreadsheet") or {}
    workbook = spreadsheet.get("workbook") or {}
    sheets = workbook.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        return None
    hidden = sum(
        1
        for sheet in sheets
        if isinstance(sheet, dict)
        and str(sheet.get("visibility") or "visible") != "visible"
    )
    return {
        "decision": "extracted",
        "sheet_count": len(sheets),
        "hidden_sheet_count": hidden,
        "note": (
            "all sheets are extracted including hidden ones; per-sheet "
            "visibility is recorded so downstream consumers can filter"
        ),
    }


def _speaker_notes_category(metadata: Dict[str, Any]) -> Dict[str, Any] | None:
    notes_slide_count = metadata.get("notes_slide_count")
    if notes_slide_count is None:
        return None
    return {
        "decision": "extracted",
        "notes_slide_count": notes_slide_count,
    }


_CATEGORY_COLLECTORS = (
    ("visible_text", _visible_text_category),
    ("tracked_changes", _tracked_changes_category),
    ("annotations", _annotations_category),
    ("hidden_sheets", _hidden_sheets_category),
    ("speaker_notes", _speaker_notes_category),
)


def summarize_content_sources(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize existing evidence keys into a content-source declaration (pure function; input untouched).

    Args:
        metadata: document metadata (existing evidence keys read-only).

    Returns:
        dict with ``scheme`` / ``primary`` / ``categories`` / ``policy``;
        categories holds only evidence-backed classes (honest absence,
        nothing invented).
    """
    categories: Dict[str, Any] = {}
    for name, collector in _CATEGORY_COLLECTORS:
        try:
            category = collector(metadata)
        except Exception:  # one failing category must not sink the declaration
            _log.debug("content-source category %s failed", name, exc_info=True)
            category = None
        if category is not None:
            categories[name] = category
    return {
        "scheme": CONTENT_SOURCE_SCHEME,
        "primary": "visible_text",
        "categories": categories,
        "policy": dict(CONTENT_SOURCE_POLICY),
    }


def stamp_content_sources(document) -> Dict[str, Any] | None:
    """Idempotently stamp ``metadata["content_sources"]`` (additive, fail-silent).

    Args:
        document: Document instance (must carry dict-shaped metadata).

    Returns:
        The stamped declaration dict; None when the document shape
        does not fit (never raises).
    """
    try:
        metadata = document.metadata
        if not isinstance(metadata, dict):
            return None
        if "content_sources" in metadata:
            return metadata["content_sources"]
        summary = summarize_content_sources(metadata)
        metadata["content_sources"] = summary
        return summary
    except Exception as exc:
        _log.debug("content-source stamping failed", exc_info=True)
        record_degradation(document, "content_sources", exc)
        return None


__all__ = [
    "CONTENT_SOURCE_POLICY",
    "CONTENT_SOURCE_SCHEME",
    "stamp_content_sources",
    "summarize_content_sources",
]
