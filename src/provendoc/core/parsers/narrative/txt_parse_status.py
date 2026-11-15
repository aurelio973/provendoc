# ============================================================
# Narrative Domain
# TXT document-level three-state parse status (derived view)
# ============================================================
"""Document-level ``parse_status`` derivation for TXT parses.

Frozen rules (txt_engineering_gaps_prereg_v1 G1): the status is a
*derived view* over already-registered evidence keys — no new
detection logic, no rewriting of source keys.

- ``failed``  — ``parse_failed`` is truthy (total failure fallback);
- ``partial`` — any loss/degradation evidence is present:
  ``truncation.truncated``, ``disguised_container`` registered,
  ``delegation.status == "failed"``,
  ``sanitization.replacement_injected > 0``, ``encoding_fallback``
  truthy, or ``txt_unresolved`` non-empty;
- ``complete`` — none of the above.

Reason strings are frozen and emitted in frozen order so the fact is
deterministic across runs.
"""

from typing import Any, Dict


def derive_parse_status(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the document-level three-state status from metadata.

    Args:
        metadata: the parse metadata dict (may be partial on the
            failure path; every lookup is defensive).

    Returns:
        ``{"status": "complete"|"partial"|"failed", "reasons": [...]}``.
        ``reasons`` is empty for ``complete``; for ``failed`` it is
        ``["parse_failed"]`` plus any partial evidence already seen.
    """
    reasons = []

    truncation = metadata.get("truncation")
    if isinstance(truncation, dict) and truncation.get("truncated"):
        reasons.append("truncation")

    if metadata.get("disguised_container"):
        reasons.append("disguised_container")

    delegation = metadata.get("delegation")
    if isinstance(delegation, dict) and delegation.get("status") == "failed":
        reasons.append("delegation_failed")

    sanitization = metadata.get("sanitization")
    if isinstance(sanitization, dict) and (sanitization.get("replacement_injected") or 0) > 0:
        reasons.append("replacement_injected")

    if metadata.get("encoding_fallback"):
        reasons.append("encoding_fallback")

    unresolved = metadata.get("txt_unresolved")
    if isinstance(unresolved, dict):
        entries = unresolved.get("entries") or unresolved.get("records")
        if entries:
            reasons.append("txt_unresolved")
    elif unresolved:
        reasons.append("txt_unresolved")

    if metadata.get("parse_failed"):
        return {"status": "failed", "reasons": ["parse_failed", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}
