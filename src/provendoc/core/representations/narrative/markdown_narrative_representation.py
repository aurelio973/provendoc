"""MarkdownNarrativeRepresentation — semantic-aware Markdown narrative representation.

Invariants: heading_path propagates at the semantic-unit level, not
just per section (hierarchy_continuity); code blocks, tables and
other structural blocks are never cut (structural_block_complete);
paragraphs and list items are never split across semantic units
(discourse_locality).

Policies: prefer splitting at discourse boundaries
(boundary_preference); selective semantic propagation instead of
blind overlap (context_propagation); semantic density measured in
discourse units, not tokens.

Backward compatible: accepts RepresentationPolicy or duck-typed
objects (normalized via coerce_to_policy).

Embedded-language re-parsing: the parser layer already re-parses
fenced code blocks with tree-sitter and records the evidence in
metadata["embedded_code_blocks"]. This layer aligns discourse code
blocks with that evidence in document order (sha1 verified verbatim;
honest downgrade on mismatch), upgrading matched blocks to
chunking=structural_ast code units — language/symbol/span facts go
into metadata and the structural-block guard applies to their
splitting and orphan merging. Unit text keeps the original fenced
block verbatim: fence lines are source document lines and must
appear in the chunk text; the inner source is derivable from the
row/byte spans in metadata.
"""

import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

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
    FineGrainedConfig,
    MultiGranularityOutput,
    MultiGranularitySplitter,
)

# reuse the txt pipeline's oversize-splitting helpers (single
# source of truth so both narrative pipelines split identically;
# markdown has no headers/footers, so _split_with_furniture is
# not used).
from provendoc.core.representations.narrative.txt_narrative_representation import (
    _is_splittable_prose,
    _split_at_hard_fallback,
    _split_at_line_boundary,
)
from provendoc.core.representations.narrative.unit_position_annotator import (
    annotate_unit_positions as _annotate_positions_impl,
)
from provendoc.core.semantics.relations import RelationType, StructuralSemanticsAnalyzer
from provendoc.core.strategies.representation_policy import RepresentationPolicy
from provendoc.core.text.markdown_blocks import content_digest


class MarkdownNarrativeRepresentation(BaseRepresentation):
    """Semantic-aware Markdown narrative representation layer."""

    def __init__(self, strategy=None, enable_fine_grained: Optional[bool] = None):
        """Initialize with a RepresentationPolicy (or duck-typed object).

        Args:
            strategy: RepresentationPolicy instance (duck-typed objects
                normalized via coerce_to_policy)
            enable_fine_grained: enable the fine-grained layer. None
                (default) follows policy.enable_fine_grained
                (contract default True: all three granularities);
                an explicit value overrides the policy.
        """
        self.policy = RepresentationPolicy.coerce_to_policy(strategy)
        self._enable_fine_grained = enable_fine_grained

        # cache the hard limit (fallback chunking path); overlap is
        # read live from policy.context_propagation.overlap_size
        self._legacy_chunk_size = self.policy.get_effective_chunk_size()

        # three-granularity splitter (lazily created on first build)
        self._multi_granularity_splitter: Optional[MultiGranularitySplitter] = None

    # ========================================
    # public
    # ========================================

    def build(self, document) -> RepresentationResult:
        """Build the chunked representation.

        Args:
            document: Document (text + metadata)

        Returns:
            RepresentationResult with semantic-aware chunks
        """
        text = document.text
        source_metadata = document.metadata

        if not text:
            return self._empty_result()

        # Step 1: semantic split → sections with heading_path
        sections = self._split_sections(text)

        # cursor aligning discourse code blocks with the parser's
        # embedded-code evidence — advances across sections, valid
        # within a single build
        match_state = {
            "evidence": source_metadata.get("embedded_code_blocks") or [],
            "cursor": 0,
        }

        # Step 2: extract RepresentationUnits and analyze structural semantics
        all_units = []
        for section in sections:
            units = self._extract_representation_units(
                section, source_metadata, match_state
            )
            all_units.extend(units)

        # Step 3: use policy grouping and retain its isolated execution trace.
        # Representation transforms below deliberately remain outside the
        # policy trace, because they operate on persisted chunks rather than
        # the policy's source-unit grouping result.
        policy_execution = None
        policy_base_chunks = []
        if all_units and hasattr(self.policy, 'apply_with_trace'):
            unit_chunks, policy_execution = self.policy.apply_with_trace(
                all_units
            )
            unit_chunks = self._isolate_embedded_code_units(unit_chunks)
            chunks = self._units_to_chunks(unit_chunks, source_metadata)
            policy_base_chunks = list(chunks)
        elif all_units and hasattr(self.policy, 'apply'):
            # Compatibility fallback for external policies that have not yet
            # adopted apply_with_trace().
            unit_chunks = self.policy.apply(all_units)
            unit_chunks = self._isolate_embedded_code_units(unit_chunks)
            chunks = self._units_to_chunks(unit_chunks, source_metadata)
            policy_base_chunks = list(chunks)
        else:
            # fallback: legacy discourse-aware chunking
            chunks = []
            chunk_index = 0
            for section in sections:
                section_chunks = self._chunk_section_semantic(
                    section, chunk_index, source_metadata
                )
                chunks.extend(section_chunks)
                chunk_index += len(section_chunks)

        representation_execution = {
            "policy_base_chunk_count": len(policy_base_chunks),
            "before_orphan_optimization_count": len(chunks),
        }

        # Step 4: semantic orphan optimization (not a pure length check)
        chunks = self._optimize_semantic_orphans(chunks)
        representation_execution["after_orphan_optimization_count"] = len(chunks)
        representation_execution["orphan_merge_count"] = sum(
            1 for chunk in chunks if chunk.merged_orphan
        )

        # Step 5: context propagation (selective, not blind overlap)
        chunks = self._apply_context_propagation(chunks)
        representation_execution["after_context_propagation_count"] = len(chunks)
        representation_execution["final_base_chunk_count"] = len(chunks)

        # Step 6: three-granularity output (coarse / base / fine)
        multi_granularity = self._build_multi_granularity(chunks)

        # Step 7: position annotation on base units (additive
        # only — no change to splitting or chunk text; base only,
        # derived coarse/fine granularities untouched)
        _annotate_positions_impl(multi_granularity.base, document)

        return RepresentationResult(
            representation_type="narrative",
            content=multi_granularity,
            metadata={
                "chunk_count": len(multi_granularity.base),
                "avg_chunk_length": round(
                    sum(len(c.text) for c in multi_granularity.base) / max(len(multi_granularity.base), 1), 2
                ),
                "contains_code": any(
                    c.unit_type == UnitType.CODE for c in multi_granularity.base
                ),
                "contains_inline_code": any(
                    "inline_code" in c.inline_semantics
                    for c in multi_granularity.base
                ),
                "transforms_applied": [
                    "section_split",
                    "semantic_block_split",
                    "discourse_boundary_respect",
                    "structural_block_complete",
                    "semantic_orphan_optimization",
                    "context_propagation",
                    "representation_policy_apply",
                    "multi_granularity",
                ],
                "policy_name": self.policy.name,
                # Policy evidence is immutable per build call and describes
                # only policy grouping. representation_execution records
                # later orphan/context transforms that can change base count.
                "policy_execution": policy_execution,
                "representation_execution": representation_execution,
                "invariants_applied": {
                    "hierarchy_continuity": self.policy.invariants.hierarchy_continuity,
                    "structural_block_complete": self.policy.invariants.structural_block_complete,
                    "discourse_locality": self.policy.invariants.discourse_locality,
                },
                "granularity": {
                    "coarse_count": len(multi_granularity.coarse),
                    "base_count": len(multi_granularity.base),
                    "fine_count": len(multi_granularity.fine),
                    "fine_enabled": multi_granularity.fine_enabled,
                },
            }
        )

    # ========================================
    # multi-granularity
    # ========================================

    def _build_multi_granularity(
        self, base_units: List[RepresentationUnit]
    ) -> MultiGranularityOutput:
        """Produce the three-granularity output from base units.

        MultiGranularitySplitter is lazily created on first use.
        enable_fine_grained is passed through from the constructor.
        """
        if self._multi_granularity_splitter is None:
            if self._enable_fine_grained is None:
                fine_enabled = self.policy.enable_fine_grained
            else:
                fine_enabled = self._enable_fine_grained
            self._multi_granularity_splitter = MultiGranularitySplitter(
                coarse_config=CoarseGrainedConfig.from_policy(self.policy.coarse),
                fine_config=FineGrainedConfig.from_policy(self.policy.fine),
                enable_fine_grained=fine_enabled,
            )
        return self._multi_granularity_splitter.process(base_units)

    # ========================================
    # section split (hierarchy-aware)
    # ========================================

    _HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")

    def _split_sections(self, text: str) -> List[Dict[str, Any]]:
        """Split by headings, keeping heading_path."""
        lines = text.splitlines()
        sections = []
        current: Dict[str, Any] = {
            "heading": None,
            "heading_level": 0,
            "heading_path": [],
            "content": [],
        }
        # Store heading levels alongside titles.  A title-only list cannot
        # correctly replace a peer heading when Markdown skips a level (for
        # example ``# Parent`` followed by ``### First`` and ``### Second``).
        # Keeping the source levels means every new heading retains only true
        # ancestors, never a same-level predecessor.
        heading_stack: List[Tuple[int, str]] = []
        in_code_block = False

        for line in lines:
            stripped = line.strip()

            # toggle code block state for both ``` and ~~~
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_code_block = not in_code_block

            match = self._HEADING_PATTERN.match(stripped)
            if match and not in_code_block:
                if current["heading"] or current["content"]:
                    sections.append({
                        **current,
                        "content": "\n".join(current["content"])
                    })

                level = len(match.group(1))
                title = match.group(2).strip()
                heading_stack = [
                    (ancestor_level, ancestor_title)
                    for ancestor_level, ancestor_title in heading_stack
                    if ancestor_level < level
                ]
                heading_stack.append((level, title))
                heading_path = [
                    ancestor_title
                    for _, ancestor_title in heading_stack
                ]

                # The heading line is *document content*, not metadata:
                # keep it as the first line of the section body so it flows
                # through unit extraction into chunk text.  Dropping it (the
                # old behaviour) meant "# Title" never appeared in any chunk.
                current = {
                    "heading": stripped,
                    "heading_level": level,
                    "heading_path": heading_path,
                    "content": [stripped],
                }
            else:
                current["content"].append(line)

        if current["heading"] or current["content"]:
            sections.append({
                **current,
                "content": "\n".join(current["content"])
            })

        return sections

    # ========================================
    # semantic chunking (discourse-aware)
    # ========================================

    def _chunk_section_semantic(
        self,
        section: Dict[str, Any],
        start_index: int,
        source_metadata: Dict[str, Any],
    ) -> List[RepresentationUnit]:
        """Semantically-aware chunking of a single section.

        Core logic: extract discourse units (paragraphs, list items,
        code blocks, ...), combine them by semantic density, respect
        the structural_block_complete invariant, and split at
        discourse boundaries.
        """
        content = section["content"]
        if not content.strip():
            return []

        # extract discourse units
        units = self._extract_discourse_units(content)

        chunks = []
        current_units: List[Dict[str, Any]] = []
        current_size = 0
        chunk_index = start_index

        soft_limit = self.policy.packaging.size_soft_limit
        hard_limit = self.policy.packaging.size_hard_limit

        for i, unit in enumerate(units):
            unit_size = len(unit["text"])
            unit_type = unit["type"]

            # structural-block invariant: code blocks/tables stay whole
            if unit_type in ("code_block", "table"):
                # keep a heading-only prefix with the code block
                # (they belong together)
                heading_only = (
                    current_units
                    and all(u["type"] == "heading" for u in current_units)
                )
                if current_units and not heading_only:
                    # flush non-heading content
                    chunks.append(self._build_chunk_from_units(
                        current_units, chunk_index, section, source_metadata
                    ))
                    chunk_index += 1
                    current_units = []
                    current_size = 0

                # code block (with any preceding heading) goes into one chunk
                current_units.append(unit)
                current_size += unit_size

                # oversized code block: flush immediately (kept whole, flagged oversized)
                if current_size > hard_limit * 2:
                    chunks.append(self._build_chunk_from_units(
                        current_units, chunk_index, section, source_metadata,
                        oversized=True
                    ))
                    chunk_index += 1
                    current_units = []
                    current_size = 0
                else:
                    # flush right after the code block (a natural boundary)
                    chunks.append(self._build_chunk_from_units(
                        current_units, chunk_index, section, source_metadata
                    ))
                    chunk_index += 1
                    current_units = []
                    current_size = 0

                continue

            # check size limits
            projected_size = current_size + unit_size + (2 if current_units else 0)

            if projected_size > hard_limit and current_units:
                # over the hard limit: close the current chunk
                chunks.append(self._build_chunk_from_units(
                    current_units, chunk_index, section, source_metadata
                ))
                chunk_index += 1
                current_units = [unit]
                current_size = unit_size
            elif projected_size > soft_limit and current_units:
                # over the soft limit: split only at a discourse
                # boundary (paragraph / list item)
                if unit_type in ("paragraph", "list_item"):
                    chunks.append(self._build_chunk_from_units(
                        current_units, chunk_index, section, source_metadata
                    ))
                    chunk_index += 1
                    current_units = [unit]
                    current_size = unit_size
                else:
                    # not a discourse boundary: keep accumulating
                    current_units.append(unit)
                    current_size = projected_size
            else:
                current_units.append(unit)
                current_size = projected_size

        # flush remaining units
        if current_units:
            chunks.append(self._build_chunk_from_units(
                current_units, chunk_index, section, source_metadata
            ))

        return chunks

    # heading pattern for discourse unit extraction (within section content)
    _INLINE_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

    def _extract_discourse_units(self, text: str) -> List[Dict[str, Any]]:
        """Extract discourse units — semantic units, not mechanical lines.

        Priority (high → low): heading (standalone unit, never
        swallowed by a paragraph or code block), code_block (fenced,
        kept whole), table (pipe table, kept whole), list_item (may
        span lines), paragraph (broken by any of the above).

        Returns:
            List of {"type": "paragraph" | "code_block" | "table" | "list_item" | "heading",
                     "text": str, "level": int (heading only)}
        """
        units = []
        lines = text.splitlines()
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            stripped = line.strip()

            # ==============================
            # Priority 1: heading (top priority, standalone unit)
            # ==============================
            heading_match = self._INLINE_HEADING_RE.match(stripped)
            if heading_match:
                units.append({
                    "type": "heading",
                    "text": stripped,
                    "level": len(heading_match.group(1)),
                })
                i += 1
                continue

            # ==============================
            # Priority 2: fenced code block (``` or ~~~)
            # ==============================
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = "```" if stripped.startswith("```") else "~~~"
                code_lines = [line]
                i += 1
                while i < n:
                    cl = lines[i]
                    code_lines.append(cl)
                    i += 1
                    if cl.strip().startswith(fence_char):
                        break  # closing fence found
                units.append({
                    "type": "code_block",
                    "text": "\n".join(code_lines),
                })
                continue

            # ==============================
            # Priority 3: table
            # ==============================
            if "|" in stripped and i + 1 < n and "---" in lines[i + 1]:
                table_lines = [line]
                i += 1
                while i < n and "|" in lines[i]:
                    table_lines.append(lines[i])
                    i += 1
                units.append({
                    "type": "table",
                    "text": "\n".join(table_lines),
                })
                continue

            # ==============================
            # Priority 4: list item
            # ==============================
            list_match = re.match(r"^([-*+]|\d+\.)\s+", stripped)
            if list_match:
                item_lines = [line]
                i += 1
                while i < n:
                    next_line = lines[i]
                    next_stripped = next_line.strip()
                    if next_stripped == "":
                        item_lines.append(next_line)
                        i += 1
                        continue
                    if re.match(r"^([-*+]|\d+\.)\s+", next_stripped):
                        break
                    if next_line.startswith("  ") or next_line.startswith("\t"):
                        item_lines.append(next_line)
                        i += 1
                    else:
                        break
                units.append({
                    "type": "list_item",
                    "text": "\n".join(item_lines).rstrip(),
                })
                continue

            # ==============================
            # blank line: skip
            # ==============================
            if stripped == "":
                i += 1
                continue

            # ==============================
            # Priority 5: paragraph
            # break immediately at heading / code fence / table / list
            # ==============================
            para_lines = [line]
            i += 1
            while i < n:
                next_line = lines[i]
                next_stripped = next_line.strip()
                # blank line → paragraph ends
                if next_stripped == "":
                    i += 1
                    break
                # heading → paragraph ends (headings are never swallowed)
                if self._INLINE_HEADING_RE.match(next_stripped):
                    break
                # code fence → paragraph ends
                if next_stripped.startswith("```") or next_stripped.startswith("~~~"):
                    break
                # list item → paragraph ends
                if re.match(r"^([-*+]|\d+\.)\s+", next_stripped):
                    break
                # table → paragraph ends
                if "|" in next_stripped and i + 1 < n and "---" in lines[i + 1]:
                    break
                para_lines.append(next_line)
                i += 1

            units.append({
                "type": "paragraph",
                "text": "\n".join(para_lines),
            })

        return units

    def _build_chunk_from_units(
        self,
        units: List[Dict[str, Any]],
        chunk_index: int,
        section: Dict[str, Any],
        source_metadata: Dict[str, Any],
        oversized: bool = False,
    ) -> RepresentationUnit:
        """Build a RepresentationUnit from discourse units."""
        text_parts = []
        unit_types = []
        for unit in units:
            text_parts.append(unit["text"])
            unit_types.append(unit["type"])

        text = "\n\n".join(text_parts)

        # detect inline semantics
        inline_semantics = self._detect_inline_semantics(text)

        # heading_path propagates at chunk level (hierarchy_continuity)
        heading_path = section.get("heading_path", [])

        metadata = {
            "chunk_index": chunk_index,
            "heading": section.get("heading"),
            "heading_level": section.get("heading_level", 0),
            "heading_path": heading_path,
            "block_type": "mixed" if len(set(unit_types)) > 1 else unit_types[0],
            "unit_types": unit_types,
            "unit_count": len(units),
            "inline_semantics": inline_semantics,
            "source": {
                "file_name": source_metadata.get("file_name"),
                "file_type": source_metadata.get("file_type"),
            },
            "length": len(text),
        }

        if oversized:
            metadata["oversized"] = True
            metadata["oversized_reason"] = "structural_block exceeds hard_limit"

        return RepresentationUnit(text=text, metadata=metadata, unit_type=UnitType.MIXED)

    # ========================================
    # semantic orphan optimization
    # ========================================

    def _optimize_semantic_orphans(self, chunks: List[RepresentationUnit]) -> List[RepresentationUnit]:
        """Semantic orphan optimization — not a pure length check.

        Rules: a lone structural block (code/table) is never merged;
        a short chunk holding a complete semantic unit is kept; a
        short chunk is merged with its neighbour when that breaks no
        invariant; a lead-in / orphan-heading chunk whose next chunk
        is an embedded code block (structural_ast) is merged into
        the CODE chunk, which keeps the CODE form — the lead-in
        narrative is rendered outside the fence on export.
        """
        if len(chunks) <= 1:
            return chunks

        optimized = []
        i = 0

        while i < len(chunks):
            current = chunks[i]
            # policy-path chunks lack length/unit_count/unit_types;
            # fall back to text and component fields to avoid KeyError.
            current_len = current.metadata.get("length", len(current.text))
            current_units = current.metadata.get(
                "unit_count", current.component_count or 1
            )
            current_types = (
                current.metadata.get("unit_types")
                or (current.component_types or [current.unit_type.value])
            )

            # structural blocks are unmergeable; the same holds for
            # structural_ast embedded code chunks
            is_structural = (
                any(t in ("code_block", "table") for t in current_types)
                or (current.metadata or {}).get("chunking") == "structural_ast"
            )

            # orphan detection considers semantic completeness, not
            # just length; size_split slices come from hard-limit
            # splitting and must not be glued back to a neighbour
            # (that would bypass the hard limit).
            is_orphan = (
                current_len < self.policy.packaging.orphan_threshold
                and not is_structural
                and current_units <= 1
                and not (current.metadata or {}).get("size_split")
            )

            if is_orphan and i < len(chunks) - 1:
                next_chunk = chunks[i + 1]
                next_types = next_chunk.component_types or [next_chunk.unit_type.value]

                # merging must not break structural_block_complete;
                # embedded code chunks are likewise never absorbed
                next_is_structural = (
                    any(t in ("code_block", "table") for t in next_types)
                    or (next_chunk.metadata or {}).get("chunking")
                    == "structural_ast"
                )
                # size_split slices are hard-limit products; absorbing
                # an orphan would exceed size_hard_limit — a barrier too.
                next_is_size_split = (next_chunk.metadata or {}).get("size_split")
                # the hard limit is a per-chunk constraint: a
                # non-size_split neighbour close to the limit is a
                # barrier too — the merged product must stay within
                # size_hard_limit.
                merge_fits_hard_limit = (
                    len(current.text) + 2 + len(next_chunk.text)
                    <= self.policy.packaging.size_hard_limit
                )

                if (
                    not next_is_structural
                    and not next_is_size_split
                    and merge_fits_hard_limit
                ):
                    # mergeable
                    merged_text = current.text + "\n\n" + next_chunk.text
                    merged_units = current_units + next_chunk.component_count
                    merged_types = current_types + next_types
                    member_heading_paths = []
                    for source_chunk in (current, next_chunk):
                        source_paths = source_chunk.metadata.get(
                            "member_heading_paths", []
                        )
                        if not source_paths and source_chunk.heading_path:
                            source_paths = [source_chunk.heading_path]
                        for path in source_paths:
                            path = list(path)
                            if path not in member_heading_paths:
                                member_heading_paths.append(path)

                    merged_chunk = RepresentationUnit(
                        text=merged_text,
                        sequence_index=current.sequence_index,
                        heading=current.heading or (current.heading_path[-1] if current.heading_path else ""),
                        heading_path=list(current.heading_path),
                        structural_info=StructuralInfo(
                            heading_level=current.structural_info.heading_level,
                            heading_path=list(current.heading_path),
                            parent_section=current.structural_info.parent_section,
                        ),
                        inline_semantics=sorted(set(
                            current.inline_semantics + next_chunk.inline_semantics
                        )),
                        component_types=merged_types,
                        component_count=merged_units,
                        merged_orphan=True,
                        source_context=current.source_context,
                        metadata={
                            "block_type": "mixed" if len(set(merged_types)) > 1 else merged_types[0],
                            "length": len(merged_text),
                            "unit_count": merged_units,
                            "unit_types": merged_types,
                            "member_heading_paths": member_heading_paths,
                        },
                        unit_type=UnitType.MIXED,
                    )
                    optimized.append(merged_chunk)
                    i += 2
                    continue

                # merge a lead-in / orphan-heading chunk into an
                # embedded code block, keeping the CODE form
                # (structural_ast key + unit_type=CODE); the lead-in
                # renders outside the fence on export. Embedded code
                # blocks only — ordinary discourse code/table chunks
                # render narratively and would lose their layout.
                next_is_embedded_code = (
                    (next_chunk.metadata or {}).get("chunking")
                    == "structural_ast"
                )
                # same hard-limit barrier: reject the merge when the
                # embedded code chunk is near the limit.
                if next_is_embedded_code and merge_fits_hard_limit:
                    merged_text = current.text + "\n\n" + next_chunk.text
                    merged_units = current_units + (
                        next_chunk.component_count or 1
                    )
                    merged_types = current_types + list(next_types)
                    member_heading_paths = []
                    for source_chunk in (current, next_chunk):
                        source_paths = source_chunk.metadata.get(
                            "member_heading_paths", []
                        )
                        if not source_paths and source_chunk.heading_path:
                            source_paths = [source_chunk.heading_path]
                        for path in source_paths:
                            path = list(path)
                            if path not in member_heading_paths:
                                member_heading_paths.append(path)

                    merged_semantic_primitives = list(
                        (current.metadata or {}).get("semantic_primitives", [])
                    ) + list(
                        (next_chunk.metadata or {}).get("semantic_primitives", [])
                    )
                    merged_heading_path = list(
                        current.heading_path or next_chunk.heading_path or []
                    )
                    merged_chunk = RepresentationUnit(
                        text=merged_text,
                        sequence_index=current.sequence_index,
                        heading=current.heading or next_chunk.heading,
                        heading_path=merged_heading_path,
                        structural_info=StructuralInfo(
                            heading_level=current.structural_info.heading_level,
                            heading_path=merged_heading_path,
                            parent_section=current.structural_info.parent_section,
                        ),
                        inline_semantics=sorted(set(
                            current.inline_semantics + next_chunk.inline_semantics
                        )),
                        component_types=merged_types,
                        component_count=merged_units,
                        merged_orphan=True,
                        source_context=current.source_context,
                        metadata={
                            # structural_ast key and code facts
                            # (language/symbols) inherited verbatim
                            # from the code chunk
                            **(next_chunk.metadata or {}),
                            "semantic_primitives": merged_semantic_primitives,
                            "block_type": "code",
                            "length": len(merged_text),
                            "unit_count": merged_units,
                            "unit_types": merged_types,
                            "member_heading_paths": member_heading_paths,
                            "orphan_merged_into_code": True,
                        },
                        unit_type=UnitType.CODE,
                    )
                    optimized.append(merged_chunk)
                    i += 2
                    continue

            # tail orphan: the loop condition i < len(chunks)-1
            # leaves a trailing orphan with no next chunk — merge
            # it back into the previous produced chunk, with the
            # same structural / size_split / hard-limit barriers;
            # no merge when the previous chunk is an embedded code
            # block (a narrative tail has no render semantics for
            # it).
            if (
                is_orphan
                and i == len(chunks) - 1
                and optimized
            ):
                prev_chunk = optimized[-1]
                prev_types = (
                    (prev_chunk.metadata or {}).get("unit_types")
                    or (prev_chunk.component_types or [prev_chunk.unit_type.value])
                )
                prev_is_structural = (
                    any(t in ("code_block", "table") for t in prev_types)
                    or (prev_chunk.metadata or {}).get("chunking")
                    == "structural_ast"
                    or (prev_chunk.metadata or {}).get("size_split")
                )
                prev_len = (prev_chunk.metadata or {}).get(
                    "length", len(prev_chunk.text)
                )
                if (
                    not prev_is_structural
                    and prev_len + 2 + current_len
                    <= self.policy.packaging.size_hard_limit
                ):
                    merged_text = prev_chunk.text + "\n\n" + current.text
                    merged_units = (
                        (prev_chunk.metadata or {}).get(
                            "unit_count", prev_chunk.component_count or 1
                        )
                        + current_units
                    )
                    merged_types = list(prev_types) + list(current_types)
                    member_heading_paths = []
                    for source_chunk in (prev_chunk, current):
                        source_paths = (source_chunk.metadata or {}).get(
                            "member_heading_paths", []
                        )
                        if not source_paths and source_chunk.heading_path:
                            source_paths = [source_chunk.heading_path]
                        for path in source_paths:
                            path = list(path)
                            if path not in member_heading_paths:
                                member_heading_paths.append(path)

                    merged_chunk = RepresentationUnit(
                        text=merged_text,
                        sequence_index=prev_chunk.sequence_index,
                        heading=prev_chunk.heading,
                        heading_path=list(prev_chunk.heading_path),
                        structural_info=StructuralInfo(
                            heading_level=prev_chunk.structural_info.heading_level,
                            heading_path=list(prev_chunk.heading_path),
                            parent_section=prev_chunk.structural_info.parent_section,
                        ),
                        inline_semantics=sorted(set(
                            prev_chunk.inline_semantics + current.inline_semantics
                        )),
                        component_types=merged_types,
                        component_count=merged_units,
                        merged_orphan=True,
                        source_context=prev_chunk.source_context,
                        metadata={
                            "block_type": (
                                "mixed"
                                if len(set(merged_types)) > 1
                                else merged_types[0]
                            ),
                            "length": len(merged_text),
                            "unit_count": merged_units,
                            "unit_types": merged_types,
                            "member_heading_paths": member_heading_paths,
                        },
                        unit_type=UnitType.MIXED,
                    )
                    optimized[-1] = merged_chunk
                    i += 1
                    continue

            optimized.append(current)
            i += 1

        # renumber
        for idx, chunk in enumerate(optimized):
            chunk.sequence_index = idx

        return optimized

    # ========================================
    # context propagation (replaces overlap)
    # ========================================

    def _apply_context_propagation(self, chunks: List[RepresentationUnit]) -> List[RepresentationUnit]:
        """Apply selective context propagation (instead of blind overlap).

        Modes: none (no propagation); heading_only (heading_path
        only); selective (heading_path + the previous chunk's last
        sentence); full (overlap-like, but semantic-aware).
        """
        mode = self.policy.context_propagation.mode
        if mode == "none" or len(chunks) <= 1:
            return chunks

        if mode == "heading_only":
            # every chunk already has heading_path from build
            return chunks

        if mode == "selective":
            # selective: heading_path + the previous chunk's last sentence
            for i in range(1, len(chunks)):
                prev_chunk = chunks[i - 1]
                curr_chunk = chunks[i]

                # inherit heading_path from the previous chunk when missing
                if not curr_chunk.heading_path:
                    curr_chunk.heading_path = list(prev_chunk.heading_path)
                    if curr_chunk.semantic_primitives.hierarchy_path != curr_chunk.heading_path:
                        curr_chunk.semantic_primitives.hierarchy_path = list(curr_chunk.heading_path)

                # propagate the previous paragraph's last sentence (when relevant)
                prev_text = prev_chunk.text
                last_sentence = self._extract_last_sentence(prev_text)
                if last_sentence and len(last_sentence) < 200:
                    curr_chunk.context_from_previous = last_sentence

            return chunks

        if mode == "full":
            # full: overlap-like, but only semantically relevant parts
            overlap_size = self.policy.context_propagation.overlap_size
            for i in range(1, len(chunks)):
                prev_chunk = chunks[i - 1]
                curr_chunk = chunks[i]

                prev_text = prev_chunk.text
                if len(prev_text) > overlap_size:
                    overlap_text = prev_text[-overlap_size:]
                    # trim overlap_text to a sentence boundary
                    overlap_text = self._trim_to_sentence_start(overlap_text)
                    curr_chunk.overlap_context = overlap_text

            return chunks

        return chunks

    def _extract_last_sentence(self, text: str) -> Optional[str]:
        """Extract the last sentence of the text."""
        # simple split on sentence-final punctuation
        sentences = re.split(r'(?<=[.!?。！？])\s+', text)
        if sentences:
            last = sentences[-1].strip()
            if len(last) > 10:  # drop tiny fragments
                return last
        return None

    def _trim_to_sentence_start(self, text: str) -> str:
        """Trim text to a sentence start (avoid mid-word cuts)."""
        # keep the text after the first sentence boundary
        match = re.search(r'(?<=[.!?。！？])\s+', text)
        if match:
            return text[match.end():]
        return text

    # ========================================
    # inline semantics detection
    # ========================================

    def _detect_inline_semantics(self, text: str) -> List[str]:
        """Detect inline semantic markers."""
        semantics = []
        if re.search(r"`[^`\n]+`", text):
            semantics.append("inline_code")
        if re.search(r"\[.+?\]\(.+?\)", text):
            semantics.append("markdown_link")
        if re.search(r"(\*\*.+?\*\*)|(\*.+?\*)", text):
            semantics.append("emphasis")
        return semantics

    # ========================================
    # helpers
    # ========================================

    # ========================================
    # RepresentationUnit extraction
    # ========================================

    def _extract_representation_units(
        self,
        section: Dict[str, Any],
        source_metadata: Dict[str, Any],
        match_state: Optional[Dict[str, Any]] = None,
    ) -> List[RepresentationUnit]:
        """Extract RepresentationUnits from a section.

        Discourse units are upgraded to RepresentationUnits carrying
        semantic primitives. match_state carries the embedded-code
        evidence and alignment cursor (see build).
        """
        content = section["content"]
        if not content.strip():
            return []

        discourse_units = self._extract_discourse_units(content)
        heading_path = section.get("heading_path", [])
        heading = section.get("heading", "")
        heading_level = section.get("heading_level", 0)

        units = []
        for i, du in enumerate(discourse_units):
            unit_type = self._map_discourse_type_to_unit_type(du["type"])

            # build semantic primitives
            sp = SemanticPrimitives(
                identity=f"{heading or 'sec'}_u{i}",
                hierarchy_path=list(heading_path),
                locality_scope=self._infer_locality_scope(du["type"]),
                semantic_role=self._infer_semantic_role(du["text"]),
                discourse_cues=self._extract_discourse_cues(du["text"]),
            )

            # code blocks and tables get the integrity guarantee automatically
            if du["type"] in ("code_block", "table"):
                sp.integrity_guarantees.append("structural_block_complete")

            unit = RepresentationUnit(
                text=du["text"],
                unit_type=unit_type,
                semantic_primitives=sp,
                heading_path=list(heading_path),
                structural_info=StructuralInfo(
                    heading_level=heading_level,
                    heading_path=list(heading_path),
                    parent_section=heading_path[-1] if heading_path else None,
                    position_in_parent=i,
                ),
                source_context=SourceContext(
                    file_name=source_metadata.get("file_name"),
                    file_type=source_metadata.get("file_type"),
                ),
                metadata={
                    "discourse_type": du["type"],
                    "section_heading": heading,
                },
            )
            if du["type"] == "code_block" and match_state is not None:
                self._upgrade_embedded_code_unit(unit, du["text"], match_state)
            # prose units over the hard limit are split at line
            # boundaries here (same contract as the txt pipeline);
            # see _split_oversized_prose_unit. Structural blocks
            # (code_block/table/list_item) stay atomic.
            units.extend(self._split_oversized_prose_unit(unit, du["type"]))

        # analyze structural semantic relations (for is_separable_from)
        analyzer = StructuralSemanticsAnalyzer()
        relations = analyzer.analyze(units)

        # store relation info in unit metadata (for policy.apply())
        relation_map: Dict[Any, List[Any]] = {}
        for r in relations:
            if r.relation_type in (RelationType.DEPENDENCY, RelationType.LOCALITY):
                if r.source_id not in relation_map:
                    relation_map[r.source_id] = []
                relation_map[r.source_id].append(r.target_id)

        for unit in units:
            uid = unit.semantic_primitives.identity
            if uid in relation_map:
                unit.semantic_primitives.dependencies.extend(relation_map[uid])
                # dedupe
                unit.semantic_primitives.dependencies = list(
                    dict.fromkeys(unit.semantic_primitives.dependencies)
                )

        return units

    def _split_oversized_prose_unit(
        self, unit: RepresentationUnit, discourse_type: str
    ) -> List[RepresentationUnit]:
        """Line-boundary splitting of prose units over the hard limit
        (same contract as the txt pipeline).

        - A splittable paragraph over packaging.size_hard_limit is
          greedily split at line boundaries (never mid-line).
        - A single giant line falls back to \n\n→\n→space→hard cut;
          the size contract then outweighs line integrity and
          split_reason records the provenance.
        - Fenced code blocks, paragraphs with pipe-table rows, and
          structural units (list_item/heading) stay atomic and are
          only flagged oversized for downstream diagnostics.
        - Slices get metadata size_split=True, a merge barrier:
          orphan optimization must not glue an oversize slice back
          to a neighbour (that would bypass the hard limit).
        """
        hard_limit = self.policy.packaging.size_hard_limit
        text = unit.text or ""
        if hard_limit <= 0 or len(text) <= hard_limit:
            return [unit]
        if discourse_type != "paragraph" or not _is_splittable_prose(text):
            # structural block over the limit: atomicity wins; only
            # record the fact for downstream diagnostics.
            unit.metadata["oversized"] = True
            unit.metadata["oversized_reason"] = "structural_block_exceeds_hard_limit"
            return [unit]

        pieces: List[str] = []
        hard_fallback = False
        for piece in _split_at_line_boundary(text, hard_limit):
            if len(piece) > hard_limit:
                pieces.extend(_split_at_hard_fallback(piece, hard_limit))
                hard_fallback = True
            else:
                pieces.append(piece)
        if len(pieces) <= 1:
            # defensive: len(text) > hard_limit should always yield
            # multiple pieces; if not, keep the unit unchanged.
            return [unit]

        identity = unit.semantic_primitives.identity
        result: List[RepresentationUnit] = []
        for k, piece_text in enumerate(pieces):
            meta = dict(unit.metadata or {})
            meta["size_split"] = True
            if hard_fallback:
                meta["split_reason"] = "hard_limit_fallback"
            result.append(RepresentationUnit(
                text=piece_text,
                unit_type=unit.unit_type,
                semantic_primitives=replace(
                    unit.semantic_primitives,
                    identity=f"{identity}#p{k}",
                ),
                semantic_role=unit.semantic_role,
                discourse_signals=list(unit.discourse_signals),
                heading_path=list(unit.heading_path),
                heading=unit.heading,
                structural_info=replace(unit.structural_info),
                source_context=(
                    replace(unit.source_context)
                    if unit.source_context is not None
                    else SourceContext()
                ),
                metadata=meta,
            ))
        return result

    # ========================================
    # embedded code unit upgrade (embedded-language re-parsing)
    # ========================================

    # backend statuses eligible for embedded code upgrade (same as the parser layer)
    _EMBEDDED_UPGRADE_STATUSES = {"ok", "recovered"}

    @staticmethod
    def _is_embedded_code_unit(unit: RepresentationUnit) -> bool:
        """Whether the unit is an upgraded embedded code block."""
        return (unit.metadata or {}).get("chunking") == "structural_ast"

    def _upgrade_embedded_code_unit(
        self,
        unit: RepresentationUnit,
        block_text: str,
        match_state: Dict[str, Any],
    ) -> None:
        """Align parser evidence in order; upgrade verified code blocks to code units.

        The evidence list contains code blocks in document order,
        mirroring this layer's discourse scan; verbatim consistency
        is sha1-verified, and a mismatch degrades honestly (the
        unit keeps its narrative form — no guessing). Upgraded text
        keeps the original fenced block verbatim (fence lines are
        source document lines); the exporter renders fenced text
        as-is without re-nesting.
        """
        evidence = match_state.get("evidence") or []
        if not evidence:
            return
        cursor = match_state.get("cursor", 0)
        lines = block_text.splitlines()

        def derived_inner(ev: Dict[str, Any]) -> str:
            """Derive the fenced inner content (minus opener/closing lines)."""
            # mirrors parser-layer extract_fenced_code_blocks
            # content: drop the opener line; drop the last line
            # when the fence is closed
            if ev.get("closed"):
                inner_lines = lines[1:-1] if len(lines) >= 2 else []
            else:
                inner_lines = lines[1:]
            return "\n".join(inner_lines)

        matched = None
        # try the cursor position first, then a small forward window
        # for sha alignment (tolerates stray block mismatches)
        for idx in range(cursor, min(cursor + 4, len(evidence))):
            ev = evidence[idx]
            inner = derived_inner(ev)
            if ev.get("content_sha1") == content_digest(inner):
                matched = ev
                match_state["cursor"] = idx + 1
                break
        if matched is None:
            # skip upgrading this block; advance the cursor anyway so
            # later blocks can still align
            match_state["cursor"] = min(cursor + 1, len(evidence))
            return
        if matched.get("status") not in self._EMBEDDED_UPGRADE_STATUSES:
            return  # degraded block: evidence already recorded by the parser

        # keep the original fenced block as the unit text (fence
        # lines are source document lines); consumers needing the
        # inner source derive it from the metadata line/byte spans.
        unit.metadata.update({
            "chunking": "structural_ast",
            "chunk_kind": "embedded_code_block",
            "language": matched.get("language"),
            "embedded_source": "markdown_fence",
            "embedded_status": matched.get("status"),
            "symbol_count": matched.get("symbol_count", 0),
            "symbols": matched.get("symbols", []),
            "diagnostic_count": matched.get("diagnostic_count", 0),
            "line_start": matched.get("line_start"),
            "line_end": matched.get("line_end"),
            "byte_start": matched.get("byte_start"),
            "byte_end": matched.get("byte_end"),
        })

    @staticmethod
    def _is_context_unit(unit: RepresentationUnit) -> bool:
        """Whether the unit is a policy-injected inherited-heading context prefix."""
        return (
            isinstance(unit.text, str)
            and unit.text.startswith("[Context:")
        )

    @staticmethod
    def _isolate_embedded_code_units(
        unit_chunks: List[List[RepresentationUnit]],
    ) -> List[List[RepresentationUnit]]:
        """Split embedded code units out of mixed groups into their own groups.

        A code unit must be its own chunk (unit_type=CODE with
        structural_ast metadata) for fenced rendering on export;
        merging it with narrative text would fall back to the MIXED
        path and lose the fence. Splitting preserves document order.

        Breadcrumb inheritance: the leading "[Context: ...]" unit
        describing the inherited heading path is copied into the
        code group (the narrative group keeps the original); if the
        group holds only context prefixes, they move wholesale to
        avoid an orphaned breadcrumb fragment.
        """
        result: List[List[RepresentationUnit]] = []
        for group in unit_chunks:
            if len(group) == 1:
                result.append(group)
                continue
            buffer: List[RepresentationUnit] = []
            for unit in group:
                if MarkdownNarrativeRepresentation._is_embedded_code_unit(unit):
                    leading_ctx: List[RepresentationUnit] = []
                    for buffered in buffer:
                        if MarkdownNarrativeRepresentation._is_context_unit(buffered):
                            leading_ctx.append(buffered)
                        else:
                            break
                    if leading_ctx and len(leading_ctx) == len(buffer):
                        # only context prefixes in the group → move instead of copy
                        buffer = []
                    code_group = list(leading_ctx) + [unit]
                    if buffer:
                        result.append(buffer)
                        buffer = []
                    result.append(code_group)
                else:
                    buffer.append(unit)
            if buffer:
                result.append(buffer)
        return result

    def _map_discourse_type_to_unit_type(self, discourse_type: str) -> UnitType:
        """Map a discourse type to a UnitType."""
        mapping = {
            "paragraph": UnitType.NARRATIVE,
            "code_block": UnitType.CODE,
            "table": UnitType.TABLE,
            "list_item": UnitType.LIST,
            "heading": UnitType.HEADING,
        }
        return mapping.get(discourse_type, UnitType.NARRATIVE)

    def _infer_locality_scope(self, discourse_type: str) -> str:
        """Infer the locality scope."""
        if discourse_type == "code_block":
            return "code_block"
        elif discourse_type == "table":
            return "table"
        elif discourse_type == "list_item":
            return "list_group"
        elif discourse_type == "paragraph":
            return "paragraph"
        return "unknown"

    def _infer_semantic_role(self, text: str) -> str:
        """Infer the semantic role from text (lightweight)."""
        text_lower = text.lower()
        if any(m in text_lower for m in ["例如", "比如", "for example", "e.g."]):
            return "example"
        elif any(m in text_lower for m in ["定义", "是指", "is defined as", "definition"]):
            return "definition"
        elif any(m in text_lower for m in ["步骤", "step", "1.", "2."]):
            return "procedure_step"
        elif text.strip().startswith("#"):
            return "heading"
        return "body"

    def _extract_discourse_cues(self, text: str) -> List[str]:
        """Extract discourse cues (lightweight)."""
        cues = []
        text_lower = text.lower()
        cue_map = {
            "for_example": ["例如", "比如", "for example", "e.g.", "举例"],
            "therefore": ["因此", "所以", "thus", "therefore", "hence"],
            "definition": ["定义", "是指", "is defined as", "refers to"],
            "continuation": ["此外", "另外", " moreover", "furthermore", "also"],
            "contrast": ["但是", "然而", "but", "however", "nevertheless"],
        }
        for cue_type, markers in cue_map.items():
            if any(m in text_lower for m in markers):
                cues.append(cue_type)
        return cues

    def _units_to_chunks(
        self,
        unit_chunks: List[List[RepresentationUnit]],
        source_metadata: Dict[str, Any],
    ) -> List[RepresentationUnit]:
        """Convert grouped RepresentationUnits into a chunk list."""
        chunks = []
        for chunk_index, unit_group in enumerate(unit_chunks):
            # an embedded code unit in its own group passes through
            # unchanged: unit_type=CODE + structural_ast metadata is
            # the key for fenced rendering and downstream structural
            # guards — never rebuilt as a MIXED narrative chunk. The
            # group may carry leading "[Context: ...]" breadcrumbs
            # (inherited on isolation); they enter the text and
            # render outside the fence on export while the chunk
            # stays a pure CODE structural block.
            if (
                unit_group
                and self._is_embedded_code_unit(unit_group[-1])
                and all(
                    self._is_context_unit(u) for u in unit_group[:-1]
                )
            ):
                single = unit_group[-1]
                heading_path = list(single.heading_path or [])
                group_text = "\n\n".join(u.text for u in unit_group)
                chunks.append(RepresentationUnit(
                    text=group_text,
                    unit_type=UnitType.CODE,
                    sequence_index=chunk_index,
                    heading=heading_path[-1] if heading_path else "",
                    heading_path=heading_path,
                    structural_info=StructuralInfo(
                        heading_level=single.structural_info.heading_level,
                        heading_path=heading_path,
                        parent_section=single.structural_info.parent_section,
                    ),
                    inline_semantics=[],
                    component_types=["code"],
                    component_count=1,
                    source_context=replace(single.source_context),
                    metadata={
                        **(single.metadata or {}),
                        "semantic_primitives": [
                            unit.semantic_primitives.to_dict()
                            for unit in unit_group
                        ],
                        "member_heading_paths": [heading_path] if heading_path else [],
                        "context_prefix_count": len(unit_group) - 1,
                    },
                ))
                continue

            text_parts = []
            unit_types = []
            heading_path = []
            member_heading_paths = []

            for unit in unit_group:
                text_parts.append(unit.text)
                unit_types.append(unit.unit_type.value)
                if unit.heading_path:
                    heading_path = unit.heading_path
                    # Context prefixes describe inherited navigation, not a
                    # source section covered by this chunk.  Excluding them
                    # keeps the provenance list focused on real sections
                    # while retaining the legacy final heading_path above.
                    if not unit.text.startswith("[Context:"):
                        path = list(unit.heading_path)
                        if path not in member_heading_paths:
                            member_heading_paths.append(path)

            text = "\n\n".join(text_parts)

            # detect inline semantics
            inline_semantics = self._detect_inline_semantics(text)

            metadata = {
                # keep semantic primitives for downstream consumers
                "semantic_primitives": [
                    unit.semantic_primitives.to_dict() for unit in unit_group
                ],
                # A short-sibling merge may legitimately contain
                # more than one section.  Keep every source path
                # explicitly instead of silently exposing only the
                # final unit's legacy heading_path.
                "member_heading_paths": member_heading_paths,
            }
            # propagate the oversize/structural-block provenance
            # flags to the chunk level (orphan merging treats
            # size_split as a barrier).
            if any((u.metadata or {}).get("size_split") for u in unit_group):
                metadata["size_split"] = True
                if any(
                    (u.metadata or {}).get("split_reason") for u in unit_group
                ):
                    metadata["split_reason"] = "hard_limit_fallback"
            if any((u.metadata or {}).get("oversized") for u in unit_group):
                metadata["oversized"] = True

            chunks.append(RepresentationUnit(
                text=text,
                unit_type=UnitType.MIXED if len(set(unit_types)) > 1 else UnitType(unit_types[0]),
                sequence_index=chunk_index,
                heading_path=list(heading_path),
                heading=heading_path[-1] if heading_path else "",
                component_types=unit_types,
                component_count=len(unit_group),
                inline_semantics=inline_semantics,
                source_context=SourceContext(
                    file_name=source_metadata.get("file_name"),
                    file_type=source_metadata.get("file_type"),
                ),
                metadata=metadata,
            ))

        return chunks

    # ========================================
    # helpers
    # ========================================

    def _empty_result(self) -> RepresentationResult:
        """Result for an empty document."""
        return RepresentationResult(
            representation_type="narrative",
            content=[],
            metadata={
                "chunk_count": 0,
                "avg_chunk_length": 0,
                "contains_code": False,
                "contains_inline_code": False,
                "transforms_applied": [],
                "policy_name": self.policy.name,
            }
        )
