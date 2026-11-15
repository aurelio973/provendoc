# -*- coding: utf-8 -*-
"""RSE (Representation Stack Evaluation) report assembly layer.

Three-layer output model:

    rse.quality_profile      <- invariant track (canonical,
                                invariant_v1 -- the only scoring
                                view)
    rse.gate_status          <- confirmed-failure three-state gate
                                (gate_v1: severity aggregation +
                                scale normalization +
                                applicability exemption)
    rse.capability_profile   <- traceability/audit archive (v2:
                                traceability coverages +
                                provenance facts + limitations +
                                evaluation block)

    compatibility            <- {"status": "no_legacy_profile"}

Naming conventions:
- identifiers avoid the re_ prefix (clashes with the re module);
- RE never collapses into a single total score; Gate and
  Capability are not folded into a score.
"""

from typing import Any, Dict, List, Optional

from provendoc.core.scenario.resolver import is_close_margin_label
from provendoc.intelligence.rse.proposed_profile import (
    build_proposed_profile,
    evaluate_profile,
)
from provendoc.intelligence.rse.scenario_quality import scenario_quality_doc

# ==================================
# Version constants (machine-readable)
# ==================================

# rse_v2: envelope key-set change (single-face convergence; see
# the envelope contract).
RSE_VERSION = "rse_v2"
INVARIANT_PROFILE_VERSION = "invariant_v1"
# gate_v2: confirmed views now come from two sources (quality
# issues + invariant red lines); aggregation unchanged.
GATE_VERSION = "gate_v2"
CAPABILITY_VERSION = "capability_v2"  # v2: evaluation status/trace 归层（capability_evaluation_block_prereg_v1）；v1 = 三覆盖 + provenance + limitations
CANDIDATE_PROFILE_VERSION = "candidate_v1"

# canonical switch completed; current is the target.
CANONICAL_CURRENT = "invariant_profile"
CANONICAL_TARGET = "invariant_profile"

# ==================================
# Quality Profile (invariant_v1, the only scoring view)
# ==================================
# Four explicit weights, same as the narrative registry's four
# invariants (0.30/0.15/0.35/0.20, sum 1).

QUALITY_DIMENSIONS: Dict[str, float] = {
    "structural_preservation": 0.30,        # ← hierarchy_preservation
    "semantic_continuity": 0.15,            # ← discourse_locality
    "granularity_appropriateness": 0.35,    # ← granularity_appropriateness
    "boundary_cleanliness": 0.20,           # ← boundary_cleanliness
}

# invariant track name -> quality dimension (1:1 projection)
_INVARIANT_TO_QUALITY = {
    "hierarchy_preservation": "structural_preservation",
    "discourse_locality": "semantic_continuity",
    "granularity_appropriateness": "granularity_appropriateness",
    "boundary_cleanliness": "boundary_cleanliness",
}

# Report-level dimension labels (rendering only)
QUALITY_DIMENSION_LABELS = {
    "structural_preservation": "结构保持",
    "semantic_continuity": "语义连续",
    "granularity_appropriateness": "粒度适切",
    "boundary_cleanliness": "边界整洁",
}

# Dimension thresholds for band rendering.
QUALITY_DIMENSION_THRESHOLDS = {
    "structural_preservation": 0.70,
    "semantic_continuity": 0.13,
    "granularity_appropriateness": 0.65,
    "boundary_cleanliness": 0.70,
}

# capability limitations -> confidence hard cap: min(cap, base)
CAPABILITY_CONFIDENCE_CAP = 0.5

# ==================================
# gate v1 (five rules)
# ==================================
# Rules 3 (scale normalization) and 4 (local vs systemic):
#   blocked (systemic critical): >= 3 confirmed critical
#       instances or density > 0.05;
#   warned (local errors): an isolated confirmed critical, or
#       systemic errors (>= 5 or density > 0.10);
#   passed: no confirmed critical/error.
# Rule 5: gate and quality_score coexist -- critical is never
# averaged away.

GATE_CRITICAL_ABSOLUTE = 3
GATE_CRITICAL_RATE_THRESHOLD = 0.05
GATE_ERROR_ABSOLUTE = 5
GATE_ERROR_RATE_THRESHOLD = 0.10

# gate severity -> lexicographic first-level rank
GATE_RANK = {
    "passed": 0,
    "warned": 1,
    "blocked": 2,
    "not_evaluated": 2,  # 无失败数据不能被奖励——与 blocked 同级（保守）
}

# Rule 1 (applicability exemption): failure mode -> structure
# class; documents without that structure do not trigger the
# gate.
GATE_MODE_CLASSES = {
    "code_explanation_split": "code",
    "explanation_code_split": "code",
    "table_description_split": "table",
    "procedure_step_split": "list_procedure",
    "list_item_split": "list_procedure",
    "heading_ambiguity": "heading_context",
    "section_context_loss": "heading_context",
    "hierarchy_ambiguity": "heading_context",
    "cross_reference_break": "heading_context",
    # semantic modes need no structure evidence, always applicable
}


def gate_exempt(mode: Any, applicability: Optional[Dict[str, bool]]) -> bool:
    """Whether a mode is exempted by applicability (rule 1).

    Shared by compute_gate_v1 (gate state) and the optimizer's
    failure_rate (lexicographic second level): an exempted failure
    counts in neither the gate nor the failure density.
    """
    if applicability is None:
        return False
    structure_class = GATE_MODE_CLASSES.get(mode or "")
    if structure_class is None:
        return False
    return not applicability.get(structure_class, True)


def chunk_structure_applicability(chunks: Optional[list]) -> Optional[Dict[str, bool]]:
    """Derive structure-gate applicability from the chunk makeup
    (rule 1).

    Returns None when it cannot be judged (no representation /
    no chunks); all gates are then treated as applicable.
    """
    if not chunks:
        return None
    has_code = False
    has_table = False
    has_list = False
    has_heading = False
    for chunk in chunks:
        unit_type = getattr(chunk, "unit_type", None)
        type_value = getattr(unit_type, "value", unit_type)
        # mixed units contain code fences; treat as code evidence
        if type_value in ("code", "mixed"):
            has_code = True
        elif type_value == "table":
            has_table = True
        elif type_value in ("list", "procedure"):
            has_list = True
        elif type_value == "heading":
            has_heading = True
        if not has_heading:
            has_heading = bool(getattr(chunk, "heading_path", None))
    return {
        "code": has_code,
        "table": has_table,
        "list_procedure": has_list,
        "heading_context": has_heading,
        "semantic": True,
    }


def compute_quality_score(
    invariant_composite: Optional[float],
) -> Optional[float]:
    """Four-dimension composite pure function: = invariant_composite.

    quality_score is the weighted composite of the four
    invariants (registry weights). Shared by the report layer
    and the optimizer so both read quality identically.
    Returns None when the invariant track degraded (honest
    absence; the caller decides the fallback).
    """
    if invariant_composite is None:
        return None
    return round(float(invariant_composite), 4)


def canonical_quality(evaluation: Optional[Dict[str, Any]]) -> Optional[float]:
    """Canonical quality score from the evaluator output (single
    read entry).

    The narrative evaluator only produces invariant_composite;
    the domain evaluators still produce their own overall.
    This is the single read entry for all consumers.
    """
    if not evaluation:
        return None
    value = evaluation.get("invariant_composite")
    if value is None:
        value = evaluation.get("overall")
    return value if isinstance(value, (int, float)) else None


def compute_gate_v1(
    confirmed_views: List[Dict[str, Any]],
    total_chunks: int,
    applicability: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """gate v1 pure function: confirmed failures -> three-state
    gate (single source of truth).

    Shared by the report layer and the optimizer (lexicographic
    first level).

    Args:
        confirmed_views: confirmed failure instance views (each
            with ``mode`` / ``severity``).
        total_chunks: normalization denominator (<= 0 is treated
            as 1 to avoid division by zero).
        applicability: structure class -> applicable (None = all
            applicable); failures of non-applicable classes are
            exempt (rule 1).

    Returns:
        gate dict (status / counts / densities / issues / rule
        thresholds).
    """
    exempted = []
    counted = []
    for view in confirmed_views:
        mode = view.get("mode") or ""
        if gate_exempt(mode, applicability):
            exempted.append(view)
            continue
        counted.append(view)

    critical_instances = [
        v for v in counted if v.get("severity") == "critical"
    ]
    error_instances = [v for v in counted if v.get("severity") == "error"]

    critical_count = len(critical_instances)
    error_count = len(error_instances)
    denominator = max(int(total_chunks or 0), 1)
    critical_rate = round(critical_count / denominator, 4)
    error_rate = round(error_count / denominator, 4)

    if (
        critical_count >= GATE_CRITICAL_ABSOLUTE
        or critical_rate > GATE_CRITICAL_RATE_THRESHOLD
    ):
        status = "blocked"
    elif (
        critical_count > 0
        or error_count >= GATE_ERROR_ABSOLUTE
        or error_rate > GATE_ERROR_RATE_THRESHOLD
    ):
        status = "warned"
    else:
        status = "passed"

    issues = sorted({v.get("mode") or "?" for v in counted})

    return {
        "gate_version": GATE_VERSION,
        "status": status,
        "critical_count": critical_count,
        "error_count": error_count,
        "critical_rate": critical_rate,
        "error_rate": error_rate,
        "total_chunks": denominator,
        "issues": issues,
        "exempted_modes": sorted({v.get("mode") or "?" for v in exempted}),
        "rules": {
            "critical_absolute": GATE_CRITICAL_ABSOLUTE,
            "critical_rate_threshold": GATE_CRITICAL_RATE_THRESHOLD,
            "error_absolute": GATE_ERROR_ABSOLUTE,
            "error_rate_threshold": GATE_ERROR_RATE_THRESHOLD,
        },
        "note": "gate v1: severity aggregation over confirmed instances, "
                "scale-normalized, three-state (local vs systemic); "
                "critical coexists with quality_score by design",
    }


def _extract_chunks(representation) -> Optional[list]:
    """Accepts MultiGranularityOutput or a flat unit list."""
    if representation is None:
        return None
    content = getattr(representation, "content", None)
    if content is None:
        return None
    if hasattr(content, "base"):
        return content.base
    if hasattr(content, "all_units"):
        return content.all_units()
    if isinstance(content, list):
        return content
    return None


def _quality_profile(
    unified: Dict[str, Any],
) -> Dict[str, Any]:
    """Quality Profile (invariant_v1, the only scoring view).

    quality_score = invariant_composite (weighted composite of
    the four invariants, registry weights). The csc candidate
    is not scored; its result is reported by candidate_profile
    separately.

    confidence: weighted average of the applicable dimensions'
    confidence, hard-capped at CAPABILITY_CONFIDENCE_CAP when
    capability limitations exist (plus an error entry).
    """
    invariants = unified.get("invariants") or {}
    invariant_composite = unified.get("invariant_composite")
    quality_skipped = unified.get("quality_evaluation") == "skipped"

    profile: Dict[str, Any] = {
        "profile_version": INVARIANT_PROFILE_VERSION,
        "role": CANONICAL_CURRENT,
        "weights": dict(QUALITY_DIMENSIONS),
    }

    if quality_skipped:
        profile.update({
            "status": "skipped",
            "dimensions": {},
            "quality_score": None,
            "confidence": None,
            "note": "quality evaluation skipped (huge-file fast-path); "
                    "gate still runs lightweight on failure modes "
                    "(re_migration_plan §4 constraint 4)",
        })
        return profile

    if invariant_composite is None:
        profile.update({
            "status": "partial",
            "dimensions": {},
            "quality_score": None,
            "confidence": None,
            "note": "invariant track failed or degraded — no quality "
                    "score (honest absence, not zero)",
        })
        return profile

    # dimension projection (invariant -> quality naming)
    dimensions: Dict[str, Any] = {}
    confidence_parts = []  # (weight, confidence)
    for inv_name, quality_name in _INVARIANT_TO_QUALITY.items():
        view = invariants.get(inv_name) or {}
        weight = QUALITY_DIMENSIONS[quality_name]
        if view.get("applicable", False) and view.get("score") is not None:
            dimensions[quality_name] = view["score"]
            confidence_parts.append((weight, view.get("confidence", 1.0)))
        else:
            dimensions[quality_name] = None

    # All-N/A documents (e.g. tiny degenerate files) have no
    # chunking decision to judge: honest absence is not_applicable
    # + quality_score=None, not a zero score.
    if not confidence_parts:
        profile.update({
            "status": "not_applicable",
            "dimensions": dimensions,
            "quality_score": None,
            "confidence": None,
            "note": (
                "no applicable quality invariants "
                "(degenerate document); honest absence, not zero"
            ),
        })
        return profile

    quality_score = compute_quality_score(invariant_composite)

    confidence = None
    if confidence_parts:
        total_weight = sum(w for w, _ in confidence_parts)
        if total_weight > 0:
            confidence = round(
                sum(w * c for w, c in confidence_parts) / total_weight, 3
            )

    profile.update({
        "status": "complete",
        "dimensions": dimensions,
        "quality_score": quality_score,
        "confidence": confidence,
    })
    return profile


def _apply_capability_influence(
    quality_profile: Dict[str, Any],
    capability: Dict[str, Any],
) -> None:
    """Capability-limitation influence.

    Hard cap on confidence: min(CAPABILITY_CONFIDENCE_CAP, base).
    Affects only confidence and errors, never quality_score --
    "cannot trace" is not "bad chunking".
    """
    limitations = capability.get("limitations") or []
    if not limitations:
        return
    errors = [
        {
            "type": "capability_limitation",
            "code": code,
            "effect": (
                "evaluation confidence capped at "
                f"{CAPABILITY_CONFIDENCE_CAP} (traceability limit)"
            ),
        }
        for code in limitations
    ]
    quality_profile["errors"] = errors
    base = quality_profile.get("confidence")
    if base is not None:
        quality_profile["confidence"] = round(
            min(CAPABILITY_CONFIDENCE_CAP, float(base)), 3
        )


def _apply_scenario_confidence_cap(
    quality_profile: Dict[str, Any],
    scenario: Any,
) -> None:
    """Low-confidence scenario label governance (P1).

    A close-margin label (single-source judgment) appends a
    quality_profile.errors entry and hard-caps confidence at
    CAPABILITY_CONFIDENCE_CAP. Only confidence and errors move;
    score / gate_status / evaluation_status stay untouched (same
    boundary as ``_apply_capability_influence``); it does not
    enter capability.limitations. High confidence / margin=None
    / malformed payload -> no action. errors are appended (must
    coexist with the limitations channel).
    """
    if not is_close_margin_label(scenario):
        return
    effect = (
        "evaluation confidence capped at "
        f"{CAPABILITY_CONFIDENCE_CAP} (scenario mislabel risk "
        "is not chunking quality)"
    )
    quality_profile.setdefault("errors", []).append(
        {
            "type": "scenario_label_low_confidence",
            "code": "runner_up_margin<=1",
            "effect": effect,
        }
    )
    base = quality_profile.get("confidence")
    if base is not None:
        quality_profile["confidence"] = round(
            min(CAPABILITY_CONFIDENCE_CAP, float(base)), 3
        )


def _gate_status(
    unified: Dict[str, Any],
    chunks: Optional[list],
) -> Dict[str, Any]:
    """gate v1: confirmed failures -> three-state aggregation."""
    failure_detail = unified.get("failure_modes") or {}
    if failure_detail.get("status") != "available":
        return {
            "gate_version": GATE_VERSION,
            "status": "not_evaluated",
            "critical_count": 0,
            "error_count": 0,
            "issues": [],
            "note": "failure modes benchmark not run; gate cannot "
                    "aggregate (honest absence)",
        }

    categories = failure_detail.get("categories") or {}
    confirmed_views = []
    for cat_views in categories.values():
        for view in cat_views or []:
            if view.get("confirmed"):
                confirmed_views.append(view)

    metadata = unified.get("metadata") or {}
    total_chunks = metadata.get("chunk_count")
    if total_chunks is None and chunks:
        total_chunks = len(chunks)

    applicability = chunk_structure_applicability(chunks)
    gate = compute_gate_v1(confirmed_views, total_chunks or 0, applicability)
    if applicability is None:
        gate["applicability"] = {
            "status": "unknown",
            "note": "no representation chunks — all gates treated as "
                    "applicable (no data dropped)",
        }
    else:
        gate["applicability"] = {
            "status": "available",
            "structure_classes": applicability,
        }

    # Constraint 4: when quality is skipped (huge fast-path) the
    # gate still aggregates from failure modes, marked lightweight.
    if unified.get("quality_evaluation") == "skipped":
        gate["evaluation_mode"] = "lightweight"
        gate["note"] += (
            "; lightweight mode: quality evaluation skipped "
            "(huge file), structure checks retained"
        )
    else:
        gate["evaluation_mode"] = "full"
    return gate


def policy_version_string(policy) -> Optional[str]:
    """Policy-parameter fingerprint as the version: ``name@hash12``.

    RepresentationPolicy has no explicit version field; the full
    fingerprint is stable across runs for identical parameters
    and changes with any parameter change (auditable).
    """
    import dataclasses as _dc
    import hashlib as _hl
    import json as _json

    if policy is None:
        return None
    name = getattr(policy, "name", "") or ""
    try:
        payload = _dc.asdict(policy)
        canonical = _json.dumps(
            payload, sort_keys=True, ensure_ascii=False, default=str,
        )
        digest = _hl.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return name or None
    return f"{name}@{digest}" if name else digest


def build_provenance_fact(
    parser_version: Optional[str],
    policy_version: Optional[str],
    execution_trace: bool,
) -> Dict[str, Any]:
    """Assemble the three provenance facts.

    Args:
        parser_version: parser code fingerprint (12-hex).
        policy_version: policy_version_string(policy) name@hash12.
        execution_trace: whether a debug execution trace was
            written for this file.

    Returns:
        A dict for build_re_report(provenance=...). Missing facts
        are None, never invented.
    """
    return {
        "parser_version": parser_version,
        "policy_version": policy_version,
        "execution_trace": bool(execution_trace),
    }


def _provenance_view(provenance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Provenance facts -> formal view; absent -> unavailable."""
    if provenance is None:
        return {
            "status": "unavailable",
            "note": "caller supplied no provenance facts "
                    "(schema: parser_version / policy_version / "
                    "execution_trace; see re_migration_plan §2.3)",
        }
    view: Dict[str, Any] = {"status": "available"}
    for key in ("parser_version", "policy_version", "execution_trace"):
        view[key] = provenance.get(key)
    return view


def _scenario_visibility_block(scenario: Any) -> Dict[str, Any]:
    """Scenario visibility block of capability_profile (narrative
    only).

    The L2 payload (metadata["scenario"]) is passed through
    verbatim, reusing scenario_quality.scenario_quality_doc as
    the fact shape. Attached only when explicitly supplied
    (non None); the block carries its own
    ``scenario_block_version`` inner version. Enters no score /
    gate / evaluation_status (zero-scoring axiom).
    """
    return {
        "scenario_block_version": "scenario_visibility_v1",
        "status": "available" if isinstance(scenario, dict) else "malformed",
        **scenario_quality_doc(scenario),
    }


def _capability_profile(
    chunks: Optional[list],
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """capability v1: only facts directly verifiable from chunk
    metadata.

    traceability coverages (absence is a fact, never a score):
        source_span_coverage   fraction of chunks carrying
                               source coordinates -- line spans
                               for txt/markdown, metadata
                               source_coordinates for container
                               formats (equally traceable)
        page_bbox_coverage     fraction carrying a page; None
                               when the document type never has
                               pages
        heading_path_coverage  fraction carrying a heading_path

    limitation codes (wired to the confidence hard cap, see
    _apply_capability_influence):
        source_span_missing    chunks exist but none carry source
                               coordinates -- not auditable back
                               to the original text

    heading_path == 0 emits no limitation: plain text without
    hierarchy is a document property, not a missing capability
    (facts and judgments stay separate).
    """
    provenance_view = _provenance_view(provenance)

    if not chunks:
        return {
            "capability_version": CAPABILITY_VERSION,
            "traceability": {
                "available": False,
                "source_span_coverage": None,
                "page_bbox_coverage": None,
                "heading_path_coverage": None,
            },
            "provenance": provenance_view,
            "limitations": [],
        }

    def _has_source_span(unit) -> bool:
        # line-based text (txt/markdown): line span
        ctx = getattr(unit, "source_context", None)
        if ctx is not None and (
            getattr(ctx, "line_start", None) is not None
            and getattr(ctx, "line_end", None) is not None
        ):
            return True
        # container formats use metadata.source_coordinates --
        # equally traceable to the line span above.
        metadata = getattr(unit, "metadata", None)
        if isinstance(metadata, dict):
            coords = metadata.get("source_coordinates")
            if isinstance(coords, dict) and coords.get(
                "coordinate_system"
            ):
                return True
        return False

    def _has_page(unit) -> bool:
        ctx = getattr(unit, "source_context", None)
        if ctx is None:
            return False
        return getattr(ctx, "page", None) is not None

    def _has_heading_path(unit) -> bool:
        return bool(getattr(unit, "heading_path", None))

    n = len(chunks)
    line_cov = sum(1 for c in chunks if _has_source_span(c)) / n
    heading_cov = sum(1 for c in chunks if _has_heading_path(c)) / n
    page_any = any(_has_page(c) for c in chunks)
    # no page info at all -> not applicable (None)
    page_cov = (
        sum(1 for c in chunks if _has_page(c)) / n if page_any else None
    )

    available = line_cov > 0 or heading_cov > 0 or (page_cov or 0) > 0

    limitations = []
    if line_cov == 0.0:
        limitations.append("source_span_missing")

    return {
        "capability_version": CAPABILITY_VERSION,
        "traceability": {
            "available": available,
            "source_span_coverage": round(line_cov, 3),
            "page_bbox_coverage": (
                round(page_cov, 3) if page_cov is not None else None
            ),
            "heading_path_coverage": round(heading_cov, 3),
        },
        "provenance": provenance_view,
        "limitations": limitations,
    }


def _candidate_profile(context_candidate) -> Dict[str, Any]:
    """Candidate dimension layer (schema slot, isolated).

    context_self_containment stays a candidate: the step-4
    promotion was based on a small n=8 regression later judged
    insufficient by the admission prereg; the deciding stage-2
    human batch has not run. Results are reported here only
    (physically isolated, never in quality_score).
    """
    note = (
        "context_self_containment is a CANDIDATE dimension "
        "(admission pending stage-2 human batch, "
        "admission_calibration_v1 §2.3/§4.1; the step-4 "
        "promotion was based on a G-batch n=8 regression "
        "judged insufficient by that prereg). Reported here "
        "only; NOT scored into quality_score."
    )
    if context_candidate is None:
        return {
            "profile_version": CANDIDATE_PROFILE_VERSION,
            "status": "not_evaluated",
            "note": note,
        }
    if hasattr(context_candidate, "to_dict"):
        entry = context_candidate.to_dict()
    elif isinstance(context_candidate, dict):
        entry = dict(context_candidate)
    else:
        return {
            "profile_version": CANDIDATE_PROFILE_VERSION,
            "status": "not_evaluated",
            "note": note,
        }
    status = "complete" if entry.get("applicable", True) else "not_applicable"
    return {
        "profile_version": CANDIDATE_PROFILE_VERSION,
        "status": status,
        "dimensions": {"context_self_containment": entry},
        "note": note,
    }


def _evaluation_status(unified: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluation completeness: a bare score is not legal; the
    report must say whether the evaluation was complete."""
    # constraint 4: huge fast-path -- quality skipped + gate runs
    # lightweight
    if unified.get("quality_evaluation") == "skipped":
        return {
            "status": "skipped",
            "detail": [
                "quality evaluation skipped (huge-file fast-path); "
                "gate runs lightweight on failure modes "
                "(re_migration_plan §4 constraint 4)"
            ],
        }

    status = "complete"
    reasons = []

    if unified.get("invariants") is None:
        status = "partial"
        reasons.append("invariant track failed or degraded")
    if (unified.get("failure_modes") or {}).get("status") != "available":
        if status == "complete":
            status = "partial"
        reasons.append("failure modes benchmark not run")

    return {"status": status, "detail": reasons}


def _proposed_quality_profile(
    chunks: Optional[list],
    document_text: Optional[str],
    policy,
    policy_version: Optional[str],
) -> Dict[str, Any]:
    """Bypass track: proposed_quality_profile_v1 (non-canonical,
    dual-track isolation).

    - add-only keys: never touches invariant_v1 fields or scores;
    - never a runtime single point of failure: any exception
      degrades to an error record;
    - composite is always provisional + PROPOSED.
    """
    try:
        ledger, observations = evaluate_profile(
            chunks or [], document_text, policy
        )
        return build_proposed_profile(
            ledger, observations, policy_version=policy_version
        )
    except Exception as exc:  # 旁路轨不拖垮主报告
        return {
            "profile_version": "proposed_quality_profile_v1",
            "status": "error",
            "canonical": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def build_re_report(
    unified_evaluation: Dict[str, Any],
    representation=None,
    provenance: Optional[Dict[str, Any]] = None,
    context_candidate=None,
    document_text: Optional[str] = None,
    policy=None,
    scenario: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the RSE three-layer envelope from the unified
    quality evaluation.

    Args:
        unified_evaluation: build_unified_evaluation() output
            (profile / invariants / invariant_composite /
            failure_modes); may be a quality-missing stub under
            the huge fast-path (quality_evaluation="skipped").
        representation: optional semantic representation
            (capability coverages + gate applicability
            evidence).
        provenance: optional provenance facts (see
            build_provenance_fact; absent facts are honestly
            marked unavailable, never invented).
        context_candidate: context_self_containment result
            (candidate slot; reported only, never scored).
        document_text: optional source document text (source-side
            checks need it; None skips them honestly).
        policy: optional RepresentationPolicy (the
            policy_forced verdict needs it; None counts as not
            judged).
        scenario: optional L2 payload
            (document.metadata["scenario"]). When not None a
            scenario visibility block is attached to
            capability_profile (facts only, zero scoring); None
            leaves the key set unchanged. Narrative-only.

    Returns:
        {"rse": {...}, "compatibility": {...}} -- for the caller
        to merge incrementally (layer versions live in
        quality_profile.profile_version).
    """
    chunks = _extract_chunks(representation)

    quality_profile = _quality_profile(unified_evaluation)

    gate = _gate_status(unified_evaluation, chunks)

    capability = _capability_profile(chunks, provenance)

    # capability limitations: confidence hard cap + error;
    # evaluation_status drops to partial.
    _apply_capability_influence(quality_profile, capability)

    # scenario visibility block + low-confidence governance:
    # visibility stays add-only/zero-scoring; a low-confidence
    # label separately hard-caps confidence.
    if scenario is not None:
        capability["scenario"] = _scenario_visibility_block(scenario)
        _apply_scenario_confidence_cap(quality_profile, scenario)

    eval_status = _evaluation_status(unified_evaluation)
    if capability.get("limitations") and eval_status["status"] == "complete":
        eval_status["status"] = "partial"
        eval_status["detail"] = list(eval_status["detail"]) + [
            "capability limitation: "
            + ", ".join(capability["limitations"])
            + " (confidence capped, see quality_profile.errors)"
        ]

    evaluation_trace = {
        # canonical switch completed; current is the invariant view
        "canonical_current": CANONICAL_CURRENT,
        "canonical_target": CANONICAL_TARGET,
        "quality_weights": dict(QUALITY_DIMENSIONS),
        "invariant_version": unified_evaluation.get(
            "invariant_version", "narrative_invariant_v1"
        ),
        "applicability": unified_evaluation.get("applicability") or {},
        "chunk_count": (
            unified_evaluation.get("metadata") or {}
        ).get("chunk_count"),
    }

    # evaluation completeness + trace layered into
    # capability_profile.
    capability["evaluation"] = {
        "status": eval_status["status"],
        "detail": eval_status["detail"],
        "trace": evaluation_trace,
    }

    rse_layer = {
        "rse_version": RSE_VERSION,
        "quality_profile": quality_profile,
        "gate_status": gate,
        "capability_profile": capability,
        "candidate_profile": _candidate_profile(context_candidate),
        "proposed_quality_profile": _proposed_quality_profile(
            chunks,
            document_text,
            policy,
            provenance.get("policy_version") if provenance else None,
        ),
    }

    # structure_provenance (w1-b): declared structure (markdown # /
    # DOCX styles / PDF bookmarks) and inferred structure
    # (heuristics) are not equal evidence; the invariant already
    # scores them at graded tiers, the envelope explains the mix.
    structure_provenance = (
        unified_evaluation.get("metadata") or {}
    ).get("structure_provenance")
    if structure_provenance:
        rse_layer["structure_provenance"] = {
            **structure_provenance,
            "note": (
                "declared=作者显式声明的结构 (markdown #/DOCX heading "
                "样式/PDF 书签); inferred=parser 启发式猜测的结构 "
                "(编号惯例/全大写/字体推断), 可能误判。invariant "
                "hierarchy_preservation 对纯猜测结构施加中间档惩罚, "
                "confidence 按 inferred_ratio 折扣评估可靠性。"
            ),
        }

    # navigation_evidence (w2): PDF bookmark navigation coverage --
    # a fact, not a score. Complements structure_provenance by
    # quantifying how much of the heading structure the author's
    # bookmarks cover. Rendered for PDF documents only.
    navigation_evidence = (
        unified_evaluation.get("metadata") or {}
    ).get("navigation_evidence")
    if navigation_evidence:
        rse_layer["navigation_evidence"] = {
            **navigation_evidence,
            "note": (
                "PDF 书签导航覆盖率 (证据事实, 不进分数): "
                "heading_bookmark_coverage=被书签背书的标题占比; "
                "bookmark_match_ratio=在正文找到对应标题块的书签占比。"
                "书签是 PDF 包内嵌的作者声明结构 — 覆盖率高时 "
                "structure_provenance.declared 的可信度更高。"
            ),
        }

    compatibility = {
        "status": "no_legacy_profile",
        "note": (
            "rse_naming_spec_v1 批次③：旧六维生产线已删除，"
            "narrative 域不再计算 legacy 视图；invariant_v1 是唯一"
            "计分口径。"
        ),
    }

    return {
        "rse": rse_layer,
        "compatibility": compatibility,
    }
