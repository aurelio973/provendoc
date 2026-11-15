"""Event Chunk Quality Index (EQI) — operational locality evaluation.

Domain: record_stream
Formats: LOG, JSONL, ndjson

Core question:
    "Are events correctly recovered and aggregated"

Unlike NarrativeQuality, this evaluator does NOT care about:
    - semantic topic continuity (events are not prose)
    - lexical cohesion (log lines have no cohesion)
    - paragraph boundaries (logs have no paragraphs)

Instead it asks two orthogonal questions (event_quality_v2.0,
dimension_taxonomy_refactor_prereg_v1 2026-10-26 — composite-question
perspective, sub-check computation logic untouched line by line):
    - unit_coherence       — units unbroken: each event lands whole in a single
                             chunk (multiline events and stacktraces included,
                             severe events never lost; sub-checks
                             event_boundary_correctness 0.4 +
                             multiline_event_integrity 0.3 +
                             stacktrace_completeness 0.3)
    - relation_continuity  — relations unbroken: within-chunk temporal order monotonic,
                             same-session/request events share one chunk
                             (sub-checks timestamp_continuity 0.5 +
                             request_session_grouping 0.5)

provenance_fidelity (nothing lost, nothing hallucinated) is structurally contained in unit_coherence:
verbatim event recoverability is the containment check itself (chunk text is the
verbatim concatenation of the events' raw data; no-loss and no-hallucination follow directly
from mapping integrity) — no standalone dimension is set; declared by registration (prereg §2.3).

The evidence layer does not enter scoring: parse_success_rate (the parser's own well-formedness rate,
the only signal-bearing one across the 678-file full corpus, but it is parse evidence) is demoted to
observations — values retained for audit and the gate, issues retained. All six
sub-check raw scores are exposed under their original names in observations.

"Good chunk" for events = incident-local event groups whose
every event is recoverable verbatim and correlated events
stay together.

Fact sources (two-layer, mirrors ConfigQuality/TableQuality):
    Layer 1 — parse contract (document.metadata):
        log:   metadata["events"] — per-event raw / timestamp /
               level / session_id / is_multiline /
               contains_traceback (log_parser ground truth)
        jsonl: metadata["records"] + metadata["invalid_lines"]
               (jsonl_parser ground truth)
    Layer 2 — RepresentationResult units (event groups): chunk
        text is the verbatim concatenation of event raws, so
        ground-truth events are located by containment with
        occurrence consumption (duplicate raws disambiguated).
    Without a parse contract the evaluator degrades to
    boundary-pattern heuristics; contract-dependent dims
    report N/A — never guessed.

Canonical weight policy (same contract as config/table quality):
    Weights are CANONICAL and FIXED. When a dimension is not
    applicable (N/A), its weight is NOT redistributed:

        overall = Σ(active_dim_score × weight) / Σ(all weights)

    A jsonl stream without session ids scores
    request_session_grouping as N/A and its overall is already
    discounted — this forbids "easy" documents from inflating
    their overall score.

Design notes:
    - traceback events are EXCLUDED from request_session_grouping:
      the representation isolates them into dedicated units by
      design (isolate_tracebacks) — that is an invariant
      (traceback_integrity), not session fragmentation.
    - timestamp_continuity scores WITHIN-chunk monotonicity only;
      cross-chunk temporal scope inversions are reported as
      advisory issues — session / record_type grouping
      deliberately trades temporal order for correlation
      locality, and the scored check targets scrambled chunks.

Confidence decomposition:
    confidence = dimension_coverage × evaluation_depth
                 × format_reliability
    (fallback fact source discounts format_reliability by 0.85,
    mirroring ConfigQuality/TableQuality; document quality warnings
    penalize it).

Skeleton mode is RETIRED since event_quality_v1.0: all six
dimensions have real implementations.
"""

import json
import re

from provendoc.core.parsers.event.log_parser import (
    LEVEL_PATTERNS,
    TIMESTAMP_PATTERNS,
)
from provendoc.intelligence.rse.quality_base import BaseQuality

# Same boundary-calibration basis as log_parser (anti-drift): an event starts at a timestamp prefix
# or a severity-level prefix
_COMPILED_TIMESTAMP_RES = [
    re.compile(pattern) for pattern in TIMESTAMP_PATTERNS
]

# Continuation prefixes (a chunk must not end with a continuation = the signal of a truncated event)
_CONTINUATION_PREFIXES = ("at ", "Caused by:", "...")


class EventQuality(BaseQuality):
    """Event-domain Chunk Quality Index (EQI).

    Evaluates chunk quality for record streams where the ingestion
    philosophy is:

        log is an append-only event sequence → chunks should
        preserve event locality → multiline events (stacktraces,
        continuations) must stay together → correlated events
        (same request_id / session) should be co-located

    Dimensions and weights (event_quality_v2.0, FROZEN):
        [PRIMARY]
        unit_coherence             1/2   events atomic in one chunk
        [SECONDARY]
        relation_continuity        1/2   order + correlation preserved

    Observations (diagnostic-only, included_in_overall=False):
        multiline_event_integrity / event_boundary_correctness /
        stacktrace_completeness / timestamp_continuity /
        request_session_grouping / parse_success_rate —
        sub-check raw scores are retained under their original names, for the invariant track's fallback
        score reads and audit consumption.

    Output structure (same shape as TableQuality):
        profile             — dimensional breakdown (N/A dims are null)
        observations        — sub-check raw scores (diagnostic layer,
                              included_in_overall=False; the invariant track's
                              profile→observations fallback reads them)
        overall             — advisory weighted sum (canonical weights)
        confidence          — evaluation reliability (decomposed)
        confidence_profile  — dimension_coverage × evaluation_depth
                              × format_reliability
        applicability       — per-dimension applicability status + reasons
        issues              — per-dimension problems
        metadata            — chunk statistics + fact_source + weight_policy
    """

    # v1.1 change (E4 uniform baseline, decision date 2026-10-25):
    #   the user adjudicated the methodology (uniform baseline vs ordinal scale; the former)
    #   — no prior over dimension importance; all six scoring weights are 1/6.
    #   (phase-0 measured evidence and the uniformity lock: see prereg:
    #   uniform_baseline_calibration_prereg_v1.md)
    #
    # v2.0 change (dimension-taxonomy refactor, decision date 2026-10-26):
    #   the user challenged "each dimension speaks its own language, not a composite-question view" → full-corpus
    #   evidence (four_domain_quality_v1, 678 files): of the six dimensions,
    #   multiline / boundary / stacktrace are constant 1.0,
    #   the session dimension is N/A 99.3%, the timestamp dimension std=0.041 and
    #   r=0.013 vs overall (noise); the only signal-bearing
    #   parse_success_rate is parse evidence. The scoring layer converges to two composite
    #   question dimensions (unit_coherence / relation_continuity),
    #   with provenance_fidelity structurally contained in unit_coherence
    #   (the verbatim-containment body, declared by registration); sub-check
    #   computation logic untouched line by line, raw scores go to observations for the invariant
    #   track's fallback reads (zero drift); parse_success_rate is demoted per its evidence-layer
    #   attribute to observations.
    #   Behavioral drift: the N/A cost goes from 1/6 to 1/2; the uniformity lock (contract E)
    #   dimension count 6→2.
    #   prereg: dimension_taxonomy_refactor_prereg_v1.md
    PROFILE_VERSION = "event_quality_v2.0"

    WEIGHTS = {
        "unit_coherence": 1 / 2,
        "relation_continuity": 1 / 2,
    }

    PRIMARY_DIMENSIONS = {
        "unit_coherence",
        # Legacy sub-check names stay in the severity set: sub-check issue
        # severity verdicts go by the original names (zero drift of gate inputs, prereg §2.4)
        "multiline_event_integrity",
        "event_boundary_correctness",
    }

    SECONDARY_DIMENSIONS = {
        "relation_continuity",
        "stacktrace_completeness",
        "timestamp_continuity",
        "request_session_grouping",
        "parse_success_rate",
    }

    # Observation-layer dimensions: sub-check raw scores exposed under their original names,
    # included_in_overall=False (prereg §2.4).
    OBSERVATION_DIMS = frozenset({
        "multiline_event_integrity",
        "event_boundary_correctness",
        "stacktrace_completeness",
        "timestamp_continuity",
        "request_session_grouping",
        "parse_success_rate",
    })

    # Demoted dimension (evidence layer does not enter scoring, prereg §2.3): the parser's own
    # well-formedness rate — values go to observations only, issues stay in the main list.
    DEMOTED_DIMS = frozenset({"parse_success_rate"})

    CONTRACT_FACT_SOURCES = frozenset({
        "log_events_contract",
        "jsonl_records_contract",
    })

    def evaluate(
        self,
        document,
        semantic_representation,
        strategy=None,
        document_quality=None,
    ):
        """Evaluate event chunk quality.

        Args:
            document: Document with log/jsonl parse contract in metadata
            semantic_representation: RepresentationResult whose content
                is a flat list of event-group units
            strategy: RepresentationPolicy (unused for events, but
                kept for interface symmetry)
            document_quality: DocumentQualityReport result

        Returns:
            dict with profile, overall, confidence, applicability,
            issues, metadata
        """
        chunks = self._extract_chunks(semantic_representation)

        if not chunks:
            return self._empty_result()

        facts = self._build_facts(document)
        mapping = self._map_events_to_chunks(facts["events"], chunks)

        # ==================================
        # Phase 1: subchecks (v1.1 logic as-is) → two composite question dimensions
        # ==================================
        subchecks = {
            "multiline_event_integrity": self._multiline_event_integrity(
                facts, mapping,
            ),
            "event_boundary_correctness": self._event_boundary_correctness(
                facts, chunks, mapping,
            ),
            "stacktrace_completeness": self._stacktrace_completeness(
                facts, chunks, mapping,
            ),
            "timestamp_continuity": self._timestamp_continuity(
                facts, mapping,
            ),
            "request_session_grouping": self._request_session_grouping(
                facts, mapping,
            ),
            "parse_success_rate": self._parse_success_rate(facts),
        }

        dim_results = {
            "unit_coherence": self._merge_subchecks(
                "unit_coherence",
                (
                    ("event_boundary_correctness", 0.4),
                    ("multiline_event_integrity", 0.3),
                    ("stacktrace_completeness", 0.3),
                ),
                subchecks,
            ),
            "relation_continuity": self._merge_subchecks(
                "relation_continuity",
                (
                    ("timestamp_continuity", 0.5),
                    ("request_session_grouping", 0.5),
                ),
                subchecks,
            ),
        }

        profile, applicability, issues = self._aggregate_dimensions(
            dim_results,
        )

        # Sub-check raw scores (including demoted dims) → observations; demoted-dim
        # issues stay in the main list (gate inputs unchanged, prereg §2.4)
        observations = self._observations_from(subchecks)
        for name in self.DEMOTED_DIMS:
            _score, applicable, _reason, sub_issues = subchecks[name]
            if applicable and sub_issues:
                issues.extend(sub_issues)

        # ==================================
        # Phase 2: canonical overall — N/A weights are not redistributed
        # ==================================
        overall = self._canonical_overall(profile, applicability)

        # ==================================
        # Phase 3: confidence (decomposed)
        # ==================================
        confidence, confidence_profile = self._confidence(
            applicability, facts, document_quality,
        )

        lengths = [
            len(c.text)
            for c in chunks
            if getattr(c, "text", None) is not None
        ]
        events = facts["events"]

        result = {
            "profile_version": self.PROFILE_VERSION,
            "profile": profile,
            "observations": observations,
            "applicability": applicability,
            "overall": round(overall, 3),
            "confidence": round(confidence, 3),
            "confidence_profile": confidence_profile,
            "issues": issues,
            "metadata": {
                "chunk_count": len(chunks),
                "avg_chunk_length": round(
                    sum(lengths) / max(len(lengths), 1), 2
                ),
                "min_chunk_length": min(lengths) if lengths else 0,
                "max_chunk_length": max(lengths) if lengths else 0,
                "fact_source": facts["source"],
                "weight_policy": "canonical_no_redistribution",
                "event_count": len(events),
                "mapped_event_count": (
                    sum(1 for hit in mapping if hit is not None)
                ),
                "multiline_event_count": sum(
                    1 for e in events if e.get("is_multiline")
                ),
                "traceback_event_count": sum(
                    1 for e in events if e.get("contains_traceback")
                ),
                "session_count": len({
                    e["session_id"]
                    for e in events
                    if e.get("session_id")
                }),
                "timestamped_event_count": sum(
                    1 for e in events if e.get("timestamp")
                ),
            },
        }
        return self._attach_invariant_track(result, chunks, document)

    # ==================================
    # invariant track attachment (event_invariants_v1)
    # ==================================

    def _attach_invariant_track(self, result, chunks, document):
        """Attach the parallel invariant track (roadmap item 9).

        Dual-track positioning: the canonical 2-dimension
        profile/overall above is the PUBLIC track; the 3 event
        invariants (event_invariants.py) are a PARALLEL additive
        view exported as ``invariants`` /
        ``invariant_composite``. The quality result is pre-seeded
        into the invariant context (``evaluation_result::event``)
        so composition causes zero recomputation. If invariant
        evaluation fails for any reason, we silently degrade to
        invariant-free output (the RSE layer records an honest
        not_evaluated ledger — same philosophy as table/code).
        """
        try:
            from provendoc.intelligence.rse.event_invariants import (
                create_event_registry,
            )
            registry = create_event_registry()
            context = {"evaluation_result::event": result}
            invariant_results = registry.evaluate_all(
                [c for c in chunks if c is not None],
                document,
                None,
                context=context,
            )
            result["invariants"] = {
                name: r.to_dict()
                for name, r in invariant_results.items()
            }
            result["invariant_composite"] = registry.composite_score(
                invariant_results, weighted=True,
            )
            result["invariant_track"] = "event_invariants_v1"
        # When the invariant registry is not ready, degrade to no invariant track (enhanced observation, not a promise).
        except Exception:
            pass
        return result

    # ==================================
    # unit extraction
    # ==================================

    @staticmethod
    def _extract_chunks(semantic_representation):
        """Extract event-group units from the representation.

        Event representations produce a flat unit list; a
        MultiGranularityOutput (defensive) maps to its base units.
        """
        if semantic_representation is None:
            return []
        content = getattr(
            semantic_representation, "content", semantic_representation,
        )
        if content is None:
            return []
        if hasattr(content, "fine") and hasattr(content, "base"):
            return list(content.base or [])
        if isinstance(content, (list, tuple)):
            return list(content)
        return []

    # ==================================
    # fact view (Layer 1: parse contract ground truth)
    # ==================================

    def _build_facts(self, document):
        """Build the ground-truth fact view.

        Prefers the parse contract (log events / jsonl records);
        without one the evaluator degrades to structure-only
        heuristics and contract-dependent dims report N/A.
        """
        metadata = getattr(document, "metadata", None) or {}

        events = metadata.get("events")
        if events:
            return {
                "source": "log_events_contract",
                "kind": "log",
                "events": [
                    e for e in events
                    if isinstance(e, dict) and e.get("raw")
                ],
                "parse": None,
            }

        records = metadata.get("records")
        if records:
            invalid = metadata.get("invalid_line_count")
            if invalid is None:
                invalid = len(metadata.get("invalid_lines") or [])
            events = [
                self._normalize_record(r)
                for r in records
                if isinstance(r, dict) and r.get("raw")
            ]
            return {
                "source": "jsonl_records_contract",
                "kind": "jsonl",
                "events": events,
                "parse": {
                    "valid": len(events),
                    "invalid": int(invalid or 0),
                },
            }

        return {
            "source": "unit_metadata_fallback",
            "kind": None,
            "events": [],
            "parse": None,
        }

    @staticmethod
    def _normalize_record(record):
        """Normalize a jsonl record fact into the event shape.

        Timestamp / level / session come from the record payload
        itself (factual — they are keys in the parsed object);
        jsonl records are single-line by construction.
        """
        payload = record.get("record")
        timestamp = None
        level = None
        session_id = None
        if isinstance(payload, dict):
            if payload.get("timestamp") is not None:
                timestamp = str(payload["timestamp"])
            if payload.get("level") is not None:
                level = str(payload["level"])
            for key in ("session_id", "request_id", "trace_id"):
                value = payload.get(key)
                if value:
                    session_id = str(value)
                    break
        return {
            "raw": record["raw"],
            "line_count": 1,
            "timestamp": timestamp,
            "level": level,
            "session_id": session_id,
            "is_multiline": False,
            "contains_traceback": False,
        }

    # ==================================
    # event → chunk mapping (containment with consumption)
    # ==================================

    @staticmethod
    def _map_events_to_chunks(events, chunks):
        """Locate every ground-truth event inside the chunks.

        Chunk text is the verbatim join of event raws, so an
        event is INTACT iff its raw is contained in one chunk.
        Occurrence consumption (replacement with a sentinel)
        disambiguates duplicate raws.

        Returns a list aligned with events: (chunk_index, pos)
        for intact events, None otherwise.
        """
        remaining = [
            (getattr(chunk, "text", "") or "") for chunk in chunks
        ]
        mapping = []
        for event in events:
            raw = event.get("raw") or ""
            hit = None
            if raw:
                for ci, text in enumerate(remaining):
                    pos = text.find(raw)
                    if pos != -1:
                        hit = (ci, pos)
                        remaining[ci] = (
                            text[:pos]
                            + "\x00" * len(raw)
                            + text[pos + len(raw):]
                        )
                        break
            mapping.append(hit)
        return mapping

    # ==================================
    # helpers
    # ==================================

    @staticmethod
    def _timestamp_family(timestamp):
        """Pattern family of an extracted timestamp (same order
        as log_parser.TIMESTAMP_PATTERNS), for comparable pairs."""
        if not timestamp:
            return None
        for family, rx in enumerate(_COMPILED_TIMESTAMP_RES):
            if rx.fullmatch(timestamp):
                return family
        return None

    @staticmethod
    def _looks_like_event_start(line):
        """Does a line look like the START of a new event?"""
        stripped = line.strip()
        if not stripped:
            return False
        # jsonl record start
        if stripped[0] in "{[":
            try:
                json.loads(stripped)
                return True
            except Exception:
                return False
        # log boundary (same basis as log_parser._is_new_event)
        for rx in _COMPILED_TIMESTAMP_RES:
            if rx.match(stripped):
                return True
        return any(
            stripped.startswith(level) for level in LEVEL_PATTERNS
        )

    @staticmethod
    def _looks_complete_end(line):
        """Does a line look like a COMPLETE ending (not a
        mid-multiline-event truncation point)?"""
        if line[:1] in (" ", "\t"):
            return False
        stripped = line.lstrip()
        if any(
            stripped.startswith(prefix)
            for prefix in _CONTINUATION_PREFIXES
        ):
            return False
        return True

    # ==================================
    # dimension: multiline_event_integrity (PRIMARY)
    # ==================================
    #
    # Multiline events (exception + stacktrace, continuation
    # lines) must be fully contained in a single chunk. A
    # stacktrace split from its triggering exception is a
    # critical violation.
    # ==================================

    def _multiline_event_integrity(self, facts, mapping):
        dim = "multiline_event_integrity"
        if facts["source"] == "unit_metadata_fallback":
            return 0.0, False, "no_parse_contract", []
        multiline = [
            (idx, event)
            for idx, event in enumerate(facts["events"])
            if event.get("is_multiline")
        ]
        if not multiline:
            return 0.0, False, "no_multiline_events", []
        intact = sum(
            1 for idx, _ in multiline if mapping[idx] is not None
        )
        issues = []
        for idx, event in multiline:
            if mapping[idx] is None:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"multiline event (line_count="
                    f"{event.get('line_count')}) split across or "
                    f"missing from chunks: "
                    f"'{event['raw'][:60]}...'",
                ))
                if len(issues) >= 5:
                    break
        score = intact / len(multiline)
        return score, True, "", issues

    # ==================================
    # dimension: event_boundary_correctness (PRIMARY)
    # ==================================
    #
    # Contract mode: every ground-truth event must be fully
    # contained in exactly one chunk — chunk boundaries then
    # align with event boundaries by construction.
    # Fallback mode (no contract): boundary-pattern heuristic —
    # each chunk must start at an event-looking line and must
    # not end on a continuation line.
    # ==================================

    def _event_boundary_correctness(self, facts, chunks, mapping):
        dim = "event_boundary_correctness"
        if facts["source"] != "unit_metadata_fallback":
            events = facts["events"]
            if not events:
                return 0.0, False, "no_events_in_contract", []
            intact = sum(1 for hit in mapping if hit is not None)
            score = intact / len(events)
            issues = []
            if score < 0.999:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"{len(events) - intact} events split across or "
                    f"missing from chunks (chunk boundary does not "
                    f"align with an event boundary)",
                ))
            return score, True, "", issues

        # fallback: boundary-pattern heuristic over chunk text
        ok = 0
        issues = []
        for idx, chunk in enumerate(chunks):
            text = getattr(chunk, "text", "") or ""
            lines = text.split("\n")
            starts_ok = self._looks_like_event_start(
                lines[0] if lines else "",
            )
            ends_ok = self._looks_complete_end(
                lines[-1] if lines else "",
            )
            if starts_ok and ends_ok:
                ok += 1
            else:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"chunk {idx}: boundary does not look like a "
                    f"real event boundary"
                    + ("" if starts_ok else " (first line is not an event start)")
                    + ("" if ends_ok else " (last line is a continuation)"),
                ))
        score = ok / len(chunks)
        return score, True, "", issues

    # ==================================
    # dimension: stacktrace_completeness (SECONDARY)
    # ==================================
    #
    # Contract mode: traceback events must be fully contained
    # in a single chunk (no truncation at chunk boundaries).
    # Fallback mode: chunks carrying traceback patterns must not
    # end on a continuation line ("at ...", "Caused by:").
    # ==================================

    def _stacktrace_completeness(self, facts, chunks, mapping):
        dim = "stacktrace_completeness"
        if facts["source"] != "unit_metadata_fallback":
            tracebacks = [
                (idx, event)
                for idx, event in enumerate(facts["events"])
                if event.get("contains_traceback")
            ]
            if not tracebacks:
                return 0.0, False, "no_traceback_events", []
            intact = sum(
                1 for idx, _ in tracebacks if mapping[idx] is not None
            )
            issues = []
            for idx, event in tracebacks:
                if mapping[idx] is None:
                    issues.append(self._issue(
                        dim, self._severity_for(dim),
                        f"traceback event truncated or split across "
                        f"chunks: '{event['raw'][:60]}...'",
                    ))
                    if len(issues) >= 5:
                        break
            score = intact / len(tracebacks)
            return score, True, "", issues

        # fallback: pattern heuristic over chunks with traces
        with_traces = []
        for idx, chunk in enumerate(chunks):
            text = getattr(chunk, "text", "") or ""
            if "Traceback" in text or any(
                line.lstrip().startswith("at ")
                for line in text.split("\n")
            ):
                with_traces.append((idx, chunk))
        if not with_traces:
            return 0.0, False, "no_traceback_patterns", []
        ok = 0
        issues = []
        for idx, chunk in with_traces:
            lines = (getattr(chunk, "text", "") or "").split("\n")
            if self._looks_complete_end(lines[-1] if lines else ""):
                ok += 1
            else:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"chunk {idx}: stacktrace truncated at chunk "
                    f"boundary (ends on a continuation line)",
                ))
        score = ok / len(with_traces)
        return score, True, "", issues

    # ==================================
    # dimension: timestamp_continuity (SECONDARY)
    # ==================================
    #
    # WITHIN-chunk monotonicity is scored: for each chunk, the
    # ground-truth events it contains (in chunk order) must have
    # non-decreasing timestamps. Only same-pattern-family
    # timestamps are compared (ISO dates vs bare times are not
    # lexicographically comparable).
    # Cross-chunk scope inversions are ADVISORY issues only —
    # session / record_type grouping deliberately trades
    # temporal order for correlation locality.
    # ==================================

    def _timestamp_continuity(self, facts, mapping):
        dim = "timestamp_continuity"
        if facts["source"] == "unit_metadata_fallback":
            return 0.0, False, "no_parse_contract", []

        by_chunk = {}
        for idx, event in enumerate(facts["events"]):
            timestamp = event.get("timestamp")
            if not timestamp:
                continue
            hit = mapping[idx]
            if hit is None:
                continue
            ci, pos = hit
            by_chunk.setdefault(ci, []).append((pos, timestamp))

        if not by_chunk:
            return 0.0, False, "no_mapped_timestamped_events", []

        pairs_ok = 0
        pairs_total = 0
        ordered = sorted(by_chunk.items())
        for _, items in ordered:
            items.sort()
            for (_, ts1), (_, ts2) in zip(items, items[1:]):
                if (
                    self._timestamp_family(ts1) is None
                    or self._timestamp_family(ts1)
                    != self._timestamp_family(ts2)
                ):
                    continue
                pairs_total += 1
                if ts1 <= ts2:
                    pairs_ok += 1

        if pairs_total == 0:
            return 0.0, False, "no_comparable_timestamp_pairs", []

        # cross-chunk scope inversions → advisory issues (not scored)
        inversions = 0
        for (_, items1), (_, items2) in zip(ordered, ordered[1:]):
            _, last = items1[-1]
            _, first = items2[0]
            if (
                self._timestamp_family(last) is not None
                and self._timestamp_family(last)
                == self._timestamp_family(first)
                and last > first
            ):
                inversions += 1

        issues = []
        if pairs_ok < pairs_total:
            issues.append(self._issue(
                dim, self._severity_for(dim),
                f"{pairs_total - pairs_ok} in-chunk timestamp "
                f"regressions (events out of chronological order)",
            ))
        if inversions:
            issues.append(self._issue(
                dim, "warning",
                f"{inversions} cross-chunk temporal scope inversions "
                f"(expected under session/record_type grouping — "
                f"correlation locality trades temporal order)",
            ))

        score = pairs_ok / pairs_total
        return score, True, "", issues

    # ==================================
    # dimension: request_session_grouping (SECONDARY)
    # ==================================
    #
    # Events sharing a session_id should be co-located in one
    # chunk. Traceback events are EXCLUDED: the representation
    # isolates them into dedicated units by design
    # (isolate_tracebacks) — that is traceback_integrity, not
    # session fragmentation.
    # ==================================

    def _request_session_grouping(self, facts, mapping):
        dim = "request_session_grouping"
        if facts["source"] == "unit_metadata_fallback":
            return 0.0, False, "no_parse_contract", []

        sessions = {}
        for idx, event in enumerate(facts["events"]):
            session_id = event.get("session_id")
            if not session_id or event.get("contains_traceback"):
                continue
            hit = mapping[idx]
            if hit is None:
                continue
            sessions.setdefault(session_id, set()).add(hit[0])

        if not sessions:
            return 0.0, False, "no_session_identifiers", []

        colocated = sum(
            1 for chunks_used in sessions.values()
            if len(chunks_used) == 1
        )
        issues = []
        for session_id, chunks_used in sessions.items():
            if len(chunks_used) > 1:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"session '{session_id}' events scattered across "
                    f"{len(chunks_used)} chunks",
                ))
                if len(issues) >= 5:
                    break
        score = colocated / len(sessions)
        return score, True, "", issues

    # ==================================
    # dimension: parse_success_rate (SECONDARY)
    # ==================================
    #
    # jsonl contract: valid records / (records + invalid lines)
    # — the parser's own ground truth. log contract: fraction of
    # events from which a timestamp or level could be extracted
    # (an unstructured .log scores low — factual, not guessed).
    # ==================================

    def _parse_success_rate(self, facts):
        dim = "parse_success_rate"
        issues = []

        if facts["source"] == "jsonl_records_contract":
            valid = len(facts["events"])
            invalid = facts["parse"]["invalid"]
            total = valid + invalid
            if total == 0:
                return 0.0, False, "no_lines_parsed", []
            if invalid:
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"{invalid} malformed lines skipped by the "
                    f"jsonl parser",
                ))
            return valid / total, True, "", issues

        if facts["source"] == "log_events_contract":
            events = facts["events"]
            if not events:
                return 0.0, False, "no_events_parsed", []
            well_formed = sum(
                1 for e in events
                if e.get("timestamp") or e.get("level")
            )
            if well_formed < len(events):
                issues.append(self._issue(
                    dim, self._severity_for(dim),
                    f"{len(events) - well_formed} events carry no "
                    f"timestamp or level (unstructured log lines)",
                ))
            return well_formed / len(events), True, "", issues

        return 0.0, False, "no_parse_contract", []
