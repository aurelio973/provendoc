# ============================================================
# Code Domain
# Document-level three-state parse status (derived view)
# ============================================================
"""Document-level ``parse_status`` derivation for code-domain parses.

Frozen rules (code_engineering_facts_prereg_v1 P1, frozen 2026-10-25):
the status is a *derived view* over already-registered evidence keys —
no new detection logic, no rewriting of source keys.

- ``failed``  — ``parse_failed`` is truthy, or the code payload status
  is ``failed`` (total parse failure);
- ``partial`` — any loss/degradation evidence is present: code status
  ``recovered`` (syntax recovery), code status ``degraded`` (grammar
  load failure), ``sanitization.replacement_injected > 0``,
  ``encoding_fallback`` truthy, or ``code_unresolved`` non-empty;
- ``complete`` — none of the above (``ok`` and the legitimate ``empty``
  payload status both land here; an empty file is a valid input and is
  never punished).

Reason strings are frozen and emitted in frozen order so the fact is
deterministic across runs.
"""

from typing import Any, Dict


def derive_code_parse_status(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the document-level three-state status from metadata.

    Args:
        metadata: the parse metadata dict (may be partial on the
            failure path; every lookup is defensive).

    Returns:
        ``{"status": "complete"|"partial"|"failed", "reasons": [...]}``.
        ``reasons`` is empty for ``complete``; for ``failed`` it is
        ``["parse_failed"]`` plus any partial evidence already seen.
    """
    reasons: list = []

    code = metadata.get("code")
    status = code.get("status") if isinstance(code, dict) else None

    if status == "recovered":
        reasons.append("syntax_recovery")
        if isinstance(code, dict) and code.get("diagnostics"):
            reasons.append("diagnostics_present")
    elif status == "degraded":
        reasons.append("grammar_degraded")

    sanitization = metadata.get("sanitization")
    if isinstance(sanitization, dict) and (sanitization.get("replacement_injected") or 0) > 0:
        reasons.append("replacement_injected")

    if metadata.get("encoding_fallback"):
        reasons.append("encoding_fallback")

    unresolved = metadata.get("code_unresolved")
    if isinstance(unresolved, dict):
        entries = unresolved.get("entries") or unresolved.get("records")
        if entries:
            reasons.append("code_unresolved")
    elif unresolved:
        reasons.append("code_unresolved")

    if metadata.get("parse_failed") or status == "failed":
        return {"status": "failed", "reasons": ["parse_failed", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}
