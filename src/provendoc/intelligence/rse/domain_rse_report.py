"""RSE three-layer envelope assembly — generic version for domain quality evaluation (multi-domain generalization).

Domains: record_stream / structured_config / spreadsheet / code

Generalized implementation of the re_migration_plan.md §9 reuse checklist (after the
§10 record_stream promotion): the domain-specific envelope pattern of record_stream is
abstracted into a contract-table-driven generic assembly — one implementation shared by four domains, event_rse_report.build_event_rse_report
degenerates into a delegating shell (public API and the existing 21 contract tests untouched, preventing behavior drift).

Output contract shared by the four domains (same shape):
    profile / overall / confidence / confidence_profile /
    applicability / issues / metadata
Differences are collected into the DOMAIN_QUALITY_CONTRACTS domain contract table:

    profile_version   self-reported version (CodeQuality does not self-report — the RSE layer assigns
                      code_quality_v1; no data fabrication, only label assignment)
    severity_key      the severity field name of an issue: ConfigQuality uses "level"
                      (legacy shape), the other three domains use "severity"
    weight_policy     config/table/event = canonical_no_redistribution
                      (N/A weights are not redistributed); code = active-weight normalization
    trace_metadata_keys  metadata fact keys copied into
                      capability_profile.evaluation.trace (layered per
                      capability_evaluation_block_prereg_v1)

Gate semantics (aligned with §10 record_stream):
    - Data source = the issue severity grading of the domain quality evaluation itself (none of the
      four domains has a failure_modes benchmark), converted into compute_gate_v1's confirmed_views
      shape; the five rules and the optimizer lexicographic order unchanged;
    - The severity vocabularies of the four domains lack a critical level (code has one extra
      info, not counted by the gate) → gate ceiling warned, blocked unreachable —
      adjudicated by-design on 2026-10-25 (gate_ceiling_adjudication_
      prereg_v1 §2: blocked is reserved for retrieval-breaking criticals; the four-domain
      issues are recoverable quality degradation; escalation has no data basis and is not fabricated),
      the gate dict carries a severity_ceiling key for machine-readable disclosure (narrative
      can reach blocked at the run level, without this key);
    - The single source of truth for exemptions = the domain quality evaluation's applicability: N/A dimensions emit no issues,
      all issues reaching the gate are applicable (applicability=None is not re-adjudicated).

Capability semantics (§9 generic constraint 5: type-dependent, per-domain evidence ceilings):
    - representation units of code/config/table all carry SourceContext
      (line_start; config/table also carry heading_path) → expect non-zero
      coverage, no source_span_missing, complete;
    - record_stream event units now carry SourceContext (log event-level
      line spans / jsonl record line numbers) → expect non-zero coverage, no
      source_span_missing, complete (the partial ceiling of §10 decision 3
      was raised by the event line-number wiring); only when events/records lack line numbers (hand-built
      synthetic documents) does it honestly fall back to source_span_missing + partial.

Invariant-track single-face convergence (config_single_face_convergence_prereg_v1,
2026-10-21; previously the guardrails §1.6 dual-track peer exposure, now converged):
    - Domains whose contract table declares the invariant_track entry → the four
      invariants are evaluated as usual, but externally they only go through two homed outlets: breach facts go to gate
      red lines (three-state confirmed views), per-item results go to the capability archive's
      invariant_ledger (a fact ledger with zero scoring);
    - invariant_composite is withdrawn from the exported envelope (phase 0: 50.2% constant 1.0,
      Spearman 0.331 vs overall — a second total without discriminating power and not redundant,
      it is a checklist, not a score); the evaluator still computes it internally for gate decisions and regression reconciliation;
    - The silent-degradation philosophy is unchanged: an exception in the evaluator-layer _attach_invariant_track
      → the red-line source is honestly absent + the ledger records not_evaluated; the canonical
      main track and the remaining gate faces take zero contagion;
    - The other domains do not declare the entry → no red-line source, no ledger; once all five domains are wired
      (record_stream closed on 2026-10-25, event_invariant_track_
      prereg_v1) this note retires — all four structural domains declare invariant_track,
      the narrative domain runs on its own track face.

All-N/A fallback (the same semantics as narrative 2026-10-04, wired in all four domains in sync):
    - When all domain dimensions are N/A (dimensions all None and non-empty), quality_profile
      status=not_applicable, quality_score/confidence=None —
      is honestly absent, not disguised as a zero score;
    - The empty-result path (the structural zero with chunk_count=0) is still partial,
      the two semantics are not mixed.

The zero-pollution / 0.0-None dichotomy / huge honest-degradation contracts match event_rse_report
(§10) clause by clause; see that module's docstring.
"""

from typing import Any, Dict, List, Optional

from provendoc.intelligence.rse.code_quality import CodeQuality
from provendoc.intelligence.rse.config_quality import ConfigQuality
from provendoc.intelligence.rse.event_quality import EventQuality
from provendoc.intelligence.rse.rse_report import (
    CANDIDATE_PROFILE_VERSION,
    GATE_VERSION,
    RSE_VERSION,
    _apply_capability_influence,
    _capability_profile,
    _extract_chunks,
    compute_gate_v1,
)
from provendoc.intelligence.rse.table_quality import TableQuality

# CodeQuality does not self-report profile_version (the result dict lacks that key) — the RSE layer assigns
# the label so the envelope is machine-readable. This is version-label assignment, not data fabrication.
# v2 (E4 uniform baseline, 2026-10-25): five-dimension weights 0.30/0.25/0.20/0.15/0.10
# → all 0.2 (uniform-baseline user adjudication).
# prereg: uniform_baseline_calibration_prereg_v1.md
# v3 (dimension_taxonomy_refactor_v1, 2026-10-26): the scoring dimensions go
# from five to two question-level composite dimensions unit_coherence / relation_continuity
# (0.5 each); sub-check raw scores stay exposed under their original names in observations (invariant-track
# profile→observations fallback with zero drift); ast_integrity is downgraded to
# observations (issues still go to the gate).
# prereg: dimension_taxonomy_refactor_prereg_v1.md
_CODE_QUALITY_PROFILE_VERSION = "code_quality_v3"


# ==================================
# Domain contract table (single source of truth)
# ==================================

DOMAIN_QUALITY_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "record_stream": {
        "evaluator": EventQuality,
        "profile_version": EventQuality.PROFILE_VERSION,
        "severity_key": "severity",
        "severity_vocabulary": ["error", "warning"],
        "weight_policy": "canonical_no_redistribution",
        "gate_source": "event_quality_issues",
        "trace_metadata_keys": [
            "fact_source", "event_count", "mapped_event_count",
        ],
        # Invariant track (event_invariant_track_prereg_v1):
        # the three result keys the evaluator's _attach_invariant_track hangs
        # on the result (track version event_invariants_v1, the track itself new in this batch); missing any one
        # → red-line source / ledger not_evaluated (silent degradation, same philosophy; empty result means
        # _empty_result early return = honest absence). The external shape is isomorphic to the
        # config/table/code convergence precedents: the composite stays out of the envelope, breach
        # facts go to gate red lines, per-item results go to the capability.invariant_ledger
        # archive (the event envelope never had a peer-level invariant_track key —
        # late-compliance, no "removal" step). The red-line thresholds follow
        # the preregistered source config_single_face_convergence_prereg_v1 §4-D2
        # (adjustments require a prereg version bump).
        "invariant_track": {
            "invariants_key": "invariants",
            "composite_key": "invariant_composite",
            "version_key": "invariant_track",
            "redline_warning_below": 0.9,
            "redline_error_below": 0.7,
        },
    },
    "structured_config": {
        "evaluator": ConfigQuality,
        "profile_version": ConfigQuality.PROFILE_VERSION,
        # ConfigQuality's legacy issue shape uses "level" rather than "severity"
        "severity_key": "level",
        "severity_vocabulary": ["error", "warning"],
        "weight_policy": "canonical_no_redistribution",
        # Structural fact-key additions (prereg config_rse_envelope §2 A3)
        "trace_metadata_keys": [
            "fact_source", "format", "entry_count",
            "derived_entry_count", "active_dimension_count",
        ],
        # Three admission criteria for dimensions (prereg §4-D4, calibrated on the phase-0 large corpus,
        # aligned with dimension_admission_calibration_prereg_v1 and the
        # v1.2/v1.3 narrowing precedents) — three necessary conditions for a dimension to enter the scoring layer:
        #   1. a continuous distribution with >5 distinct values (a near-constant 1.0 dimension has no discriminating power);
        #   2. low noise: std >= 0.05 and N/A < 50% (large areas of inapplicability are a
        #      noise surface, not a signal surface);
        #   3. tunability: ±50% weight perturbation does not flip sample rankings (phase-0
        #      measured Spearman >= 0.9978 — ranking insensitivity to weights is itself
        #      the evidence that "tuning weights gains nothing in this domain").
        # Deterministic binary-assertion dimensions (path_addressability-style per-key addressing checks)
        # do not meet the scoring conditions → they all go to gate red lines; this batch's single-face
        # convergence is their homing precedent.
        #
        # Invariant track (config_single_face_convergence_prereg_v1):
        # the three result keys the evaluator's _attach_invariant_track hangs on the result;
        # missing any one → red-line source / ledger not_evaluated (silent degradation, same philosophy).
        # The external shape has converged: the composite stays out of the envelope, breach facts go to gate
        # red lines, per-item results go to the capability.invariant_ledger archive.
        # Red-line thresholds (preregistered per prereg §4-D2; adjustments require a prereg version bump):
        "invariant_track": {
            "invariants_key": "invariants",
            "composite_key": "invariant_composite",
            "version_key": "invariant_track",
            "redline_warning_below": 0.9,
            "redline_error_below": 0.7,
        },
    },
    "spreadsheet": {
        "evaluator": TableQuality,
        "profile_version": TableQuality.PROFILE_VERSION,
        "severity_key": "severity",
        "severity_vocabulary": ["error", "warning"],
        "weight_policy": "canonical_no_redistribution",
        "trace_metadata_keys": ["fact_source"],
        # Invariant track (table_single_face_convergence_prereg_v1):
        # the three result keys the evaluator's _attach_invariant_track hangs on the result;
        # missing any one → red-line source / ledger not_evaluated (silent degradation, same philosophy;
        # phase-0 evidence: empty result means _empty_result early return = honest absence,
        # zero swallowed exceptions). The external shape is isomorphic to the config convergence precedent: the composite
        # stays out of the envelope, breach facts go to gate red lines, per-item results go to the
        # capability.invariant_ledger archive (table never had a peer-level
        # invariant_track key — late-compliance, no "removal" step).
        # The red-line thresholds follow the preregistered source config_single_face_convergence_prereg_v1 §4-D2
        # (adjustments require a prereg version bump).
        "invariant_track": {
            "invariants_key": "invariants",
            "composite_key": "invariant_composite",
            "version_key": "invariant_track",
            "redline_warning_below": 0.9,
            "redline_error_below": 0.7,
        },
    },
    "code": {
        "evaluator": CodeQuality,
        "profile_version": _CODE_QUALITY_PROFILE_VERSION,
        "severity_key": "severity",
        "severity_vocabulary": ["error", "info", "warning"],
        # code's overall is normalized over active weights (N/A dimensions excluded from the denominator)
        "weight_policy": "active_weight_normalization",
        "trace_metadata_keys": [
            "fact_source", "language", "parse_status",
        ],
        # Invariant track (code_single_face_convergence_prereg_v1):
        # the three result keys the evaluator's _attach_invariant_track hangs on the result
        # (mounted since roadmap item 9, track version code_invariants_v1);
        # missing any one → red-line source / ledger not_evaluated (silent degradation, same philosophy;
        # phase-0 evidence: empty result means _empty_result early return = honest absence,
        # zero swallowed exceptions). The external shape is isomorphic to the config/table convergence precedent:
        # the composite stays out of the envelope, breach facts go to gate red lines, per-item results go to the
        # capability.invariant_ledger archive (the code envelope never had a peer-level
        # invariant_track key — late-compliance, no "removal" step).
        # The red-line thresholds follow the preregistered source config_single_face_convergence_prereg_v1 §4-D2
        # (adjustments require a prereg version bump).
        "invariant_track": {
            "invariants_key": "invariants",
            "composite_key": "invariant_composite",
            "version_key": "invariant_track",
            "redline_warning_below": 0.9,
            "redline_error_below": 0.7,
        },
    },
}


def get_domain_quality_contract(domain: str) -> Optional[Dict[str, Any]]:
    """Domain name → quality-evaluation contract (unregistered domains return None; the caller skips the envelope)."""
    return DOMAIN_QUALITY_CONTRACTS.get(domain)


def _domain_quality_profile(
    contract: Dict[str, Any],
    domain_quality_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Quality Profile: direct projection of the domain quality-evaluation dimensions (canonical per domain)."""
    profile_version = contract["profile_version"]
    profile: Dict[str, Any] = {
        "profile_version": profile_version,
        "role": "canonical_current",
        "weights": dict(contract["evaluator"].WEIGHTS),
        "weight_policy": contract["weight_policy"],
    }

    if not domain_quality_result:
        profile.update({
            "status": "skipped",
            "dimensions": {},
            "quality_score": None,
            "confidence": None,
            "note": (
                "domain quality not evaluated (huge-file fast-path or "
                "empty representation); honest absence, not zero"
            ),
        })
        return profile

    chunk_count = (domain_quality_result.get("metadata") or {}).get(
        "chunk_count"
    )
    if not chunk_count:
        # Empty result of the domain quality evaluation (no chunks to evaluate): the score is the structural
        # zero of an empty result, not a quality judgement — marked partial, not masquerading as complete.
        profile.update({
            "status": "partial",
            "dimensions": dict(domain_quality_result.get("profile") or {}),
            "quality_score": domain_quality_result.get("overall"),
            "confidence": domain_quality_result.get("confidence"),
            "note": (
                "domain quality empty result (no chunks to evaluate); "
                "scores are structural zeros of the empty result, "
                "not a quality judgement"
            ),
        })
        return profile

    dimensions = dict(domain_quality_result.get("profile") or {})

    # All-N/A fallback (the same semantics as narrative 2026-10-04, all four domains in sync):
    # With all dimensions N/A, the canonical overall is the structural zero 0/Σ(all weights),
    # which would be misread as "poor quality"; honest absence → not_applicable + None
    # (honest absence, not zero). The empty-result path (structural zeros above)
    # does not enter this branch — the two semantics are not mixed.
    if dimensions and all(v is None for v in dimensions.values()):
        profile.update({
            "status": "not_applicable",
            "dimensions": dimensions,
            "quality_score": None,
            "confidence": None,
            "note": (
                "all dimensions not applicable (degenerate "
                "document); honest absence, not zero"
            ),
        })
        return profile

    profile.update({
        "status": "complete",
        # Direct projection of the domain dimensions: N/A dimensions stay None (no guessing, no zeroing)
        "dimensions": dimensions,
        "quality_score": domain_quality_result.get("overall"),
        "confidence": domain_quality_result.get("confidence"),
        "note": (
            f"dimensions projected directly from {profile_version}; "
            "overall is the canonical composite as computed by "
            "the domain evaluator"
        ),
    })
    return profile


def _domain_gate_status(
    contract: Dict[str, Any],
    domain_quality_result: Optional[Dict[str, Any]],
    chunks: Optional[List[Any]],
) -> Dict[str, Any]:
    """gate v2: domain quality issues + invariant red lines → three-state aggregation into confirmed views."""
    # Severity reachability ceiling (a domain property, derived from the vocabulary rather than hardcoded — a single
    # source of truth shared with the contract table; gate_ceiling_adjudication_prereg_v1 §4-D1:
    # the ceiling is a vocabulary-determined structural fact; unevaluated branches carry it too). Note the semantics is
    # "reachable ceiling", not "reached this time".
    severity_ceiling = (
        "blocked"
        if "critical" in contract["severity_vocabulary"]
        else "warned"
    )

    metadata = (domain_quality_result or {}).get("metadata") or {}
    total_chunks = metadata.get("chunk_count")
    if total_chunks is None and chunks:
        total_chunks = len(chunks)

    if not domain_quality_result or not total_chunks:
        return {
            "gate_version": GATE_VERSION,
            "status": "not_evaluated",
            "severity_ceiling": severity_ceiling,
            "critical_count": 0,
            "error_count": 0,
            "issues": [],
            "note": (
                "domain quality not evaluated or no chunks; this domain "
                "has no failure_modes benchmark to fall back on "
                "(honest absence, no lightweight mode)"
            ),
        }

    # Domain issue → confirmed failure views (compute_gate_v1 input shape:
    # mode + severity). The severity key name follows the domain contract (ConfigQuality uses level).
    severity_key = contract["severity_key"]
    confirmed_views = [
        {
            "mode": issue.get("dimension") or "unknown",
            "severity": issue.get(severity_key),
        }
        for issue in (domain_quality_result.get("issues") or [])
    ]

    # Invariant red lines (single-face convergence): breach facts enter the gate from a dual source
    # (config_single_face_convergence_prereg_v1 §4-D2; undeclared domains
    # return an empty list with zero drift).
    confirmed_views.extend(
        _invariant_redline_views(contract, domain_quality_result)
    )

    # Issues are emitted only on applicable dimensions, so exemptions need no re-adjudication (N/A dimensions emit no issues,
    # every issue reaching the gate is applicable — applicability=None is not re-adjudicated).
    gate = compute_gate_v1(
        confirmed_views,
        total_chunks,
        applicability=None,
    )
    gate["applicability"] = {
        "status": "available",
        "source": "domain_quality_applicability",
        "note": (
            "exemption single source of truth is the domain quality "
            "applicability: N/A dimensions emit no issues, so every "
            "issue reaching the gate is applicable by construction"
        ),
    }
    # Machine-readable disclosure of the reachable ceiling (prereg §4-D1; note prose dual channel)
    gate["severity_ceiling"] = severity_ceiling
    gate["evaluation_mode"] = "full"
    gate["note"] += (
        "; domain severity vocabulary: "
        + "/".join(contract["severity_vocabulary"])
        + " (no critical level — ceiling is warned)"
    )
    if contract.get("invariant_track"):
        gate["note"] += (
            "; invariant red lines included (dual-source "
            "confirmed views, single-face convergence)"
        )
    return gate


def _invariant_ledger(
    contract: Dict[str, Any],
    domain_quality_result: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Invariant fact ledger (capability layer; the fact outlet after single-face convergence).

    Assembled only for domains whose contract table declares the invariant_track entry (structured_config
    first; the isomorphic table/code convergence is registered as a follow-up opt-in batch). Each
    InvariantResult enters the ledger as-is (score/applicable/diagnostics/issues/
    confidence) — a fact ledger with zero scoring; it enters no scoring surface other than
    quality_score / dimensions / evaluation_status / gate counts.

    Domains whose contract table does not declare invariant_track return None → the capability has no
    such key (the keyset of the other domains is unchanged).
    """
    track = contract.get("invariant_track")
    if not track:
        return None

    invariants_key = track["invariants_key"]
    composite_key = track["composite_key"]
    version_key = track["version_key"]

    def _not_evaluated(note: str) -> Dict[str, Any]:
        return {
            "status": "not_evaluated",
            "track_version": None,
            "invariants": {},
            "note": note,
        }

    single_face_note = (
        "invariant facts archive (single-face convergence): "
        "breakages surface as gate red lines; per-item results "
        "recorded here only — composite is internal, never exported"
    )

    if not domain_quality_result:
        return _not_evaluated(
            "domain quality not evaluated (huge-file fast-path or "
            "empty representation); invariant ledger absent "
            "(honest absence). " + single_face_note
        )

    invariants = domain_quality_result.get(invariants_key) or {}
    composite = domain_quality_result.get(composite_key)
    version = domain_quality_result.get(version_key)
    if not invariants or composite is None or not version:
        # Evaluator-layer silent degradation (invariant-track exception swallowed) or a track-less evaluator version
        return _not_evaluated(
            "invariant track absent or silently degraded on the "
            "evaluation result (missing invariants/composite/"
            "version key); canonical dimension track and gate "
            "issue views unaffected. " + single_face_note
        )

    return {
        "status": "available",
        "track_version": version,
        "invariants": {
            name: (
                entry.to_dict()
                if hasattr(entry, "to_dict")
                else dict(entry)
            )
            for name, entry in invariants.items()
        },
        "note": single_face_note,
    }


def _invariant_redline_views(
    contract: Dict[str, Any],
    domain_quality_result: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Invariant breaches → gate red-line confirmed views (single-face convergence).

    Preregistered mapping (prereg §4-D2; adjustments require a prereg version bump):
        - not applicable (applicable=False) → not into the gate (the same exemption semantics as
          issues being emitted only on applicable dimensions);
        - score < redline_error_below (0.7) → error;
        - non-empty issues (concrete breach entries present = structural evidence) → error;
        - score < redline_warning_below (0.9) → warning;
        - otherwise → no red line.
    Silent degradation (evaluator-layer track exception) → return an empty list (the red-line source is honestly absent),
    fabricating no red lines and leaving canonical uncontaminated.
    """
    track = contract.get("invariant_track")
    if not track or not domain_quality_result:
        return []

    invariants = (
        domain_quality_result.get(track["invariants_key"]) or {}
    )
    composite = domain_quality_result.get(track["composite_key"])
    version = domain_quality_result.get(track["version_key"])
    if not invariants or composite is None or not version:
        return []

    warn_below = track["redline_warning_below"]
    error_below = track["redline_error_below"]

    views: List[Dict[str, Any]] = []
    for name, entry in invariants.items():
        data = (
            entry.to_dict()
            if hasattr(entry, "to_dict")
            else dict(entry)
        )
        if not data.get("applicable"):
            continue
        score = data.get("score")
        if score is None:
            continue
        if score < error_below or data.get("issues"):
            severity = "error"
        elif score < warn_below:
            severity = "warning"
        else:
            continue
        views.append({"mode": f"invariant:{name}", "severity": severity})
    return views


def _domain_evaluation_status(
    domain_quality_result: Optional[Dict[str, Any]],
    quality_profile: Dict[str, Any],
    capability: Dict[str, Any],
) -> Dict[str, Any]:
    """Evaluation completeness: bare scores are illegitimate; the evaluation must state whether it is complete."""
    if not domain_quality_result:
        return {
            "status": "skipped",
            "detail": [
                "domain quality not evaluated (huge-file fast-path or "
                "empty representation)"
            ],
        }

    status = quality_profile.get("status") or "complete"
    reasons: List[str] = []

    if status == "partial":
        reasons.append("domain quality empty result — no applicable chunks")

    limitations = capability.get("limitations") or []
    if limitations:
        status = "partial"
        reasons.append(
            "capability limitation: "
            + ", ".join(limitations)
            + " (confidence capped, see quality_profile.errors)"
        )

    return {"status": status, "detail": reasons}


def build_domain_rse_report(
    domain: str,
    domain_quality_result: Optional[Dict[str, Any]],
    representation=None,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the generic RSE three-layer envelope from a domain quality-evaluation result.

    Args:
        domain: the domain name (a DOMAIN_QUALITY_CONTRACTS key; narrative is not in this table
            — it goes through build_re_report's invariant-track envelope).
        domain_quality_result: the return value of the domain evaluator's evaluate().
            None = not evaluated (huge fast path); all layers degrade honestly.
        representation: optional semantic representation (structural evidence
            for the capability coverage).
        provenance: optional provenance three-facts (see
            build_provenance_fact; when absent, the capability layer honestly marks
            unavailable and fabricates no placeholder values).

    Returns:
        {"rse": {...}, "compatibility": {...}}
        — an incremental envelope with the same shape as build_re_report / build_event_rse_report,
        for the caller to merge into unified output incrementally (after the RSE version bump the top-level
        cqi_version field is removed).

    Raises:
        ValueError: domain is not registered (the caller should consult the contract table first).
    """
    contract = DOMAIN_QUALITY_CONTRACTS.get(domain)
    if contract is None:
        raise ValueError(
            f"unknown domain for RSE envelope: {domain!r}; "
            f"registered: {sorted(DOMAIN_QUALITY_CONTRACTS)}"
        )

    chunks = _extract_chunks(representation)

    quality_profile = _domain_quality_profile(contract, domain_quality_result)

    gate = _domain_gate_status(contract, domain_quality_result, chunks)

    capability = _capability_profile(chunks, provenance)

    # Invariant fact ledger (only-add-keys: undeclared domains return
    # None → the capability has no such key; the other domains' keyset is unchanged)
    invariant_ledger = _invariant_ledger(
        contract, domain_quality_result,
    )

    # capability-limitation influence mechanism (same as narrative):
    # a hard confidence cap + error; quality_score untouched.
    _apply_capability_influence(quality_profile, capability)

    eval_status = _domain_evaluation_status(
        domain_quality_result, quality_profile, capability,
    )

    metadata = (domain_quality_result or {}).get("metadata") or {}
    evaluation_trace: Dict[str, Any] = {
        "domain": domain,
        "canonical_current": contract["profile_version"],
        "quality_weights": dict(contract["evaluator"].WEIGHTS),
        "weight_policy": contract["weight_policy"],
        "gate_source": contract.get("gate_source") or f"{domain}_quality_issues",
        "gate_severity_vocabulary": list(
            contract["severity_vocabulary"]
        ),
        "primary_dimensions": sorted(
            contract["evaluator"].PRIMARY_DIMENSIONS
        ),
        "chunk_count": metadata.get("chunk_count"),
    }
    for key in contract["trace_metadata_keys"]:
        evaluation_trace[key] = metadata.get(key)

    # capability_evaluation_block_prereg_v1 §2.1 (same for all domains): completeness
    # + dossier layered into capability_profile. A pure relocation; criteria and content unchanged.
    capability["evaluation"] = {
        "status": eval_status["status"],
        "detail": eval_status["detail"],
        "trace": evaluation_trace,
    }

    if invariant_ledger is not None:
        # Ledger homing: the per-item invariant fact ledger goes into capability (single-face convergence —
        # the composite stays out of the envelope, breach facts go through gate red lines; see prereg §3)
        capability["invariant_ledger"] = invariant_ledger

    rse_layer = {
        "rse_version": RSE_VERSION,
        "domain": domain,
        "quality_profile": quality_profile,
        "gate_status": gate,
        "capability_profile": capability,
        "candidate_profile": {
            "profile_version": CANDIDATE_PROFILE_VERSION,
            "status": "not_applicable",
            "note": (
                f"{domain} domain has no candidate dimensions yet; "
                "namespace reserved for future candidates"
            ),
        },
    }
    compatibility = {
        "status": "no_legacy_profile",
        "note": (
            f"{domain} domain never had a legacy six-dimension "
            "profile; the domain quality view is the first and "
            "canonical quality profile (no compatibility debt)"
        ),
    }

    return {
        "rse": rse_layer,
        "compatibility": compatibility,
    }
