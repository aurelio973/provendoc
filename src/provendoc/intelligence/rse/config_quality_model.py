"""Model layer of ConfigQuality — data classes and the metric registry.

Contents (split from config_quality.py in 2026-09, verbatim moves):
- FormatCapabilityProfile   format-capability profile (Phase 0 detection product)
- _EntryFact / _FactView    fact-layer contract views (Layer 1)
- MetricDefinition + _METRIC_DEFINITIONS   formal definitions of the six canonical dimensions

Design constitution and overall definitions: see the provendoc.intelligence.rse.config_quality
module docstring; this file introduces no behavior change.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ==================================
# Applicability System
# ==================================
#
# Formal declarative system for metric activation conditions.
# Each dimension declares when it is applicable based on the
# document's FormatCapabilityProfile (+ fact availability).
#
# An inapplicable metric is NOT "low quality" — it is structurally
# meaningless. A metric without evidence (e.g. value checks
# without a contract) is N/A with a confidence penalty.
# ==================================


@dataclass
class FormatCapabilityProfile:
    """Detected format capabilities — determines which quality dimensions apply.

    Extracted from document.metadata + contract facts BEFORE scoring.
    """
    has_hierarchy: bool = False
    """Source format supports nesting. JSON/YAML/TOML=True, ENV/flat INI=False."""

    hierarchy_depth: int = 0
    """Actual nesting depth. has_hierarchy=True but depth=1 is weak signal."""

    has_interpolation: bool = False
    """Contains variable references (${VAR}, {{ ref }}, env interpolation)."""

    has_namespace_collision: bool = False
    """Multiple parents share same leaf key name
    (e.g. redis.host + mysql.host)."""

    representation_flattened: bool = False
    """Representation flattens tree into flat paths.
    ConfigRepresentation=True (every entry carries a full path)."""

    sibling_count: int = 0
    """Number of sibling key groups with >= 2 members."""

    format_type: str = "unknown"
    """Detected format: json, yaml, toml, ini, env, etc."""


# ==================================
# Fact Layer — contract-backed facts
# ==================================


@dataclass
class _EntryFact:
    """Evaluation facts for a single entry (contract first, representation fallback synthesis)."""
    path_str: str
    value: Any
    masked: bool
    duplicate: bool
    override_chain: Optional[List[Dict[str, Any]]]
    references: List[Dict[str, Any]]
    doc_index: int = 0
    derived: bool = False
    """v1.1: 派生投影 entry（source_precision="line_only"）。

    值与行号投影自父 entry（TOML 内联容器展开）。
    """


@dataclass
class _FactView:
    """Evaluation-facts view: entries/containers/refs synthesized from contract or representation."""
    source: str  # "config_entry_v1" | "representation_fallback"
    entries: List[_EntryFact] = field(default_factory=list)
    containers: List[Dict[str, Any]] = field(default_factory=list)
    container_paths: Set[str] = field(default_factory=set)
    refs: List[Dict[str, Any]] = field(default_factory=list)
    duplicate_key_semantics: Optional[str] = None
    loss_reasons: List[str] = field(default_factory=list)


# ==================================
# Metric Definition Registry
# ==================================


@dataclass
class MetricDefinition:
    """Formal definition of a ConfigQuality metric dimension."""
    name: str
    purpose: str
    theoretical_basis: str
    limitations: str
    role: str  # "canonical", "operational", "diagnostic"
    weight: float
    classification: str  # "primary" or "secondary"
    sub_checks: Tuple[str, ...] = ()


_METRIC_DEFINITIONS = {
    "hierarchy_continuity": MetricDefinition(
        name="hierarchy_continuity",
        purpose="Is the config tree connected in both directions "
                "after flattening — every entry's ancestors exist, "
                "every empty container is retained, and depth is "
                "preserved?",
        theoretical_basis=(
            "Bhattacharya et al. (2013) Automated Reasoning about "
            "Config — hierarchical structure integrity is a necessary "
            "condition for config understanding. Rabkin & Katz (2011) "
            "HotCloud — config dependencies rely on correct hierarchy. "
            "Merged from hierarchy_preservation + nesting_integrity "
            "(0.18 + 0.10): both probed tree connectivity; a single "
            "fault double-penalized and attribution was blurred."
        ),
        limitations=(
            "Connectivity checks parser-recorded containers (contract "
            "path); in the fallback path ancestors are synthesized "
            "from entry prefixes and the check degenerates to "
            "trivially true. Container retention only verifies marker "
            "presence, not marker correctness."
        ),
        role="canonical",
        weight=0.30,
        classification="primary",
        sub_checks=(
            "connectivity", "container_retention", "depth_preservation",
        ),
    ),
    "path_integrity": MetricDefinition(
        name="path_integrity",
        purpose="Does every unit carry a complete, non-empty semantic "
                "path — and do colliding leaf names carry enough "
                "namespace context to disambiguate?",
        theoretical_basis=(
            "Chen & Jiang (2017) Config Cognition — semantic path "
            "completeness is the foundation of config understanding. "
            "Xu et al. (2015) — namespace ambiguity causes retrieval "
            "failures when flattened representations lose context. "
            "Tang et al. (2015) — semantic path stability depends on "
            "namespace preservation. Merged from "
            "semantic_path_completeness + namespace_disambiguation: "
            "both are properties of the path itself."
        ),
        limitations=(
            "Completeness checks non-empty paths only; correctness is "
            "verified by value_fidelity. Disambiguation is a basic "
            "segment-count check (colliding leaves need >= 2 segments)."
        ),
        role="canonical",
        weight=0.17,
        classification="primary",
        sub_checks=("completeness", "disambiguation"),
    ),
    "locality_preservation": MetricDefinition(
        name="locality_preservation",
        purpose="Are sibling keys (same parent path) kept adjacent "
                "in the fine-grained unit sequence?",
        theoretical_basis=(
            "Hesselbo et al. (2020) Program Slicing for Configurations "
            "— scope locality directly affects maintainability and "
            "retrieval quality. Sibling keys separated by unrelated "
            "content degrade semantic retrieval."
        ),
        limitations=(
            "Measures fine-unit index adjacency, not physical text "
            "adjacency. A base unit may contain multiple sibling keys "
            "without penalty. Comment binding and section locality "
            "are v2 sub-check candidates (not yet measured)."
        ),
        role="canonical",
        weight=0.13,
        classification="primary",
        sub_checks=("sibling_adjacency",),
    ),
    "value_fidelity": MetricDefinition(
        name="value_fidelity",
        purpose="Does every scalar value round-trip through its "
                "semantic path — coverage (source→entry) and "
                "correctness (entry→source) of the path→value "
                "bijection?",
        theoretical_basis=(
            "Cito et al. (2019) Using Production Data for Testing "
            "— information fidelity under flattening operations; "
            "round-trip identity test. Lewis et al. (2020) RAG — "
            "chunk integrity for retrieval. Merged from "
            "flattening_stability + scalar_extraction_correctness: "
            "the recall/precision directions of one bijection."
        ),
        limitations=(
            "Checked from structured entry facts (contract value "
            "fields) — chunk.text is NEVER re-parsed. Without a "
            "contract the dimension is N/A (no evidence); a source "
            "degraded to a parse-error marker is also N/A "
            "(unverifiable source, failure surfaced as issue). "
            "Multidoc sources (structured_data as a list of docs) "
            "are matched via dual keys (plain + doc-indexed). "
            "Masked (sensitive) entries are exempt on both sides. "
            "String comparison only — no type coercion. v1.1: "
            "container-valued entries (TOML inline-container "
            "parents and derived intermediates) are excluded from "
            "correctness to avoid double counting — their value "
            "fidelity is carried by scalar children (derived "
            "projections, source_precision=line_only, included "
            "in both coverage and correctness)."
        ),
        role="canonical",
        weight=0.20,
        classification="primary",
        sub_checks=("coverage", "correctness"),
    ),
    "dependency_integrity": MetricDefinition(
        name="dependency_integrity",
        purpose="Are cross-references (interpolation, template refs) "
                "detected, traced, and resolvable?",
        theoretical_basis=(
            "Schwarz et al. (2021) Detecting Interpolation "
            "Vulnerabilities — three-level assessment: detection → "
            "traceability → resolvability. Rabkin & Katz (2011) — "
            "config dependencies are first-class citizens."
        ),
        limitations=(
            "Contract references carry a five-state resolution "
            "status; only 'resolved' counts toward level 3. "
            "External/dynamic references are traceable but not "
            "resolvable within the document."
        ),
        role="canonical",
        weight=0.15,
        classification="secondary",
        sub_checks=("detection", "traceability", "resolvability"),
    ),
    "source_anomaly_handling": MetricDefinition(
        name="source_anomaly_handling",
        purpose="Are source anomalies (duplicate keys, override "
                "chains, parse conflicts) handled with recorded "
                "evidence? Score = evidence quality; issues expose "
                "the risk itself.",
        theoretical_basis=(
            "Sayagh et al. (2017) Config Smells — duplicate keys "
            "are a configuration anti-pattern. Xu et al. (2015) "
            "How Not to Lie in Config Files — duplicates cause "
            "silent override bugs. Score-vs-issue split: the score "
            "carries overall quality, issues carry risk exposure."
        ),
        limitations=(
            "Score measures HANDLING evidence (override_chain "
            "present, no severe loss reasons) — NOT anomaly "
            "presence. Presence of duplicates always generates an "
            "issue (warning when evidenced, error when not). "
            "Semantic duplicates (db_host vs database.host) are "
            "not detected. Included in overall in v1 with an "
            "explicit included_in_overall=true marker; moving it "
            "out of the formal weight model is a v2 decision "
            "(risk exposure would be carried by issues only)."
        ),
        role="diagnostic",
        weight=0.05,
        classification="secondary",
        sub_checks=("duplicate_evidence", "loss_reasons"),
    ),
}
