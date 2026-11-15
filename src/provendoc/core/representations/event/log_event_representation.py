# ============================================================
# log_event_representation.py
# Record Stream Domain Representation
# ============================================================
from collections import defaultdict

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation


class LogEventRepresentation(BaseRepresentation):
    """Log event stream representation — session / incident grouping strategies."""

    # Anchor level set: a hit makes the event an incident anchor
    # (compared after case normalization). Traceback events are
    # anchors unconditionally.
    INCIDENT_ANCHOR_LEVELS = frozenset(
        {"error", "critical", "fatal", "panic", "alert", "emerg"}
    )

    def __init__(
            self,
            grouping_strategy="session",
            preserve_temporal_locality=True,
            max_events_per_unit=20,
            isolate_tracebacks=True,
            incident_context_before=5,
            incident_context_after=3
    ):
        """Initialize the representation.

        Args:
            grouping_strategy: "session" (default, group by session) or
                "incident" (error/traceback anchors ± context window).
            preserve_temporal_locality: keep temporal locality of events.
            max_events_per_unit: max events per unit.
            isolate_tracebacks: traceback events become standalone units.
            incident_context_before: incident window size (events before).
            incident_context_after: incident window size (events after).
        """
        self.grouping_strategy = grouping_strategy
        self.preserve_temporal_locality = preserve_temporal_locality
        self.max_events_per_unit = max_events_per_unit
        self.isolate_tracebacks = isolate_tracebacks
        self.incident_context_before = incident_context_before
        self.incident_context_after = incident_context_after

    # ========================================================
    # public
    # ========================================================
    def build(self,document):
        """Group metadata["events"] into record_stream units.

        Under the incident strategy each group carries anchor/window
        facts (sharing the same plan as grouping, avoiding divergent
        re-derivation).

        Args:
            document: Document (metadata["events"] holds parsed events).

        Returns:
            RepresentationResult: representation_type="record_stream";
            empty content when there are no events.
        """
        events = document.metadata.get("events",[])

        if not events:
            return RepresentationResult(
                representation_type="record_stream",
                content=[],
                metadata={
                    "unit_count": 0,
                    "event_count": 0
                }
            )
        grouped = self._group_events(events)
        annotations = [None] * len(grouped)
        if self.grouping_strategy == "incident":
            # Incident groups carry per-group facts (anchors/window),
            # sharing the same plan as the pure grouping view to avoid
            # divergent re-derivation.
            _, annotations = self._incident_plan(events)
        units = []
        for idx, group in enumerate(grouped):
            units.append(
                self._build_unit(
                    group, idx, document, incident=annotations[idx]
                )
            )

        metadata = {
            "unit_count": len(units),
            "event_count": len(events),
            "grouping_strategy": self.grouping_strategy,
            "representation_invariants": [
                "event_atomicity",
                "traceback_integrity",
                "temporal_locality",
                "session_locality"
            ],
            "transforms_applied": [
                "multiline_event_recovery",
                "session_grouping",
                "traceback_preservation",
                "temporal_grouping"
            ]
        }
        if self.grouping_strategy == "incident":
            anchor_count = sum(
                1
                for event in events
                if self._is_incident_anchor(event)
            )
            metadata["representation_invariants"].append(
                "incident_locality"
            )
            metadata["transforms_applied"].append(
                "incident_grouping"
            )
            metadata["incident_window"] = {
                "before": self.incident_context_before,
                "after": self.incident_context_after,
                "anchor_count": anchor_count,
                "incident_unit_count": sum(
                    1
                    for info in annotations
                    if info is not None
                )
            }

        return RepresentationResult(
            representation_type="record_stream",
            content=units,
            metadata=metadata
        )

    # ========================================================
    # grouping
    # ========================================================
    def _group_events(self,events):
        # ====================================================
        # session grouping
        # ====================================================
        if self.grouping_strategy == "session":
            return self._group_by_session(events)

        # ====================================================
        # temporal grouping
        # ====================================================
        if self.grouping_strategy == "temporal":
            return self._group_temporal(events)

        # ====================================================
        # incident context window grouping
        # ====================================================
        if self.grouping_strategy == "incident":
            return self._incident_plan(events)[0]

        # ====================================================
        # fixed-size fallback
        # ====================================================
        return self._group_fixed(events)


    # ========================================================
    # session grouping
    # ========================================================
    def _group_by_session(self,events):
        grouped = defaultdict(list)
        anonymous = []

        for event in events:
            session_id = event.get("session_id")

            # traceback isolation
            if self.isolate_tracebacks and event.get("contains_traceback"):
                grouped[f"traceback_{id(event)}"].append(event)
                continue

            if session_id:
                grouped[session_id].append(event)
            else:
                anonymous.append(event)

        # anonymous failback
        if anonymous:
            grouped["_anonymous"] = anonymous

        return list(grouped.values())

    # ========================================================
    # incident context window grouping
    #
    # Anchor = a traceback event or an event whose normalized level
    # falls in INCIDENT_ANCHOR_LEVELS. Windows are defined by event
    # count (stream order ± before/after), not by timestamp —
    # timestamps are best-effort parsed mixed-format strings, so
    # time arithmetic on them is unreliable; they are recorded only
    # as metadata facts (temporal_scope). Adjacent or overlapping
    # windows are merged to avoid overlapping fragments when
    # anchors are dense. Background events (not covered by any
    # window) are grouped by session (same as the session
    # strategy); traceback isolation is unnecessary there —
    # tracebacks are all anchors and always inside a window. With no
    # anchors it degrades to session grouping.
    # ========================================================
    def _is_incident_anchor(self, event):
        if event.get("contains_traceback"):
            return True
        level = str(event.get("level") or "").strip().lower()
        return level in self.INCIDENT_ANCHOR_LEVELS

    def _incident_plan(self, events):
        """Return (groups, annotations): the grouping view and per-group incident facts.

        annotations maps 1:1 to groups: an incident group gets a facts
        dict, a background group gets None. All groups are ordered by
        their first event's stream position, preserving inter-unit
        temporal locality. Incident groups are exempt from
        max_events_per_unit splitting (semantics first) — the window
        parameters already bound their size.
        """
        total = len(events)
        anchors = [
            index
            for index, event in enumerate(events)
            if self._is_incident_anchor(event)
        ]
        if not anchors:
            groups = self._group_by_session(events)
            return groups, [None] * len(groups)

        windows = []
        for anchor in anchors:
            start = max(0, anchor - self.incident_context_before)
            end = min(total, anchor + self.incident_context_after + 1)
            if windows and start <= windows[-1]["end"]:
                windows[-1]["end"] = max(windows[-1]["end"], end)
                windows[-1]["anchors"].append(anchor)
            else:
                windows.append(
                    {"start": start, "end": end, "anchors": [anchor]}
                )

        covered = set()
        for window in windows:
            covered.update(range(window["start"], window["end"]))

        # (first-event stream position, group, annotation) — sorted on output
        entries = []
        for window in windows:
            group = events[window["start"]:window["end"]]
            info = {
                "anchors": list(window["anchors"]),
                "anchor_count": len(window["anchors"]),
                "window": {
                    "start": window["start"],
                    "end": window["end"]
                },
                "merged": len(window["anchors"]) > 1,
                "context_events": len(group)
            }
            entries.append((window["start"], group, info))

        # Background events group by session (same as the session
        # strategy: anonymous events go to _anonymous, stream order
        # preserved within groups). Background never contains
        # tracebacks — tracebacks are all anchors and already inside
        # windows.
        background_by_session = defaultdict(list)
        for index, event in enumerate(events):
            if index not in covered:
                session_id = event.get("session_id")
                if session_id:
                    background_by_session[session_id].append(
                        (index, event)
                    )
                else:
                    background_by_session["_anonymous"].append(
                        (index, event)
                    )
        for group in background_by_session.values():
            entries.append(
                (group[0][0], [event for _, event in group], None)
            )

        entries.sort(key=lambda item: item[0])
        groups = [entry[1] for entry in entries]
        annotations = [entry[2] for entry in entries]
        return groups, annotations

    # ========================================================
    # temporal grouping
    # ========================================================
    def _group_temporal(self,events):
        groups = []
        current = []

        for event in events:
            current.append(event)
            if len(current) >= self.max_events_per_unit:
                groups.append(current)
                current = []

        if current:
            groups.append(current)

        return groups

    # ========================================================
    # fixed grouping
    # ========================================================
    def _group_fixed(self, events):
        return [
            events[i:i + self.max_events_per_unit]
            for i in range(0, len(events), self.max_events_per_unit)
        ]

    # ========================================================
    # build representation unit
    # ========================================================
    def _build_unit(self,events,unit_index,document=None,incident=None):
        text = self._merge_events(events)
        levels = [
            e.get("level")
            for e in events
            if e.get("level")
        ]
        event_types = [
            e.get("event_type")
            for e in events
        ]
        tracebacks = sum(
            1
            for e in events
            if e.get("contains_traceback")
        )
        timestamps = [
            e.get("timestamp")
            for e in events
            if e.get("timestamp")
        ]

        metadata = {
            "unit_index": unit_index,
            "event_count": len(events),
            "severity_distribution": self._count_levels(levels),
            "event_types": sorted(set(event_types)),
            "traceback_count": tracebacks,
            "temporal_scope": {
                "start":
                    timestamps[0]
                    if timestamps
                    else None,
                "end":
                    timestamps[-1]
                    if timestamps
                    else None
            },
            "session_ids":
            list({
                e.get("session_id")
                for e in events
                if e.get("session_id")
            }),
        }
        # incident_locality exists only under the incident strategy
        # (zero drift on the default path: no new metadata keys).
        semantics = {
            "event_atomicity": True,
            "traceback_integrity": True,
            "temporal_locality": self.preserve_temporal_locality,
            "session_locality": (
                self.grouping_strategy == "session"
                or (
                    self.grouping_strategy == "incident"
                    and incident is None
                )
            )
        }
        if self.grouping_strategy == "incident":
            semantics["incident_locality"] = incident is not None
        metadata["representation_semantics"] = semantics
        metadata["semantic_role"] = (
            "incident_unit"
            if incident is not None
            else "event_group"
        )
        if incident is not None:
            metadata["incident"] = incident

        return RepresentationUnit(
            text=text,
            unit_type=UnitType.NARRATIVE,
            source_context=self._unit_source_context(events, document),
            metadata=metadata
        )

    # ========================================================
    # source context (line span)
    # ========================================================
    @staticmethod
    def _unit_source_context(events, document):
        """Aggregate the group's event line spans into a unit-level SourceContext.

        Line spans are filled only when every event in the group has
        line_start/line_end — with partial misses, min/max cannot
        cover the lineless events, so the span stays honestly empty
        (missing is not faked). session/incident groups may be
        non-contiguous; the span is the closed interval of the
        first/last event lines (a covering interval, not a line
        list), matching the covering semantics of the code domain's
        line span.
        """
        file_name = None
        file_type = None
        if document is not None:
            file_name = document.metadata.get("file_name")
            file_type = document.metadata.get("file_type")

        line_start = None
        line_end = None
        if events and all(
            event.get("line_start") is not None
            and event.get("line_end") is not None
            for event in events
        ):
            line_start = min(event["line_start"] for event in events)
            line_end = max(event["line_end"] for event in events)

        return SourceContext(
            file_name=file_name,
            file_type=file_type,
            line_start=line_start,
            line_end=line_end,
        )

    # ========================================================
    # merge
    # ========================================================
    def _merge_events(self,events):
        merged = []
        for event in events:
            merged.append(event["raw"])
        return "\n\n".join(merged)

    # ========================================================
    # level count
    # ========================================================
    def _count_levels(self,levels):
        counts = {}
        for level in levels:
            counts[level] = counts.get(level,0) + 1
        return counts
