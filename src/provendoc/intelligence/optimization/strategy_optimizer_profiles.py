"""Strategy Optimizer profiles and search-space definitions.

Contains (split from strategy_optimizer.py in 2026-09, verbatim relocation):
- OptimizationProfile   document-type-aware optimization objectives (preset profiles)
- Search-space constants  BOUNDARY_OPTIONS / SCALE_TIERS /
                        DEFAULT_STRATEGY_SPACE / HIERARCHICAL_PHASES /
                        SCALE_ORDER / hard-constraint mode sets / lexicographic weight constants
- policy_spec / spec_to_policy   policy spec ↔ RepresentationPolicy

For the design constitution and overall conventions see the provendoc.intelligence.
optimization.strategy_optimizer module docstring. The relocation only fixes one legacy
mypy type debt (Literal narrowing of context_mode) and involves no
runtime behavior change.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from provendoc.core.strategies.representation_policy import (
    BoundaryPreference,
    RepresentationPolicy,
    RuntimePackagingPolicy,
)
from provendoc.integrity.failure_modes import FailureMode

# ==================================
# policy search space (module level)
# ==================================
#
# The search space is defined in POLICY terms — the same
# levers that the cqi_validity experiment showed produce
# genuinely different chunking behavior:
#
#     boundary    — where chunks are allowed to split
#                   (token_limit / discourse_unit /
#                   heading_boundary / structural_block)
#     scale       — packaging size tier (soft/hard token limits)
#
# structural_relations is derived from boundary: the mechanical
# token_limit baseline runs with all semantics off; every other
# boundary runs the full semantic machinery. This mirrors the
# A/B/C/D strategy definitions in the validity experiment.
# ==================================

BOUNDARY_OPTIONS = [
    "token_limit",        # mechanical baseline (A)
    "discourse_unit",     # semantic paragraph units (B)
    "heading_boundary",   # section-sized, heading-driven (C)
    "structural_block",   # structure-aware, code/table glue (D)
]

SCALE_TIERS = {
    "small":  (120, 200),   # (soft, hard)
    "medium": (250, 400),
    "large":  (450, 700),
}

DEFAULT_STRATEGY_SPACE = {
    "boundary": list(BOUNDARY_OPTIONS),
    "scale": list(SCALE_TIERS.keys()),
    # Only heading_boundary materializes the True setting; other
    # boundaries normalize it to False in policy_spec().
    "merge_short_siblings": [False, True],
}

HIERARCHICAL_PHASES = [
    {"param": "boundary", "values": list(BOUNDARY_OPTIONS)},
    {"param": "scale", "values": list(SCALE_TIERS.keys())},
    {"param": "merge_short_siblings", "values": [False, True]},
]

# ==================================
# controlled-search constants (2026-09-07 decision, open question 3)
# ==================================

SCALE_ORDER = ["small", "medium", "large"]
"""Scale tier ordering — controlled search only moves to ADJACENT tiers."""

# ==================================
# hard constraints (violations disqualify a candidate outright)
# ==================================
#
# 1. protected_block — a confirmed (confidence >= 0.6) structure
#    break at critical/error severity: code blocks, tables,
#    procedure steps, list groups split across chunks.
# 2. cross_section — a confirmed semantic-drift instance: a chunk
#    that crosses into an unrelated section. Severity is ignored
#    here: crossing sections is exactly the forbidden behavior.
# 3. hard_limit — a chunk exceeding the strategy's hard limit,
#    EXCEPT when the chunk is a single protected block (one fenced
#    code block / one markdown table): structural completeness
#    beats size by design (see RuntimePackagingPolicy docs).
# 4. min_threshold — an invariant score below the profile
#    threshold (profile.min_thresholds are hard, not discounts).
#    Key names are invariant-track names (hierarchy_preservation / granularity_
#    appropriateness, etc.); they migrated with the old six-dimension pipeline in batch 4.
#    The report-layer banding thresholds (rse_report.QUALITY_DIMENSION_THRESHOLDS)
#    are a separate convention (band rendering, not hard constraints); not reused here.
# 5. cross_sheet_merge — no merging across sheets without
#    evidence. N/A for narrative/config domains; enforced when
#    the table domain joins the optimizer.
# ==================================

PROTECTED_BLOCK_MODES = {
    FailureMode.CODE_EXPLANATION_SPLIT,
    FailureMode.EXPLANATION_CODE_SPLIT,
    FailureMode.TABLE_DESCRIPTION_SPLIT,
    FailureMode.PROCEDURE_STEP_SPLIT,
    FailureMode.LIST_ITEM_SPLIT,
}

CROSS_SECTION_MODES = {FailureMode.SEMANTIC_DRIFT}

_REJECTION_SEVERITIES = ("critical", "error")

# ==================================
# objective composition (2026-09-11 step 4 refactor: lexicographic total order)
#
# rank_key = (gate_rank,
#             failure_rate,          # severity-weighted / chunk-normalized, no cap
#             -quality_composite,    # invariant_v1 canonical (new five dimensions)
#             -advisory_preference,
#             complexity_penalty)
#
# Lower wins; disqualified always ranks first. The additive composition (canonical - penalty
# + advisory - complexity) and HARD_FAILURE_PENALTY_CAP are both retired: the cap
# let high-scoring candidates "buy off" critical structural damage (§7 item 6,
# forbidden by the adversarial acceptance).
#
# FailureMode → severity-level mapping audit (an explicit obligation before retiring the
# penalty cap; the severity single source of truth = integrity/failure_modes.py
# FAILURE_MODE_DEFINITIONS):
#
#   critical (1)
#     CODE_EXPLANATION_SPLIT   → HC1 protected_block (disqualify)
#                                 + gate v1 first level (critical density)
#   error (7)
#     EXPLANATION_CODE_SPLIT   → HC1 + lexicographic second level
#     PROCEDURE_STEP_SPLIT     → HC1 + lexicographic second level
#     LIST_ITEM_SPLIT          → HC1 + lexicographic second level
#     TABLE_DESCRIPTION_SPLIT  → severity=warning, see below
#     DEFINITION_EXAMPLE_SPLIT → lexicographic second level (no HC, density catches it)
#     SECTION_CONTEXT_LOSS    → lexicographic second level
#     DISCOURSE_SPLIT          → lexicographic second level
#   warning (4)
#     HEADING_AMBIGUITY        → sanity flags (same as before retirement)
#     CROSS_REFERENCE_BREAK    → sanity flags
#     HIERARCHY_AMBIGUITY      → sanity flags
#     SEMANTIC_DRIFT           → HC2 cross_section (a confirmed instance is a violation)
#                                 + sanity flags
#     TABLE_DESCRIPTION_SPLIT  → sanity flags (HC1 rejects only
#                                 critical/error; warning keeps its advisory role)
#
# Conclusion: all non-blocking confirmed failures are caught by lexicographic levels 1/2,
# nothing disappears silently; warnings keep their advisory role (sanity flags).
# ==================================

HARD_FAILURE_SEVERITY_WEIGHTS = {
    "critical": 1.0,
    "error": 0.7,
}
"""Confirmed instances weighted by severity for the failure-rate
lexicographic level; warnings/info are free (those feed the sanity
flags, not the ordering)."""

ADVISORY_PREFERENCE_BONUS = {
    "boundary": 0.02,   # candidate keeps the profile baseline boundary
    "scale": 0.01,      # candidate keeps the profile baseline scale
}
"""Advisory preference: a small bonus for staying on the profile
baseline, so a candidate must beat the baseline by a real margin
to displace it (ties resolve toward the baseline)."""

COMPLEXITY_PENALTY_PER_LEVER = 0.01
"""Complexity penalty: each policy lever deviating from the profile
baseline costs this much (boundary / scale / merge_short_siblings)."""


# ==================================
# OptimizationProfile
# ==================================


@dataclass
class OptimizationProfile:
    """Document-type-aware optimization objective.

    Different document types care about different invariants.
    A CSV file needs high hierarchy_preservation but discourse_
    locality is irrelevant. A markdown doc needs boundary_cleanliness
    and hierarchy_preservation. A plain text story needs discourse_
    locality.

    Profile-first (2026-09-07 decision): each profile declares a
    baseline strategy (``baseline``) and a controlled boundary
    space (``allowed_boundaries``). The optimizer searches an
    enumerated neighborhood of the baseline — never the full space.

    Attributes:
        name: profile identifier
        min_thresholds: invariant name -> minimum acceptable score.
            HARD constraints since the 2026-09-07 decision: a
            candidate whose invariant score falls below the
            threshold is disqualified outright, not discounted.
            (The old six-dimension 1.5x prioritize reweighting was retired with the legacy
            canonical term; the field has been deleted.)
        baseline: profile baseline strategy
            {boundary, scale, merge_short_siblings}. Missing keys
            fall back to the module defaults (discourse_unit /
            medium / False).
        allowed_boundaries: controlled boundary space. Empty means
            "semantic boundaries only, baseline first" — the
            mechanical token_limit control is excluded because it
            runs with all semantics off and can only win by metric
            gaming (see _sanity_check).
    """

    name: str = "balanced"
    min_thresholds: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    allowed_boundaries: list = field(default_factory=list)

    # ==================================
    # baseline / controlled space resolution
    # ==================================

    _BASELINE_DEFAULTS = {
        "boundary": "discourse_unit",
        "scale": "medium",
        "merge_short_siblings": False,
    }

    def resolved_baseline(self) -> dict:
        """Baseline with module defaults filled in."""
        merged = dict(self._BASELINE_DEFAULTS)
        merged.update(self.baseline or {})
        return merged

    def controlled_boundaries(self) -> list:
        """Enumerated boundary space for controlled search.

        Baseline first (deterministic ordering), then the profile's
        allowed boundaries, falling back to the semantic set. The
        mechanical ``token_limit`` control group is never included
        implicitly — it must be requested explicitly via
        ``strategy_space`` (it is an experimental control, not a
        deployable policy).
        """
        if self.allowed_boundaries:
            base = self.resolved_baseline()["boundary"]
            ordered = [base]
            ordered.extend(
                b for b in self.allowed_boundaries
                if b != base and b in BOUNDARY_OPTIONS
            )
            return ordered
        base = self.resolved_baseline()["boundary"]
        rest = [
            b for b in (
                "discourse_unit", "heading_boundary", "structural_block",
            )
            if b != base
        ]
        return [base] + rest

    # ==================================
    # preset profiles
    # ==================================

    @classmethod
    def balanced(cls):
        """Default — uses the canonical composite directly, no reweighting."""
        return cls(name="balanced")

    @classmethod
    def markdown(cls):
        """Markdown — structure and boundaries matter most."""
        return cls(
            name="markdown",
            min_thresholds={
                "hierarchy_preservation": 0.6,
            },
            baseline={"boundary": "heading_boundary", "scale": "medium"},
            allowed_boundaries=[
                "heading_boundary", "structural_block", "discourse_unit",
            ],
        )

    @classmethod
    def plain_text(cls):
        """Plain text — cohesion and size matter most."""
        return cls(
            name="plain_text",
            min_thresholds={
                "granularity_appropriateness": 0.5,
            },
            baseline={"boundary": "discourse_unit", "scale": "medium"},
            allowed_boundaries=[
                "discourse_unit", "structural_block", "heading_boundary",
            ],
        )

    @classmethod
    def code_heavy(cls):
        """Code-heavy docs — structure is everything, cohesion is noise.

        Manual opt-in only — deliberately NOT auto-routed
        (optimizer_code_bypass_prereg_v1 D3=B, 2026-10-25): corpus
        evidence showed auto-routing code-heavy markdown here
        changes no optimizer outcome (markdown()'s controlled
        space already includes structural_block; top-6 code-heavy
        docs scored identically under both profiles) while halving
        the candidate space.
        """
        return cls(
            name="code_heavy",
            min_thresholds={
                "hierarchy_preservation": 0.7,
            },
            baseline={"boundary": "structural_block", "scale": "medium"},
            allowed_boundaries=["structural_block", "heading_boundary"],
        )

    @classmethod
    def csv(cls):
        """CSV/tabular — structure only, cohesion irrelevant."""
        return cls(
            name="csv",
            min_thresholds={
                "hierarchy_preservation": 0.7,
                "granularity_appropriateness": 0.4,
            },
            baseline={"boundary": "structural_block", "scale": "medium"},
            allowed_boundaries=["structural_block"],
        )

    @classmethod
    def auto(cls, file_path):
        """Auto-detect profile from file extension."""
        suffix = Path(file_path).suffix.lower()
        if suffix in (
            ".md", ".markdown",
            ".mdown", ".mkdn", ".mdwn", ".mdx", ".rmd", ".qmd",
        ):
            return cls.markdown()
        if suffix == ".csv":
            return cls.csv()
        if suffix in (".yaml", ".yml", ".toml"):
            return cls.plain_text()
        return cls.plain_text()


def policy_spec(
    boundary: str, scale: str,
    merge_short_siblings: bool = False,
) -> dict:
    """Build a user-facing policy spec from policy-space choices.

    ``merge_short_siblings`` is meaningful only for heading-boundary
    policies.  Keeping it in the spec makes a strategy's structural
    behavior explicit and lets the optimizer compare it fairly rather
    than hiding the merge as an implementation-side mutation.
    """
    soft, hard = SCALE_TIERS[scale]
    mechanical = boundary == "token_limit"
    return {
        "boundary": boundary,
        "scale": scale,
        "structural_relations": not mechanical,
        "merge_short_siblings": (
            boundary == "heading_boundary" and merge_short_siblings
        ),
        "soft_limit": soft,
        "hard_limit": hard,
    }


def spec_to_policy(spec: dict) -> RepresentationPolicy:
    """Materialize a RepresentationPolicy from a policy spec.

    Semantics mirror the calibration experiment strategies:
    token_limit = mechanical baseline with all semantics off;
    every other boundary runs full invariants + context propagation.
    """
    from provendoc.core.strategies.representation_policy import (
        ContextPropagation,
        IntegrityRules,
        SemanticInvariants,
    )

    boundary = spec["boundary"]
    soft = spec.get("soft_limit") or SCALE_TIERS[spec["scale"]][0]
    hard = spec.get("hard_limit") or SCALE_TIERS[spec["scale"]][1]

    if boundary == "token_limit":
        return RepresentationPolicy(
            name=f"opt_{boundary}_{spec['scale']}",
            boundary_preference=BoundaryPreference(
                primary="token_limit",
                use_structural_relations=False,
            ),
            invariants=SemanticInvariants(
                hierarchy_continuity=False,
                structural_block_complete=False,
                discourse_locality=False,
            ),
            integrity_rules=IntegrityRules(
                code_blocks=False, tables=False,
                blockquotes=False, list_groups=False,
            ),
            context_propagation=ContextPropagation(mode="none"),
            packaging=RuntimePackagingPolicy(
                size_soft_limit=soft, size_hard_limit=hard,
            ),
        )

    context_mode: Literal["heading_only", "selective"] = (
        "heading_only" if boundary == "heading_boundary" else "selective"
    )
    merge_short_siblings = (
        boundary == "heading_boundary"
        and bool(spec.get("merge_short_siblings", False))
    )
    return RepresentationPolicy(
        name=f"opt_{boundary}_{spec['scale']}",
        boundary_preference=BoundaryPreference(
            primary=boundary,
            merge_short_sibling_sections=merge_short_siblings,
        ),
        context_propagation=ContextPropagation(
            mode=context_mode,
            semantic_dependency_locality=(boundary == "structural_block"),
        ),
        packaging=RuntimePackagingPolicy(
            size_soft_limit=soft, size_hard_limit=hard,
        ),
    )
