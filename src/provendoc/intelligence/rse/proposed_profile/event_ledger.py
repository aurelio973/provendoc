# -*- coding: utf-8 -*-
"""Event ledger (proposed_quality_profile_v1, DDR §2.1/§2.2).

Iron-rule anchors (re_quality_profile_proposed_proposed_v1.md §2.2):

    Rule 1  every event has exactly one primary dimension (primary_dimension);
            diagnostic_views is a read-only side channel and must not contain the primary dimension;
    Rule 2  record events first, judge quality second — the ledger is the sole scoring source of truth,
            dimension scores are always derived from the ledger (see evaluators/profile);
    Rule 3  Policy Compliance adjudicates policy_forced — policy-forced events
            are recorded (counts_toward_quality=False) but stay out of dimension scores.

Single source of truth for events: the event_type → primary_dimension registry (EVENT_TYPES) is the only
mapping; evaluators must not reroute privately. Changing a primary dimension = changing the registry = an explicit design change.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# schema freeze marker (DDR §2.1/§2.5): event schema, event-type registry,
# dimension set, severity enum, and the exact profile top-level-key topology follow this version.
# Any change = an explicit design change: bump version + decision-record entry + frozen-test sync.
# v1.1 (2026-09-16): registered html_comment_split (D1) — an HTML comment block
# cut across chunks is isomorphic to a fence cut (R-016 forensics); decision record in DDR §9.
SCHEMA_VERSION = "event_ledger_v1.1"

# ==================================
# dimension constants (naming clarification in DDR §1.1: D0 is currently an observation item, not a fifth formal dimension)
# ==================================

DIM_CONTENT = "content_preservation"        # D0 (v1: observation-only, not scored)
DIM_STRUCTURAL = "structural_integrity"     # D1
DIM_BOUNDARY = "boundary_soundness"         # D2
DIM_CONTEXT = "contextual_sufficiency"      # D3
DIM_SEGMENTATION = "segmentation_fitness"   # D4

# formal dimensions that can carry events (D0 produces no scoring events in v1)
SCORING_DIMENSIONS = (
    DIM_STRUCTURAL,
    DIM_BOUNDARY,
    DIM_CONTEXT,
    DIM_SEGMENTATION,
)

# ==================================
# event-type registry (single source of truth for rule 1)
# ==================================
# event_type → primary_dimension. Event types outside the registry are always refused entry.

EVENT_TYPES: Dict[str, str] = {
    # --- D1 structural_integrity (deterministic structural evidence) ---
    "code_fence_broken": DIM_STRUCTURAL,
    "html_comment_split": DIM_STRUCTURAL,
    "table_split": DIM_STRUCTURAL,
    "orphan_heading": DIM_STRUCTURAL,
    "heading_survival_gap": DIM_STRUCTURAL,
    # --- D2 boundary_soundness (cut-point legality; event rate only) ---
    "illegal_boundary": DIM_BOUNDARY,
    "missed_protected_boundary": DIM_BOUNDARY,
    # --- D3 contextual_sufficiency (per-chunk contract checks) ---
    "dangling_opening": DIM_CONTEXT,
    "missing_heading_context": DIM_CONTEXT,
    "unresolvable_reference": DIM_CONTEXT,
    "context_overload": DIM_CONTEXT,
    # --- D4 segmentation_fitness (two-phase, rule 3 applies here) ---
    "source_unit_split": DIM_SEGMENTATION,
    "cross_section_merge": DIM_SEGMENTATION,
}

_VALID_SEVERITIES = ("critical", "error", "warning")


@dataclass
class QualityEvent:
    """One auditable quality event (DDR §2.1 schema).

    counts_toward_quality is a derived value (rule 3): events with policy_forced=True
    are recorded but not counted; callers may not set it directly.
    """

    event_id: str
    event_type: str
    primary_dimension: str
    chunk_ids: List[str] = field(default_factory=list)
    severity: str = "error"
    policy_forced: bool = False
    # (line_start, line_end) or (None, None) — pass None honestly when facts are missing
    source_spans: List[Tuple[Optional[int], Optional[int]]] = field(
        default_factory=list
    )
    # read-only side-channel view: which dimensions the event is also a diagnostic signal for (never the primary dimension)
    diagnostic_views: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def counts_toward_quality(self) -> bool:
        """Whether this event counts toward the quality score (policy-forced ones are recorded only)."""
        return not self.policy_forced

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "primary_dimension": self.primary_dimension,
            "chunk_ids": list(self.chunk_ids),
            "severity": self.severity,
            "policy_forced": self.policy_forced,
            "counts_toward_quality": self.counts_toward_quality,
            "source_spans": [list(s) for s in self.source_spans],
            "diagnostic_views": list(self.diagnostic_views),
            "detail": dict(self.detail),
        }


@dataclass
class EventLedger:
    """Event ledger: the sole scoring source of truth (rule 2).

    book() is the only entry point and enforces the three iron rules; dimension scores can only
    be derived from ledger.events (see profile.build_proposed_profile);
    any scoring path that bypasses the ledger is a violation.
    """

    events: List[QualityEvent] = field(default_factory=list)
    _seq: int = 0

    def book(self, event: QualityEvent) -> QualityEvent:
        """Book one event, enforcing the invariants of iron rules 1/3."""
        registered = EVENT_TYPES.get(event.event_type)
        if registered is None:
            raise ValueError(
                f"unknown event_type {event.event_type!r}; "
                f"registered types: {sorted(EVENT_TYPES)}"
            )
        if event.primary_dimension != registered:
            raise ValueError(
                f"event_type {event.event_type!r} must have "
                f"primary_dimension={registered!r}, "
                f"got {event.primary_dimension!r} (rule 1: single "
                f"primary dimension from registry)"
            )
        if event.primary_dimension not in SCORING_DIMENSIONS:
            raise ValueError(
                f"primary_dimension {event.primary_dimension!r} is not "
                f"a scoring dimension (D0 is observation-only in v1)"
            )
        if event.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}, "
                f"got {event.severity!r}"
            )
        views = list(event.diagnostic_views or [])
        if event.primary_dimension in views:
            raise ValueError(
                f"diagnostic_views must not contain primary dimension "
                f"{event.primary_dimension!r} (rule 1: views are "
                f"read-only side channels)"
            )
        for view in views:
            if view not in SCORING_DIMENSIONS:
                raise ValueError(
                    f"diagnostic view {view!r} is not a registered "
                    f"dimension"
                )
        event.diagnostic_views = views
        self._seq += 1
        event.event_id = f"evt-{self._seq:04d}-{event.event_type}"
        self.events.append(event)
        return event

    def events_for(self, dimension: str, scoring_only: bool = True):
        """Derived interface: events of a dimension (the read side of rule 2).

        scoring_only=True (default) filters out policy_forced events —
        rule 3: policy-forced behavior is recorded only, never scored.
        """
        return [
            e
            for e in self.events
            if e.primary_dimension == dimension
            and (e.counts_toward_quality or not scoring_only)
        ]

    def to_dict(self) -> List[Dict[str, Any]]:
        """Serialize to a list of dicts."""
        return [e.to_dict() for e in self.events]


# ==================================
# Rule 3: Policy Compliance adjudication (single source of truth)
# ==================================
# When an evaluator sees a "suspected bad behavior" it must first ask: is this policy-forced?
# Adjudication functions are centralized here; evaluators must not inline their own judgments (or exemption criteria drift).


def _policy_hard_limits(policy) -> List[int]:
    """Collect hard capacity limits (characters) declared on the policy.

    The base-granularity constraint is packaging.size_hard_limit; the coarse-packaging
    constraint is coarse.hard_max_chars. An empty return = the policy declares
    no hard limit (unadjudicated; record an event rather than silently exempt).
    """
    limits: List[int] = []
    packaging = getattr(policy, "packaging", None)
    if packaging is not None:
        value = getattr(packaging, "size_hard_limit", None)
        if value:
            limits.append(int(value))
    coarse = getattr(policy, "coarse", None)
    if coarse is not None:
        value = getattr(coarse, "hard_max_chars", None)
        if value:
            limits.append(int(value))
    return limits


def policy_forced_for_split(combined_chars: Optional[int], policy) -> bool:
    """policy_forced adjudication for source_unit_split.

    A split whose merged size exceeds a hard limit is policy-forced (capacity constraint),
    not over-fragmentation. The tightest constraint (min) is the binding limit —
    exemptions err strict: record extra events rather than silently pass a suspicious split.
    A missing policy or no hard limit counts as unadjudicated (False).
    """
    if policy is None or combined_chars is None:
        return False
    limits = _policy_hard_limits(policy)
    if not limits:
        return False
    return int(combined_chars) > min(limits)


def policy_forced_for_merge(policy) -> bool:
    """policy_forced adjudication for cross_section_merge.

    When boundary_preference.merge_short_sibling_sections=True,
    merging across short sibling sections is explicitly policy-requested behavior — recorded but not counted.
    """
    if policy is None:
        return False
    boundary = getattr(policy, "boundary_preference", None)
    if boundary is None:
        return False
    return bool(
        getattr(boundary, "merge_short_sibling_sections", False)
    )
