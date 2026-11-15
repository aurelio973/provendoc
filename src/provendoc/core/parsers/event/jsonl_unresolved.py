# -*- coding: utf-8 -*-
"""jsonl domain object-level unresolved-registry derivation (jsonl_unresolved_prereg_v1 frozen).

Derived-view discipline:

- no new detection logic — every record is derived 1:1 from the existing
  fact source ``invalid_lines`` (per-item ``{"line": n, "reason": ...}``);
- the existing fact-source key is kept verbatim, never rewritten or
  migrated (the single source of truth stays in the original key; this
  module produces the object-level detail view under the marker_contract
  v1 contract);
- construction goes through
  ``core.foundation.unresolved_registry.build_unresolved_record``
  (contract validation, one bad record does not spoil the batch);
  writing goes through ``stamp_unresolved_registry``
  (write only when non-empty, honest absence);
- ``placeholder_text`` is always ``None`` (the body preview is a records
  reconstruction, invalid lines have no placeholder line, PDF precedent);
- fail-silent degradation: any input exception is caught by the caller's
  guard; this module itself only defensively skips unexpected shapes
  (entry not a dict / line not a positive int / reason not a string).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)

# error_summary truncation threshold matches the txt F3 delegation failure (120 chars)
_ERROR_SUMMARY_MAX = 120


def derive_jsonl_unresolved(
    invalid_lines: Any,
    max_line_length: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Derive unresolved-registry records from the existing ``invalid_lines`` fact source (prereg §2).

    Args:
        invalid_lines: the parser's line-scan accumulated failure list
            (``[{"line": n, "reason": ...}, ...]``, n is 1-based).
        max_line_length: the effective per-line length cap (OVERSIZED_LINE
            evidence field).

    Returns:
        A contract-compliant record list (preserving fact-source order;
        may be empty — the caller writes via ``stamp_unresolved_registry``
        only when non-empty).

    Mapping (prereg §3 frozen, exhaustive for the current fact source):

    - ``reason == "oversized_line"`` -> ``OVERSIZED_LINE``;
    - any other reason (``json.loads`` exception string) ->
      ``INVALID_JSON_LINE``;
    - single-tier note: the fact source only produces the ``unresolved``
      tier (invalid line content is fully discarded — no partial
      recovery, no unsupported-type semantics), already registered as
      deferred in prereg §2.
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(invalid_lines, list):
        return records
    for entry in invalid_lines:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line")
        reason = entry.get("reason")
        # bool is an int subclass, exclude explicitly; line coordinates are 1-based positive ints
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            continue
        if not isinstance(reason, str) or not reason:
            continue
        if reason == "oversized_line":
            record = build_unresolved_record(
                object_type="line",
                object_id=f"line-{line}",
                resolution="unresolved",
                reason_code="OVERSIZED_LINE",
                source="document",
                placeholder_text=None,
                line=line,
                max_line_length=max_line_length,
            )
        else:
            record = build_unresolved_record(
                object_type="line",
                object_id=f"line-{line}",
                resolution="unresolved",
                reason_code="INVALID_JSON_LINE",
                source="document",
                placeholder_text=None,
                line=line,
                error_summary=reason[:_ERROR_SUMMARY_MAX],
            )
        if record is not None:
            records.append(record)
    return records
