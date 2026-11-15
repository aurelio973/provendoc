# -*- coding: utf-8 -*-
"""Object-level unresolved-record derivation for the code domain (code_unresolved_prereg_v1, frozen).

Derived-view discipline:

- No new detection logic — every record is derived 1:1 from existing fact
  sources: the four diagnostic codes in the ``metadata["code"]["diagnostics"]``
  export window (syntax_error / missing_node / node_limit_reached / parse_failed);
- Existing fact-source keys are kept verbatim — ``metadata["code"]`` is byte-for-byte
  unchanged (single source of truth stays at the original keys); this module only
  produces an object-level detail view under the marker_contract v1 contract;
- Records are built via ``unresolved_registry.build_unresolved_record`` (contract
  validation: one bad record never corrupts a batch); writing is done by the caller
  via ``stamp_unresolved_registry`` (non-empty writes only, honest absence);
- ``placeholder_text`` is always ``None`` (source text lives intact in
  ``Document.text``, no placeholder lines);
- Scope = export window (``_DIAGNOSTICS_EXPORT_LIMIT`` 100 in contracts.py): no
  records for out-of-window diagnostics (prereg §1 F8; the full count lives in
  ``stats.diagnostic_count``);
- Unknown ``code`` values: defensive skip + debug log, never MALFORMED (closed-set
  discipline, same precedent as config-domain unknown reference states);
- Failures degrade silently: any unexpected shape (non-dict / missing code / bad
  shape) is skipped defensively and never blocks the main parse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)

_log = logging.getLogger(__name__)

# message truncation matches jsonl / txt / epub / config (120 chars);
# the snippet fact source is already clipped to 120 (_clip_snippet) — derived verbatim.
_MESSAGE_MAX = 120

# prereg §2 — diagnostic code → (resolution, reason_code) map, 1:1,
# uppercase, no invented words. Codes outside the four are defensively skipped —
# new codes must first extend the prereg (add-only, no re-meaning).
_DIAG_CODE_MAP = {
    "syntax_error": ("partial", "SYNTAX_ERROR"),
    "missing_node": ("partial", "MISSING_NODE"),
    "node_limit_reached": ("partial", "NODE_LIMIT_REACHED"),
    "parse_failed": ("unresolved", "PARSE_FAILED"),
}

# The six coordinate fields SourceSpan.to_dict flattens into the diagnostic dict
# (contracts.py L102-110); spanless diagnostics carry none.
_SPAN_FIELDS = (
    "line_start", "line_end",
    "col_start", "col_end",
    "byte_start", "byte_end",
)


def _str_or_none(value: Any) -> Optional[str]:
    """Defensive narrowing of evidence strings."""
    return value if isinstance(value, str) else None


def _int_or_none(value: Any) -> Optional[int]:
    """Defensive narrowing of coordinates (bools are not coordinates)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def derive_code_unresolved(
    code_metadata: Any,
) -> List[Dict[str, Any]]:
    """Derive object-level unresolved records from the ``metadata["code"]`` export dict.

    Order frozen = ``diagnostics`` list order, 1:1 (k is the stable index inside
    the export window); object_id ``diag-{k}``; spanless diagnostics
    (node_limit_reached / parse_failed) use source ``document``, spanned ones use
    ``line-{n}``.

    Args:
        code_metadata: the ``metadata["code"]`` export dict (from
            ``CodeStructure.to_metadata``); unexpected shapes return an empty list.

    Returns:
        Contract-compliant records (possibly empty — honest absence is the caller's call).
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(code_metadata, dict):
        return records
    diagnostics = code_metadata.get("diagnostics")
    if not isinstance(diagnostics, list):
        return records

    language = _str_or_none(code_metadata.get("language"))
    backend = _str_or_none(code_metadata.get("backend"))
    status = _str_or_none(code_metadata.get("status"))

    for k, diagnostic in enumerate(diagnostics):
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        if not isinstance(code, str) or not code:
            continue
        mapped = _DIAG_CODE_MAP.get(code)
        if mapped is None:
            # Code outside the closed set: defensive skip (never MALFORMED),
            # reason logged per prereg §2.
            _log.debug(
                "code unresolved: unknown diagnostic code %r skipped",
                code,
            )
            continue
        resolution, reason_code = mapped

        line_start = _int_or_none(diagnostic.get("line_start"))
        source = f"line-{line_start}" if line_start is not None else "document"

        evidence: Dict[str, Any] = {}
        if language is not None:
            evidence["language"] = language
        if backend is not None:
            evidence["backend"] = backend
        if status is not None:
            evidence["status"] = status
        severity = _str_or_none(diagnostic.get("severity"))
        if severity is not None:
            evidence["severity"] = severity
        message = _str_or_none(diagnostic.get("message"))
        if message is not None:
            evidence["message"] = message[:_MESSAGE_MAX]
        snippet = _str_or_none(diagnostic.get("snippet"))
        if snippet is not None:
            evidence["snippet"] = snippet
        for field in _SPAN_FIELDS:
            value = _int_or_none(diagnostic.get(field))
            if value is not None:
                evidence[field] = value

        record = build_unresolved_record(
            object_type="diagnostic",
            object_id=f"diag-{k}",
            resolution=resolution,
            reason_code=reason_code,
            source=source,
            placeholder_text=None,
            **evidence,
        )
        if record is not None:
            records.append(record)
    return records
