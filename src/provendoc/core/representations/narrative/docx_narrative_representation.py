# ============================================================
# docx_narrative_representation.py
# Narrative Domain Representation
# ============================================================

from dataclasses import replace

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.representations.narrative.granularity import (
    CoarseGrainedConfig,
    MultiGranularityOutput,
    MultiGranularitySplitter,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    _split_at_hard_fallback,
)
from provendoc.core.strategies.representation_policy import RepresentationPolicy


class DocxNarrativeRepresentation(BaseRepresentation):
    """
        DOCX narrative representation.

        Focus:
        - hierarchy persistence
        - section continuity
        - structural role preservation
    """
    def __init__(
            self,
            strategy=None,
            preserve_heading_context=True,
            max_blocks_per_unit=8
    ):
        """Initialize the DOCX narrative representation.

        Args:
            strategy: RepresentationPolicy or a compatible object (auto-coerced).
            preserve_heading_context: units keep their heading context.
            max_blocks_per_unit: max blocks per unit.
        """
        self.policy = RepresentationPolicy.coerce_to_policy(strategy)
        self.preserve_heading_context = preserve_heading_context
        self.max_blocks_per_unit = max_blocks_per_unit

    # ========================================================
    # public
    # ========================================================
    def build(self,document):
        """Build narrative three-granularity units from docx_blocks.

        toc_entry blocks are TOC navigation evidence (duplicating the
        real headings) and produce no narrative units; the parser
        keeps them verbatim in docx_blocks.

        Args:
            document: Document (metadata["docx_blocks"] holds the block facts).

        Returns:
            RepresentationResult: representation_type="narrative";
            content is an empty MultiGranularityOutput when there are
            no narrative blocks.
        """
        blocks = document.metadata.get("docx_blocks",[])
        # toc_entry blocks are navigation evidence (printed TOC lines); they
        # duplicate the real headings and must not become narrative units.
        # The parser preserves them verbatim in docx_blocks / metadata.
        narrative_blocks = [
            block for block in blocks if block.get("type") != "toc_entry"
        ]
        if not narrative_blocks:
            return RepresentationResult(
                representation_type="narrative",
                content=MultiGranularityOutput(),
                metadata={"unit_count": 0}
            )
        grouped = self._group_blocks(narrative_blocks)
        units = []

        # Source mapping: block index range (start, end) into
        # narrative_blocks (docx_blocks after toc_entry exclusion).  One
        # unit may fold several blocks — the range is the many-to-one
        # source span mapping consumed by the provenance pass.
        #
        # Size enforcement: oversized groups split into multiple
        # units via block-level greedy packing (tables are atomic
        # barriers); a single huge-line block falls back to
        # sentence-aware hard splitting. block_range is recomputed
        # exactly per pack — no overlap, no gap.
        block_cursor = 0
        unit_index = 0
        for group in grouped:
            if not group:
                # defensive: never emit an empty unit (text cannot be empty)
                continue
            group_units = self._build_units_for_group(
                group,
                unit_index,
                document.metadata,
                block_start=block_cursor,
            )
            units.extend(group_units)
            unit_index += len(group_units)
            block_cursor += len(group)

        # three-layer granularity
        granularity_splitter = MultiGranularitySplitter(
            coarse_config=CoarseGrainedConfig.from_policy(self.policy.coarse),
            enable_fine_grained=self.policy.enable_fine_grained,
        )
        granularity_output = granularity_splitter.process(units)

        return RepresentationResult(
            representation_type="narrative",
            content=granularity_output,
            metadata={
                "unit_count": len(units),
                "block_count": len(blocks),
                "granularity": {
                    "coarse_count": len(granularity_output.coarse),
                    "base_count": len(granularity_output.base),
                    "fine_count": len(granularity_output.fine),
                    "fine_enabled": granularity_output.fine_enabled,
                },
                "representation_invariants": [
                    "hierarchy_preserved",
                    "section_locality",
                    "structural_roles_preserved"
                ]
            }
        )

    # ========================================================
    # grouping
    # ========================================================
    def _group_blocks(self,blocks):
        groups = []
        current = []
        current_section = None

        for block in blocks:
            heading_path = tuple(block.get("heading_path",[]))

            # section changed
            if current and current_section is not None and heading_path != current_section:
                groups.append(current)
                current = []
            current_section = heading_path
            current.append(block)

            # soft grouding limit
            if len(current) >= self.max_blocks_per_unit:
                groups.append(current)
                current = []

        if current:
            groups.append(current)
        return groups

    # ========================================================
    # size enforcement
    # ========================================================
    @staticmethod
    def _block_text(block) -> str:
        """Text contribution of a block — isomorphic, char for char, to
        the text_parts construction in _build_unit (heading → '# '
        prefix line; table → [TABLE] + rows; others verbatim), used for
        size-packing length and small-group zero-drift checks."""
        block_type = block["type"]
        if block_type == "heading":
            level = block.get("heading_level", 1)
            return f"{'#' * level} {block['text']}"
        if block_type == "table":
            parts = ["[TABLE]"]
            parts.extend(" | ".join(row) for row in block["rows"])
            return "\n\n".join(parts)
        return block["text"]

    def _build_units_for_group(
            self,
            group,
            start_unit_index,
            source_metadata,
            block_start=0,
    ):
        """group → unit list, adding base-layer size enforcement.

        Semantic alignment (family contract parity): a docx block =
        a Word paragraph = the family's "line" (block-level integrity,
        c4); a table block is an atomic barrier (table atomicity, c2).
        A group within size_hard_limit → single unit via the original
        _build_unit, zero drift; oversized → block-level greedy packing:
        - multi-block packs ≤ limit, each pack size_split=True;
        - a single huge-line block (itself > limit): body/heading →
          sentence-aware hard fallback split into pieces, each piece
          carrying copied provenance + split_reason; table →
          oversized atomic unit, not split (contract keys match the
          family);
        - block_range exact per pack, continuous coverage with no
          overlap and no gap.
        """
        limit = self.policy.packaging.size_hard_limit
        group_len = len("\n\n".join(self._block_text(b) for b in group))
        if limit <= 0 or group_len <= limit:
            return [self._build_unit(
                group, start_unit_index, source_metadata,
                block_start=block_start,
            )]

        # Block-level greedy packing (a table block contributes as one
        # atomic whole and is never split).
        packs = []
        current = []
        current_len = 0
        for block in group:
            block_len = len(self._block_text(block))
            if current and current_len + 2 + block_len > limit:
                packs.append(current)
                current = []
                current_len = 0
            current.append(block)
            current_len = block_len if current_len == 0 else (
                current_len + 2 + block_len
            )
        if current:
            packs.append(current)

        units = []
        unit_index = start_unit_index
        cursor = block_start
        import copy as _copy
        for pack in packs:
            pack_len = len("\n\n".join(self._block_text(b) for b in pack))
            single_oversize = len(pack) == 1 and pack_len > limit
            if single_oversize and pack[0]["type"] == "table":
                # Table atomicity: c2 integrity wins; register the
                # oversize fact as-is.
                unit = self._build_unit(
                    pack, unit_index, source_metadata,
                    block_start=cursor,
                )
                unit.metadata["oversized"] = True
                unit.metadata["oversized_reason"] = (
                    "structural_block exceeds hard_limit"
                )
                units.append(unit)
                unit_index += 1
            elif single_oversize:
                # Single huge body/heading line: sentence-aware hard
                # fallback (same machine as the family: the txt layer's
                # _split_at_hard_fallback).
                unit = self._build_unit(
                    pack, unit_index, source_metadata,
                    block_start=cursor,
                )
                pieces = _split_at_hard_fallback(unit.text, limit)
                if len(pieces) <= 1:
                    # Defensive: if the fallback cannot split (should not
                    # happen), emit as-is.
                    units.append(unit)
                    unit_index += 1
                else:
                    for piece in pieces:
                        piece_unit = replace(
                            unit,
                            text=piece,
                            sequence_index=unit_index,
                            metadata=_copy.deepcopy(unit.metadata),
                        )
                        piece_unit.metadata["size_split"] = True
                        piece_unit.metadata["split_reason"] = (
                            "hard_limit_fallback"
                        )
                        units.append(piece_unit)
                        unit_index += 1
            else:
                # Multi-block pack: greedy packing guarantees ≤ limit;
                # size_split provenance.
                unit = self._build_unit(
                    pack, unit_index, source_metadata,
                    block_start=cursor,
                )
                unit.metadata["size_split"] = True
                units.append(unit)
                unit_index += 1
            cursor += len(pack)
        return units

    # ========================================================
    # build unit
    # ========================================================
    def _build_unit(
            self,
            blocks,
            unit_index,
            source_metadata,
            block_start=0,
    ):
        text_parts = []
        structural_roles = []
        heading_path = []

        for block in blocks:
            block_type = block["type"]
            structural_roles.append(block_type)
            heading_path = block.get("heading_path",[])

            # ================================================
            # heading
            # ================================================
            if block_type == "heading":
                level = block.get("heading_level",1)
                text_parts.append(
                    f"{'#' * level} "
                    f"{block['text']}"
                )
                continue

            # ================================================
            # table
            # ================================================
            if block_type == "table":
                text_parts.append("[TABLE]")

                for row in block["rows"]:
                    text_parts.append(" | ".join(row))

                continue

            # ================================================
            # normal text
            # ================================================
            text_parts.append(block["text"])

        from provendoc.base.models.representation_unit import SemanticPrimitives

        # ================================================
        # node-level source_ref / resolution_status
        # (docx_node_source_ref_prereg_v1 §2.2):
        #   - paragraph_index_range: [min, max] over non-None
        #     paragraph_index / preceding_paragraph_index values of
        #     member blocks; None when the unit has no coordinate at
        #     all (sdt-only units / leading table). The block-level
        #     coordinate authority remains docx_block_range above —
        #     referenced, never duplicated.
        #   - resolution_status: "partial" iff any member image block
        #     was not extracted or any member diagram was not
        #     readable (the only frozen partial fact sources);
        #     document-level docx_unresolved never demotes a unit
        #     (marker_contract §4 node/document separation).
        # ================================================
        paragraph_coords = []
        has_partial_fact = False
        for block in blocks:
            for key in ("paragraph_index", "preceding_paragraph_index"):
                value = block.get(key)
                if value is not None:
                    paragraph_coords.append(value)
            if (
                block.get("type") == "image"
                and not block.get("extracted", True)
            ) or (
                block.get("type") == "diagram"
                and not block.get("readable", True)
            ):
                has_partial_fact = True
        source_ref = {
            "part": "word/document.xml",
            "paragraph_index_range": (
                [min(paragraph_coords), max(paragraph_coords)]
                if paragraph_coords
                else None
            ),
        }

        return RepresentationUnit(
            text="\n\n".join(text_parts),
            unit_type=UnitType.NARRATIVE,
            sequence_index=unit_index,
            heading_path=list(heading_path) if self.preserve_heading_context else [],
            semantic_primitives=SemanticPrimitives(
                identity=(
                    f"docx:{source_metadata.get('sha256', 'unknown')}"
                    f":{unit_index}"
                ),
                hierarchy_path=list(heading_path) if self.preserve_heading_context else [],
                semantic_role="body",
            ),
            source_context=SourceContext(
                file_name=source_metadata.get("file_name"),
                file_type=source_metadata.get("file_type")
            ),
            metadata={
                "block_count": len(blocks),
                "docx_block_range": [block_start, block_start + len(blocks) - 1],
                "structural_roles": sorted(set(structural_roles)),
                "representation_semantics": {
                    "hierarchy_preserved": True,
                    "section_locality": True,
                    "structural_roles_preserved": True
                },
                "source_ref": source_ref,
                "resolution_status": (
                    "partial" if has_partial_fact else "complete"
                ),
                "evidence_type": "native_xml",
            }
        )

