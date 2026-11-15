"""PDF representation runtime over reconstructed page evidence.

This module is deliberately separate from TXT/Markdown representation.  PDF
base units carry page, bounding-box and evidence references, while coarse units
are context packages assembled only after reading reconstruction is complete.
It does not modify extraction evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    SourceContext,
    StructuralInfo,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.representations.narrative.granularity import (
    CoarseGrainedConfig,
    MultiGranularitySplitter,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    _split_at_hard_fallback,
)
from provendoc.core.strategies.representation_policy import RepresentationPolicy


@dataclass
class _PendingBlock:
    text: str
    unit_type: UnitType
    page: int | None
    pages: list[int]
    heading_path: list[str]
    heading_level: int
    bboxes: list[list[float]]
    evidence_ids: list[str]
    source_kinds: list[str]
    confidence: float
    merged_from: int
    # Spec §8: table structure facts derived by the reconstruction layer
    # (table_status / columns_confidence / detection method); None for
    # non-table blocks.
    table_facts: dict[str, Any] | None = None
    # d4 size-enforcement provenance (prereg
    # pdf_size_enforcement_family_alignment_prereg_v1 §2.1): sentence-aware
    # split pieces carry size_split/split_reason; oversized table/figure atoms
    # carry oversized. Off by default — zero drift on the non-oversized path.
    size_split: bool = False
    split_reason: str | None = None
    oversized: bool = False


class PDFRepresentationRuntime(BaseRepresentation):
    """Build three granularity views from reconstructed PDF blocks.

    The input is ``Document.metadata['pdf_blocks']`` from :class:`PDFPipeline`.
    Each input block already has a deterministic reading order and has retained
    its page / bbox / evidence identifiers.  The representation layer only
    packages that reconstructed reading into retrieval units.
    """

    def __init__(
        self,
        strategy=None,
        attach_heading_path: bool = True,
        preserve_page_scope: bool = True,
        merge_short_blocks: bool = True,
        short_block_threshold: int = 120,
    ):
        """Configure the strategy and short-block merge options."""
        self.policy = RepresentationPolicy.coerce_to_policy(strategy)
        self.attach_heading_path = attach_heading_path
        self.preserve_page_scope = preserve_page_scope
        self.merge_short_blocks = merge_short_blocks
        self.short_block_threshold = short_block_threshold

    def build(self, document) -> RepresentationResult:
        """Build the three granularity views from reconstructed blocks."""
        blocks = document.metadata.get("pdf_blocks", [])
        # Spec engineering principle: dual page numbering — declared page labels
        # attach to each unit following the physical page number; the physical page
        # number stays the sole authority, the label is a registered fact only.
        label_by_page = self._label_by_page(document.metadata.get("pdf_navigation"))
        base_units = self._build_base_units(
            blocks, document.metadata, label_by_page
        )
        output = MultiGranularitySplitter(
            coarse_config=CoarseGrainedConfig.from_policy(self.policy.coarse),
            enable_fine_grained=self.policy.enable_fine_grained,
        ).process(base_units)

        return RepresentationResult(
            representation_type="narrative",
            content=output,
            metadata={
                "chunk_count": len(output.base),
                "base_units": len(output.base),
                "coarse_units": len(output.coarse),
                "fine_units": len(output.fine),
                "avg_chunk_length": self._average_length(output.base),
                "contains_code": False,
                "contains_inline_code": False,
                "transforms_applied": [
                    "pdf_evidence_preserved",
                    "pdf_reading_reconstruction",
                    "pdf_heading_path_attachment",
                    "pdf_context_packaging",
                    "multi_granularity",
                ],
                "representation_invariants": [
                    "evidence_coordinates_preserved",
                    "hierarchy_continuity",
                    "page_locality_preserved",
                ],
                "granularity": output.stats(),
            },
        )

    def _build_base_units(
        self,
        blocks: list[dict[str, Any]],
        document_metadata: dict[str, Any],
        label_by_page: dict[int, str] | None = None,
    ) -> list[RepresentationUnit]:
        units: list[RepresentationUnit] = []
        heading_stack: list[tuple[int, str]] = []
        pending: _PendingBlock | None = None
        # d4 size enforcement (prereg pdf_size_enforcement_family_alignment_
        # prereg_v1 §2.1) — same policy as d3 docx: merge cap + sentence-aware
        # splitting of single oversized narrative blocks + oversized table/figure
        # atoms. Zero drift on the non-oversized path.
        limit = self.policy.packaging.size_hard_limit

        def flush_pending() -> None:
            """Flush the pending block into a base unit."""
            nonlocal pending
            if pending is None:
                return
            units.append(
                self._make_unit(
                    pending,
                    sequence_index=len(units),
                    document_metadata=document_metadata,
                    label_by_page=label_by_page or {},
                )
            )
            pending = None

        for block in blocks:
            text = str(block.get("text", "")).strip()
            if not text:
                continue

            if block.get("type") == "heading":
                flush_pending()
                level = max(1, int(block.get("heading_level") or 1))
                heading_stack = [item for item in heading_stack if item[0] < level]
                heading_stack.append((level, text))
                continue

            heading_path = [heading for _, heading in heading_stack]
            heading_level = heading_stack[-1][0] if heading_stack else 0
            candidate = self._pending_from_block(
                block=block,
                text=text,
                heading_path=heading_path,
                heading_level=heading_level,
            )

            # Tables and figures are representation atoms: each gets its own
            # base unit and is never folded into nearby narrative text.
            if candidate.unit_type != UnitType.NARRATIVE:
                flush_pending()
                if limit > 0 and len(candidate.text) > limit:
                    # d4: oversized table/figure atoms are never split — honestly
                    # registered oversized (contract keys same family as d3 docx / container).
                    candidate = replace(candidate, oversized=True)
                units.append(
                    self._make_unit(
                        candidate,
                        sequence_index=len(units),
                        document_metadata=document_metadata,
                        label_by_page=label_by_page or {},
                    )
                )
                continue

            if limit > 0 and len(candidate.text) > limit:
                # d4: single oversized narrative block — sentence-aware hard-fallback
                # splitting into pieces (same machinery as txt/docx, prereg §2.1.2);
                # each piece copies the block's provenance (multiple pieces of one block
                # share bboxes/evidence_ids — many-to-one is legal).
                flush_pending()
                pieces = _split_at_hard_fallback(candidate.text, limit)
                if len(pieces) <= 1:
                    # Defensive: if the fallback splitter cannot split (should not
                    # happen), emit as-is.
                    units.append(
                        self._make_unit(
                            candidate,
                            sequence_index=len(units),
                            document_metadata=document_metadata,
                            label_by_page=label_by_page or {},
                        )
                    )
                else:
                    for piece in pieces:
                        units.append(
                            self._make_unit(
                                replace(
                                    candidate,
                                    text=piece,
                                    size_split=True,
                                    split_reason="hard_limit_fallback",
                                ),
                                sequence_index=len(units),
                                document_metadata=document_metadata,
                                label_by_page=label_by_page or {},
                            )
                        )
                continue

            if pending is not None and self._can_merge_pending(pending, candidate):
                pending.text += "\n\n" + candidate.text
                pending.bboxes.extend(candidate.bboxes)
                pending.evidence_ids.extend(candidate.evidence_ids)
                pending.source_kinds.extend(candidate.source_kinds)
                pending.confidence = min(pending.confidence, candidate.confidence)
                pending.merged_from += candidate.merged_from
                continue

            flush_pending()
            pending = candidate

        flush_pending()
        return units

    def _can_merge_pending(
        self,
        pending: _PendingBlock | None,
        candidate: _PendingBlock,
    ) -> bool:
        if not self.merge_short_blocks or pending is None:
            return False
        if len(candidate.text) >= self.short_block_threshold:
            return False
        if (
            self.policy.packaging.size_hard_limit > 0
            and len(pending.text) + 2 + len(candidate.text)
            > self.policy.packaging.size_hard_limit
        ):
            # d4: merge cap — if merging would exceed the limit, seal the pending
            # block (prereg §2.1.1); pending accumulation is no longer unbounded.
            return False
        if pending.heading_path != candidate.heading_path:
            return False
        if self.preserve_page_scope and pending.page != candidate.page:
            return False
        return True

    @staticmethod
    def _pending_from_block(
        *,
        block: dict[str, Any],
        text: str,
        heading_path: list[str],
        heading_level: int,
    ) -> _PendingBlock:
        bbox = block.get("bbox") or []
        pages = [int(page) for page in block.get("pages", []) if page is not None]
        if not pages and block.get("page") is not None:
            pages = [int(block["page"])]
        raw_bboxes = block.get("bboxes") or ([bbox] if bbox else [])
        evidence_ids = list(block.get("evidence_ids") or [])
        if block.get("evidence_id"):
            evidence_ids.insert(0, block["evidence_id"])
        block_type = str(block.get("type", "text"))
        unit_type = {
            "table": UnitType.TABLE,
            "figure": UnitType.MIXED,
        }.get(block_type, UnitType.NARRATIVE)
        table_facts: dict[str, Any] | None = None
        if block_type == "table":
            # Spec §8: carry the reconstruction layer's derived table
            # structure facts verbatim onto the representation unit.
            table_facts = {
                "table_status": block.get("table_status"),
                "table_status_reasons": list(
                    block.get("table_status_reasons") or []
                ),
                "table_detection_method": block.get("table_detection_method"),
                "column_count": block.get("column_count"),
                "columns_confidence": block.get("columns_confidence"),
                "ragged_row_count": block.get("ragged_row_count"),
                "rows_recovered": block.get("rows_recovered"),
                "raw_region_preserved": block.get("raw_region_preserved"),
            }
        return _PendingBlock(
            text=text,
            unit_type=unit_type,
            page=block.get("page"),
            pages=list(dict.fromkeys(pages)),
            heading_path=list(heading_path),
            heading_level=heading_level,
            bboxes=[list(item) for item in raw_bboxes if item],
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            source_kinds=[str(block.get("source_kind", "native_text"))],
            confidence=float(block.get("confidence", 1.0)),
            merged_from=int(block.get("merged_from", 1)),
            table_facts=table_facts,
        )

    def _make_unit(
        self,
        pending: _PendingBlock,
        *,
        sequence_index: int,
        document_metadata: dict[str, Any],
        label_by_page: dict[int, str] | None = None,
    ) -> RepresentationUnit:
        heading_path = list(pending.heading_path) if self.attach_heading_path else []
        page = pending.page if self.preserve_page_scope else None
        label_by_page = label_by_page or {}
        page_label = (
            label_by_page.get(int(page))
            if isinstance(page, int)
            else None
        )
        semantic_role = (
            "table" if pending.unit_type == UnitType.TABLE
            else "figure" if pending.unit_type == UnitType.MIXED
            else "body"
        )
        return RepresentationUnit(
            text=pending.text,
            unit_type=pending.unit_type,
            sequence_index=sequence_index,
            heading=heading_path[-1] if heading_path else "",
            heading_path=heading_path,
            structural_info=StructuralInfo(
                heading_level=pending.heading_level,
                heading_path=heading_path,
                parent_section=heading_path[-1] if heading_path else None,
                page_number=page,
            ),
            source_context=SourceContext(
                file_name=document_metadata.get("file_name"),
                file_type="pdf",
                page=page,
            ),
            semantic_primitives=SemanticPrimitives(
                identity=f"pdf:{document_metadata.get('sha256', 'unknown')}:{sequence_index}",
                hierarchy_path=heading_path,
                locality_scope="page" if self.preserve_page_scope else "section",
                semantic_role=semantic_role,
                integrity_guarantees=(
                    ["hierarchy_continuity", "structural_block_complete"]
                    if pending.unit_type != UnitType.NARRATIVE
                    else ["hierarchy_continuity", "discourse_locality"]
                ),
                confidence=pending.confidence,
            ),
            metadata={
                # d4: heading key added to metadata (prereg §2.2 family interop
                # alignment — docx/epub already carry it; downstream metadata
                # consumers were observed to be blind without it).
                "heading": heading_path[-1] if heading_path else "",
                "heading_path": heading_path,
                "semantic_role": (
                    "table_content" if pending.unit_type == UnitType.TABLE
                    else "figure_description" if pending.unit_type == UnitType.MIXED
                    else "narrative_content"
                ),
                "source_kind": "pdf_reconstruction",
                "pdf_page": page,
                "pdf_page_label": page_label,
                "pdf_pages": pending.pages if self.preserve_page_scope else [],
                "pdf_bboxes": pending.bboxes,
                "pdf_evidence_ids": pending.evidence_ids,
                "pdf_source_kinds": list(dict.fromkeys(pending.source_kinds)),
                "pdf_merged_blocks": pending.merged_from,
                # Spec §8: table units carry the derived structure facts
                # (table_status / columns_confidence / detection method) so
                # consumers can distrust partial tables explicitly.
                **(
                    {f"pdf_table_{key}": value for key, value in pending.table_facts.items()}
                    if pending.table_facts
                    else {}
                ),
                "representation_semantics": {
                    "evidence_coordinates_preserved": True,
                    "hierarchy_continuity": True,
                    "page_locality": self.preserve_page_scope,
                },
                # d4 size-enforcement provenance (prereg §2.1.4): carried only on the
                # oversized path; zero new metadata keys on the non-oversized path.
                **(
                    {
                        "size_split": True,
                        "split_reason": pending.split_reason
                        or "hard_limit_fallback",
                    }
                    if pending.size_split
                    else {}
                ),
                **(
                    {
                        "oversized": True,
                        "oversized_reason": (
                            "structural_block exceeds hard_limit"
                        ),
                    }
                    if pending.oversized
                    else {}
                ),
            },
        )

    @staticmethod
    def _label_by_page(navigation: Any) -> dict[int, str]:
        """Invert the serialized label map (label → page) to page → label."""
        if not isinstance(navigation, dict):
            return {}
        label_page_map = navigation.get("label_page_map")
        if not isinstance(label_page_map, dict):
            return {}
        by_page: dict[int, str] = {}
        for label, page in label_page_map.items():
            if isinstance(page, int) and page not in by_page:
                by_page[page] = str(label)
        return by_page

    @staticmethod
    def _average_length(units: list[RepresentationUnit]) -> float:
        if not units:
            return 0.0
        return round(sum(len(unit.text) for unit in units) / len(units), 2)
