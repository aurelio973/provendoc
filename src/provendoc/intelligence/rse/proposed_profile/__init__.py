# -*- coding: utf-8 -*-
"""proposed_quality_profile_v1 — candidate quality profile (a DDR proposal, not canonical).

Design doc: re_quality_profile_proposed_v1.md

Status: PROPOSED. Physically isolated from rse_report.quality_profile (invariant_v1, canonical),
the two tracks run in parallel with no cross-mixing. Until the preregistered validation protocol (DDR §6.2) passes,
this profile's composite must not serve externally as a quality verdict.

Where the six hard rules (DDR §2.2) land in this package:
    1. single primary dimension per event + read-only diagnostic_views  → event_ledger.EventLedger.book
    2. record events before judging quality                    → evaluator ordering (events first)
    3. Policy Compliance adjudicates policy_forced            → event_ledger.policy_forced_for_*
    4. D2 outputs event rates only (0.8/0.7 scales retired)     → evaluators.evaluate_boundary
    5. tiered admission (deterministic → event audit)          → all evaluators are deterministic checks
    6. weights are always provisional                         → profile.PROVISIONAL_WEIGHTS
"""

from provendoc.intelligence.rse.proposed_profile.evaluators import (
    evaluate_content_observation,
    evaluate_profile,
)
from provendoc.intelligence.rse.proposed_profile.event_ledger import (
    DIM_BOUNDARY,
    DIM_CONTENT,
    DIM_CONTEXT,
    DIM_SEGMENTATION,
    DIM_STRUCTURAL,
    EVENT_TYPES,
    EventLedger,
    QualityEvent,
    policy_forced_for_merge,
    policy_forced_for_split,
)
from provendoc.intelligence.rse.proposed_profile.profile import (
    PROPOSED_PROFILE_VERSION,
    build_proposed_profile,
)

__all__ = [
    "DIM_BOUNDARY",
    "DIM_CONTEXT",
    "DIM_CONTENT",
    "DIM_SEGMENTATION",
    "DIM_STRUCTURAL",
    "EVENT_TYPES",
    "EventLedger",
    "QualityEvent",
    "policy_forced_for_merge",
    "policy_forced_for_split",
    "evaluate_content_observation",
    "evaluate_profile",
    "PROPOSED_PROFILE_VERSION",
    "build_proposed_profile",
]
