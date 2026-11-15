"""Config Chunk Quality Index — semantic structure integrity for
configuration files.

This is the config-domain quality evaluator. It evaluates CHUNK
quality for structured configuration files (JSON, YAML, TOML, INI)
where the ingestion philosophy is:

    config is a key-value tree → chunks should preserve
    every path → each chunk is one addressable leaf or subtree

==================================
Ground Truth Philosophy (the core constitution)
==================================

Config quality evaluation is a PROXY METRIC SYSTEM. It does NOT measure ground truth.
ConfigQuality judges whether config FACTS are correctly parsed,
retained, represented and traceable — never whether the config
is semantically "correct" for the business.

ConfigQuality approximates:
    ✅ hierarchy continuity — is the tree connected both ways?
    ✅ path integrity — is every key fully pathed & unambiguous?
    ✅ locality preservation — are siblings kept adjacent?
    ✅ value fidelity — does every value round-trip?
    ✅ dependency integrity — are references traceable?

ConfigQuality does NOT measure:
    ❌ runtime correctness of config values
    ❌ whether the config will parse without errors
    ❌ semantic "meaning" of configuration keys

==================================
Six-Canonical-Dimension Model
==================================

The evaluator converged from 9 overlapping dimensions to 6
canonical dimensions (each with sub-checks preserved as
breakdowns — merging never loses diagnostic granularity):

    hierarchy_continuity       0.30
        merged hierarchy_preservation + nesting_integrity.
        Bidirectional checks: child→parent path connectivity,
        parent→child empty-container retention, depth
        preservation. Name aligns with the representation
        layer's declared integrity_guarantees vocabulary.

    path_integrity             0.17
        merged semantic_path_completeness +
        namespace_disambiguation. Path completeness for every
        unit; colliding leaf names carry namespace context.

    locality_preservation      0.13
        Sibling keys (same parent) stay adjacent in the
        fine-grained unit sequence.

    value_fidelity             0.20
        merged flattening_stability + scalar_extraction_
        correctness — the two directions (recall/precision) of
        the same path→value bijection. MUST be checked from
        structured entry facts (config_entry_v1.1 contract);
        chunk.text is never re-parsed. Without a contract the
        dimension is N/A (no evidence), not degraded. v1.1:
        container-valued entries are excluded from correctness
        (anti-double-counting); inline-container children enter
        as derived projections (source_precision=line_only).

    dependency_integrity       0.15
        Cross-references: existence, traceability, five-state
        resolution (resolved/unresolved/cyclic/external/dynamic).

    source_anomaly_handling    0.05
        Duplicate keys, override chains, parse conflicts.
        Score = handling-EVIDENCE quality (not anomaly
        presence); issues expose the risk:
        score carries overall quality, issues carry risk.
        role="diagnostic" but included_in_overall=True in v1
        (v2 candidate: move out of the formal weight model,
        risk exposure via issues only).

    description_binding stays a diagnostic/issue (not a
    dimension): JSON has no comments; applicability is
    format-dependent and not yet formalized.

    0.30 + 0.17 + 0.13 + 0.20 + 0.15 + 0.05 = 1.00

    Weights FROZEN as config_quality_v1 (calibrated on the clean
    post-bugfix corpus: value_fidelity mean=0.985/std=0.056,
    path×hierarchy r=0.031 — no redundant explanation).

==================================
Two-Layer Evaluation Architecture
==================================

Layer 1 — Fact layer (contract facts):
    Dimension facts are read from the config_entry_v1 contract
    (Document.metadata["config_entries"]) whenever available:
    entries (path/value/duplicate/override_chain), containers
    (child_count/value_state), five-state references, and loss
    reasons. This REPLACES the legacy text re-parsing logic
    (e.g. ``text.split(": ", 1)``).

    When no contract is present, facts are synthesized from the
    representation units (representation_fallback) — value
    checks become N/A, and fact_source reports the source.

Layer 2 — Representation layer (chunk shapes):
    Chunk-shape dimensions (path completeness, sibling
    locality, container retention) are evaluated over the
    representation units themselves: fine units (entry atoms)
    for ordering/locality, base units (container groups) for
    group statistics.

==================================
Canonical Weight Policy (N/A weights not redistributed)
==================================

Weights are CANONICAL and FIXED. When a dimension is not
applicable (N/A), its weight is NOT redistributed — the overall
score is:

    overall = Σ(active_dim_score × weight) / Σ(all weights)

This forbids a single-block configuration from inflating its
overall score by deactivating dimensions. Cross-format ceilings
differ (comparison across formats is meaningless); within-format
scoring stays honest.

==================================
Sensitive-Value Exemption
==================================

Masking is a retrieval-layer concern (representation renders
sensitive values as "****"). The evaluator exempts masked entries from
value-identity checks — masked facts are quality-neutral.

==================================
Academic References
==================================

- Rabkin & Katz (2011) Static Extraction of Config Dependencies,
  HotCloud — configuration dependency analysis
- Schwarz et al. (2021) Detecting Interpolation Vulnerabilities in
  Configuration Files, ESEC/FSE — variable reference traceability
- Hesselbo et al. (2020) Program Slicing for Configurations,
  ICSE-SEIP — scope locality and configuration maintainability
- Cito et al. (2019) Using Production Data for Testing, ICSE-NIER
  — flattening stability and information fidelity
- Xu et al. (2015) How Not to Lie in Config Files, FSE —
  namespace disambiguation and duplicate key detection
- Tang et al. (2015) Long-lived Configs, CSUR — configuration
  evolution and semantic path stability
- Lewis et al. (2020) RAG — chunk integrity for retrieval
- Bhattacharya et al. (2013) Automated Reasoning about Config,
  ICSE-SEIP — hierarchical structure integrity
- Chen & Jiang (2017) Config Cognition, MSR — semantic path
  completeness and config understanding
- Sayagh et al. (2017) Config Smells, SCAM — duplicate keys
  and interpolation as configuration anti-patterns

==================================
Design Principles
==================================

1. The evaluator is separate from the representation layer.
2. The evaluator measures chunk quality, NOT document quality.
3. The profile (dimensional breakdown) is the primary output.
4. Applicability is checked BEFORE scoring — inapplicable
   dimensions are reported as N/A, not 0 or 1.
5. Weights are canonical and fixed — N/A dimensions do NOT
   participate in weight redistribution.
6. Confidence is decomposed into dimension_coverage,
   evaluation_depth, and format_reliability.
7. Facts come from the config_entry_v1 contract first;
   representation text is never re-parsed for values.
8. Masked entries are exempt from value-identity checks.
9. Six canonical dimensions only — do NOT add a dimension per
   problem; new signals enter as sub-checks or issues.
10. Score carries overall quality; issues carry risk exposure
    (source anomalies get high-severity issues regardless of
    their score impact).

==================================
Module Organization (2026-09 split, zero behavior change)
==================================

- config_quality_model.py        data classes and metric registry (five-layer contract facts)
- config_quality_facts.py        fact layer + format capabilities + applicability (mixin)
- config_quality_dimensions.py   six canonical dimension checks (mixin)
- config_quality_reporting.py    confidence / issues / statistics / empty result (mixin)
- config_quality.py (this file)   facade: class constants + evaluate orchestration
"""

import re
from typing import Any, Dict

from provendoc.intelligence.rse.config_quality_dimensions import (
    ConfigQualityDimensionsMixin,
)
from provendoc.intelligence.rse.config_quality_facts import (
    ConfigQualityFactsMixin,
)
from provendoc.intelligence.rse.config_quality_model import (
    _METRIC_DEFINITIONS,
    FormatCapabilityProfile,
    MetricDefinition,
)
from provendoc.intelligence.rse.config_quality_reporting import (
    ConfigQualityReportingMixin,
)

__all__ = [
    "ConfigQuality",
    "FormatCapabilityProfile",
    "MetricDefinition",
]


class ConfigQuality(
    ConfigQualityFactsMixin,
    ConfigQualityDimensionsMixin,
    ConfigQualityReportingMixin,
):
    """Config-domain Chunk Quality Index.

    Evaluates chunk quality across 6 canonical dimensions with an
    Applicability System that activates/deactivates dimensions
    based on format capabilities and fact availability.

    Dimensions and canonical weights (FROZEN as config_quality_v1):
        [PRIMARY]
        hierarchy_continuity       0.30
        path_integrity             0.17
        locality_preservation      0.13
        value_fidelity             0.20
        [SECONDARY]
        dependency_integrity       0.15
        source_anomaly_handling    0.05  (diagnostic role,
                                          included_in_overall=True)

    Applicability System:
        Each dimension has an applicability condition based on
        FormatCapabilityProfile (+ fact availability). Inactive
        dimensions are reported as N/A (null) and do NOT receive
        redistributed weight — the overall score divides by the
        TOTAL canonical weight (1.00), keeping within-format
        scoring honest.

    Two-Layer Evaluation:
        Facts come from the config_entry_v1 contract (Layer 1);
        chunk-shape checks run over fine/base representation
        units (Layer 2). fact_source reports which layer was used.
        value_fidelity is N/A without contract facts.

    Output structure:
        profile             — dimensional breakdown (4 scoring dims,
                              v1.2 narrowing: included_in_overall)
        observations        — diagnostic-only dims (2 dims excluded
                              from overall in v1.2; values preserved
                              for gate/invariant/audit consumers)
        overall             — advisory weighted sum (canonical weights)
        confidence          — evaluation reliability (decomposed)
        confidence_profile  — dimension_coverage × evaluation_depth × format_reliability
        applicability       — per-dimension applicability status + reasons
        format_capability   — detected format capabilities
        metric_definitions  — formal metric identity cards
        issues              — per-dimension problems + anomaly risks
        metadata            — chunk statistics + fact_source +
                              weight_policy + dimension_breakdowns
    """

    # v1.2 change (scoring narrowing, decision date 2026-10-18):
    #   the scoring layer narrows to four "deterministic + parameter-improvable" dims (user rule:
    #   dead-value/untrusted-evidence dims stay out of scoring). Per the
    #   config_dimension_vitality_prereg_v1 vitality test:
    #   dependency_integrity is a constant dim (all 12 applicable files stuck at
    #   0.6, resolved items constantly zero), source_anomaly_handling semantically
    #   is "handling-evidence quality" (constantly 1.0 with no anomalies) — both are source-text/
    #   traceability signals, moved to the capability/observation layers (source text → DQR,
    #   traceability → capability_profile).
    #   - four dims renormalized proportionally: the v1.1 four dims total 0.80, each dim
    #     w/0.80 (mathematically equivalent to order preservation under no N/A redistribution)
    #   - both demoted dims keep issues (gate inputs unchanged) and keep invariant
    #     track inputs (invariants read scores back via observations; see
    #     config_invariants.py ReferenceTraceability)
    #   - prereg: config_scoring_narrowing_prereg_v1.md
    #
    # v1.3 change (path_integrity constant demotion, decision date 2026-10-18):
    #   user-authorized empirical weight review (144 samples, 8 families × 6 factors × 3 variants
    #   + 4 disambiguation-reachability scenarios) measured path_integrity constantly 1.0; negatives
    #   are structurally unconstructible under the contract + full-path representation architecture (top-level
    #   same-name override merging never hits the disambiguation branch; cross-parent same-name full-path
    #   is already naturally disambiguated) — demoted by the same v1.2 criterion (dead values leave the scoring layer):
    #   weight 0.2125 zeroed, values kept in observations (demotion
    #   not deletion; the PathAddressability invariant reads scores back via observations,
    #   track inputs unchanged). The three dims keep the v1.1 canonical ratios
    #   (0.30/0.13/0.20) renormalized proportionally.
    #   prereg: config_quality_v13_path_integrity_demotion_prereg_v1.md
    #
    # v1.4 change (E4 uniform baseline, decision date 2026-10-25):
    #   user-adjudicated methodology (uniform-baseline vs ordinal-scale, former chosen)
    #   — no prior on dimension importance; all three scoring weights are 1/3,
    #   **no longer preserving** the v1.1 canonical ratios (the 0.30/0.13/0.20
    #   ratio-preservation chain ends here; history stays as frozen at the time).
    #   stage-0 measured evidence: dimension admission criterion 3 (±50% weight perturbation
    #   Spearman ≥ 0.9978, ranking insensitive to weights) → uniformization
    #   does not hurt ranking discrimination. N/A dim cost goes from the original ratio weights
    #   (0.206~0.476) to a uniform 1/3.
    #   uniform lock: tests/test_scoring_sensitivity.py contract E
    #   (per-dim identity + sum≈1) — changing any weight turns tests red,
    #   requiring a prereg version bump. A second round (E4b) requires corpus evidence
    #   + user sign-off.
    #   prereg: uniform_baseline_calibration_prereg_v1.md
    WEIGHTS = {
        "hierarchy_continuity": 1 / 3,
        "locality_preservation": 1 / 3,
        "value_fidelity": 1 / 3,
    }

    # v1.1 change (relative to config_quality_v1):
    #   - contract config_entry_v1.1 adds inline-container derived-projection expansion
    #     (TOML; parent=source fact, child=derived projection,
    #     source_precision="line_only")
    #   - value_fidelity anti-double-counting: container-value entries stay out of correctness
    #     (their value fidelity is carried by scalar sub-items); derived scalar sub-items join
    #     coverage/correctness
    #   - weights unchanged (v1 frozen values), PROFILE_VERSION bumped explicitly
    #
    # v1.2 change: scoring layer narrowed to four dims + demoted dims moved to observations
    #   (see the WEIGHTS comment block above), PROFILE_VERSION bumped
    #
    # v1.3 change: path_integrity constant demotion (144 samples measured constantly 1.0,
    #   negatives structurally unconstructible; see the WEIGHTS comment block), three dims renormalized,
    #   PROFILE_VERSION bumped
    # v1.4 change: three-dim weights uniformized to 1/3 (E4 uniform baseline; see the WEIGHTS
    #   comment block), PROFILE_VERSION bumped
    PROFILE_VERSION = "config_quality_v1.4"

    # v1: all six dims participate in overall (including the diagnostic-role
    # source_anomaly_handling) — explicitly labeled in output to avoid
    # "diagnostic dim vs quality dim" semantic conflict; moving it out of the formal weight model is a separate v2
    # decision (downstream overall_basis consumers must be synced then).
    #
    # v1.2 (the formal landing of that "separate v2 decision" above):
    # dependency_integrity / source_anomaly_handling leave the scoring layer,
    # included_in_overall=False, values kept in observations.
    # v1.3: path_integrity demoted on measured constant evidence (constantly 1.0, negatives structurally
    # unconstructible), included_in_overall=False).
    INCLUDED_IN_OVERALL = {
        "hierarchy_continuity": True,
        "path_integrity": False,
        "locality_preservation": True,
        "value_fidelity": True,
        "dependency_integrity": False,
        "source_anomaly_handling": False,
    }

    # scoring layer (version positions = the WEIGHTS key set; three dims since v1.3)
    PRIMARY_DIMENSIONS = {
        "hierarchy_continuity",
        "locality_preservation",
        "value_fidelity",
    }

    # observation layer (v1.2 demoted dims: deterministic values kept,
    # out of overall; issues/gate and invariant track inputs unchanged.
    # v1.3: path_integrity joins — completeness/disambiguation
    # computation kept, for reads during pathological-representation regressions)
    SECONDARY_DIMENSIONS = {
        "path_integrity",
        "dependency_integrity",
        "source_anomaly_handling",
    }

    # v1.2 alias: semantic naming, pointing at the demoted-dim set above
    OBSERVATION_DIMENSIONS = SECONDARY_DIMENSIONS

    METRIC_DEFINITIONS = _METRIC_DEFINITIONS

    # ==================================
    # interpolation patterns (fallback detection only)
    # ==================================

    _INTERPOLATION_PATTERNS = [
        re.compile(r"\$\{(\w+(?:\.\w+)*)\}"),  # ${VAR} or ${db.host}
        re.compile(r"\{\{\s*([\w.]+)\s*\}\}"),  # {{ ref.path }}
        re.compile(r"\$\(([^)]+)\)"),  # $(command)
        re.compile(r"@\{([^}]+)\}"),  # @{expression}
    ]

    _DOC_PREFIX_RE = re.compile(r"^doc\d+\.")

    # severe parse/contract anomalies (unhandled risk events).
    # Explicit token list, NOT a broad regex: "recover"/"error"
    # catch-alls misfire on handled-outcome reasons (a successful
    # recovery is a HANDLED result, not a hard error). Convention:
    # severe loss reasons contain one of these tokens.
    _SEVERE_LOSS_TOKENS = (
        "parse_failed",      # contract_parse_failed / toml_parse_failed
        "build_failed",      # contract_build_failed
        "nesting_too_deep",  # subtree dropped (RecursionError)
    )

    @classmethod
    def _is_severe_loss(cls, reason: str) -> bool:
        """Whether a loss reason is an unhandled hard exception (data-loss grade)."""
        return any(tok in reason for tok in cls._SEVERE_LOSS_TOKENS)

    # ==================================
    # public API
    # ==================================

    def evaluate(
        self,
        document,
        semantic_representation,
        strategy=None,
        document_quality=None,
    ):
        """Evaluate config chunk quality.

        Args:
            document: Document with structured_data + config_entries
                contract in metadata
            semantic_representation: RepresentationResult with
                MultiGranularityOutput (or legacy flat unit list)
            strategy: RepresentationPolicy (unused for config, but
                kept for interface symmetry)
            document_quality: DocumentQualityReport result

        Returns:
            dict with profile, overall, confidence, issues, metadata,
            applicability, format_capability, metric_definitions
        """
        fine_units, base_units = self._extract_units(
            semantic_representation
        )
        if not base_units and not fine_units:
            return self._empty_result()

        # ==================================
        # Layer 1: fact view (contract first, fallback synthesized)
        # ==================================
        facts = self._build_fact_view(document, fine_units, base_units)
        if not facts.entries:
            return self._empty_result()

        # ==================================
        # Phase 0: Format Capability Profile detection
        # ==================================
        profile = self._detect_format_capabilities(
            document, facts, fine_units, base_units
        )

        # ==================================
        # Phase 1: Applicability check
        # (format capabilities + fact availability)
        # ==================================
        applicability = self._check_applicability(
            profile, facts,
            source_verifiable=self._source_verifiable(document),
        )

        # ==================================
        # Phase 2: Compute dimensions
        # (Layer 1 facts + Layer 2 unit shape; sub-check breakdowns)
        # ==================================
        breakdowns: Dict[str, Dict[str, Any]] = {}
        dimensions = {
            "hierarchy_continuity": self._hierarchy_continuity(
                document, facts, fine_units, base_units, profile,
                breakdowns,
            ),
            "path_integrity": self._path_integrity(
                facts, fine_units, base_units, profile, breakdowns,
            ),
            "locality_preservation": self._locality_preservation(
                facts, fine_units, profile
            ),
            "value_fidelity": self._value_fidelity(
                document, facts, profile, breakdowns
            ),
            "dependency_integrity": self._dependency_integrity(
                facts, fine_units, base_units, profile
            ),
            "source_anomaly_handling": self._source_anomaly_handling(
                facts, breakdowns
            ),
        }

        # ==================================
        # Phase 3: Canonical overall — N/A weights not redistributed
        # overall = Σ(active × w) / Σ(all w)
        # ==================================
        active_dims = {
            d for d in self.WEIGHTS
            if applicability[d]["applicable"]
        }
        total_weight = sum(self.WEIGHTS.values())
        if total_weight > 0:
            overall = (
                sum(
                    dimensions[d] * self.WEIGHTS[d]
                    for d in active_dims
                )
                / total_weight
            )
        else:
            overall = 0.0

        # ==================================
        # Phase 4: Confidence
        # ==================================
        confidence, confidence_profile = self._confidence(
            applicability, profile, document_quality, facts
        )

        # ==================================
        # Phase 5: Issues (score problems + anomaly risks)
        # ==================================
        issues = self._generate_issues(
            dimensions, applicability, profile, facts
        )

        # ==================================
        # Phase 6: Build output
        # ==================================
        stat_units = base_units or fine_units
        lengths = [len(u.text) for u in stat_units]

        # Build profile — N/A dimensions report null
        # v1.2: contains only the four scoring dims (the WEIGHTS key set)
        dim_profile = {}
        for dim in self.WEIGHTS:
            if applicability[dim]["applicable"]:
                dim_profile[dim] = round(dimensions[dim], 3)
            else:
                dim_profile[dim] = None

        # Build observations — v1.2 demoted dims: deterministic values kept,
        # out of overall; issues (gate inputs) and the invariant track
        # (QBI profile→observations fallback) inputs unchanged
        observation_values = {}
        for dim in self.SECONDARY_DIMENSIONS:
            if applicability[dim]["applicable"]:
                observation_values[dim] = round(dimensions[dim], 3)
            else:
                observation_values[dim] = None

        # Build metric definitions for external inspection
        metric_defs = {}
        for dim, defn in _METRIC_DEFINITIONS.items():
            metric_defs[dim] = {
                "purpose": defn.purpose,
                "theoretical_basis": defn.theoretical_basis,
                "limitations": defn.limitations,
                "role": defn.role,
                "weight": defn.weight,
                "classification": defn.classification,
                "sub_checks": list(defn.sub_checks),
                "included_in_overall": self.INCLUDED_IN_OVERALL[dim],
            }

        result = {
            "profile": dim_profile,
            "profile_version": self.PROFILE_VERSION,
            "observations": {
                **observation_values,
                "observation_policy": (
                    "diagnostic_only_not_in_overall"
                ),
            },
            "overall": round(overall, 3),
            "confidence": round(confidence, 3),
            "confidence_profile": confidence_profile,
            "issues": issues,
            "applicability": applicability,
            "format_capability": {
                "has_hierarchy": profile.has_hierarchy,
                "hierarchy_depth": profile.hierarchy_depth,
                "has_interpolation": profile.has_interpolation,
                "has_namespace_collision": (
                    profile.has_namespace_collision
                ),
                "representation_flattened": (
                    profile.representation_flattened
                ),
                "sibling_count": profile.sibling_count,
                "format_type": profile.format_type,
            },
            "metric_definitions": metric_defs,
            "metadata": {
                "chunk_count": len(stat_units),
                "avg_chunk_length": round(
                    sum(lengths) / max(len(lengths), 1), 2
                ),
                "min_chunk_length": min(lengths) if lengths else 0,
                "max_chunk_length": max(lengths) if lengths else 0,
                "format": profile.format_type,
                "sibling_groups": self._count_sibling_groups(facts),
                "reference_count": self._count_references(
                    facts, fine_units, base_units
                ),
                # two-layer reporting
                "fact_source": facts.source,
                "entry_count": len(facts.entries),
                "derived_entry_count": sum(
                    1 for e in facts.entries if e.derived
                ),
                "container_count": len(
                    {p for p in facts.container_paths if p}
                ),
                "fine_unit_count": len(fine_units),
                "base_unit_count": len(base_units),
                "weight_policy": "canonical_fixed",
                "active_dimension_count": len(active_dims),
                "dimension_breakdowns": breakdowns,
            },
        }
        return self._attach_invariant_track(
            result, fine_units or base_units, document
        )

    def _attach_invariant_track(self, result, units, document):
        """Attach the parallel invariant track (roadmap item 9).

        Dual-track positioning: the canonical
        profile/overall above is the PUBLIC track; the 4 config
        invariants (config_invariants.py) are a PARALLEL additive
        view exported as ``invariants`` / ``invariant_composite``.
        The domain quality result is pre-seeded into the invariant context so
        composition causes zero recomputation. If invariant
        evaluation fails for any reason, we silently degrade to
        invariant-free output — the invariant track must NEVER
        break the canonical evaluation path.
        """
        try:
            from provendoc.intelligence.rse.config_invariants import (
                create_config_registry,
            )
            registry = create_config_registry()
            context = {"evaluation_result::config": result}
            invariant_results = registry.evaluate_all(
                units, document, None, context=context,
            )
            result["invariants"] = {
                name: r.to_dict()
                for name, r in invariant_results.items()
            }
            result["invariant_composite"] = registry.composite_score(
                invariant_results, weighted=True,
            )
            result["invariant_track"] = "config_invariants_v1"
        # degrade to no invariant track when the invariant registry is not ready (enhancement observation, not a promise).
        except Exception:
            pass
        return result
