"""Code domain semantic invariants.

3 invariants that capture the semantic properties a code file's
representation MUST preserve — the code-domain counterpart of
narrative_invariants.py (roadmap item 9).

Code ingestion philosophy (from CodeQuality):

    code is an AST of structural units → chunks should preserve
    unit boundaries → each chunk is a complete structural unit
    (function / class / import block), never a fragment

Invariants (semantic questions, not measurement techniques):

    1. StructuralUnitCompleteness — is every structural unit fully covered and unfragmented?
    2. SymbolContinuity           — do symbol reference chains stay resolvable?
    3. ImportPreservation         — is import dependency context preserved?

==================================
Composition contract (anti-drift)
==================================

Code facts live in the code_structure_v1 contract (document
metadata["code"]: symbols with spans, structural units, parse
status) consumed by CodeQuality. These invariants COMPOSE CodeQuality
dimension scores via QualityBackedInvariant — the evaluator runs once
per pass (context-cached) and each invariant re-projects its
source dimensions into one semantic assertion. Facts are never
re-derived here.

INVARIANT_TO_DIMENSIONS_MAP is the machine-readable composition
declaration — the code-domain analogue of narrative's
INVARIANT_TO_PROFILE_MAP.

==================================
Dual-Track Positioning
==================================

CodeQuality's 5 canonical dimensions are the PUBLIC track. These
invariants are a PARALLEL additive view with their own weight
system, exported as invariants / invariant_composite on the evaluation
result. N/A dimensions are excluded from composition (canonical
weight policy) — a file without class-like containers cannot
inflate its structural completeness through not-applicable dims.

==================================
Weight design
==================================

    structural_unit_completeness: 0.50  (a split function is lost context)
    symbol_continuity:           0.30  (references are the retrieval fabric)
    import_preservation:         0.20  (imports anchor external context)
"""

from provendoc.intelligence.rse.code_quality import CodeQuality
from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    QualityBackedInvariant,
)

INVARIANT_TO_DIMENSIONS_MAP = {
    "structural_unit_completeness": {
        "source_dimensions": [
            "ast_integrity", "function_completeness",
            "class_boundary_correctness",
        ],
        "mapping": "sub-weighted mean (0.4 / 0.3 / 0.3)",
        "semantic_question": (
            "Is every function/class/struct-like unit spanned by "
            "exactly one complete chunk — never fragmented, never "
            "lost, never duplicated across chunks?"
        ),
        "invariant_weight": 0.50,
    },
    "symbol_continuity": {
        "source_dimensions": ["symbol_continuity"],
        "mapping": "direct",
        "semantic_question": (
            "Do statically observable references remain resolvable "
            "within the represented units (with dynamic/external "
            "targets explicitly marked, never guessed)?"
        ),
        "invariant_weight": 0.30,
    },
    "import_preservation": {
        "source_dimensions": ["import_dependency_preservation"],
        "mapping": "direct",
        "semantic_question": (
            "Are import/use/include statements retained so each "
            "chunk keeps its external dependency context?"
        ),
        "invariant_weight": 0.20,
    },
}

DUAL_TRACK_NOTE = (
    "Code quality evaluation is dual-track: the 5 canonical dimensions "
    "(code_structure_v1 facts) produce the public profile/overall; "
    "these 3 invariants are a parallel additive view composed from "
    "the same fact layer. invariant_composite is never a drop-in "
    "replacement for overall."
)


class StructuralUnitCompleteness(QualityBackedInvariant):
    """Every structural unit is contained by exactly one complete chunk.

    Semantic question: "Given a function or class span in the
    source, is there exactly one chunk that fully contains it?"

    A unit split across two chunks loses its boundary identity
    (retrieval returns fragments); a unit contained by zero chunks
    is silently lost; a unit spanned by two overlapping chunks
    duplicates content. All three break the 'chunk = complete
    structural unit' contract.

    Composes ast_integrity (parse status baseline — recovered
    parses are usable but reduce certainty), function_completeness
    and class_boundary_correctness (span coverage over exactly-one
    container).
    """

    domain = "code"
    quality_factory = CodeQuality
    dimension_weights = {
        "ast_integrity": 0.4,
        "function_completeness": 0.3,
        "class_boundary_correctness": 0.3,
    }

    @property
    def name(self) -> str:
        """Invariant name."""
        return "structural_unit_completeness"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Every function/method/class-like structural unit is "
            "contained by exactly one complete chunk — units are "
            "neither fragmented across chunks nor lost."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "AST-based code chunking (cAST, 2022): structural "
            "boundaries are the natural chunk boundaries; "
            "fragmenting a unit breaks both syntax and semantics."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.50


class SymbolContinuity(QualityBackedInvariant):
    """Statically observable references stay resolvable.

    Semantic question: "For every call/reference/extends edge the
    parser observed, is the referencing span retained in a chunk,
    and does its target resolve to a known symbol (or carry an
    explicit dynamic/external marker)?"

    References are the retrieval fabric of code: a question about
    'who calls X' is answerable only if reference edges survive.
    Dynamic and external targets are quality-neutral — the
    invariant never invents resolution.
    """

    domain = "code"
    quality_factory = CodeQuality
    dimension_weights = {"symbol_continuity": 1.0}

    @property
    def name(self) -> str:
        """Invariant name."""
        return "symbol_continuity"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Reference relationships (call/reference/extends/"
            "implements/mixin) remain represented and resolvable; "
            "dynamic/external targets are explicitly marked rather "
            "than guessed."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Program dependence graphs (Ferrante et al., 1987): "
            "reference edges carry the semantic relations between "
            "definitions — losing edges severs the graph even when "
            "nodes survive."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.30


class ImportPreservation(QualityBackedInvariant):
    """Import statements retain their external dependency context.

    Semantic question: "Is every import/use/include statement from
    the source retained inside some chunk?"

    An import is the chunk's only link to external semantics; a
    function chunk whose imports were dropped is syntactically
    complete but semantically ambiguous (which ``json`` module?
    which alias?). Imports are cheap to keep and expensive to lose.
    """

    domain = "code"
    quality_factory = CodeQuality
    dimension_weights = {"import_dependency_preservation": 1.0}

    @property
    def name(self) -> str:
        """Invariant name."""
        return "import_preservation"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Import/use/include statements are retained in the "
            "representation so each structural chunk keeps its "
            "external dependency context."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical rationale."""
        return (
            "Module systems: imports bind local names to external "
            "definitions (name resolution scoping); removing them "
            "breaks name resolution for every retained reference."
        )

    @property
    def weight(self) -> float:
        """Mounted weight."""
        return 0.20


def create_code_registry() -> InvariantRegistry:
    """Create and populate the code domain invariant registry."""
    registry = InvariantRegistry("code")
    registry.register(StructuralUnitCompleteness())
    registry.register(SymbolContinuity())
    registry.register(ImportPreservation())
    return registry
