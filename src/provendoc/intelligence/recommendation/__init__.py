"""Recommendation — deterministic, rule-driven tuning advice.

The default evaluate() advice path. Three layers, in increasing cost:

    rules   (this package) — deterministic rule engine over the versioned
            TOML table; the default. Inputs: strategy params, evaluation
            result, confirmed retrieval failure modes, document profile.
    compare — cheap verification: apply a suggested param set and
            re-evaluate quality side by side (wired in sdk/client.py).
    search  — heavyweight StrategyOptimizer exploration; explicit only,
            never the evaluate() default (intelligence/optimization/).

Public surface:
    load_rules / RuleTableError   — rule table loading + validation
    RuleRecommender               — the engine
    RecommendationContext         — the four engine inputs
    strategy_params_from_policy   — RepresentationPolicy → flat dict
    build_document_profile        — document + DQR → profile facts
"""

from provendoc.intelligence.recommendation.engine import (
    RecommendationContext,
    RuleRecommender,
    build_document_profile,
    strategy_params_from_policy,
    summarize_failure_report,
)
from provendoc.intelligence.recommendation.rules import (
    Rule,
    RuleTableError,
    load_rules,
)

__all__ = [
    "RecommendationContext",
    "Rule",
    "RuleRecommender",
    "RuleTableError",
    "build_document_profile",
    "load_rules",
    "strategy_params_from_policy",
    "summarize_failure_report",
]
