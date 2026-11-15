"""Recommendation rule table — versioned, data-driven, auditable.

Rules live in ``recommendation_rules.toml`` (shipped as package data), not
in code. Each rule is a deterministic mapping from a diagnostic signal
(low quality dimension / confirmed retrieval failure / chunk metric) to one
parameter suggestion, carrying:

    issue           — what is wrong (dimension name or failure mode)
    cause           — why the engine believes so (evidence-based)
    recommendation  — {parameter, from, to}  ("from" filled at runtime)
    expected_effect — what should improve (a hypothesis, never a promise)
    side_effects    — mandatory trade-offs; rules must never claim a
                      change "will definitely improve" quality
    confidence      — corpus hit-rate style estimate in [0, 1]; seeded
                      values are marked in verified_by until the offline
                      optimizer + review_ledger adjudication replaces them
    conflicts_with  — explicit rule-id conflicts; same-parameter rules
                      with different targets conflict implicitly

``when`` guards make a rule conditional on the current strategy params
and the document profile, so the same issue can map to different fixes
depending on context (e.g. section-driven docs vs flat docs).

Loading is strict: a malformed rule table raises RuleTableError. Callers
(the SDK) catch that and degrade recommendations to ``unavailable`` —
a broken rule file must never break evaluate().
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on py3.10 via tomli dependency
    import tomli as tomllib

SCHEMA_VERSION = 1

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "recommendation_rules.toml"

# Trigger kinds the engine knows how to evaluate. Unknown kinds are a
# schema error at load time (fail fast on the data file, not at runtime).
TRIGGER_KINDS = (
    "dimension_below",      # invariant score[dimension] < threshold
    "failure_mode_present",  # confirmed retrieval failure of mode present
    "metric_above",         # evaluation metadata metric > threshold
    "metric_below",         # evaluation metadata metric < threshold
)

# Operators allowed inside when.params guards.
_GUARD_OPERATORS = ("eq", "not_eq", "in", "gt", "ge", "lt", "le")


class RuleTableError(ValueError):
    """Raised when the rule table fails schema validation."""


@dataclass(frozen=True)
class Rule:
    """One deterministic recommendation rule."""

    rule_id: str
    issue: str
    cause: str
    trigger: Dict[str, Any]
    recommend_parameter: str
    recommend_to: Any
    expected_effect: str
    side_effects: List[str] = field(default_factory=list)
    confidence: float = 0.5
    conflicts_with: List[str] = field(default_factory=list)
    verified_by: Dict[str, Any] = field(default_factory=dict)
    when: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "rule_id": self.rule_id,
            "issue": self.issue,
            "cause": self.cause,
            "trigger": self.trigger,
            "recommend": {
                "parameter": self.recommend_parameter,
                "to": self.recommend_to,
            },
            "expected_effect": self.expected_effect,
            "side_effects": list(self.side_effects),
            "confidence": self.confidence,
            "conflicts_with": list(self.conflicts_with),
            "verified_by": dict(self.verified_by),
            "when": dict(self.when),
        }


def _require(condition: bool, rule_id: str, message: str) -> None:
    if not condition:
        raise RuleTableError(f"rule {rule_id!r}: {message}")


def _parse_rule(raw: Dict[str, Any], index: int) -> Rule:
    rule_id = raw.get("id")
    # Inline isinstance checks (not _require) so mypy narrows the
    # ``raw.get`` unions to concrete types for the rest of the function.
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RuleTableError(f"rule <#{index}>: missing or empty 'id'")

    issue = raw.get("issue")
    if not isinstance(issue, str) or not issue.strip():
        raise RuleTableError(f"rule {rule_id!r}: missing or empty 'issue'")

    cause = raw.get("cause", "")
    _require(isinstance(cause, str), rule_id, "'cause' must be a string")

    trigger = raw.get("trigger")
    if not isinstance(trigger, dict):
        raise RuleTableError(f"rule {rule_id!r}: 'trigger' must be a table")
    kind = trigger.get("kind")
    _require(
        kind in TRIGGER_KINDS,
        rule_id,
        f"trigger.kind must be one of {TRIGGER_KINDS}, got {kind!r}",
    )
    if kind == "dimension_below":
        _require(
            isinstance(trigger.get("dimension"), str),
            rule_id,
            "dimension_below trigger needs a 'dimension' string",
        )
        _require(
            isinstance(trigger.get("threshold"), (int, float))
            and not isinstance(trigger.get("threshold"), bool),
            rule_id,
            "dimension_below trigger needs a numeric 'threshold'",
        )
    elif kind == "failure_mode_present":
        modes = trigger.get("modes", trigger.get("mode"))
        if isinstance(modes, str):
            modes = [modes]
        if (
            not isinstance(modes, list)
            or not modes
            or not all(isinstance(item, str) for item in modes)
        ):
            raise RuleTableError(
                f"rule {rule_id!r}: failure_mode_present trigger needs a "
                "'mode' string or a non-empty 'modes' list (any confirmed "
                "mode fires the rule)"
            )
        trigger = {**trigger, "modes": list(modes)}
        trigger.pop("mode", None)
    else:  # metric_above / metric_below
        _require(
            isinstance(trigger.get("metric"), str),
            rule_id,
            f"{kind} trigger needs a 'metric' string",
        )
        _require(
            isinstance(trigger.get("threshold"), (int, float))
            and not isinstance(trigger.get("threshold"), bool),
            rule_id,
            f"{kind} trigger needs a numeric 'threshold'",
        )

    recommend = raw.get("recommend")
    if not isinstance(recommend, dict):
        raise RuleTableError(f"rule {rule_id!r}: 'recommend' must be a table")
    parameter = recommend.get("parameter")
    if not isinstance(parameter, str) or not parameter.strip():
        raise RuleTableError(
            f"rule {rule_id!r}: recommend.parameter must be a non-empty string"
        )
    _require(
        "to" in recommend,
        rule_id,
        "recommend.to is required (concrete value — relative hints "
        "are documentation, not instructions)",
    )

    expected_effect = raw.get("expected_effect", "")
    _require(
        isinstance(expected_effect, str),
        rule_id,
        "'expected_effect' must be a string",
    )

    side_effects = raw.get("side_effects", [])
    _require(
        isinstance(side_effects, list)
        and all(isinstance(item, str) for item in side_effects),
        rule_id,
        "'side_effects' must be a list of strings",
    )

    confidence = raw.get("confidence", 0.5)
    _require(
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= confidence <= 1.0,
        rule_id,
        "'confidence' must be a number in [0, 1]",
    )

    conflicts_with = raw.get("conflicts_with", [])
    _require(
        isinstance(conflicts_with, list)
        and all(isinstance(item, str) for item in conflicts_with),
        rule_id,
        "'conflicts_with' must be a list of rule ids",
    )

    verified_by = raw.get("verified_by", {})
    _require(
        isinstance(verified_by, dict), rule_id, "'verified_by' must be a table"
    )

    when = raw.get("when", {})
    _require(isinstance(when, dict), rule_id, "'when' must be a table")
    params_guard = when.get("params", {})
    _require(
        isinstance(params_guard, dict), rule_id, "when.params must be a table"
    )
    for param_name, condition in params_guard.items():
        _require(
            isinstance(condition, dict),
            rule_id,
            f"when.params.{param_name} must be a table of operators",
        )
        unknown_ops = set(condition) - set(_GUARD_OPERATORS)
        _require(
            not unknown_ops,
            rule_id,
            f"when.params.{param_name}: unknown operators {sorted(unknown_ops)} "
            f"(allowed: {list(_GUARD_OPERATORS)})",
        )

    return Rule(
        rule_id=rule_id.strip(),
        issue=issue.strip(),
        cause=cause,
        trigger=dict(trigger),
        recommend_parameter=parameter.strip(),
        recommend_to=recommend["to"],
        expected_effect=expected_effect,
        side_effects=list(side_effects),
        confidence=float(confidence),
        conflicts_with=list(conflicts_with),
        verified_by=dict(verified_by),
        when=dict(when),
    )


def load_rules(path: Optional[Path] = None) -> List[Rule]:
    """Load and validate the rule table.

    Args:
        path: override path (tests); defaults to the shipped
            ``recommendation_rules.toml`` beside this module.

    Returns:
        List of validated rules in file order.

    Raises:
        RuleTableError: on any schema violation (unknown trigger kind,
            missing required field, duplicate id, dangling conflicts_with).
    """
    rules_path = Path(path) if path is not None else DEFAULT_RULES_PATH
    try:
        raw_text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleTableError(f"rule table unreadable: {exc}") from exc
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleTableError(f"rule table is not valid TOML: {exc}") from exc

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RuleTableError(
            f"unsupported rule schema_version {version!r} "
            f"(expected {SCHEMA_VERSION})"
        )

    raw_rules = data.get("rule")
    if not isinstance(raw_rules, list):
        raise RuleTableError("rule table must contain [[rule]] entries")

    rules = [_parse_rule(raw, index) for index, raw in enumerate(raw_rules)]

    ids = [rule.rule_id for rule in rules]
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise RuleTableError(f"duplicate rule ids: {duplicates}")

    known_ids = set(ids)
    for rule in rules:
        dangling = [c for c in rule.conflicts_with if c not in known_ids]
        if dangling:
            raise RuleTableError(
                f"rule {rule.rule_id!r}: conflicts_with references unknown "
                f"rule ids {dangling}"
            )

    return rules
