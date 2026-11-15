# -*- coding: utf-8 -*-
"""txt-domain object-level unresolved-registry derivation (txt_unresolved_prereg_v1 frozen).

Derived-view discipline:

- No new detection logic — every record is derived from existing
  observation keys (``truncation`` / ``disguised_container`` /
  ``delegation``);
- Existing observation keys stay untouched — not rewritten, not
  migrated (the single source of truth remains the original keys;
  this module produces an object-level detail view under the
  marker_contract v1 contract);
- Records are built via
  ``core.foundation.unresolved_registry.build_unresolved_record``
  (contract validation fails one record, not the batch); written via
  ``stamp_unresolved_registry`` (write only when non-empty — honest
  absence);
- ``placeholder_text`` is always ``None`` (txt has no body
  placeholder lines; PDF precedent);
- Silent failure degradation: input anomalies are guarded by the
  caller; this module itself only skips unexpected shapes
  defensively.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)
from provendoc.core.structure.special_modules import CODE_FENCE_RE

# prereg §3 frozen list (v1, append-only, semantics never changed);
# the two F1 tail kinds share the tail anchor.
_TAIL_REASON_CODES = {
    "INCOMPLETE_TRAILING_SEQUENCE": "TRUNCATED_TRAILING_SEQUENCE",
    "INCOMPLETE_UTF8_SEQUENCE_AT_EOF": "TRUNCATED_UTF8_SEQUENCE_AT_EOF",
}


def _append(records: List[Dict[str, Any]],
            record: Optional[Dict[str, Any]]) -> None:
    """append-if-valid (silently skip a record when build's contract validation fails)."""
    if record is not None:
        records.append(record)


def derive_txt_unresolved(
        metadata: Dict[str, Any], text: str,
) -> List[Dict[str, Any]]:
    """Derive txt-domain unresolved-registry records from existing observation keys (prereg §2 frozen rules).

    Args:
        metadata: Document.metadata carrying the observation keys
            (read-only).
        text: cleaned body text (for line anchoring; the coordinate
            system matches the engineering batch's position fields:
            1-based).

    Returns:
        A contract-compliant record list (possibly empty — the
        caller writes via ``stamp_unresolved_registry`` only when
        non-empty).
    """
    records: List[Dict[str, Any]] = []
    lines = text.splitlines()
    tail_id = f"line-{len(lines)}" if lines else "document"

    # F1 truncation (one record per evidence kind; unknown kinds are
    # skipped — a new kind must be added to the prereg first
    # (append-only), no mapping guessed here).
    truncation = metadata.get("truncation")
    if isinstance(truncation, dict) and truncation.get("truncated"):
        evidence = truncation.get("evidence")
        kinds = [k for k in evidence if isinstance(k, str)] \
            if isinstance(evidence, list) else []
        for kind in kinds:
            if kind in _TAIL_REASON_CODES:
                _append(records, build_unresolved_record(
                    object_type="tail",
                    object_id=tail_id,
                    resolution="partial",
                    reason_code=_TAIL_REASON_CODES[kind],
                    source="eof",
                    placeholder_text=None,
                    evidence=kinds,
                    truncation_ref=True,
                ))
            elif kind == "UNCLOSED_CODE_FENCE":
                fence_lines = [
                    i for i, ln in enumerate(lines, 1)
                    if CODE_FENCE_RE.match(ln.strip())
                ]
                _append(records, build_unresolved_record(
                    object_type="code_fence",
                    object_id=(
                        f"line-{fence_lines[-1]}"
                        if fence_lines else tail_id
                    ),
                    resolution="partial",
                    reason_code="UNCLOSED_CODE_FENCE",
                    source="document",
                    placeholder_text=None,
                    evidence=kinds,
                    fence_line_count=len(fence_lines),
                ))

    # F2 disguised container (lenient tier: container interpretation
    # abandoned per policy; the whole document is the object).
    disguised = metadata.get("disguised_container")
    if isinstance(disguised, dict):
        _append(records, build_unresolved_record(
            object_type="container",
            object_id="document",
            resolution="unsupported",
            reason_code="DISGUISED_CONTAINER_PARSED_AS_TEXT",
            source="header",
            placeholder_text=None,
            family=disguised.get("family"),
            evidence_hex=disguised.get("evidence_hex"),
            action=disguised.get("action"),
        ))

    # F3 delegation failure (structured interpretation not recovered,
    # plain text recovered → partial; ok / not_attempted derive
    # nothing — neither success nor policy-driven non-delegation is
    # an absence).
    delegation = metadata.get("delegation")
    if isinstance(delegation, dict) and delegation.get("status") == "failed":
        _append(records, build_unresolved_record(
            object_type="subtype_structure",
            object_id="document",
            resolution="partial",
            reason_code="SUBTYPE_DELEGATION_FAILED",
            source="document",
            placeholder_text=None,
            subtype=delegation.get("subtype"),
            delegate=delegation.get("delegate"),
            error_summary=delegation.get("reason"),
            fallback=delegation.get("fallback"),
        ))

    return records
