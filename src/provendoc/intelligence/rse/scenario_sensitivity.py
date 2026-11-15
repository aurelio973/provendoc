# -*- coding: utf-8 -*-
"""Scenario counterfactual sensitivity dimension (RSE, audit-class — observation without thresholds).

Implements scenario_low_confidence_governance_prereg_v1 §2.3 (P3).
Pure observation: for documents where the scenario label drove a
chunking-strategy branch, the dual-run quality gap (scenario policy
vs generic policy) is a mislabel evidence stream — a counterfactual
win (generic strictly better) is a *candidate* mislabel signal, said
out loud and never gated on:

* nothing in this module enters a quality score, a gate, or a
  pipeline decision (audit-only; a gate here would invite label
  tuning, the exact score-gaming the framework forbids);
* the dual run itself (chunking both ways + scoring both ways) is
  the CALLER's job — offline probes and sampled corpora only;
* admission for future gating requires human-labeled ground truth
  accumulated to sufficient n and follows the csc admission-prereg
  precedent (explicitly out of scope for this batch).

Input contract: per-document observation rows with two optional
quality scores (``quality_scenario`` / ``quality_generic``) and the
resolver payload facts (label, runner_up margin — see
``is_close_margin_label`` in core/scenario/resolver.py for the
single-source low-confidence verdict). Malformed inputs are counted
honestly, never coerced. Pure functions; no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

_LABEL_FALLBACK = "general_prose"
_CLOSE_MARGIN_THRESHOLD = 1


def _valid_score(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _clean_label(label: Any) -> Optional[str]:
    if isinstance(label, str) and label:
        return label
    return None


def _clean_margin(margin: Any) -> Optional[int]:
    if isinstance(margin, int) and not isinstance(margin, bool):
        return margin
    return None


def scenario_sensitivity_doc(
    label: Any,
    runner_up_margin: Any,
    quality_scenario: Any,
    quality_generic: Any,
) -> dict[str, Any]:
    """Per-document counterfactual observation row (facts, no verdicts).

    ``sensitivity`` is the signed gap ``scenario - generic`` (positive:
    the scenario policy scored higher); ``counterfactual_wins`` is True
    only when the generic run is *strictly* better. Either score
    missing → both ``None`` (an honest incomparable row, never a
    coerced zero). ``low_confidence`` mirrors the resolver single
    source (label non-fallback + margin int ≤ threshold).
    """
    label_clean = _clean_label(label)
    margin_clean = _clean_margin(runner_up_margin)
    score_scenario = _valid_score(quality_scenario)
    score_generic = _valid_score(quality_generic)

    if score_scenario is not None and score_generic is not None:
        sensitivity = round(score_scenario - score_generic, 6)
        counterfactual_wins = score_generic > score_scenario
    else:
        sensitivity = None
        counterfactual_wins = None

    low_confidence = (
        label_clean is not None
        and label_clean != _LABEL_FALLBACK
        and margin_clean is not None
        and margin_clean <= _CLOSE_MARGIN_THRESHOLD
    )

    return {
        "label": label_clean,
        "runner_up_margin": margin_clean,
        "quality_scenario": score_scenario,
        "quality_generic": score_generic,
        "sensitivity": sensitivity,
        "counterfactual_wins": counterfactual_wins,
        "low_confidence": low_confidence,
    }


def scenario_sensitivity_batch(
    rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Corpus-level aggregation over sensitivity observation rows.

    Observations only — every number is a corpus fact, none is a
    gate. ``counterfactual_wins`` lists the mislabel CANDIDATES
    (generic policy strictly outscored the scenario policy) verbatim;
    accumulating this list toward a labeled ground-truth set is the
    explicitly-deferred admission prerequisite (§0). Empty input
    yields zeroed stats (an honest empty batch, never a failure).
    """
    materialized = [r for r in rows if isinstance(r, dict)]
    total = len(materialized)
    comparable = sum(
        1
        for r in materialized
        if r.get("counterfactual_wins") is not None
    )
    wins = [
        {
            "label": r.get("label"),
            "runner_up_margin": r.get("runner_up_margin"),
            "sensitivity": r.get("sensitivity"),
            "low_confidence": r.get("low_confidence"),
        }
        for r in materialized
        if r.get("counterfactual_wins") is True
    ]
    low_confidence_count = sum(
        1 for r in materialized if r.get("low_confidence") is True
    )
    return {
        "total": total,
        "comparable": comparable,
        "low_confidence": low_confidence_count,
        "counterfactual_win_count": len(wins),
        "counterfactual_wins": wins,
        "note": (
            "audit-only observation per "
            "scenario_low_confidence_governance_prereg_v1 §2.3: "
            "never a gate, never a score input; mislabel candidates "
            "await human-labeled ground truth before any admission "
            "prereg"
        ),
    }


__all__ = [
    "scenario_sensitivity_batch",
    "scenario_sensitivity_doc",
]
