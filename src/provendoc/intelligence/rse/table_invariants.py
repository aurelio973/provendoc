"""Table domain semantic invariants.

3 invariants that capture the semantic properties a spreadsheet
document's representation MUST preserve — the table-domain
counterpart of narrative_invariants.py (roadmap item 9).

Table ingestion philosophy (from TableQuality):

    table is a schema-bound row collection → chunks should
    preserve row integrity and schema → each chunk is a complete,
    self-describing row group

Invariants (semantic questions, not measurement techniques):

    1. RowAtomicity     — is any row ever split across two chunks?
    2. SchemaStability  — can header bindings be addressed from every row group, is region coverage complete?
    3. CellFidelity     — can every rendered cell be traced back to the canvas source,
                          with the correct row-group region/sheet locality?

==================================
Composition contract
==================================

Table facts live in the spreadsheet_v1 contract consumed by
TableQuality. These invariants COMPOSE TableQuality dimension scores via
QualityBackedInvariant — facts are never re-derived here.

TableQuality's dimension logic is real since table_quality_v1.1 (the old
TODO-skeleton placeholder scores are retired). SKELETON_MODE is
kept as False for historical/monitoring purposes; the confidence
cap only applies while it is True.

INVARIANT_TO_DIMENSIONS_MAP is the machine-readable composition
declaration — the table-domain analogue of narrative's
INVARIANT_TO_PROFILE_MAP.

==================================
Dual-Track Positioning
==================================

TableQuality's canonical profile (table_quality_v2.0 — three
composite dimensions, sub-check scores in the observations
diagnostic layer) is the PUBLIC track. These invariants are a
PARALLEL additive view, exported as invariants / invariant_composite
on the evaluation result.

==================================
Weight design
==================================

    row_atomicity:    0.40  (a split row is a corrupt record)
    schema_stability: 0.35  (schema is what makes rows readable)
    cell_fidelity:    0.25  (cell traceability / locality are
                             spreadsheet-specific recovery)
"""

from provendoc.intelligence.rse.table_quality import TableQuality
from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    QualityBackedInvariant,
)

#: Historical flag from the skeleton era: while TableQuality dimension
#: logic was TODO stubs, invariant confidence was capped at 0.5 so
#: placeholder scores could not be mistaken for verified
#: guarantees. Since table_quality_v1.1 the dimensions are real —
#: the flag stays False and the cap is inactive.
SKELETON_MODE = False

_SKELETON_CONFIDENCE_CAP = 0.5

INVARIANT_TO_DIMENSIONS_MAP = {
    "row_atomicity": {
        "source_dimensions": ["row_integrity"],
        "mapping": "direct",
        "semantic_question": (
            "Is every row contained by exactly one chunk — rows are "
            "atomic records, never split across chunk boundaries?"
        ),
        "invariant_weight": 0.40,
    },
    "schema_stability": {
        "source_dimensions": [
            "header_binding", "table_integrity",
        ],
        "mapping": "sub-weighted mean (0.6 / 0.4)",
        "semantic_question": (
            "Is the header binding (column → name) preserved and "
            "addressable from every row group, with each table "
            "region's data rows fully covered by base units?"
        ),
        "invariant_weight": 0.35,
    },
    "cell_fidelity": {
        "source_dimensions": [
            "cell_source_fidelity", "region_locality",
        ],
        "mapping": "sub-weighted mean (0.6 / 0.4)",
        "semantic_question": (
            "Is every rendered cell traceable back to its canvas "
            "source cell (no dropped / duplicated cells), and do "
            "row groups carry the correct region/sheet locality?"
        ),
        "invariant_weight": 0.25,
    },
}

DUAL_TRACK_NOTE = (
    "Table quality evaluation is dual-track: the canonical profile "
    "(table_quality_v2.0) produces the public profile/overall; these "
    "3 invariants are a parallel additive view composed from the "
    "same fact layer (spreadsheet_v1 contract). Dimension logic "
    "is real since table_quality_v1.1 — invariant confidence is not "
    "capped."
)


class RowAtomicity(QualityBackedInvariant):
    """Rows are atomic records — never split across chunks.

    Semantic question: "Does any chunk boundary fall inside a row?"

    A row is the minimal self-describing unit of a table: splitting
    one across chunks corrupts the record (half the columns in one
    chunk, half in another). This is the tabular analogue of
    narrative's 'no chunk breaks a code fence' and config's 'no
    chunk breaks a path'.

    Composed from: row_integrity (row-level units map 1:1 to the
    source data rows of each table region).
    """

    domain = "table"
    quality_factory = TableQuality
    dimension_weights = {"row_integrity": 1.0}
    confidence_cap = _SKELETON_CONFIDENCE_CAP if SKELETON_MODE else None

    @property
    def name(self) -> str:
        """The invariant name."""
        return "row_atomicity"

    @property
    def description(self) -> str:
        """The invariant's semantic description."""
        return (
            "Every table row is contained by exactly one chunk — "
            "row records are atomic and never fragmented across "
            "chunk boundaries."
        )

    @property
    def theoretical_basis(self) -> str:
        """The theoretical basis."""
        return (
            "Relational model (Codd, 1970): a tuple is the atomic "
            "unit of meaning; partial tuples are not smaller "
            "records but corrupted ones."
        )

    @property
    def weight(self) -> float:
        """The mount weight."""
        return 0.40


class SchemaStability(QualityBackedInvariant):
    """Headers stay addressable; region coverage stays complete.

    Semantic question: "Can each row group resolve its columns
    against the contract header binding, and does every table
    region's data get fully covered by base units?"

    Schema is what makes rows interpretable: a row group whose
    header binding diverges from the contract is a parsing
    artifact, not a record. Complete region coverage means no
    table silently loses rows.

    Composed from: header_binding (0.6) + table_integrity (0.4).
    """

    domain = "table"
    quality_factory = TableQuality
    dimension_weights = {
        "header_binding": 0.6,
        "table_integrity": 0.4,
    }
    confidence_cap = _SKELETON_CONFIDENCE_CAP if SKELETON_MODE else None

    @property
    def name(self) -> str:
        """The invariant name."""
        return "schema_stability"

    @property
    def description(self) -> str:
        """The invariant's semantic description."""
        return (
            "The header binding (column → name) is preserved and "
            "addressable from every row group, and every table "
            "region's data rows are fully covered by base units."
        )

    @property
    def theoretical_basis(self) -> str:
        """The theoretical basis."""
        return (
            "Schema-on-read consistency: tabular semantics are "
            "defined by the header schema (names as addresses, "
            "dtypes as value domains); schema drift across chunks "
            "breaks record interpretation."
        )

    @property
    def weight(self) -> float:
        """The mount weight."""
        return 0.35


class CellFidelity(QualityBackedInvariant):
    """Cells stay traceable; row groups keep their locality.

    Semantic question: "Is every rendered cell traceable back to
    its canvas source cell, and does each row group carry the
    correct region/sheet locality?"

    Cell traceability (cell_ids resolve to non-blank canvas data
    cells exactly once) is the spreadsheet's fidelity contract:
    a rendered value with no source cell is a hallucination, a
    source cell rendered twice is a duplication. Region/sheet
    locality keeps related records discoverable together.

    Composed from: cell_source_fidelity (0.6) + region_locality (0.4).
    """

    domain = "table"
    quality_factory = TableQuality
    dimension_weights = {
        "cell_source_fidelity": 0.6,
        "region_locality": 0.4,
    }
    confidence_cap = _SKELETON_CONFIDENCE_CAP if SKELETON_MODE else None

    @property
    def name(self) -> str:
        """The invariant name."""
        return "cell_fidelity"

    @property
    def description(self) -> str:
        """The invariant's semantic description."""
        return (
            "Every rendered cell is traceable back to its canvas "
            "source cell (no dropped or duplicated cells), and row "
            "groups carry the correct region/sheet locality."
        )

    @property
    def theoretical_basis(self) -> str:
        """The theoretical basis."""
        return (
            "Layout normalization: presentation formats compress "
            "semantics into layout (merged cells carry one value "
            "for many positions); recovery means materializing the "
            "compressed semantics at every position — and being "
            "able to prove where each rendered value came from."
        )

    @property
    def weight(self) -> float:
        """The mount weight."""
        return 0.25


def create_table_registry() -> InvariantRegistry:
    """Create and populate the table domain invariant registry."""
    registry = InvariantRegistry("table")
    registry.register(RowAtomicity())
    registry.register(SchemaStability())
    registry.register(CellFidelity())
    return registry
