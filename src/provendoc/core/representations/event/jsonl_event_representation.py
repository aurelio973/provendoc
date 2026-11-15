# ============================================================
# Record Stream Domain Representation
# JSONL → Event / Record Semantic Units
# ============================================================
from collections import defaultdict

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation


class JsonlEventRepresentation(BaseRepresentation):
    """JSONL/NDJSON record-stream representation — groups records into event units by strategies like record_type."""

    def __init__(
            self,
            grouping_strategy="record_type",
            preserve_record_atomicity=True,
            max_records_per_unit=50,
            isolate_embedding=True
    ):
        """Initialize the representation.

        Args:
            grouping_strategy: record grouping strategy (default "record_type").
            preserve_record_atomicity: a single record is never split across units.
            max_records_per_unit: maximum number of records per unit.
            isolate_embedding: embedding-type records get their own units.
        """
        self.grouping_strategy = grouping_strategy
        self.preserve_record_atomicity = preserve_record_atomicity
        self.max_records_per_unit = max_records_per_unit
        self.isolate_embeddings = isolate_embedding

    # ========================================================
    # public
    # ========================================================
    def build(self,document):
        """Group metadata["records"] into record_stream units.

        Args:
            document: Document (metadata["records"] holds the parsed records).

        Returns:
            RepresentationResult: representation_type="record_stream";
            empty content when there are no records.
        """
        records = document.metadata.get("records",[])

        if not records:
            return RepresentationResult(
                representation_type="record_stream",
                content=[],
                metadata={
                    "unit_count": 0,
                    "record_count": 0
                }
            )
        grouped = self._group_records(records)
        units = []

        for idx,group in enumerate(grouped):
            units.append(
                self._build_unit(group,idx,document)
            )
        return RepresentationResult(
            representation_type="record_stream",
            content=units,
            metadata={
                "unit_count": len(units),
                "record_count": len(records),
                "grouping_strategy": self.grouping_strategy,
                "representation_invariants": [
                    "record_atomicity",
                    "stream_locality",
                    "type_locality",
                    "semantic_scope_preserved"
                ],
                "transforms_applied": [
                    "record_grouping",
                    "stream_packaging",
                    "semantic_scope_preservation"
                ]
            }
        )

    # ========================================================
    # grouping
    # ========================================================
    def _group_records(self,records):
        # ====================================================
        # type grouping
        # ====================================================
        if self.grouping_strategy == "record_type":
            return self._group_by_type(records)

        # ====================================================
        # stream grouping
        # ====================================================
        if self.grouping_strategy == "stream":
            return self._group_stream(records)

        # ====================================================
        # fixed fallback
        # ====================================================
        return self._group_fixed(records)

    # ========================================================
    # type grouping
    # ========================================================
    def _group_by_type(self,records):
        grouped = defaultdict(list)

        for r in records:
            record_type = r.get(
                "record_type",
                "unknown"
            )

            # embeddings isolated
            if self.isolate_embeddings and record_type == "embedding":
                grouped[f"embedding_{id(r)}"].append(r)
                continue

            grouped[record_type].append(r)

        return list(grouped.values())

    # ========================================================
    # stream grouping
    # ========================================================
    def _group_stream(self,records):
        groups = []
        current = []

        for record in records:
            current.append(record)
            if len(current) >= self.max_records_per_unit:
                groups.append(current)
                current = []

        if current:
            groups.append(current)
        return groups

    # ========================================================
    # fixed grouping
    # ========================================================
    def _group_fixed(self,records):
        return [
            records[
                i:
                i + self.max_records_per_unit
            ]
            for i in range(
                0,
                len(records),
                self.max_records_per_unit
            )
        ]

    # ========================================================
    # build representation unit
    # ========================================================
    def _build_unit(self, records, unit_index, document=None):
        text = self._merge_records(records)
        record_types = [r.get("record_type") for r in records]
        all_keys = set()
        for r in records:
            all_keys.update(r.get("keys", []))

        return RepresentationUnit(
            text=text,
            unit_type=UnitType.NARRATIVE,
            source_context=self._unit_source_context(records, document),
            metadata={
                "unit_index": unit_index,
                "record_count": len(records),
                "record_types": sorted(set(record_types)),
                "keys_in_unit": sorted(all_keys),
                "representation_semantics": {
                    "record_atomicity": self.preserve_record_atomicity,
                    "stream_locality": True,
                    "type_locality": self.grouping_strategy == "record_type",
                    "semantic_scope_preserved": True
                },
                "semantic_role": "record_group"
            }
        )

    # ========================================================
    # source context (line-number span)
    # ========================================================
    @staticmethod
    def _unit_source_context(records, document):
        """Aggregate a unit-level SourceContext from the group's
        record line numbers.

        The line span is filled only when every record in the
        group carries "line" (the 1-based line number the
        jsonl parser records for each valid record) — with
        partial misses, min/max cannot cover the lineless
        records, so the span stays empty honestly (missing
        never masquerades). One record per line:
        line_start = line_end = the record's line number; a
        group span is the closed interval between the first
        and last record line numbers (a covering range).
        """
        file_name = None
        file_type = None
        if document is not None:
            file_name = document.metadata.get("file_name")
            file_type = document.metadata.get("file_type")

        line_start = None
        line_end = None
        if records and all(
            r.get("line") is not None for r in records
        ):
            line_start = min(r["line"] for r in records)
            line_end = max(r["line"] for r in records)

        return SourceContext(
            file_name=file_name,
            file_type=file_type,
            line_start=line_start,
            line_end=line_end,
        )

    # ========================================================
    # merge
    # ========================================================
    def _merge_records(self, records):
        merged = []
        for r in records:
            merged.append(r["raw"])
        return "\n".join(merged)

    # ========================================================
    # key count
    # ========================================================
    def _count_keys(self,keys):
        counts = {}
        for k in keys:
            counts[k] = counts.get(k,0) + 1
        return counts
