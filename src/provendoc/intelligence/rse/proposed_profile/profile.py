# -*- coding: utf-8 -*-
"""Profile composition for proposed_quality_profile_v1.

composite = equal-weight 0.25 x the 4 evaluable dimensions; N/A
dimensions renormalize (never silently scored as full marks).
Weights stay provisional: status is always PROPOSED, composite
carries provisional=True with declared validation gaps, and no
quality_score is exported before D1-D4 validation completes.
"""

from typing import Any, Dict, List, Optional

from provendoc.intelligence.rse.proposed_profile.event_ledger import (
    DIM_BOUNDARY,
    DIM_CONTENT,
    DIM_CONTEXT,
    DIM_SEGMENTATION,
    DIM_STRUCTURAL,
    SCHEMA_VERSION,
    SCORING_DIMENSIONS,
    EventLedger,
)

PROPOSED_PROFILE_VERSION = "proposed_quality_profile_v1"

# Equal provisional weights: 0.25 each; D0 observation carries no weight.
PROVISIONAL_WEIGHTS: Dict[str, float] = {
    DIM_STRUCTURAL: 0.25,
    DIM_BOUNDARY: 0.25,
    DIM_CONTEXT: 0.25,
    DIM_SEGMENTATION: 0.25,
    DIM_CONTENT: 0.0,  # 观察项，v1 不计分（DDR §1.1）
}

_VALIDATION_GAPS = [
    "no cross-type admission evidence yet (DDR §6.2: en/zh x type x "
    "length matrix, >=30 paired observations pending)",
    "no retrieval-QA benchmark adjudication yet (final authority per "
    "DDR rule 6)",
    "scale-free event rates only; no calibrated thresholds (retired "
    "0.8/0.7 scale stays retired per DDR rule 4)",
]

# Dimension score derivation (always from ledger/observation).


def _structural_score(observation: Dict[str, Any]) -> Optional[float]:
    if not observation.get("applicable"):
        return None
    denominator = observation.get("denominator") or 0
    if denominator <= 0:
        return None
    return round(1.0 - observation["events"] / denominator, 4)


def _boundary_score(observation: Dict[str, Any]) -> Optional[float]:
    if not observation.get("applicable"):
        return None
    rate = observation.get("invalid_boundary_rate")
    if rate is None:
        # No evaluatable cuts is not perfection: honest N/A.
        return None
    return round(1.0 - rate, 4)


def _context_score(observation: Dict[str, Any]) -> Optional[float]:
    if not observation.get("applicable"):
        return None
    denominator = observation.get("denominator") or 0
    if denominator <= 0:
        return None
    return round(1.0 - observation["events"] / denominator, 4)


def _segmentation_score(observation: Dict[str, Any]) -> Optional[float]:
    if not observation.get("applicable"):
        return None
    denominator = observation.get("denominator") or 0
    if denominator <= 0:
        return None
    # Provisional: unforced events / total chunks.
    return round(1.0 - observation["unforced_events"] / denominator, 4)


_SCORERS = {
    DIM_STRUCTURAL: _structural_score,
    DIM_BOUNDARY: _boundary_score,
    DIM_CONTEXT: _context_score,
    DIM_SEGMENTATION: _segmentation_score,
}


def build_proposed_profile(
    ledger: EventLedger,
    observations: Dict[str, Dict[str, Any]],
    policy_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Compose the proposed_quality_profile_v1 from a ledger
    and per-dimension observations.

    Facts and derived scores only: composite = equal-weight mean
    of evaluable dimensions; all N/A -> score None (honest gap,
    no fabricated 1.0); D0 rides along as observation-only with
    weight 0, outside the composite.
    """
    dimensions: Dict[str, Any] = {}
    scores: List[float] = []

    for dim in SCORING_DIMENSIONS:
        observation = observations.get(dim) or {"applicable": False}
        weight = PROVISIONAL_WEIGHTS[dim]
        entry: Dict[str, Any] = {
            "weight": weight,
            "score": None,
            "observation": observation,
        }
        score = _SCORERS[dim](observation)
        if score is not None:
            entry["score"] = score
            scores.append(score)
        dimensions[dim] = entry

    composite = None
    if scores:
        composite = round(sum(scores) / len(scores), 4)

    content_observation = observations.get(DIM_CONTENT) or {
        "applicable": False
    }

    return {
        "profile_version": PROPOSED_PROFILE_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "status": "PROPOSED",
        "canonical": False,
        "provisional": True,
        "weights": dict(PROVISIONAL_WEIGHTS),
        "weight_note": (
            "equal weights are provisional pending pre-registered "
            "validation (DDR rule 6); do not compare with "
            "invariant_v1 composite without explicit labeling"
        ),
        "dimensions": dimensions,
        "composite": composite,
        "composite_basis": {
            "scored_dimensions": len(scores),
            "total_dimensions": len(SCORING_DIMENSIONS),
            "renormalized": len(scores) < len(SCORING_DIMENSIONS),
        },
        "content_observation": content_observation,
        "event_ledger": ledger.to_dict(),
        "event_count": len(ledger.events),
        "scoring_event_count": sum(
            1 for e in ledger.events if e.counts_toward_quality
        ),
        "policy_version": policy_version,
        "validation_gaps": list(_VALIDATION_GAPS),
        "release_note": (
            "bleeding-stop policy: until D1-D4 pass the "
            "pre-registered validation matrix, the composite in "
            "this profile must not be exported as a quality "
            "verdict; gate three-state + factual events only "
            "(DDR §8)"
        ),
    }
