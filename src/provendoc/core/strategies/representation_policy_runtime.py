"""Runtime engine methods for RepresentationPolicy (mixin).

Contains all the private machinery the apply loop depends on:
- execution tracing (_new_execution_trace / _chunk_trace_summary)
- short sibling section merging (_merge_short_sibling_sections and
  its evidence helpers)
- Phase 1 signal enhancement (_build_merge_constraints)
- boundary decision (_should_split_at_boundary /
  _is_inside_structural_block)
- Phase 3 invariant enforcement (_enforce_invariants /
  _fix_broken_structural_blocks)
- context propagation (_propagate_context) and explanation
  (_explain_merge)

These methods are composed into RepresentationPolicy (see
provendoc.core.strategies.representation_policy) as a mixin; the
fields are provided by that facade dataclass. Method bodies are
behaviorally identical to the pre-split code of 2026-09 — the move
only fixed pre-existing mypy type debt (local variable
annotations), with no runtime behavior change.
"""

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from provendoc.core.strategies.representation_policy_layers import (
        BoundaryPreference,
        ContextPropagation,
        IntegrityRules,
        OptimizationConfig,
        RuntimePackagingPolicy,
        SemanticInvariants,
    )


class RepresentationPolicyRuntimeMixin:
    """Private engine methods for RepresentationPolicy (no
    fields, methods only).

    Fields (invariants / boundary_preference / integrity_rules /
    context_propagation / semantic_density / packaging / coarse /
    scale / optimization / ...) are all declared by the facade
    dataclass RepresentationPolicy; the annotations here only
    provide the contract for static checking.
    """

    if TYPE_CHECKING:
        name: str
        invariants: "SemanticInvariants"
        boundary_preference: "BoundaryPreference"
        integrity_rules: "IntegrityRules"
        context_propagation: "ContextPropagation"
        packaging: "RuntimePackagingPolicy"
        optimization: "OptimizationConfig"

    def _new_execution_trace(self, units: List[Any]) -> dict:
        """Create one serializable trace per execution, never on ``self``."""
        boundary = self.boundary_preference
        return {
            "schema_version": 1,
            "policy_name": self.name,
            "input_unit_count": len(units),
            "policy_constraints": {
                "boundary_preference": {
                    "primary": boundary.primary,
                    "fallback_chain": list(boundary.fallback_chain),
                    "use_structural_relations": (
                        boundary.use_structural_relations
                    ),
                    "merge_short_sibling_sections": (
                        boundary.merge_short_sibling_sections
                    ),
                    "short_section_ratio": boundary.short_section_ratio,
                },
                "packaging": {
                    "size_soft_limit": self.packaging.size_soft_limit,
                    "size_hard_limit": self.packaging.size_hard_limit,
                    "orphan_threshold": self.packaging.orphan_threshold,
                },
                "semantic_invariants": {
                    "hierarchy_continuity": (
                        self.invariants.hierarchy_continuity
                    ),
                    "structural_block_complete": (
                        self.invariants.structural_block_complete
                    ),
                    "discourse_locality": self.invariants.discourse_locality,
                },
                "integrity_rules": {
                    "code_blocks": self.integrity_rules.code_blocks,
                    "tables": self.integrity_rules.tables,
                    "blockquotes": self.integrity_rules.blockquotes,
                    "list_groups": self.integrity_rules.list_groups,
                },
                "context_propagation": {
                    "mode": self.context_propagation.mode,
                    "overlap_size": self.context_propagation.overlap_size,
                },
            },
            "initial_chunk_count": 0,
            "initial_chunks": [],
            "short_sibling_merge": {
                "enabled": boundary.merge_short_sibling_sections,
                "applicable": False,
                "short_section_limit": None,
                "pre_chunk_count": 0,
                "post_chunk_count": 0,
                "merge_count": 0,
                "skipped_reasons": [],
                "iterations": [],
            },
            "invariant_enforcement": {
                "enabled": False,
                "before_chunk_count": 0,
                "after_chunk_count": 0,
                "actions": [],
            },
            "final_chunk_count": 0,
            "final_chunks": [],
        }

    def _chunk_trace_summary(self, chunk: List[Any]) -> dict:
        """Return compact source-structure evidence for one policy chunk."""
        heading_paths = []
        content_unit_count = 0
        context_unit_count = 0
        for unit in chunk:
            if self._is_context_unit(unit):
                context_unit_count += 1
                continue
            content_unit_count += 1
            path = list(getattr(unit, "heading_path", []) or [])
            if path and path not in heading_paths:
                heading_paths.append(path)

        return {
            "unit_count": len(chunk),
            "content_unit_count": content_unit_count,
            "context_unit_count": context_unit_count,
            "char_estimate": self._chunk_chars(chunk),
            "heading_paths": heading_paths,
            "contains_independent_structural_block": (
                self._has_independent_structural_block(chunk)
            ),
        }

    def _merge_short_sibling_sections(
        self, chunks: List[List[Any]], trace: Optional[dict] = None,
    ) -> List[List[Any]]:
        """Greedily merge eligible adjacent short sibling sections.

        The trace captures every evaluated adjacent pair for each greedy
        iteration, including rejected pairs and their structural or capacity
        reasons.  Those records are execution evidence rather than a policy
        quality label.
        """
        if trace is not None:
            trace["pre_chunk_count"] = len(chunks)

        if not self.boundary_preference.merge_short_sibling_sections:
            if trace is not None:
                trace["skipped_reasons"].append("disabled")
                trace["post_chunk_count"] = len(chunks)
            return chunks

        if self.boundary_preference.primary != "heading_boundary":
            if trace is not None:
                trace["skipped_reasons"].append(
                    "requires_heading_boundary"
                )
                trace["post_chunk_count"] = len(chunks)
            return chunks

        if len(chunks) < 2:
            if trace is not None:
                trace["skipped_reasons"].append("fewer_than_two_chunks")
                trace["post_chunk_count"] = len(chunks)
            return chunks

        soft_limit = self.packaging.size_soft_limit
        short_limit = max(
            1,
            int(soft_limit * self.boundary_preference.short_section_ratio),
        )
        if trace is not None:
            trace["applicable"] = True
            trace["short_section_limit"] = short_limit

        merged = [list(chunk) for chunk in chunks]
        iteration_number = 0
        while len(merged) > 1:
            iteration_number += 1
            candidate_pairs: List[Dict[str, Any]] = []
            eligible_candidates: List[Dict[str, Any]] = []
            for index in range(len(merged) - 1):
                left = merged[index]
                right = merged[index + 1]
                left_info = self._section_chunk_info(left)
                right_info = self._section_chunk_info(right)
                left_chars = self._chunk_chars(left)
                right_chars = self._chunk_chars(right)
                combined_chars = left_chars + right_chars
                left_structural = self._has_independent_structural_block(left)
                right_structural = self._has_independent_structural_block(right)
                rejection_reasons = []

                if left_info is None:
                    rejection_reasons.append(
                        "left_not_heading_led_section"
                    )
                if right_info is None:
                    rejection_reasons.append(
                        "right_not_heading_led_section"
                    )
                if left_info is not None and right_info is not None:
                    if left_info["parent_path"] != right_info["parent_path"]:
                        rejection_reasons.append("different_parent_path")
                    if (left_info["heading_level"]
                            != right_info["heading_level"]):
                        rejection_reasons.append("different_heading_level")
                if left_chars >= short_limit:
                    rejection_reasons.append("left_not_short")
                if right_chars >= short_limit:
                    rejection_reasons.append("right_not_short")
                if combined_chars > soft_limit:
                    rejection_reasons.append("combined_exceeds_soft_limit")
                if left_structural:
                    rejection_reasons.append(
                        "left_contains_independent_structural_block"
                    )
                if right_structural:
                    rejection_reasons.append(
                        "right_contains_independent_structural_block"
                    )

                pair = {
                    "left_index": index,
                    "right_index": index + 1,
                    "left": self._chunk_trace_summary(left),
                    "right": self._chunk_trace_summary(right),
                    "combined_char_estimate": combined_chars,
                    "eligible": not rejection_reasons,
                    "rejection_reasons": rejection_reasons,
                    "selected": False,
                }
                candidate_pairs.append(pair)
                if pair["eligible"]:
                    eligible_candidates.append(pair)

            iteration = {
                "iteration": iteration_number,
                "candidate_pairs": candidate_pairs,
                "eligible_candidates": [
                    {
                        "left_index": candidate["left_index"],
                        "right_index": candidate["right_index"],
                        "combined_char_estimate": (
                            candidate["combined_char_estimate"]
                        ),
                    }
                    for candidate in eligible_candidates
                ],
                "selected_merge": None,
            }

            if not eligible_candidates:
                if trace is not None:
                    trace["iterations"].append(iteration)
                break

            selected = min(
                eligible_candidates,
                key=lambda candidate: candidate["combined_char_estimate"],
            )
            selected["selected"] = True
            index = selected["left_index"]
            before_left = self._chunk_trace_summary(merged[index])
            before_right = self._chunk_trace_summary(merged[index + 1])
            combined = self._combine_sibling_sections(
                merged[index], merged[index + 1],
            )
            merged[index] = combined
            del merged[index + 1]
            iteration["selected_merge"] = {
                "left_index": index,
                "right_index": index + 1,
                "selection_basis": "minimum_combined_char_estimate",
                "before": {"left": before_left, "right": before_right},
                "after": self._chunk_trace_summary(combined),
            }
            if trace is not None:
                trace["iterations"].append(iteration)
                trace["merge_count"] += 1

        if trace is not None:
            trace["post_chunk_count"] = len(merged)
        return merged

    def _section_chunk_info(self, chunk: List[Any]) -> Optional[dict]:
        """Return structural identity only for an actual heading-led section."""
        for unit in chunk:
            if self._is_context_unit(unit):
                continue
            unit_type = getattr(unit, "unit_type", None)
            if (unit_type is None
                    or getattr(unit_type, "value", None) != "heading"):
                return None

            path = list(getattr(unit, "heading_path", []) or [])
            if not path:
                return None
            structural_info = getattr(unit, "structural_info", None)
            heading_level = getattr(structural_info, "heading_level", 0)
            return {
                "parent_path": path[:-1],
                "heading_level": heading_level,
            }
        return None

    @staticmethod
    def _is_context_unit(unit: Any) -> bool:
        """Identify synthetic inherited-heading context units."""
        return (
            hasattr(unit, "text")
            and isinstance(unit.text, str)
            and unit.text.startswith("[Context:")
        )

    def _has_independent_structural_block(self, chunk: List[Any]) -> bool:
        """Return whether a section contains a code block or Markdown table.

        Short sibling merging is a granularity optimization, never a
        justification for hiding independently addressable structures inside
        a broader chunk.  Unit types are the canonical signal; text checks
        keep the guard effective for legacy or externally-created units that
        do not carry the full structural taxonomy.
        """
        for unit in chunk:
            if self._is_context_unit(unit):
                continue

            unit_type = getattr(unit, "unit_type", None)
            unit_type_value = getattr(unit_type, "value", unit_type)
            if unit_type_value in {"code", "code_block", "table"}:
                return True

            metadata = getattr(unit, "metadata", {}) or {}
            if metadata.get("discourse_type") in {"code_block", "table"}:
                return True

            text = getattr(unit, "text", "")
            if not isinstance(text, str):
                continue
            if re.search(r"(?m)^\s*(```|~~~)", text):
                return True
            if re.search(
                r"(?m)^\s*\|.+\|\s*$\n^\s*\|\s*:?-{3,}",
                text,
            ):
                return True

        return False

    def _chunk_chars(self, chunk: List[Any]) -> int:
        """Estimate the content character count of a chunk
        (excluding synthetic context prefixes)."""
        return sum(
            self._estimate_chars(unit)
            for unit in chunk
            if not self._is_context_unit(unit)
        )

    def _combine_sibling_sections(
        self, left: List[Any], right: List[Any],
    ) -> List[Any]:
        """Combine siblings and retain a single parent-path context prefix."""
        right_without_context = [
            unit for unit in right
            if not self._is_context_unit(unit)
        ]
        combined = list(left) + right_without_context

        left_info = self._section_chunk_info(left)
        parent_path = left_info["parent_path"] if left_info else []
        if not parent_path:
            return combined

        context_indices = [
            index for index, unit in enumerate(combined)
            if self._is_context_unit(unit)
        ]
        if context_indices:
            # Retain the first prefix only; both sections share this
            # parent and duplicate context would become body noise.
            first_index = context_indices[0]
            context_unit = combined[first_index]
            combined = [
                unit for index, unit in enumerate(combined)
                if not self._is_context_unit(unit) or index == first_index
            ]
            context_unit.text = f"[Context: {' > '.join(parent_path)}]"
            context_unit.heading_path = list(parent_path)
            return combined

        # A merged sibling pair may start a document and therefore not
        # have received propagation during the initial grouping.  Add
        # one explicit parent prefix so the merged representation still
        # carries hierarchy context under heading_only/selective modes.
        if self.context_propagation.mode != "none":
            from provendoc.base.models.representation_unit import (
                RepresentationUnit,
                SemanticRole,
                UnitType,
            )
            combined.insert(0, RepresentationUnit(
                text=f"[Context: {' > '.join(parent_path)}]",
                unit_type=UnitType.HEADING,
                semantic_role=SemanticRole.REFERENCE,
                heading_path=list(parent_path),
            ))
        return combined

    def _build_merge_constraints(self, units: List[Any]) -> dict:
        """Phase 1: build merge constraints from StructuralSemanticsAnalyzer.

        Returns:
            dict mapping unit_index → reason string,
            indicating that units[i] must be merged with previous.
        """
        # Mechanical baseline policies explicitly disable
        # structural-relation merging
        if not self.boundary_preference.use_structural_relations:
            return {}

        constraints = {}
        try:
            from provendoc.core.semantics.relations import (
                RelationType,
                StructuralSemanticsAnalyzer,
            )
            analyzer = StructuralSemanticsAnalyzer()
            relations = analyzer.analyze(units)

            # Build unit_id → index map
            id_to_index = {}
            for i, unit in enumerate(units):
                if hasattr(unit, 'semantic_primitives'):
                    uid = unit.semantic_primitives.identity
                    if uid:
                        id_to_index[uid] = i

            # DEPENDENCY and strong LOCALITY relations → must merge
            for rel in relations:
                if rel.relation_type == RelationType.DEPENDENCY and rel.strength >= 0.8:
                    src_idx = id_to_index.get(rel.source_id)
                    tgt_idx = id_to_index.get(rel.target_id)
                    if src_idx is not None and tgt_idx is not None:
                        # Source must stay with target
                        constraints[src_idx] = rel.reason
                elif rel.relation_type == RelationType.LOCALITY and rel.strength >= 0.9:
                    src_idx = id_to_index.get(rel.source_id)
                    tgt_idx = id_to_index.get(rel.target_id)
                    if src_idx is not None and tgt_idx is not None:
                        if abs(src_idx - tgt_idx) == 1:
                            constraints[max(src_idx, tgt_idx)] = rel.reason
        # If building the constraint graph fails, drop the adjacency
        # constraints (policy-layer degradation, not a hard failure).
        except Exception:
            pass

        return constraints

    def _should_split_at_boundary(self, prev_unit: Any, curr_unit: Any) -> bool:
        """Check BoundaryPreference to decide if we should split here."""
        bp = self.boundary_preference

        if bp.primary == "heading_boundary":
            # Split when current unit is a heading
            if hasattr(curr_unit, 'unit_type'):
                try:
                    if curr_unit.unit_type.value == "heading":
                        return True
                except AttributeError:
                    pass
            # Split when the section (heading_path) changes between
            # adjacent units — section boundaries are heading boundaries.
            if (hasattr(prev_unit, 'heading_path') and
                    hasattr(curr_unit, 'heading_path')):
                prev_path = prev_unit.heading_path or []
                curr_path = curr_unit.heading_path or []
                if prev_path and curr_path and prev_path != curr_path:
                    return True
            return False

        elif bp.primary == "structural_block":
            # Split when locality_scope changes
            if (hasattr(prev_unit, 'semantic_primitives') and
                hasattr(curr_unit, 'semantic_primitives')):
                prev_scope = prev_unit.semantic_primitives.locality_scope
                curr_scope = curr_unit.semantic_primitives.locality_scope
                if (prev_scope != "unknown" and curr_scope != "unknown"
                    and prev_scope != curr_scope):
                    return True
            return False

        elif bp.primary == "discourse_unit":
            # Split at discourse boundaries (blank lines, topic changes)
            if (hasattr(prev_unit, 'semantic_primitives') and
                hasattr(curr_unit, 'semantic_primitives')):
                curr_cues = set(curr_unit.semantic_primitives.discourse_cues)
                # 2026-10-04 dead-code fix: the old check for
                # "transition"/"boundary" looked for names the cue
                # extractor (_extract_discourse_cues) never produces
                # — the preferential split of class-B policies never
                # fired, one root cause of B/C/D being identical.
                # Transition is now decided from actual cue semantics:
                #   contrast (but/however) and therefore (therefore/
                #   so) are discourse moves → split points;
                #   continuation (moreover/furthermore) explicitly
                #   means continuation → no split; for_example /
                #   definition belong to the preceding text → no
                #   split.
                if curr_cues & {"contrast", "therefore"}:
                    return True
            return False

        # token_limit: no preferential split
        return False

    def _is_inside_structural_block(self, chunk: List[Any], unit: Any) -> bool:
        """Check IntegrityRules: is this unit inside a structural block?"""
        if not self.integrity_rules.code_blocks and not self.integrity_rules.tables:
            return False

        if not hasattr(unit, 'semantic_primitives'):
            return False

        unit_scope = unit.semantic_primitives.locality_scope

        # Check if previous unit in chunk is in the same structural block
        if chunk:
            last = chunk[-1]
            if hasattr(last, 'semantic_primitives'):
                last_scope = last.semantic_primitives.locality_scope
                if unit_scope != "unknown" and unit_scope == last_scope:
                    if unit_scope == "code_block" and self.integrity_rules.code_blocks:
                        return True
                    if unit_scope == "table" and self.integrity_rules.tables:
                        return True
                    if unit_scope == "list_group" and self.integrity_rules.list_groups:
                        return True

        return False

    def _enforce_invariants(
        self,
        units: List[Any],
        chunks: List[List[Any]],
        trace_actions: Optional[List[dict]] = None,
    ) -> List[List[Any]]:
        """Phase 3: verify invariants and record only actual repairs."""
        if not chunks:
            return chunks

        result = list(chunks)

        # Check structural_block_complete: no chunk should end or start
        # in the middle of a code block / table.
        if self.invariants.structural_block_complete:
            result = self._fix_broken_structural_blocks(
                result, trace_actions,
            )

        return result

    def _fix_broken_structural_blocks(
        self,
        chunks: List[List[Any]],
        trace_actions: Optional[List[dict]] = None,
    ) -> List[List[Any]]:
        """Merge chunks that break structural block integrity."""
        if len(chunks) < 2:
            return chunks

        fixed = [chunks[0]]

        for i in range(1, len(chunks)):
            prev = fixed[-1]
            curr = chunks[i]

            # Check if prev ends inside a structural block and curr continues it
            should_merge = False
            locality_scope = None
            if prev and curr:
                last = prev[-1]
                first = curr[0]
                if (hasattr(last, 'semantic_primitives') and
                    hasattr(first, 'semantic_primitives')):
                    last_scope = last.semantic_primitives.locality_scope
                    first_scope = first.semantic_primitives.locality_scope
                    if (last_scope != "unknown" and
                        last_scope == first_scope and
                        last_scope in ("code_block", "table", "list_group")):
                        should_merge = True
                        locality_scope = last_scope

            if should_merge:
                before = {
                    "left": self._chunk_trace_summary(prev),
                    "right": self._chunk_trace_summary(curr),
                }
                fixed[-1] = prev + curr
                if trace_actions is not None:
                    trace_actions.append({
                        "action": "merge_broken_structural_block",
                        "locality_scope": locality_scope,
                        "before": before,
                        "after": self._chunk_trace_summary(fixed[-1]),
                    })
            else:
                fixed.append(curr)

        return fixed

    def _explain_merge(self, unit_a: Any, unit_b: Any) -> str:
        """Explain why two units were merged."""
        reasons = []

        # Check locality scope
        if hasattr(unit_a, 'semantic_primitives') and hasattr(unit_b, 'semantic_primitives'):
            a_scope = unit_a.semantic_primitives.locality_scope
            b_scope = unit_b.semantic_primitives.locality_scope
            if a_scope != "unknown" and a_scope == b_scope:
                reasons.append(f"same locality_scope: {a_scope}")

            # Check dependencies
            a_id = unit_a.semantic_primitives.identity
            b_id = unit_b.semantic_primitives.identity
            if b_id and b_id in unit_a.semantic_primitives.dependencies:
                reasons.append(f"{unit_a.semantic_primitives.identity} depends on {b_id}")
            if a_id and a_id in unit_b.semantic_primitives.dependencies:
                reasons.append(f"{unit_b.semantic_primitives.identity} depends on {a_id}")

        if not reasons:
            return "semantic constraint (is_separable_from=False)"
        return "; ".join(reasons)

    def _estimate_chars(self, unit: Any) -> int:
        """Character count of a unit (size is measured in characters,
        the same unit as size_soft/hard_limit)."""
        text = unit.text if hasattr(unit, 'text') else str(unit)
        return len(text)

    def _propagate_context(self, previous_chunk: List[Any], next_unit: Any) -> List[Any]:
        """Prepare context for a new chunk per the
        context_propagation policy."""
        context_units: List[Any] = []

        if self.context_propagation.mode == "none":
            return context_units

        # Collect heading_path context (preserving full hierarchy
        # inheritance):
        # - a heading unit's heading_path ends with the heading
        #   itself → strip it and take the parent path
        # - a content unit's heading_path ends with its section's
        #   heading → keep the full path (otherwise second-level
        #   heading inheritance would be lost: when a split lands
        #   mid-section, the new chunk would keep only the
        #   first-level context)
        # - the new chunk starts with its own heading (heading unit
        #   first) → context is that heading's parent path
        # - keep only the deepest paths (drop prefixes contained by
        #   longer paths, avoiding the redundancy of having both
        #   "[Context: A]" and "[Context: A > B]")
        if self.invariants.hierarchy_continuity:
            from provendoc.base.models.representation_unit import (
                RepresentationUnit,
                SemanticRole,
                UnitType,
            )

            def _is_heading(u) -> bool:
                return (hasattr(u, 'unit_type') and u.unit_type is not None
                        and u.unit_type.value == "heading")

            if _is_heading(next_unit):
                # New chunk starts with a heading: the heading brings
                # its own section, so context only needs the parent
                # path
                parent = next_unit.heading_path[:-1] if (
                    hasattr(next_unit, 'heading_path') and len(next_unit.heading_path) > 1
                ) else []
                paths = [tuple(parent)] if parent else []
            else:
                # New chunk continues the previous environment:
                # inherit the deepest full path of the last chunk
                candidates = set()
                for unit in previous_chunk:
                    if hasattr(unit, 'heading_path') and unit.heading_path:
                        p = tuple(unit.heading_path[:-1]) if _is_heading(unit) else tuple(unit.heading_path)
                        if p:
                            candidates.add(p)
                paths = [p for p in candidates
                         if not any(q != p and q[:len(p)] == p for q in candidates)]
                paths.sort(key=len)

            for path_key in paths:
                context_unit = RepresentationUnit(
                    text=f"[Context: {' > '.join(path_key)}]",
                    unit_type=UnitType.HEADING,
                    semantic_role=SemanticRole.REFERENCE,
                )
                context_units.append(context_unit)

        # Collect dependency context (need-based): only when the
        # first unit of the new chunk depends on a unit in the
        # previous chunk is the depended-on unit copied into the new
        # chunk's context. Typical scenario: when the hard limit
        # force-cuts "lead-in → code block", the code block needs to
        # carry its own lead-in. Note that dependencies must not be
        # taken from the tail of the previous chunk in reverse —
        # that would copy the full text of content that already
        # lives complete in the previous chunk (such as a colon
        # lead-in paragraph), causing duplicated content across
        # adjacent chunks.
        if (self.context_propagation.mode in ("selective", "full") and
            self.context_propagation.semantic_dependency_locality and
            hasattr(next_unit, 'semantic_primitives')):
            for dep_id in next_unit.semantic_primitives.dependencies:
                for prev in previous_chunk:
                    if (hasattr(prev, 'semantic_primitives') and
                        prev.semantic_primitives.identity == dep_id):
                        context_units.append(prev)
                        break

        return context_units
