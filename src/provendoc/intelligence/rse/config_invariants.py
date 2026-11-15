"""Config domain semantic invariants.

4 invariants that capture the semantic properties a config file's
representation MUST preserve — the config-domain counterpart of
narrative_invariants.py (roadmap item 9).

Config ingestion philosophy (from ConfigQuality):

    config is a key-value tree → chunks should preserve
    every path → each chunk is one addressable leaf or subtree

Invariants (semantic questions, not measurement techniques):

    1. TreeConnectivity      — is the config tree bidirectionally connected with siblings kept adjacent?
    2. PathAddressability    — does every key have a unique, complete, addressable path?
    3. ValueRoundTrip        — does every path→value bijection round-trip losslessly?
    4. ReferenceTraceability — are cross-references traceable, with anomalies exposed as explicit risks?

==================================
Composition contract (anti-drift)
==================================

Unlike narrative invariants (which compute from chunk text via
shared primitives), config facts live in the config_entry_v1.1
contract consumed by ConfigQuality. These invariants therefore COMPOSE
ConfigQuality dimension scores via QualityBackedInvariant — the quality
evaluator runs once per pass (context-cached) and each invariant
re-projects its source dimensions into one semantic assertion.
Facts are never re-derived here; primitive-level drift is
structurally impossible.

    INVARIANT_TO_DIMENSIONS_MAP (below) is the machine-readable
    declaration of this composition — the config-domain analogue
    of narrative's INVARIANT_TO_PROFILE_MAP.

==================================
Dual-Track Positioning
==================================

ConfigQuality's 6-dimension profile/overall (frozen weights
config_quality_v1) is the PUBLIC canonical track. These invariants
are a PARALLEL additive view with their own weight system,
exported as invariants / invariant_composite on the evaluation result
(never merged into profile/overall). N/A dimensions are excluded
from invariant composition, mirroring the canonical weight
policy — no score inflation via not-applicable dimensions.

==================================
Weight design
==================================

    tree_connectivity:       0.35  (a broken tree breaks addressing)
    path_addressability:     0.25  (paths are the retrieval key)
    value_round_trip:        0.25  (wrong values are silent failures)
    reference_traceability:  0.15  (references/anomalies are risk, not core recall)
"""

from provendoc.intelligence.rse.config_quality import ConfigQuality
from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    QualityBackedInvariant,
)

INVARIANT_TO_DIMENSIONS_MAP = {
    "tree_connectivity": {
        "source_dimensions": [
            "hierarchy_continuity", "locality_preservation",
        ],
        "mapping": "sub-weighted mean (0.65 / 0.35)",
        "semantic_question": (
            "Is the config tree connected both ways (child→parent, "
            "parent→child) and are sibling keys kept adjacent?"
        ),
        "invariant_weight": 0.35,
    },
    "path_addressability": {
        "source_dimensions": ["path_integrity"],
        "mapping": "direct",
        "semantic_question": (
            "Does every key carry a full, unambiguous path (colliding "
            "names disambiguated by namespace context)?"
        ),
        "invariant_weight": 0.25,
    },
    "value_round_trip": {
        "source_dimensions": ["value_fidelity"],
        "mapping": "direct",
        "semantic_question": (
            "Does every path→value pair from the contract survive "
            "into the representation without loss or invention?"
        ),
        "invariant_weight": 0.25,
    },
    "reference_traceability": {
        "source_dimensions": [
            "dependency_integrity", "source_anomaly_handling",
        ],
        "mapping": "sub-weighted mean (0.6 / 0.4)",
        "semantic_question": (
            "Are cross-references traceable (five-state resolution), "
            "and are source anomalies (duplicates, override chains) "
            "exposed as explicit risk rather than silently dropped?"
        ),
        "invariant_weight": 0.15,
    },
}

DUAL_TRACK_NOTE = (
    "Config quality evaluation is dual-track: the 6 canonical dimensions "
    "(config_quality_v1 frozen weights) produce the public profile/"
    "overall; these 4 invariants are a parallel additive view "
    "composed from the same fact layer. invariant_composite is "
    "never a drop-in replacement for overall."
)


class TreeConnectivity(QualityBackedInvariant):
    """The config tree is bidirectionally connected; siblings stay adjacent.

    Semantic question: "Can every represented fact be walked back to
    the root, and does every retained container still expose its
    children in source order?"

    Composes hierarchy_continuity (child→parent connectivity,
    parent→child empty-container retention, depth preservation) with
    locality_preservation (sibling adjacency in the fine-grained
    unit sequence). A chunking that rips a subtree from its parent
    or interleaves sibling keys breaks addressing even if every
    individual entry survives.
    """

    domain = "config"
    quality_factory = ConfigQuality
    dimension_weights = {
        "hierarchy_continuity": 0.65,
        "locality_preservation": 0.35,
    }

    @property
    def name(self) -> str:
        """Invariant name."""
        return "tree_connectivity"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "The config key-value tree stays connected in both "
            "directions (every child reaches its parent path; every "
            "parent retains its children) and sibling keys remain "
            "adjacent in the representation sequence."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Tree data structures: connectivity is the minimal "
            "condition for path addressing. Locality: B-trees and "
            "document stores keep siblings contiguous for range "
            "reads — interleaved siblings defeat both."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.35


class PathAddressability(QualityBackedInvariant):
    """Every key is fully pathed and unambiguous.

    Semantic question: "Given any represented entry, is its full
    path from the root recoverable — and do colliding leaf names
    carry namespace context so two 'port' keys never look alike?"

    This is the retrieval-critical invariant: config retrieval is
    path-addressed. An entry whose path is truncated or ambiguous
    is effectively lost even when its value survives.
    """

    domain = "config"
    quality_factory = ConfigQuality
    dimension_weights = {"path_integrity": 1.0}

    @property
    def name(self) -> str:
        """Invariant name."""
        return "path_addressability"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Every represented config entry carries a complete, "
            "unambiguous path from the root; colliding leaf names "
            "are disambiguated by namespace context."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Path addressing in hierarchical stores (XPath, JSON "
            "Pointer RFC 6901): addressability requires unique, "
            "complete paths — ambiguity is information loss."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.25


class ValueRoundTrip(QualityBackedInvariant):
    """Every path→value pair round-trips losslessly.

    Semantic question: "For every entry in the config_entry_v1.1
    contract, does the representation carry exactly the same value
    — no loss (recall) and no invention (precision)?"

    Composes the value_fidelity dimension (the two directions of
    the path→value bijection). Without a contract this invariant is
    N/A (no evidence), not degraded — honesty over optimism.
    """

    domain = "config"
    quality_factory = ConfigQuality
    dimension_weights = {"value_fidelity": 1.0}

    @property
    def name(self) -> str:
        """Invariant name."""
        return "value_round_trip"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Every path→value fact from the source contract "
            "survives into the representation unchanged — values "
            "are neither lost nor invented."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Round-trip property testing: serialization is correct "
            "iff decode(encode(x)) == x for the path→value "
            "bijection. Bijection = recall AND precision."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.25


class ReferenceTraceability(QualityBackedInvariant):
    """References resolve or explain themselves; anomalies surface as risk.

    Semantic question: "Is every cross-reference traceable through
    the five-state resolution model, and are duplicate keys /
    override chains exposed as explicit issues rather than silently
    resolved away?"

    Composes dependency_integrity with source_anomaly_handling.
    The anomaly sub-signal measures handling-EVIDENCE quality — the
    invariant does NOT penalize a document for having duplicates;
    it penalizes a representation for hiding them.
    """

    domain = "config"
    quality_factory = ConfigQuality
    dimension_weights = {
        "dependency_integrity": 0.6,
        "source_anomaly_handling": 0.4,
    }

    @property
    def name(self) -> str:
        """Invariant name."""
        return "reference_traceability"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Cross-references remain traceable (resolved/unresolved/"
            "cyclic/external/dynamic five-state model) and source "
            "anomalies are surfaced as explicit risk, never silently "
            "absorbed."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Reference integrity in dependency graphs: dangling "
            "references must be observable, not hidden. Risk "
            "exposure principle: anomalies carry information — "
            "silently resolving them destroys it."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.15


def create_config_registry() -> InvariantRegistry:
    """Create and populate the config domain invariant registry."""
    registry = InvariantRegistry("config")
    registry.register(TreeConnectivity())
    registry.register(PathAddressability())
    registry.register(ValueRoundTrip())
    registry.register(ReferenceTraceability())
    return registry
