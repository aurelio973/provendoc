"""Scenario audit dimension (RSE, audit-class — observation without thresholds).

Implements scenario_framework_prereg_v1 §3.1 (landed ahead of the
freeze). Pure
observation: these numbers describe how the L2 label layer behaves
on a corpus — they are never gates and never feed the quality
scores (a gate here would invite label tuning, the exact
score-gaming the framework forbids).

Dimensions (per prereg §3.1):

* ``scenario_label_present`` — share of documents whose scenario
  payload carries a non-empty label (pipeline injection audit;
  missing labels mean a wiring leak, not a document defect);
* ``scenario_evidence_trace_present`` — share carrying a non-empty
  resolution trace (``evidence`` list);
* ``fallback_rate`` — share resolved to ``general_prose`` (corpus
  composition observation, NOT quality — fallback is a legal
  label);
* ``runner_up_close_margin`` — list of runner-up margins whose
  voting_kinds difference is <= 1 (the mislabel high-risk zone
  named by the prereg); reported per-document, never aggregated
  into a score;
* ``label_distribution`` — raw counts per label.

Input contract: an iterable of ``metadata["scenario"]`` dicts (the
``scenario_resolver_v2`` payload shape); malformed/missing entries
are counted honestly, never coerced. Pure functions; no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable

from provendoc.core.scenario.resolver import is_close_margin_label

GENERAL_PROSE_LABEL = "general_prose"


def _label_of(scenario: Any) -> str | None:
    if isinstance(scenario, dict):
        label = scenario.get("label")
        if isinstance(label, str) and label:
            return label
    return None


def _has_trace(scenario: Any) -> bool:
    return (
        isinstance(scenario, dict)
        and isinstance(scenario.get("evidence"), list)
        and len(scenario["evidence"]) > 0
    )


def _runner_up_margin(scenario: Any) -> int | None:
    if not isinstance(scenario, dict):
        return None
    runner_up = scenario.get("runner_up")
    if not isinstance(runner_up, dict):
        return None
    margin = runner_up.get("margin")
    if not isinstance(margin, dict):
        return None
    diff = margin.get("voting_kinds")
    return diff if isinstance(diff, int) else None


def scenario_quality_doc(scenario: Any) -> dict[str, Any]:
    """Per-document audit row (facts only, no verdicts).

    A missing/malformed scenario payload is an honest ``absent`` row,
    not an error.
    """
    label = _label_of(scenario)
    return {
        "label_present": label is not None,
        "label": label,
        "evidence_trace_present": _has_trace(scenario),
        "runner_up_margin": _runner_up_margin(scenario),
    }


def scenario_quality_batch(scenarios: Iterable[Any]) -> dict[str, Any]:
    """Batch-level audit aggregate over per-document scenario payloads.

    Observations only — every number is a corpus fact, none is a
    gate. Empty input yields zeroed stats (an honest empty batch,
    never a failure).
    """
    # materialize once: the close-margin scan re-reads each payload
    # (single-source predicate in governance prereg §2.0) — a plain
    # Iterable input must not be exhausted mid-function.
    payloads = list(scenarios)
    rows = [scenario_quality_doc(s) for s in payloads]
    total = len(rows)
    labeled = sum(1 for r in rows if r["label_present"])
    traced = sum(1 for r in rows if r["evidence_trace_present"])
    fallback = sum(
        1 for r in rows if r["label"] == GENERAL_PROSE_LABEL
    )
    # scenario_low_confidence_governance_prereg_v1 §2.0: the close-
    # margin predicate is the resolver-owned single source; this
    # list merely applies it per row (semantics locked against the
    # previous inline criterion by governance t8).
    close_margin_docs = [
        {
            "label": r["label"],
            "runner_up_margin": r["runner_up_margin"],
        }
        for scenario, r in zip(payloads, rows)
        if is_close_margin_label(scenario)
    ]
    distribution: dict[str, int] = {}
    for r in rows:
        if r["label"] is not None:
            distribution[r["label"]] = distribution.get(r["label"], 0) + 1

    return {
        "extractor": "scenario_quality_v1",
        "docs": total,
        "scenario_label_present": {
            "present": labeled,
            "total": total,
            "ratio": (labeled / total) if total else None,
        },
        "scenario_evidence_trace_present": {
            "present": traced,
            "total": total,
            "ratio": (traced / total) if total else None,
        },
        "fallback_rate": (fallback / total) if total else None,
        # per-document mislabel high-risk list (v1: report only)
        "runner_up_close_margin": close_margin_docs,
        "label_distribution": distribution,
        "note": (
            "audit-only observation per scenario_framework prereg "
            "§3.1: never a gate, never a score input; fallback_rate "
            "is corpus composition, not quality"
        ),
    }


__all__ = [
    "GENERAL_PROSE_LABEL",
    "scenario_quality_batch",
    "scenario_quality_doc",
]
