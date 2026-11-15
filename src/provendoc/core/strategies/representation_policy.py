"""RepresentationPolicy — facade over the layered representation
policy architecture (full spec: docs/*/internal/representation_policy_layers.md).

Layered architecture:

    1. Semantic Invariants (must never be violated):
       - hierarchy_continuity: heading_path must propagate across chunks
       - structural_block_complete: structural blocks (code, tables)
         must never be cut apart
       - discourse_locality: discourse units (paragraphs, list items)
         must not be split across chunks

    2. Representation Policies (how semantic units are organized):
       - boundary_preference: prefer splitting at discourse boundaries
       - integrity_rules: which structures must stay intact
       - context_propagation: selective semantic propagation
         (replaces blind overlap)
       - semantic_density: based on discourse-unit count, not token
         count

    3. Runtime Packaging (split out of SemanticDensity):
       - size_soft_limit / size_hard_limit / chunk_size / overlap
         These are packaging constraints (embedding window /
         retrieval latency / model context); they must not define
         representation truth, only constrain representation
         deployment.

    4. Runtime Scale Policy (huge files are a runtime concern,
       not a chunking strategy):
       - evaluation_weighting: per-document weighting / capped
         contribution
       - representation_scaling: lazy expansion / folded
         visualization
       - density_adaptation: section-level / windowed hierarchy
       - benchmark_tagging: separate taxonomy for huge/stress/
         stream

    5. Optimization Config (runtime behavior):
       - quality_feedback / retry_policy / online_adjustment

Module layout (2026-09 split, zero behavior change):
    - representation_policy_layers.py   — five-layer policy dataclasses
    - representation_policy_runtime.py  — private engine methods for
      the apply loop (mixin)
    - representation_policy.py (this file) — facade dataclass +
      compatibility re-exports

ChunkingStrategy compatibility:
    - chunk_size / overlap are kept, relocated into
      RuntimePackagingPolicy
    - get_effective_chunk_size() / get_effective_overlap() keep
      their signatures
    - from_legacy() keeps its signature and routes internally to
      the packaging layer
"""

from dataclasses import dataclass, field
from typing import Any, List, Tuple

from provendoc.core.strategies.representation_policy_layers import (
    BenchmarkTagging,
    BoundaryPreference,
    BoundaryPrimary,
    CoarseGranularityPolicy,
    ContextPropagation,
    ContextPropagationMode,
    DensityAdaptation,
    EvaluationWeighting,
    FineGranularityPolicy,
    IntegrityRules,
    OptimizationConfig,
    RepresentationScaling,
    RuntimePackagingPolicy,
    RuntimeScalePolicy,
    ScoreObservation,
    SemanticDensity,
    SemanticInvariants,
)
from provendoc.core.strategies.representation_policy_runtime import (
    RepresentationPolicyRuntimeMixin,
)

__all__ = [
    "RepresentationPolicy",
    "SemanticInvariants",
    "BoundaryPreference",
    "BoundaryPrimary",
    "IntegrityRules",
    "ContextPropagation",
    "ContextPropagationMode",
    "SemanticDensity",
    "CoarseGranularityPolicy",
    "RuntimePackagingPolicy",
    "ScoreObservation",
    "EvaluationWeighting",
    "RepresentationScaling",
    "DensityAdaptation",
    "BenchmarkTagging",
    "RuntimeScalePolicy",
    "OptimizationConfig",
]


@dataclass
class RepresentationPolicy(RepresentationPolicyRuntimeMixin):
    """Representation policy — integrates the five-layer
    architecture.

    Responsibility: deciding how semantic units are organized (not
    how they are cut).
    Semantic Layer decides: what must stay together / what belongs
    to the same unit / locality / dependencies / hierarchy.
    Runtime Packaging Layer decides: token budget / retrieval
    granularity / context window.
    """

    name: str = "semantic_baseline"

    # --- Layer 1: Semantic Invariants ---
    invariants: SemanticInvariants = field(default_factory=SemanticInvariants)

    # --- Layer 2: Representation Policies (slimmed) ---
    boundary_preference: BoundaryPreference = field(default_factory=BoundaryPreference)
    integrity_rules: IntegrityRules = field(default_factory=IntegrityRules)
    context_propagation: ContextPropagation = field(default_factory=ContextPropagation)
    semantic_density: SemanticDensity = field(default_factory=SemanticDensity)

    # --- Layer 3: Runtime Packaging (NEW) ---
    packaging: RuntimePackagingPolicy = field(default_factory=RuntimePackagingPolicy)

    # --- Coarse Granularity: structure-first context packs ---
    # Independent of base packaging: coarse organizes context by
    # structure and whole-document relative scale.
    coarse: CoarseGranularityPolicy = field(default_factory=CoarseGranularityPolicy)

    # --- Fine Granularity: chunk_size-budget sentence-level split ---
    # Independent of base packaging: fine splits above the
    # paragraph level by character budget; chunk_size / overlap
    # live here (spec: granularity_redefinition_v1).
    fine: FineGranularityPolicy = field(default_factory=FineGranularityPolicy)

    # --- Layer 4: Runtime Scale Policy (NEW) ---
    scale: RuntimeScalePolicy = field(default_factory=RuntimeScalePolicy)

    # --- Granularity: three-granularity persistence switch ---
    # coarse / base are always produced; fine is gated by this
    # switch. The contract settled on "all three granularities
    # persisted by default", hence the default True.
    enable_fine_grained: bool = True

    # --- Layer 5: Optimization Config ---
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)

    # --- Backward Compatibility (delegate to packaging) ---
    # Legacy field names are kept, but actual storage is delegated
    # to the packaging layer
    paragraph_merge: bool = True
    """[DEPRECATED] 使用 boundary_preference 替代。"""

    preserve_headings: bool = True
    """[DEPRECATED] 使用 invariants.hierarchy_continuity 替代。"""

    collapse_blank_lines: bool = True
    """[DEPRECATED] 清洗行为由 core/cleaning 的 CleaningLevel 控制
    （本字段无运行时效果，仅 from_legacy 兼容镜像）。"""

    long_text_strategy: str = "sliding_window"
    """[DEPRECATED] 超长文本由 packaging 的 size_soft_limit /
    size_hard_limit 控制（本字段无运行时效果，仅兼容镜像）。"""

    # ======================================
    # helpers (delegate to packaging)
    # ======================================

    def get_effective_chunk_size(self) -> int:
        """Return the effective chunk_size (legacy compatibility;
        delegated to packaging)."""
        return self.packaging.get_effective_chunk_size()

    def get_min_chunk_size(self) -> int:
        """Return the chunking soft limit for consumers such as
        quality-evaluation strategies."""
        return self.packaging.get_min_chunk_size()

    def get_effective_overlap(self) -> int:
        """Return the effective overlap size (in characters),
        delegated to the packaging layer."""
        return self.packaging.get_effective_overlap(self.context_propagation)

    @classmethod
    def from_legacy(cls, **kwargs) -> "RepresentationPolicy":
        """Create a RepresentationPolicy from legacy
        ChunkingStrategy parameters.

        chunk_size / overlap are routed into the packaging layer,
        not into semantic_density.

        Example:
            policy = RepresentationPolicy.from_legacy(
                chunk_size=500,
                overlap=50,
                preserve_headings=True,
            )
        """
        policy = cls()

        if "chunk_size" in kwargs:
            cs = kwargs["chunk_size"]
            policy.packaging.chunk_size = cs
            policy.packaging.size_soft_limit = cs
            policy.packaging.size_hard_limit = int(cs * 1.6)
            # semantic_density gets approximate semantic targets only
            policy.semantic_density.discourse_unit_target = max(3, cs // 100)
            policy.semantic_density.paragraph_target = max(2, cs // 150)

        if "overlap" in kwargs:
            ov = kwargs["overlap"]
            policy.packaging.overlap = ov          # 旧字段兼容
            policy.context_propagation.overlap_size = ov  # 新字段同步
            if ov > 0:
                policy.context_propagation.mode = "selective"
            else:
                policy.context_propagation.mode = "none"

        if "preserve_headings" in kwargs:
            policy.invariants.hierarchy_continuity = kwargs["preserve_headings"]
            policy.preserve_headings = kwargs["preserve_headings"]

        if "paragraph_merge" in kwargs:
            policy.paragraph_merge = kwargs["paragraph_merge"]
            policy.boundary_preference.primary = (
                "discourse_unit" if kwargs["paragraph_merge"] else "token_limit"
            )

        if "orphan_threshold" in kwargs:
            policy.packaging.orphan_threshold = kwargs["orphan_threshold"]

        if "long_text_strategy" in kwargs:
            policy.long_text_strategy = kwargs["long_text_strategy"]

        if "collapse_blank_lines" in kwargs:
            policy.collapse_blank_lines = kwargs["collapse_blank_lines"]

        if "name" in kwargs:
            policy.name = kwargs["name"]

        return policy

    @staticmethod
    def coerce_to_policy(strategy: Any) -> "RepresentationPolicy":
        """Coerce an arbitrary strategy object into a
        RepresentationPolicy.

        Removes the duplicated isinstance + from_legacy adapter
        code previously repeated across the 6 representation
        files. Every representation __init__ should call this.

        Args:
            strategy: None | RepresentationPolicy | ChunkingStrategy

        Returns:
            A RepresentationPolicy instance

        Raises:
            TypeError: if strategy is not a supported type
        """
        if strategy is None:
            return RepresentationPolicy()
        elif isinstance(strategy, RepresentationPolicy):
            return strategy
        else:
            # Try ChunkingStrategy duck-typing
            try:
                return RepresentationPolicy.from_legacy(
                    chunk_size=getattr(strategy, 'chunk_size', 500),
                    overlap=getattr(strategy, 'overlap', 50),
                    orphan_threshold=getattr(strategy, 'orphan_threshold', 120),
                    paragraph_merge=getattr(strategy, 'paragraph_merge', True),
                    preserve_headings=getattr(strategy, 'preserve_headings', True),
                    collapse_blank_lines=getattr(strategy, 'collapse_blank_lines', True),
                    long_text_strategy=getattr(strategy, 'long_text_strategy', 'sliding_window'),
                    name=getattr(strategy, 'name', 'legacy'),
                )
            except Exception:
                raise TypeError(
                    f"strategy must be RepresentationPolicy or ChunkingStrategy, "
                    f"got {type(strategy).__name__}"
                )

    # ======================================
    # apply: organize semantic units (three-phase core logic)
    # ======================================

    def apply(self, units: List[Any]) -> List[List[Any]]:
        """Apply the policy and return only the chunks, keeping
        existing callers compatible."""
        chunks, _ = self.apply_with_trace(units)
        return chunks

    def apply_with_trace(
        self, units: List[Any],
    ) -> Tuple[List[List[Any]], dict]:
        """Apply the policy and return a stateless, auditable
        execution trace.

        The trace records only policy constraints and actual
        execution evidence — candidate section pairs, rejection
        reasons, greedy choices and integrity repairs; it does not
        turn policy names or execution preferences into quality
        scores. The caller owns the returned dict, so concurrent
        reuse of one policy instance cannot share or clobber any
        mutable ``last_trace``-style state.
        """
        trace = self._new_execution_trace(units)
        if not units:
            return [], trace

        # ==================================
        # Phase 1: signal enhancement — StructuralSemanticsAnalyzer
        # ==================================
        # Build structural relations, producing the relation graph of
        # "which units must not be separated". These relations
        # supplement the is_separable_from() decision.
        must_merge_pairs = self._build_merge_constraints(units)

        # ==================================
        # Phase 2: boundary decision — greedy grouping + semantic
        # constraints
        # ==================================
        chunks: List[List[Any]] = []
        current_chunk: List[Any] = []
        current_chars = 0

        for i, unit in enumerate(units):
            unit_chars = self._estimate_chars(unit)

            # ── Heading always opens a new chunk ──────────────────────
            # When hierarchy_continuity is enabled, a heading unit starts
            # a fresh chunk so that the heading stays with its own section
            # content rather than dangling at the previous chunk's tail.
            # (A_fixed_window disables hierarchy_continuity → unaffected.)
            if (self.invariants.hierarchy_continuity and current_chunk
                    and hasattr(unit, 'unit_type')
                    and unit.unit_type is not None
                    and unit.unit_type.value == "heading"
                    and not all(getattr(u, 'unit_type', None) is not None
                                and u.unit_type.value == "heading"
                                for u in current_chunk)):
                chunks.append(current_chunk)
                current_chunk = self._propagate_context(current_chunk, unit)
                current_chars = sum(self._estimate_chars(u) for u in current_chunk)

            # Check semantic constraints: is_separable_from +
            # structural relations
            must_add = False

            if current_chunk:
                # Check is_separable_from
                if self.invariants.discourse_locality:
                    last_unit = current_chunk[-1]
                    if hasattr(last_unit, 'is_separable_from') and hasattr(unit, 'is_separable_from'):
                        if not last_unit.is_separable_from(unit):
                            must_add = True

                # Check structural relations (from Phase 1)
                if not must_add and i in must_merge_pairs:
                    must_add = True

                # Check IntegrityRules: structural_block_complete
                if not must_add and self.invariants.structural_block_complete:
                    if self._is_inside_structural_block(current_chunk, unit):
                        must_add = True

            # Check BoundaryPreference — prefer to split at boundaries.
            # Guard: if current_chunk contains only context units freshly
            # propagated by _propagate_context (e.g. "[Context: ...]" heading
            # units), do NOT split again — the boundary decision has already
            # been made for the incoming unit.  Re-splitting here would call
            # _propagate_context a second time, producing duplicate
            # "[Context: ...]" prefixes and orphan context-only chunks.
            only_context = bool(current_chunk) and all(
                getattr(u, 'unit_type', None) is not None
                and u.unit_type.value == "heading"
                and hasattr(u, 'text') and u.text.startswith("[Context:")
                for u in current_chunk
            )
            prefer_split = False
            if current_chunk and not must_add and not only_context:
                # 2026-10-04 policy-collapse fix: the soft limit is a
                # merge constraint (packaging docstring: "below this
                # value no split is triggered, keep merging"), so
                # preferential splitting must first fill the soft
                # limit. Before the fix, any discourse cue triggered a
                # split and scale only capped without merging, so the
                # B/C/D/F/G semantic policies produced byte-identical
                # output and the optimizer's search space existed in
                # name only. Behind the threshold, the scale tiers
                # actually diverge in splitting behavior.
                if current_chars >= self.packaging.size_soft_limit:
                    prefer_split = self._should_split_at_boundary(
                        current_chunk[-1], unit
                    )

            # Char limit check (packaging layer, can be overridden by must_add)
            would_exceed = (current_chars + unit_chars >
                          self.packaging.size_soft_limit)

            if (would_exceed or prefer_split) and not must_add and current_chunk:
                chunks.append(current_chunk)
                current_chunk = self._propagate_context(current_chunk, unit)
                current_chars = sum(self._estimate_chars(u) for u in current_chunk)

            current_chunk.append(unit)
            current_chars += unit_chars

            # Hard limit check
            if (current_chars > self.packaging.size_hard_limit and
                not must_add and len(current_chunk) > 1):
                overflow = current_chunk.pop()
                # If what remains after the pop consists solely of
                # propagated context units, do not emit it as an orphan
                # chunk — the new chunk re-propagates context anyway.
                leftover_is_context_only = bool(current_chunk) and all(
                    getattr(u, 'unit_type', None) is not None
                    and u.unit_type.value == "heading"
                    and hasattr(u, 'text') and u.text.startswith("[Context:")
                    for u in current_chunk
                )
                if not leftover_is_context_only:
                    chunks.append(current_chunk)
                current_chunk = self._propagate_context(current_chunk, overflow)
                current_chunk.append(overflow)
                current_chars = sum(self._estimate_chars(u) for u in current_chunk)

        if current_chunk:
            chunks.append(current_chunk)

        # Keep the policy-stage result separate from later representation
        # transforms such as orphan optimization and context propagation.
        trace["initial_chunk_count"] = len(chunks)
        trace["initial_chunks"] = [
            self._chunk_trace_summary(chunk) for chunk in chunks
        ]

        # Short sibling sections are an exception to the normal
        # heading boundary, not a replacement for it.  The opt-in
        # post-pass can see complete sections and merge only eligible
        # neighboring siblings while retaining their headings.
        chunks = self._merge_short_sibling_sections(
            chunks, trace["short_sibling_merge"],
        )

        # ==================================
        # Phase 3: integrity check — SemanticInvariants
        # ==================================
        # Verify invariants and optionally retry on violation.
        invariant_trace = trace["invariant_enforcement"]
        invariant_trace["enabled"] = (
            self.optimization.retry_on_invariant_violation
        )
        invariant_trace["before_chunk_count"] = len(chunks)
        if self.optimization.retry_on_invariant_violation:
            chunks = self._enforce_invariants(
                units, chunks, invariant_trace["actions"],
            )
        invariant_trace["after_chunk_count"] = len(chunks)

        trace["final_chunk_count"] = len(chunks)
        trace["final_chunks"] = [
            self._chunk_trace_summary(chunk) for chunk in chunks
        ]
        return chunks, trace
