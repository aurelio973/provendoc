# -*- coding: utf-8 -*-
"""epub-domain object-level unresolved registry derivation (epub_unresolved_prereg_v1, frozen).

Derived-view discipline:

- No new detection logic — every record derives 1:1 from existing fact sources:
  ``epub_package["failed_chapters"]`` (per-chapter parse failures),
  ``epub_package["skipped_spine_items"]`` (three-type spine-item skips),
  ``epub_navigation["error"]`` (TOC document parse failure);
- Existing fact-source keys stay as-is — never rewritten or migrated (the
  single source of truth remains in the original keys; this module produces
  the object-level detail view under the marker_contract v1 contract);
- Records are built via ``core.foundation.unresolved_registry.build_unresolved_record``
  (contract validation — one bad entry never spoils the batch); writing is done
  by the caller via ``stamp_unresolved_registry`` (write non-empty only, honest
  absence);
- ``placeholder_text`` is always ``None`` (failed chapters and skipped items
  never entered the body text, no placeholder line, PDF precedent);
- Failures degrade silently: any input exception is caught by the caller's
  guard; this module itself only defensively skips unexpected shapes
  (non-dict entries / non-string href / non-string error).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)

# error_summary truncation convention matches jsonl / txt delegation failure (120 chars)
_ERROR_SUMMARY_MAX = 120

# prereg §2 — skipped_spine_items reason literals → dedicated codes (exhaustive mapping).
# Future parser-added literals must update the prereg first, then extend the mapping;
# MALFORMED is a transitional escape hatch, not a long-term home.
_SKIP_REASON_CODES = {
    "missing_manifest_item": "SPINE_IDREF_UNRESOLVED",
    "non_xhtml_spine_item": "SPINE_ITEM_NON_XHTML",
    "missing_zip_member": "SPINE_ITEM_MISSING_MEMBER",
}


def _error_summary(error: Any) -> Optional[str]:
    """Defensively truncate an error string (non-string input returns None)."""
    if not isinstance(error, str) or not error:
        return None
    return error[:_ERROR_SUMMARY_MAX]


def _derive_failed_chapters(
    failed_chapters: Any,
) -> List[Dict[str, Any]]:
    """F1: per-chapter parse-failure facts → CHAPTER_PARSE_FAILED records (1:1)."""
    records: List[Dict[str, Any]] = []
    if not isinstance(failed_chapters, list):
        return records
    for entry in failed_chapters:
        if not isinstance(entry, dict):
            continue
        href = entry.get("href")
        if not isinstance(href, str) or not href:
            continue
        summary = _error_summary(entry.get("error"))
        if summary is None:
            continue
        record = build_unresolved_record(
            object_type="chapter",
            object_id=href,
            resolution="unresolved",
            reason_code="CHAPTER_PARSE_FAILED",
            source=href,
            placeholder_text=None,
            error_summary=summary,
        )
        if record is not None:
            records.append(record)
    return records


def _derive_skipped_spine_items(
    skipped_spine_items: Any,
) -> List[Dict[str, Any]]:
    """F2: spine-item skip facts → three-type dedicated-code records (1:1, original order)."""
    records: List[Dict[str, Any]] = []
    if not isinstance(skipped_spine_items, list):
        return records
    for entry in skipped_spine_items:
        if not isinstance(entry, dict):
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason:
            continue
        href = entry.get("href")
        idref = entry.get("idref")
        # Locator prefers href; missing_manifest_item branch facts carry only idref.
        # isinstance narrowing guarantees locator is a str for type checkers (bad shapes skipped first).
        locator: str
        if isinstance(href, str) and href:
            locator = href
        elif isinstance(idref, str) and idref:
            locator = idref
        else:
            continue
        has_idref = isinstance(idref, str) and bool(idref)
        code = _SKIP_REASON_CODES.get(reason)
        if code is not None:
            # Exhaustive mapping: missing_manifest_item branch facts carry only idref
            # (no href); the other two types carry href + idref.
            record = build_unresolved_record(
                object_type="spine_item",
                object_id=locator,
                resolution=(
                    "unsupported"
                    if code == "SPINE_ITEM_NON_XHTML"
                    else "unresolved"
                ),
                reason_code=code,
                source=locator,
                placeholder_text=None,
                **({"idref": idref} if code != "SPINE_IDREF_UNRESOLVED"
                   and has_idref else {}),
            )
        else:
            # Unknown reason literal → MALFORMED fallback (escape hatch, not expected to trigger)
            record = build_unresolved_record(
                object_type="spine_item",
                object_id=locator,
                resolution="unresolved",
                reason_code="MALFORMED",
                source=locator,
                placeholder_text=None,
                error_summary=reason[:_ERROR_SUMMARY_MAX],
            )
        if record is not None:
            records.append(record)
    return records


def _derive_navigation(
    navigation: Any,
) -> List[Dict[str, Any]]:
    """F3: nav/NCX TOC document parse failure → a single record (or empty)."""
    if not isinstance(navigation, dict):
        return []
    error = navigation.get("error")
    summary = _error_summary(error)
    if summary is None:
        return []
    path = navigation.get("path")
    if not isinstance(path, str) or not path:
        return []
    kind = navigation.get("kind")
    record = build_unresolved_record(
        object_type="navigation",
        object_id=path,
        resolution="unresolved",
        reason_code="NAV_DOCUMENT_UNREADABLE",
        source=path,
        placeholder_text=None,
        kind=kind if isinstance(kind, str) else None,
        error_summary=summary,
    )
    return [record] if record is not None else []


def derive_epub_unresolved(
    failed_chapters: Any,
    skipped_spine_items: Any,
    navigation: Any,
) -> List[Dict[str, Any]]:
    """Derive unresolved-registry records from existing fact sources (prereg §2).

    Args:
        failed_chapters: ``epub_package["failed_chapters"]``
            (``[{"href": str, "error": str}, ...]``).
        skipped_spine_items: ``epub_package["skipped_spine_items"]``
            (``[{"idref", "href"?, "reason"}, ...]`` three-type literals).
        navigation: ``epub_navigation`` dict (a single NAV_DOCUMENT_UNREADABLE
            record is derived only when ``error`` is non-empty).

    Returns:
        A contract-compliant record list, order frozen as F1 → F2 → F3,
        each group keeping the fact source's original order (may be empty —
        the caller writes only non-empty via ``stamp_unresolved_registry``).
    """
    return (
        _derive_failed_chapters(failed_chapters)
        + _derive_skipped_spine_items(skipped_spine_items)
        + _derive_navigation(navigation)
    )
