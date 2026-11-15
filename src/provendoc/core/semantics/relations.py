"""Unified Relation Layer — structural relations between representation units.

This is the SINGLE truth source for "what depends on what" in text.
No other module should define relation types or analyzers.

Core idea:
    Not "understand language meaning", but "identify what cannot be separated".

    We don't need to know:
        - Deep philosophical meaning of a paragraph
        - Subject-verb-object structure
        - Semantic role labeling

    We only need to know:
        - This sentence explains the previous one (for_example / therefore)
        - This section belongs to the same heading
        - This code block belongs to this heading
        - This example illustrates the preceding concept
        - This traceback is the same event

Relation primitives (only 3, by design):
    DEPENDENCY:     A depends on B (splitting loses context for A)
    CONTINUITY:     A and B are continuous (same discourse flow)
    LOCALITY:       A and B share a semantic boundary (code block / table / list)

Integration point:
    RepresentationPolicy.apply() Phase 1 calls StructuralSemanticsAnalyzer.analyze(),
    converts StructuralRelation list to merge constraints, injects into greedy
    grouping decisions. Representation files no longer call this module directly.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List


class RelationType(Enum):
    """Structural relation types — minimal primitive set.

    Design principle:
        - Only 3 base relations, avoid discourse ontology explosion
        - Don't distinguish "problem_solution" / "claim_evidence" etc.
        - Only answer "can these be separated?", not "what's the relationship?"

    Three primitives:
        DEPENDENCY:     Explicit or implicit one-way dependency (A depends on B)
        CONTINUITY:     Continuity (A and B are continuation of same discourse flow)
        LOCALITY:       Locality (A and B share same semantic boundary, e.g. code/table)
    """
    DEPENDENCY = auto()
    CONTINUITY = auto()
    LOCALITY = auto()


@dataclass(frozen=True)
class StructuralRelation:
    """A lightweight structural relation.

    Indicates: source unit and target unit should not be split into different chunks.
    """
    source_id: str
    target_id: str
    relation_type: RelationType
    strength: float = 1.0  # 0.0-1.0, how coercive the relation is
    reason: str = ""       # human-readable reason


class StructuralSemanticsAnalyzer:
    """Structural semantics analyzer.

    Based on RepresentationUnit's lightweight signals,
    builds a relation graph of "which units cannot be separated".
    """

    def __init__(self):
        """Initialize the analyzer (relations are rebuilt per analyze call)."""
        self.relations: List[StructuralRelation] = []

    def analyze(self, units: List[Any]) -> List[StructuralRelation]:
        """Analyze unit list, return all structural relations.

        Args:
            units: RepresentationUnit list

        Returns:
            StructuralRelation list
        """
        self.relations = []

        # 1. Analyze discourse cues
        self._analyze_discourse_cues(units)

        # 2. Analyze hierarchy relations
        self._analyze_hierarchy(units)

        # 3. Analyze locality scope
        self._analyze_locality(units)

        # 4. Analyze explicit dependencies
        self._analyze_explicit_dependencies(units)

        # 5. Analyze colon lead-in -> code block binding
        self._analyze_code_intro(units)

        return self.relations

    def _analyze_discourse_cues(self, units: List[Any]):
        """Analyze relations based on discourse cues."""
        for i, unit in enumerate(units):
            if not hasattr(unit, 'semantic_primitives'):
                continue

            cues = unit.semantic_primitives.discourse_cues
            unit_id = unit.semantic_primitives.identity

            if not unit_id:
                continue

            for cue in cues:
                # All discourse cues uniformly map to DEPENDENCY or CONTINUITY
                # Don't distinguish specific cue types, avoid relation explosion
                if cue in ("for_example", "therefore", "because", "in_other_words",
                           "definition", "example"):
                    # These cues indicate source depends on target
                    if i > 0:
                        prev_unit = units[i - 1]
                        if hasattr(prev_unit, 'semantic_primitives'):
                            prev_id = prev_unit.semantic_primitives.identity
                            if prev_id:
                                self.relations.append(StructuralRelation(
                                    source_id=unit_id,
                                    target_id=prev_id,
                                    relation_type=RelationType.DEPENDENCY,
                                    strength=0.9,
                                    reason=f"discourse cue '{cue}' implies dependency on previous",
                                ))

                elif cue in ("continuation", "then", "next", "also", "moreover", "furthermore"):
                    # These cues indicate continuity
                    if i > 0:
                        prev_unit = units[i - 1]
                        if hasattr(prev_unit, 'semantic_primitives'):
                            prev_id = prev_unit.semantic_primitives.identity
                            if prev_id:
                                self.relations.append(StructuralRelation(
                                    source_id=unit_id,
                                    target_id=prev_id,
                                    relation_type=RelationType.CONTINUITY,
                                    strength=0.7,
                                    reason=f"discourse cue '{cue}' implies continuity",
                                ))

    def _analyze_hierarchy(self, units: List[Any]):
        """Analyze relations based on hierarchy_path."""
        for i, unit in enumerate(units):
            if not hasattr(unit, 'semantic_primitives'):
                continue

            unit_id = unit.semantic_primitives.identity
            unit_path = unit.semantic_primitives.hierarchy_path

            if not unit_id or not unit_path:
                continue

            # Compare hierarchy with previous unit
            if i > 0:
                prev_unit = units[i - 1]
                if hasattr(prev_unit, 'semantic_primitives'):
                    prev_id = prev_unit.semantic_primitives.identity
                    prev_path = prev_unit.semantic_primitives.hierarchy_path

                    if prev_id and prev_path:
                        # Check same section / continuity
                        if self._common_prefix(unit_path, prev_path):
                            self.relations.append(StructuralRelation(
                                source_id=unit_id,
                                target_id=prev_id,
                                relation_type=RelationType.CONTINUITY,
                                strength=0.6,
                                reason="Same hierarchy section",
                            ))

                        # Check subsection / continuity
                        if self._is_subpath(prev_path, unit_path):
                            self.relations.append(StructuralRelation(
                                source_id=unit_id,
                                target_id=prev_id,
                                relation_type=RelationType.CONTINUITY,
                                strength=0.7,
                                reason="Unit is subsection of previous",
                            ))

    # Locality scopes that represent *structural* blocks which must not
    # be split apart by chunking.  Plain prose paragraphs ("paragraph") are
    # intentionally excluded — two adjacent paragraphs in the same section
    # are *not* a structural block; they are independent discourse units and
    # should be separable.  Treating paragraphs as a locality group (as the
    # previous version did) chained every paragraph into one giant merge
    # constraint, collapsing B/C/D/E into a single huge chunk on long docs.
    _STRUCTURAL_LOCALITY_SCOPES = frozenset({
        "code_block", "table", "list_group",
    })

    def _analyze_locality(self, units: List[Any]):
        """Analyze relations based on locality_scope.

        Only *structural* scopes (code_block / table / list_group) generate
        LOCALITY relations.  "paragraph" scope is intentionally skipped so
        adjacent prose paragraphs remain separable — otherwise the entire
        document collapses into one merge chain.
        """
        scope_groups: Dict[str, List[str]] = {}

        for unit in units:
            if not hasattr(unit, 'semantic_primitives'):
                continue

            unit_id = unit.semantic_primitives.identity
            scope = unit.semantic_primitives.locality_scope

            if not unit_id or scope == "unknown":
                continue
            # Only structural blocks generate locality constraints.
            if scope not in self._STRUCTURAL_LOCALITY_SCOPES:
                continue

            if scope not in scope_groups:
                scope_groups[scope] = []
            scope_groups[scope].append(unit_id)

        # Build relations between units in same scope
        # All locality scopes uniformly map to LOCALITY
        # Don't distinguish code/table/list, avoid relation explosion
        for scope, unit_ids in scope_groups.items():
            if len(unit_ids) < 2:
                continue

            # Chain connections (adjacent units)
            for i in range(len(unit_ids) - 1):
                self.relations.append(StructuralRelation(
                    source_id=unit_ids[i + 1],
                    target_id=unit_ids[i],
                    relation_type=RelationType.LOCALITY,
                    strength=0.9,
                    reason=f"Same locality scope: {scope}",
                ))

    def _analyze_explicit_dependencies(self, units: List[Any]):
        """Analyze relations based on explicit dependencies."""
        for unit in units:
            if not hasattr(unit, 'semantic_primitives'):
                continue

            unit_id = unit.semantic_primitives.identity
            deps = unit.semantic_primitives.dependencies

            if not unit_id:
                continue

            for dep_id in deps:
                self.relations.append(StructuralRelation(
                    source_id=unit_id,
                    target_id=dep_id,
                    relation_type=RelationType.DEPENDENCY,
                    strength=1.0,
                    reason="Explicit dependency",
                ))

    # Colon lead-in suffixes / phrases: a prose paragraph ending like this
    # introduces the fenced code block that immediately follows it.
    _LEAD_IN_SUFFIXES = (":", "：")
    _LEAD_IN_PHRASES = ("as follows", "as shown below", "like this", "like so", "如下")

    def _analyze_code_intro(self, units: List[Any]):
        """Colon lead-in → code block binding.

        Pattern from real retrieval failures:
            "First, install Alembic and initialize the migration environment:"
            followed immediately by a fenced code block.

        The code block depends on its lead-in paragraph — splitting them
        leaves the code without instructions and the instructions without
        code. Modeled as DEPENDENCY (source = code unit, target = lead-in
        paragraph) so the pair stays in the same chunk.
        """
        for i in range(len(units) - 1):
            curr, nxt = units[i], units[i + 1]
            if not (hasattr(curr, "semantic_primitives") and
                    hasattr(nxt, "semantic_primitives")):
                continue

            # Next unit must be a code block
            if nxt.semantic_primitives.locality_scope != "code_block":
                continue

            # Current unit must be prose (paragraph scope or unknown)
            if curr.semantic_primitives.locality_scope not in ("paragraph", "unknown"):
                continue

            text = (getattr(curr, "text", "") or "").rstrip()
            if not text:
                continue

            text_lower = text.lower()
            is_lead_in = (
                text.endswith(self._LEAD_IN_SUFFIXES)
                or any(text_lower.endswith(p) for p in self._LEAD_IN_PHRASES)
            )
            if not is_lead_in:
                continue

            curr_id = curr.semantic_primitives.identity
            nxt_id = nxt.semantic_primitives.identity
            if curr_id and nxt_id:
                self.relations.append(StructuralRelation(
                    source_id=nxt_id,
                    target_id=curr_id,
                    relation_type=RelationType.DEPENDENCY,
                    strength=0.85,
                    reason="colon lead-in precedes code block",
                ))

    @staticmethod
    def _common_prefix(path1: List[str], path2: List[str]) -> bool:
        """Check if two paths share a common prefix (at least one level)."""
        if not path1 or not path2:
            return False
        min_len = min(len(path1), len(path2))
        return min_len > 0 and path1[:min_len - 1] == path2[:min_len - 1]

    @staticmethod
    def _is_subpath(parent: List[str], child: List[str]) -> bool:
        """Check if child is a subpath of parent."""
        if len(child) <= len(parent):
            return False
        return child[:len(parent)] == parent


def build_locality_boundaries(
    units: List[Any],
    relations: List[StructuralRelation],
    min_strength: float = 0.7,
) -> Dict[str, List[str]]:
    """Build locality boundaries based on strong relations.

    Returns: scope_id → unit_id_list mapping
    """
    # Initialize: each unit is its own boundary
    boundaries: Dict[str, List[str]] = {}
    unit_to_boundary: Dict[str, str] = {}

    for unit in units:
        if not hasattr(unit, 'semantic_primitives'):
            continue
        unit_id = unit.semantic_primitives.identity
        if not unit_id:
            continue
        scope = unit.semantic_primitives.locality_scope
        if scope == "unknown":
            scope = f"auto_{unit_id}"

        if scope not in boundaries:
            boundaries[scope] = []
        boundaries[scope].append(unit_id)
        unit_to_boundary[unit_id] = scope

    # Merge boundaries based on strong relations
    for relation in relations:
        if relation.strength < min_strength:
            continue

        src_scope = unit_to_boundary.get(relation.source_id)
        tgt_scope = unit_to_boundary.get(relation.target_id)

        if src_scope and tgt_scope and src_scope != tgt_scope:
            # Merge tgt_scope into src_scope
            for uid in boundaries[tgt_scope]:
                if uid not in boundaries[src_scope]:
                    boundaries[src_scope].append(uid)
                unit_to_boundary[uid] = src_scope
            boundaries[tgt_scope] = []

    # Remove empty boundaries
    return {k: v for k, v in boundaries.items() if v}
