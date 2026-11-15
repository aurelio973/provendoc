"""Event domain semantic invariants.

3 invariants that capture the semantic properties a record-stream
document's representation MUST preserve — the event-domain
counterpart of narrative_invariants.py / table_invariants.py /
code_invariants.py (roadmap item 9, record_stream closure).

Event ingestion philosophy (from EventQuality):

    log is an append-only event sequence → chunks should preserve
    event locality → multiline events (stacktraces, continuations)
    must stay together → correlated events (same request_id /
    session) should be co-located

Invariants (semantic questions, not measurement techniques):

    1. EventAtomicity        — is any event split across two chunks?
    2. TracebackIntegrity    — are traceback events complete, not truncated?
    3. CorrelationLocality   — do correlated events of the same session/request
                               share one chunk?

==================================
Composition contract
==================================

Event facts live in the log events / jsonl records contracts
consumed by EventQuality (metadata["events"] /
metadata["records"]). These invariants COMPOSE EventQuality
dimension scores via QualityBackedInvariant — facts are never
re-derived here (the historical drift lesson of narrative, see invariant.py).

INVARIANT_TO_DIMENSIONS_MAP is the machine-readable composition
declaration — the event-domain analogue of table/code
INVARIANT_TO_DIMENSIONS_MAP.

==================================
Dual-Track Positioning
==================================

EventQuality's canonical profile (event_quality_v2.0 — two
composite dimensions, sub-check scores in the observations
diagnostic layer) is the PUBLIC track. These invariants are a
PARALLEL additive view,
exported as invariants / invariant_composite on the evaluation
result (track version event_invariants_v1).

Sub-check coverage is 4/6 BY DESIGN (event_invariant_track_
prereg_v1 §5): timestamp_continuity is deliberately excluded
(session/record_type grouping trades temporal order for
correlation locality — EventQuality scores within-chunk
monotonicity and reports cross-chunk inversions as advisory
only); parse_success_rate is a source-document property, not a
chunking invariant. Do not backfill these without a prereg bump.

==================================
Weight design
==================================

    event_atomicity:       0.50  (a split event is a corrupt
                                  record — same rationale as
                                  table row_atomicity)
    traceback_integrity:   0.30  (a truncated stacktrace is the
                                  single most expensive event
                                  loss for incident analysis)
    correlation_locality:  0.20  (correlated events stay
                                  discoverable together)
"""

from provendoc.intelligence.rse.event_quality import EventQuality
from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    QualityBackedInvariant,
)

#: EventQuality's dimension logic has been fully real since event_quality_v1.0
#: (the skeleton era has retired) — there is no skeleton confidence cap.
SKELETON_MODE = False

_INVARIANT_TRACK_VERSION = "event_invariants_v1"

INVARIANT_TO_DIMENSIONS_MAP = {
    "event_atomicity": {
        "source_dimensions": [
            "multiline_event_integrity",
            "event_boundary_correctness",
        ],
        "mapping": "sub-weighted mean (0.5 / 0.5)",
        "semantic_question": (
            "Is every event (single-line or multiline with "
            "continuations) contained by exactly one chunk — "
            "events are atomic records, never split across chunk "
            "boundaries?"
        ),
        "invariant_weight": 0.50,
    },
    "traceback_integrity": {
        "source_dimensions": ["stacktrace_completeness"],
        "mapping": "direct",
        "semantic_question": (
            "Is every traceback event complete and untruncated — "
            "the representation's traceback isolation never cuts "
            "a stacktrace from its frames?"
        ),
        "invariant_weight": 0.30,
    },
    "correlation_locality": {
        "source_dimensions": ["request_session_grouping"],
        "mapping": "direct",
        "semantic_question": (
            "Do events sharing a session/request identifier stay "
            "co-located in one chunk — correlated events remain "
            "discoverable together?"
        ),
        "invariant_weight": 0.20,
    },
}

DUAL_TRACK_NOTE = (
    "Event quality evaluation is dual-track: the canonical profile "
    "(event_quality_v2.0) produces the public "
    "profile/overall; these 3 invariants are a parallel additive "
    "view composed from the same fact layer (log events / jsonl "
    "records contracts). Dimension logic is real since "
    "event_quality_v1.0 — invariant confidence is not capped."
)


class EventAtomicity(QualityBackedInvariant):
    """Events are atomic records — never split across chunks.

    Semantic question: "Does any chunk boundary fall inside an
    event?"

    An event is the minimal self-describing unit of a record
    stream: splitting one across chunks corrupts the record (an
    exception without its stacktrace, a traceback frame without
    its exception). This is the event-stream analogue of
    narrative's 'no chunk breaks a code fence', config's 'no
    chunk breaks a path', and table's row atomicity.

    Composed from: multiline_event_integrity (0.5) +
    event_boundary_correctness (0.5) — the two primary sub-checks
    (v1.0-era canonical dimensions, now observations entries under
    the v2.0 taxonomy; the near-equal weights 0.30 / 0.25 taken symmetrically).
    """

    domain = "event"
    quality_factory = EventQuality
    dimension_weights = {
        "multiline_event_integrity": 0.5,
        "event_boundary_correctness": 0.5,
    }
    confidence_cap = None

    @property
    def name(self) -> str:
        """Invariant name."""
        return "event_atomicity"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Every event is contained by exactly one chunk — "
            "events are atomic records, never fragmented across "
            "chunk boundaries."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Append-only event semantics (Kafka/log-structured "
            "records): an event is the atomic unit of incident "
            "meaning; a partial event is not a smaller record "
            "but a corrupted one."
        )

    @property
    def weight(self) -> float:
        """Mounting weight."""
        return 0.50


class TracebackIntegrity(QualityBackedInvariant):
    """Tracebacks stay complete — never truncated by chunking.

    Semantic question: "Is any traceback event cut at a chunk
    boundary?"

    A stacktrace missing its tail frames (or severed from its
    triggering exception) is the single most expensive loss for
    incident analysis — the root frame is usually at the bottom.
    EventQuality's own design note names this invariant
    ("that is an invariant (traceback_integrity), not session
    fragmentation") — this class lands that note.

    Composed from: stacktrace_completeness (direct).
    """

    domain = "event"
    quality_factory = EventQuality
    dimension_weights = {"stacktrace_completeness": 1.0}
    confidence_cap = None

    @property
    def name(self) -> str:
        """Invariant name."""
        return "traceback_integrity"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Traceback events are fully contained in single "
            "chunks — no stacktrace is truncated at a chunk "
            "boundary or severed from its triggering exception."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Diagnostic completeness: stack unwinding (Java/Python "
            "throw semantics) attaches causal meaning to the full "
            "frame chain; truncation destroys the deepest frame "
            "first, which is precisely where root causes live."
        )

    @property
    def weight(self) -> float:
        """Mounting weight."""
        return 0.30


class CorrelationLocality(QualityBackedInvariant):
    """Correlated events stay co-located per session/request.

    Semantic question: "Are events sharing a session/request
    identifier scattered across chunks?"

    Incident reconstruction is a correlation join: request-scoped
    events scattered across chunks break the operational locality
    the representation exists to provide. Traceback events are
    excluded (they are isolated into dedicated units by design —
    that is traceback_integrity, not session fragmentation).

    Composed from: request_session_grouping (direct).
    """

    domain = "event"
    quality_factory = EventQuality
    dimension_weights = {"request_session_grouping": 1.0}
    confidence_cap = None

    @property
    def name(self) -> str:
        """Invariant name."""
        return "correlation_locality"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Events sharing a session/request identifier stay "
            "co-located in one chunk — correlated events remain "
            "discoverable together."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Operational locality principle: incident analysis is "
            "a correlation join over request-scoped events "
            "(trace/request id propagation, Dapper-style); "
            "scattering a session across chunks breaks the join "
            "locality the representation exists to provide."
        )

    @property
    def weight(self) -> float:
        """Mounting weight."""
        return 0.20


def create_event_registry() -> InvariantRegistry:
    """Create and populate the event domain invariant registry."""
    registry = InvariantRegistry("event")
    registry.register(EventAtomicity())
    registry.register(TracebackIntegrity())
    registry.register(CorrelationLocality())
    return registry
