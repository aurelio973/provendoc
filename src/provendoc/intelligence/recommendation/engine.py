"""Rule-based recommendation engine — the default evaluate() advice path.

Deterministic and cheap: no search, no re-chunking. The engine reads four
inputs (strategy params, evaluation result, retrieval failure summary,
document profile), matches them against the versioned TOML rule table, filters by
``when`` guards, sorts by evidence strength, and resolves conflicts.

Output discipline (mirrors the RE envelope honesty contract):

* Every recommendation is a *hypothesis* with mandatory side effects —
  never phrased as a guaranteed improvement.
* Every recommendation sets ``requires_re_evaluation = True`` and carries
  a ``verification`` block pointing at the compare/search modes.
* Conflicting suggestions are suppressed, not silently merged; the
  suppressed entry tells the user to escalate to compare/search.
* If the evaluation itself is low-confidence, the payload says so instead
  of pretending the advice is solid.

The engine never raises on evaluation data problems; ``recommend()``
returns a structured ``unavailable``/``no_recommendations`` payload. Only
rule-table schema errors propagate (from ``load_rules``) so callers can
decide — the SDK catches them and degrades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from provendoc.intelligence.recommendation.rules import (
    SCHEMA_VERSION,
    Rule,
    load_rules,
)

OUTPUT_SCHEMA_VERSION = 1

# Severity rank used for sorting failure-driven rules (higher first).
_SEVERITY_RANK = {"critical": 4, "error": 3, "warning": 2, "info": 1}

_ESCALATION_HINT = (
    "conflicting recommendations — verify candidates with "
    "evaluate(recommend='compare') or evaluate(recommend='search')"
)

_DISCLAIMER = (
    "Recommendations are diagnostic hypotheses derived from deterministic "
    "rules, not guaranteed improvements. Every suggestion lists its "
    "side effects and requires re-evaluation: verify with "
    "evaluate(recommend='compare') or evaluate(recommend='search') "
    "before adopting."
)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


@dataclass(frozen=True)
class RecommendationContext:
    """The four engine inputs, normalized to plain dicts.

    strategy_params   — current RepresentationPolicy as a flat param dict
                        (native names; see strategy_params_from_policy).
    evaluation        — evaluator result dict (profile,
                        issues, applicability, metadata, confidence...).
    failure_summary   — compact RetrievalFailureBenchmark summary:
                        {mode: {"total", "confirmed", "max_severity",
                        "max_confidence"}} plus totals. May be {} when
                        detection was unavailable.
    document_profile  — document-level facts (see build_document_profile).
    """

    strategy_params: Dict[str, Any]
    evaluation: Dict[str, Any]
    failure_summary: Dict[str, Any] = field(default_factory=dict)
    document_profile: Dict[str, Any] = field(default_factory=dict)


def strategy_params_from_policy(policy: Any) -> Dict[str, Any]:
    """Flatten a RepresentationPolicy into the native param dict.

    Mirrors the strategy_params block the SDK already exposes in
    metadata["benchmark"]["strategy_params"], but reads directly from the
    policy object so the engine is usable outside client.evaluate().
    """
    return {
        "size_soft_limit": policy.packaging.size_soft_limit,
        "size_hard_limit": policy.packaging.size_hard_limit,
        "boundary_primary": policy.boundary_preference.primary,
        "context_mode": policy.context_propagation.mode,
        "overlap_size": policy.context_propagation.overlap_size,
        "discourse_unit_target": policy.semantic_density.discourse_unit_target,
        "paragraph_target": policy.semantic_density.paragraph_target,
        "merge_short_sibling_sections": (
            policy.boundary_preference.merge_short_sibling_sections
        ),
        "short_section_ratio": policy.boundary_preference.short_section_ratio,
        "hierarchy_continuity": policy.invariants.hierarchy_continuity,
        "structural_block_complete": policy.invariants.structural_block_complete,
        "discourse_locality": policy.invariants.discourse_locality,
        # derived legacy view, for rules/users that think in chunk_size
        "chunk_size": policy.get_effective_chunk_size(),
        "overlap": policy.get_effective_overlap(),
    }


def build_document_profile(document: Any, dqr: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Document-level facts the ``when.document`` guards can reference.

    Pure observation of document.text (the working text chunks were built
    from) plus DQR scores. Cheap regex-free counting by line inspection —
    this runs on every evaluate() call in the default rules mode.
    """
    text = getattr(document, "text", None) or ""
    lines = text.splitlines()

    heading_count = 0
    code_fence_count = 0
    table_separator_count = 0
    bullet_lines = 0
    ordered_step_lines = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").startswith(" "):
            heading_count += 1
        if stripped.startswith("```"):
            code_fence_count += 1
        if "|---" in line or "| ---" in line:
            table_separator_count += 1
        if stripped[:2] in ("- ", "* ", "+ ") or stripped.startswith("• "):
            bullet_lines += 1
        elif len(stripped) > 3 and stripped[0].isdigit():
            dot = stripped.find(".")
            # Tightened 2026-10-30: digit prefixes must be pure digits with ≤3 digits (step
            # numbers rarely exceed 3; "2026. Revision"-style year lines were previously counted
            # as steps, wrongly arming the when.document.has_steps guard on purely narrative documents).
            if (
                0 < dot <= 3
                and stripped[:dot].isdigit()
                and stripped[dot + 1 : dot + 2] == " "
            ):
                ordered_step_lines += 1

    dqr = dqr if isinstance(dqr, dict) else {}
    profile = dqr.get("profile")
    dqr_profile = profile if isinstance(profile, dict) else {}

    return {
        "length": len(text),
        "line_count": len(lines),
        "heading_count": heading_count,
        "has_headings": heading_count > 0,
        "has_code_blocks": code_fence_count >= 2,
        "has_tables": table_separator_count > 0,
        "has_lists": bullet_lines >= 2,
        "has_steps": ordered_step_lines >= 2,
        "dqr_overall": _number(dqr.get("overall")),
        "structure_signals": _number(dqr_profile.get("structure_signals")),
    }


def summarize_failure_report(report: Any) -> Dict[str, Any]:
    """Compact a RetrievalFailureBenchmark FailureReport into the
    engine-ready failure_summary shape.

    Per-mode counts distinguish detected (total) from confirmed
    (confidence >= CONFIRMED_THRESHOLD) — the engine only fires
    failure-mode rules on confirmed evidence.
    """
    modes: Dict[str, Any] = {}
    for failure in report.failures:
        entry = modes.setdefault(
            failure.mode.value,
            {
                "total": 0,
                "confirmed": 0,
                "max_severity": "info",
                "max_confidence": 0.0,
            },
        )
        entry["total"] += 1
        if failure.confirmed:
            entry["confirmed"] += 1
        entry["max_confidence"] = max(
            entry["max_confidence"], round(failure.confidence, 3)
        )
        if _SEVERITY_RANK.get(failure.severity, 0) > _SEVERITY_RANK.get(
            entry["max_severity"], 0
        ):
            entry["max_severity"] = failure.severity
    return {
        "risk_score": report.risk_score,
        "total": len(report.failures),
        "confirmed": len(report.confirmed_failures()),
        "modes": modes,
    }


class RuleRecommender:
    """Match diagnostic signals against the TOML rule table."""

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        """Load the rule table (custom rules can be injected)."""
        self._rules = rules if rules is not None else load_rules()

    @property
    def rule_count(self) -> int:
        """Number of loaded rules."""
        return len(self._rules)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def recommend(self, context: RecommendationContext) -> Dict[str, Any]:
        """Produce the recommendations payload for one evaluation.

        Never raises on evaluation data; a structurally unusable
        evaluation result degrades to status ``unavailable`` with a reason.
        """
        invariants = context.evaluation.get("invariants")
        if not isinstance(invariants, dict):
            return self._payload(
                status="unavailable",
                reason="evaluation lacks invariant scores; "
                "recommendations need per-invariant scores to match "
                "rules against",
                context=context,
            )

        matched: List[Dict[str, Any]] = []
        for rule in self._rules:
            trigger_evidence = self._evaluate_trigger(rule, context)
            if trigger_evidence is None:
                continue
            if not self._guards_pass(rule, context):
                continue
            current = context.strategy_params.get(rule.recommend_parameter)
            if current == rule.recommend_to:
                continue  # no-op: current params already satisfy the rule
            matched.append(
                self._build_item(rule, context, current, trigger_evidence)
            )

        if not matched:
            return self._payload(
                status="no_recommendations",
                reason="no rule fired for the current diagnostics — either "
                "quality is within rule thresholds or the issue is not "
                "covered by the rule table (escalate to "
                "recommend='compare' / 'search' to explore)",
                context=context,
            )

        items, suppressed = self._resolve_conflicts(matched)
        return self._payload(
            status="available",
            context=context,
            items=items,
            suppressed=suppressed,
        )

    # ------------------------------------------------------------------
    # trigger evaluation
    # ------------------------------------------------------------------

    def _evaluate_trigger(
        self, rule: Rule, context: RecommendationContext
    ) -> Optional[Dict[str, Any]]:
        """Return trigger evidence when the rule fires, else None."""
        trigger = rule.trigger
        kind = trigger["kind"]

        if kind == "dimension_below":
            dimension = trigger["dimension"]
            # respect applicability: an N/A invariant must not fire rules
            view = (
                (context.evaluation.get("invariants") or {}).get(dimension)
                or {}
            )
            if not view.get("applicable", True):
                return None
            score = _number(view.get("score"))
            if score is None or score >= trigger["threshold"]:
                return None
            return {
                "kind": kind,
                "dimension": dimension,
                "score": round(score, 3),
                "threshold": trigger["threshold"],
                "severity_rank": 3,  # dimension findings sort like errors
                "sort_gap": trigger["threshold"] - score,
            }

        if kind == "failure_mode_present":
            modes = trigger["modes"]
            summary_modes = context.failure_summary.get("modes") or {}
            hits = [
                (mode, summary_modes[mode])
                for mode in modes
                if summary_modes.get(mode, {}).get("confirmed")
            ]
            if not hits:
                return None
            # strongest confirmed mode wins the evidence slot
            mode, entry = max(
                hits,
                key=lambda pair: (
                    _SEVERITY_RANK.get(pair[1].get("max_severity", "info"), 1),
                    pair[1].get("max_confidence", 0.0),
                ),
            )
            severity = entry.get("max_severity", "warning")
            return {
                "kind": kind,
                "mode": mode,
                "confirmed": entry["confirmed"],
                "max_severity": severity,
                "max_confidence": entry.get("max_confidence"),
                "severity_rank": _SEVERITY_RANK.get(severity, 1),
                "sort_gap": _SEVERITY_RANK.get(severity, 1) / 4.0,
            }

        metric = trigger.get("metric")
        value = _number((context.evaluation.get("metadata") or {}).get(metric))
        if value is None:
            return None
        threshold = trigger["threshold"]
        if kind == "metric_above" and value <= threshold:
            return None
        if kind == "metric_below" and value >= threshold:
            return None
        return {
            "kind": kind,
            "metric": metric,
            "value": round(value, 3),
            "threshold": threshold,
            "severity_rank": 2,
            "sort_gap": min(abs(value - threshold) / (abs(threshold) or 1.0), 1.0),
        }

    # ------------------------------------------------------------------
    # when guards
    # ------------------------------------------------------------------

    def _guards_pass(self, rule: Rule, context: RecommendationContext) -> bool:
        when = rule.when or {}

        for param, condition in (when.get("params") or {}).items():
            current = context.strategy_params.get(param)
            if not self._check_condition(current, condition):
                return False

        doc_guard = when.get("document") or {}
        profile = context.document_profile
        for flag in ("has_headings", "has_code_blocks", "has_tables",
                     "has_lists", "has_steps"):
            if flag in doc_guard and bool(profile.get(flag)) is not bool(
                doc_guard[flag]
            ):
                return False
        if "min_length" in doc_guard and (
            profile.get("length", 0) < doc_guard["min_length"]
        ):
            return False
        if "max_length" in doc_guard and (
            profile.get("length", 0) > doc_guard["max_length"]
        ):
            return False
        if "structure_score_below" in doc_guard:
            score = _number(profile.get("structure_signals"))
            if score is None or score >= doc_guard["structure_score_below"]:
                return False

        return True

    @staticmethod
    def _check_condition(current: Any, condition: Dict[str, Any]) -> bool:
        for operator, expected in condition.items():
            if operator == "eq" and current != expected:
                return False
            if operator == "not_eq" and current == expected:
                return False
            if operator == "in" and current not in expected:
                return False
            if operator in ("gt", "ge", "lt", "le"):
                current_num = _number(current)
                expected_num = _number(expected)
                if current_num is None or expected_num is None:
                    return False
                if operator == "gt" and not current_num > expected_num:
                    return False
                if operator == "ge" and not current_num >= expected_num:
                    return False
                if operator == "lt" and not current_num < expected_num:
                    return False
                if operator == "le" and not current_num <= expected_num:
                    return False
        return True

    # ------------------------------------------------------------------
    # item building / conflict resolution
    # ------------------------------------------------------------------

    def _build_item(
        self,
        rule: Rule,
        context: RecommendationContext,
        current: Any,
        trigger_evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        issue = context.evaluation.get("issues") or []
        related_issue = next(
            (entry for entry in issue
             if isinstance(entry, dict) and entry.get("dimension") == rule.issue),
            None,
        )
        return {
            "rule_id": rule.rule_id,
            "issue": rule.issue,
            "cause": rule.cause,
            "evidence": {
                "trigger": {
                    key: value
                    for key, value in trigger_evidence.items()
                    if key not in ("severity_rank", "sort_gap")
                },
                "quality_issue": related_issue,
            },
            "recommendation": {
                "parameter": rule.recommend_parameter,
                "from": current,
                "to": rule.recommend_to,
            },
            "expected_effect": rule.expected_effect,
            "side_effects": list(rule.side_effects),
            "confidence": rule.confidence,
            "requires_re_evaluation": True,
            "verification": {
                "method": "compare",
                "suggested_params": {rule.recommend_parameter: rule.recommend_to},
                "note": "apply suggested_params over the current params and "
                "compare the quality evaluation before adopting",
            },
            "provenance": dict(rule.verified_by),
            # internal sort keys (stripped before output)
            "_severity_rank": trigger_evidence["severity_rank"],
            "_sort_gap": trigger_evidence["sort_gap"],
            "_conflicts_with": list(rule.conflicts_with),
        }

    def _resolve_conflicts(
        self, matched: List[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Sort by evidence strength, then drop conflicting losers.

        Conflict sources: explicit ``conflicts_with`` ids, two rules
        touching the same parameter with different targets (conflict),
        and two rules touching the same parameter with the same target
        (duplicate — the weaker one is redundant). The loser is not
        deleted — it moves to ``suppressed`` with the winner's id and,
        for real conflicts, an escalation hint, so the user can still
        compare both.
        """
        ordered = sorted(
            matched,
            key=lambda item: (
                -item["_severity_rank"],
                -item["_sort_gap"],
                -item["confidence"],
                item["rule_id"],
            ),
        )

        kept: List[Dict[str, Any]] = []
        suppressed: List[Dict[str, Any]] = []
        kept_ids: set[str] = set()
        kept_param_targets: Dict[str, Any] = {}

        for item in ordered:
            conflict_with = next(
                (
                    kept_item
                    for kept_item in kept
                    if kept_item["rule_id"] in item["_conflicts_with"]
                    or item["rule_id"] in kept_item["_conflicts_with"]
                ),
                None,
            )
            parameter = item["recommendation"]["parameter"]
            duplicate_of = None
            if conflict_with is None and parameter in kept_param_targets:
                same_parameter = next(
                    kept_item
                    for kept_item in kept
                    if kept_item["recommendation"]["parameter"] == parameter
                )
                if kept_param_targets[parameter] != item["recommendation"]["to"]:
                    conflict_with = same_parameter
                else:
                    duplicate_of = same_parameter
            if duplicate_of is not None:
                suppressed.append({
                    "rule_id": item["rule_id"],
                    "issue": item["issue"],
                    "recommendation": item["recommendation"],
                    "reason": (
                        f"duplicate of selected rule "
                        f"{duplicate_of['rule_id']!r} (same parameter and "
                        "target, different evidence)"
                    ),
                })
                continue
            if conflict_with is not None:
                suppressed.append({
                    "rule_id": item["rule_id"],
                    "issue": item["issue"],
                    "recommendation": item["recommendation"],
                    "reason": (
                        f"conflicts with selected rule "
                        f"{conflict_with['rule_id']!r}"
                    ),
                    "escalation": _ESCALATION_HINT,
                })
                continue
            kept.append(item)
            kept_ids.add(item["rule_id"])
            kept_param_targets[parameter] = item["recommendation"]["to"]

        for item in kept:
            item.pop("_severity_rank", None)
            item.pop("_sort_gap", None)
            item.pop("_conflicts_with", None)

        return kept, suppressed

    # ------------------------------------------------------------------
    # payload assembly
    # ------------------------------------------------------------------

    def _payload(
        self,
        status: str,
        context: RecommendationContext,
        reason: Optional[str] = None,
        items: Optional[List[Dict[str, Any]]] = None,
        suppressed: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "rule_schema_version": SCHEMA_VERSION,
            "mode": "rules",
            "status": status,
            "engine": "deterministic_rule_engine",
            "rules_loaded": self.rule_count,
            "disclaimer": _DISCLAIMER,
        }
        if reason:
            payload["reason"] = reason
        if items is not None:
            payload["items"] = items
            payload["suppressed"] = suppressed or []
            modes = context.failure_summary.get("modes") or {}
            payload["failure_modes_detected"] = {
                mode: entry.get("confirmed", 0)
                for mode, entry in modes.items()
                if entry.get("confirmed")
            }
        return payload
