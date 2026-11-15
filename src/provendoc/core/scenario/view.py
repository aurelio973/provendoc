# -*- coding: utf-8 -*-
"""Scenario soft view (architecture experiment, 2026-11-04): first slice of the
three-layer soft-semantics direction (generic parsing / scenario
understanding / task layer).

This module is a READ-ONLY projection over ``(domain_signals,
ScenarioDecision)`` that adds what the hard decision layer deliberately
does not carry:

- a deterministic confidence estimate (basis + identity + vote mass),
- first-class uncertainty (``uncertain`` flag instead of silently
  treating thin wins as truth),
- ALL present alternative families (mixed scenarios), not only the
  single runner-up the resolver records,
- shadow families (content conventions with votes but no quorum, e.g.
  watchlist contract content inside a sales order).

Design constraints (scenario prereg + low-confidence governance prereg):

- read-only: inputs are never mutated; resolver + domain_signals stay
  the single source of the hard decision; consumers can ignore the view
  entirely and nothing breaks;
- pure and deterministic: same inputs -> same view; no IO, no model;
- data, not contract: the numeric thresholds below are REGISTERED
  calibration starting points (scenario README, 2026-11-04 trial), not
  frozen contract numbers — the field-level GT round is expected to
  retune them;
- defensive: never raises on malformed payloads; degrades like the
  resolver does.

Motivating measured case (00c8791 Cumulus Sales Order FP, 2026-11-04
probe): a contract-shaped document whose only present family is
``invoice`` via mention-level evidence (invoice_word + total_fields,
no identity marker) wins the quorum solo — ``runner_up is None`` so
the close-margin governance (which needs an opponent) cannot see it.
The view prices that shape as uncertain instead of flipping the
document identity.

Corpus numbers (business_records 50-file human GT, 2026-11-04 probe):
confident TPs 8/18 at strong-identity 0.90; the solo FP is correctly
flagged, but 10/18 TPs ride quorum wins without identity evidence and
land uncertain at 0.50 — the uncertainty axis is right (identity
declaration is the discriminator) yet identity evidence is too sparse
for the gate to act alone; the field-level GT round and soft
constraints are the fix. No mixed instances in this corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from provendoc.core.scenario.resolver import (
    BASIS_FALLBACK,
    BASIS_QUORUM,
    BASIS_STRONG,
    LABEL_GENERAL_PROSE,
    ScenarioDecision,
    _ELIGIBLE_FAMILIES,
)

# Registered calibration starting points (data, not contract; see
# module docstring). Retuning belongs to the field-level GT round.
QUORUM_BASE_CONFIDENCE = 0.50
IDENTITY_BONUS = 0.10
EXTRA_VOTE_STEP = 0.05
STRONG_IDENTITY_CONFIDENCE = 0.90
STRONG_CONTENT_CONFIDENCE = 0.80
FALLBACK_CONFIDENCE = 0.10
QUORUM_CONFIDENCE_CAP = 0.75
UNCERTAIN_BELOW = 0.60
MIXED_SEPARATION = 0.15


@dataclass(frozen=True)
class ScenarioView:
    """Soft view of one scenario decision: price certainty, keep audit."""

    label: str
    basis: str
    confidence: float
    uncertain: bool
    identity: bool
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    shadow_families: list[dict[str, Any]] = field(default_factory=list)
    mixed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (lists copied, never aliased)."""
        return {
            "label": self.label,
            "basis": self.basis,
            "confidence": self.confidence,
            "uncertain": self.uncertain,
            "identity": self.identity,
            "alternatives": [dict(a) for a in self.alternatives],
            "shadow_families": [dict(s) for s in self.shadow_families],
            "mixed": self.mixed,
            "evidence": dict(self.evidence),
        }


def _identity_winner(
    label: str, marker_counts: dict[str, Any], quorum: Any
) -> bool:
    """Re-derive identity declaration for a family.

    Single re-derivation over ``quorum.strong_kinds`` (same table the
    resolver uses): the family carries identity iff some identity entry
    for it has its marker at min_count. A v2-era payload without the
    quorum table degrades to content-level (deterministic, never a
    raise) — same contract as ``_strong_eligible``.
    """
    return _strong_marker_hit(label, marker_counts, quorum, identity_only=True)


def _qualifies_strong(
    label: str, marker_counts: dict[str, Any], quorum: Any
) -> bool:
    """Re-derive strong qualification for a family (identity or
    content-level), so alternatives are priced by the basis they would
    actually have won with — not a flat quorum estimate."""
    return _strong_marker_hit(label, marker_counts, quorum, identity_only=False)


def _strong_marker_hit(
    label: str, marker_counts: dict[str, Any], quorum: Any,
    *, identity_only: bool,
) -> bool:
    if not isinstance(quorum, dict):
        return False
    entries = quorum.get("strong_kinds")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("family") != label:
            continue
        if identity_only and not entry.get("identity"):
            continue
        marker = entry.get("marker")
        min_count = entry.get("min_count", 1)
        if marker and marker_counts.get(marker, 0) >= min_count:
            return True
    return False


def _family_confidence(
    voting_kinds: int, identity: bool, basis: str
) -> float:
    """Deterministic confidence estimate (registered trial formula)."""
    if basis == BASIS_STRONG:
        return (
            STRONG_IDENTITY_CONFIDENCE
            if identity
            else STRONG_CONTENT_CONFIDENCE
        )
    if basis == BASIS_QUORUM:
        conf = QUORUM_BASE_CONFIDENCE
        if identity:
            conf += IDENTITY_BONUS
        conf += EXTRA_VOTE_STEP * max(0, voting_kinds - 2)
        return min(conf, QUORUM_CONFIDENCE_CAP)
    return FALLBACK_CONFIDENCE


def _family_facts(families: Any, name: str) -> dict[str, Any]:
    facts = families.get(name) if isinstance(families, dict) else None
    return facts if isinstance(facts, dict) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def scenario_view(
    signals: dict[str, Any], decision: ScenarioDecision
) -> ScenarioView:
    """Project (domain_signals, decision) into the soft view.

    Read-only and deterministic; never raises. ``signals`` may be any
    payload shape (missing/None keys degrade, mirroring the resolver).
    """
    families = (
        signals.get("families")
        if isinstance(signals, dict)
        else None
    )
    families = families if isinstance(families, dict) else {}
    quorum = (
        signals.get("quorum") if isinstance(signals, dict) else None
    )

    label = decision.label
    basis = decision.basis
    winner_facts = _family_facts(families, label)
    marker_counts = winner_facts.get("marker_counts")
    marker_counts = (
        marker_counts if isinstance(marker_counts, dict) else {}
    )
    winner_voting = _int(winner_facts.get("voting_kinds"))
    identity = _identity_winner(label, marker_counts, quorum)

    if basis == BASIS_FALLBACK or label == LABEL_GENERAL_PROSE:
        confidence = FALLBACK_CONFIDENCE
        identity = False
    else:
        confidence = _family_confidence(winner_voting, identity, basis)

    alternatives: list[dict[str, Any]] = []
    shadow_families: list[dict[str, Any]] = []
    for name, facts in sorted(families.items()):
        if not isinstance(facts, dict) or name == label:
            continue
        voting = _int(facts.get("voting_kinds"))
        if voting <= 0 and not facts.get("present"):
            continue
        entry = {
            "family": name,
            "voting_kinds": voting,
            "total_count": _int(facts.get("total_count")),
        }
        if facts.get("present") and name in _ELIGIBLE_FAMILIES:
            # only eligible families could have been the label, so only
            # they are alternatives; watchlist content (contract,
            # financial_statement, ...) is shadow — evidence without a
            # claim, exactly the mixed-scenario signal the Cumulus FP
            # case lacked
            alt_counts = facts.get("marker_counts")
            alt_counts = (
                alt_counts if isinstance(alt_counts, dict) else {}
            )
            alt_basis = (
                BASIS_STRONG
                if _qualifies_strong(name, alt_counts, quorum)
                else BASIS_QUORUM
            )
            entry["confidence"] = _family_confidence(
                voting, _identity_winner(name, alt_counts, quorum),
                alt_basis,
            )
            alternatives.append(entry)
        else:
            shadow_families.append(entry)

    mixed = any(
        confidence - alt["confidence"] < MIXED_SEPARATION
        for alt in alternatives
    )

    evidence = {
        "winner": {
            "voting_kinds": winner_voting,
            "total_count": _int(winner_facts.get("total_count")),
            "marker_counts": dict(marker_counts),
            "identity": identity,
        },
        "decision_trace": [dict(e) for e in decision.evidence],
        "confidence_formula": (
            f"strong: {STRONG_IDENTITY_CONFIDENCE}/{STRONG_CONTENT_CONFIDENCE}"
            f" | quorum: {QUORUM_BASE_CONFIDENCE} +{IDENTITY_BONUS}"
            f" identity +{EXTRA_VOTE_STEP}/extra vote, cap"
            f" {QUORUM_CONFIDENCE_CAP} | fallback:"
            f" {FALLBACK_CONFIDENCE} | uncertain below"
            f" {UNCERTAIN_BELOW}"
        ),
    }

    return ScenarioView(
        label=label,
        basis=basis,
        confidence=round(confidence, 2),
        uncertain=confidence < UNCERTAIN_BELOW,
        identity=identity,
        alternatives=alternatives,
        shadow_families=shadow_families,
        mixed=mixed,
        evidence=evidence,
    )
