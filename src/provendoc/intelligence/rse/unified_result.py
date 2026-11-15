"""Unified evaluation-result synthesizer (narrative track).

Contract (artifact_contract.md §3):
    failure_modes is a first-class evaluation dimension, complementary to continuous metrics:
    - the invariant track (invariant_composite) answers "structural health"
    - failure_modes (discrete diagnostics) answers "retrieval risk"

    Iron rule: failure_modes.risk_score must never be mixed into the invariant_composite weighting;
    if an optimizer uses it, it must be a separate strategy rule, never a covert rewrite of the objective.

This module only does the "synthesis" — merging the return dicts of evaluators such as NarrativeQuality
with the FailureReport into a single evaluation payload
(evaluation/*_evaluation.json), recomputing no metric.
"""

from dataclasses import asdict
from typing import Any, Dict, Optional

from provendoc.integrity.failure_modes import CONFIRMED_THRESHOLD

# failure mode → three-layer classification (matching the FailureMode enum)
CATEGORY_STRUCTURE_BREAK = {
    "code_explanation_split",
    "table_description_split",
    "procedure_step_split",
    "list_item_split",
}
CATEGORY_CONTEXT_LOSS = {
    "heading_ambiguity",
    "section_context_loss",
    "cross_reference_break",
    "hierarchy_ambiguity",
}
CATEGORY_SEMANTIC_DRIFT = {
    "definition_example_split",
    "explanation_code_split",
    "discourse_split",
    "semantic_drift",
}


def _failure_confirmed(f) -> bool:
    """Whether a failure instance (dataclass or dict after asdict) is confirmed."""
    if isinstance(f, dict):
        return float(f.get("confidence", 1.0)) >= CONFIRMED_THRESHOLD
    return bool(getattr(f, "confirmed", True))


def _risk_status(risk_score: float, failures: list) -> str:
    """Risk status label (display only, never enters overall)."""
    if any(
        getattr(f, "severity", "") == "critical"
        or (isinstance(f, dict) and f.get("severity") == "critical")
        for f in failures
        if _failure_confirmed(f)
    ):
        return "critical"
    if risk_score >= 0.4:
        return "warning"
    if risk_score > 0:
        return "minor"
    return "clean"


def _failure_view(failure) -> Dict[str, Any]:
    """Formal view of a single failure instance."""
    if hasattr(failure, "__dataclass_fields__"):
        data = asdict(failure)
    elif isinstance(failure, dict):
        data = dict(failure)
    else:
        data = {"description": str(failure)}

    mode = data.get("mode")
    if mode is not None and hasattr(mode, "value"):
        mode = getattr(mode, "value")

    affected = data.get("affected_chunks") or []
    confidence = float(data.get("confidence", 1.0))
    return {
        "mode": mode,
        "severity": data.get("severity", "warning"),
        "description": data.get("description", ""),
        "affected_unit_indices": affected,
        "evidence": data.get("evidence", {}),
        "fix_strategy": data.get("fix_strategy", ""),
        "confidence": confidence,
        "confirmed": confidence >= CONFIRMED_THRESHOLD,
        "blocking": bool(data.get("blocking", False)),
    }


def _categorize(failure_views: list) -> Dict[str, list]:
    categories: Dict[str, list] = {
        "structure_break": [],
        "context_loss": [],
        "semantic_drift": [],
    }
    for view in failure_views:
        mode = view.get("mode") or ""
        if mode in CATEGORY_STRUCTURE_BREAK:
            categories["structure_break"].append(view)
        elif mode in CATEGORY_CONTEXT_LOSS:
            categories["context_loss"].append(view)
        elif mode in CATEGORY_SEMANTIC_DRIFT:
            categories["semantic_drift"].append(view)
        else:
            # Unknown modes fall back to structure_break to avoid data loss
            categories["structure_break"].append(view)
    return categories


def build_unified_evaluation(
    evaluation_result: Dict[str, Any],
    failure_report=None,
    document_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Synthesize the unified evaluation result (the formal payload of the evaluation JSON).

    Args:
        evaluation_result: the dict returned by an evaluator (e.g. NarrativeQuality.evaluate),
                    all fields preserved verbatim.
        failure_report: the FailureReport of RetrievalFailureBenchmark.run()
                        (may be None — with no benchmark run, failure_modes is marked
                        not_run, the schema is constant).
        document_id: optional document identifier.

    Returns:
        The unified evaluation dict. Evaluator numbers are preserved verbatim (no key of evaluation_result is touched);
        failure_modes joins the profile as a first-class dimension and provides fully expandable diagnostics.
    """
    unified = dict(evaluation_result)

    if document_id is not None:
        unified.setdefault("document_id", document_id)

    # failure report → views
    if failure_report is None:
        failure_payload = None
    elif hasattr(failure_report, "__dataclass_fields__"):
        failure_payload = asdict(failure_report)
    elif isinstance(failure_report, dict):
        failure_payload = dict(failure_report)
    else:
        failure_payload = None

    if failure_payload is not None:
        raw_failures = failure_payload.get("failures") or []
        failure_views = [_failure_view(f) for f in raw_failures]
        risk_score = float(failure_payload.get("risk_score") or 0.0)
        status = _risk_status(risk_score, raw_failures)
        summary = failure_payload.get("summary") or {}

        # FailureMode enum keys → str
        summary = {
            (k.value if hasattr(k, "value") else str(k)): v
            for k, v in summary.items()
        }

        failure_dimension = {
            "status": "available",
            "risk_score": round(risk_score, 3),
            "mode": "risk",
            "summary": summary,
            "total_count": len(failure_views),
        }
        failure_detail = {
            "status": "available",
            "total_count": len(failure_views),
            "risk_score": round(risk_score, 3),
            "risk_status": status,
            "summary": summary,
            "categories": _categorize(failure_views),
        }
    else:
        failure_dimension = {
            "status": "not_run",
            "risk_score": None,
            "mode": "risk",
            "summary": {},
            "total_count": 0,
        }
        failure_detail = {
            "status": "not_run",
            "total_count": 0,
            "risk_score": None,
            "risk_status": None,
            "summary": {},
            "categories": {
                "structure_break": [],
                "context_loss": [],
                "semantic_drift": [],
            },
        }

    # profile gains the failure_modes first-class dimension (summary level)
    profile = dict(unified.get("profile") or {})
    profile["failure_modes"] = failure_dimension
    unified["profile"] = profile

    # Fully expandable diagnostics (at the same level as issues / applicability)
    unified["failure_modes"] = failure_detail

    return unified
