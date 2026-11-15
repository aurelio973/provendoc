"""Five-layer policy dataclasses for RepresentationPolicy.

Layered definition (aligned with
docs/*/internal/representation_policy_layers.md):

    1. Semantic Invariants (must never be violated):
       - hierarchy_continuity: heading_path must propagate across
         chunks
       - structural_block_complete: structural blocks (code,
         tables) must never be cut apart
       - discourse_locality: discourse units (paragraphs, list
         items) must not be split across chunks

    2. Representation Policies (how semantic units are organized):
       - boundary_preference: prefer splitting at discourse
         boundaries
       - integrity_rules: which structures must stay intact
       - context_propagation: selective semantic propagation
         (replaces blind overlap)
       - semantic_density: based on discourse-unit count, not token
         count

    3. Runtime Packaging (split out of SemanticDensity):
       - size_soft_limit / size_hard_limit / chunk_size / overlap
       - coarse: structure-first context packs (independent of base
         packaging)
       These are packaging constraints (embedding window / retrieval
       latency / model context); they must not define representation
       truth, only constrain representation deployment.

    4. Runtime Scale Policy (huge files are a runtime concern,
       not a chunking strategy):
       - evaluation_weighting: per-document weighting / capped
         contribution
       - representation_scaling: lazy expansion / folded
         visualization
       - density_adaptation: section-level / windowed hierarchy
       - benchmark_tagging: separate taxonomy for huge/stress/stream

    5. Optimization Config (runtime behavior):
       - quality_feedback / retry_policy / online_adjustment

The facade class RepresentationPolicy (assembling these layers and
running the apply loop) lives in
provendoc.core.strategies.representation_policy.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional

# Legal values for boundary primary / context propagation mode
# (for config-layer validation and narrowing).
BoundaryPrimary = Literal[
    "discourse_unit", "heading_boundary", "structural_block", "token_limit",
]
ContextPropagationMode = Literal["none", "selective", "heading_only", "full"]


# ======================================
# 1. Semantic Invariants
# ======================================

@dataclass
class SemanticInvariants:
    """Semantic relations that must never be broken."""

    hierarchy_continuity: bool = True
    """heading_path 必须在 chunk 之间传播。
    如果 chunk A 属于 "## Section 1"，chunk B 也必须携带这个上下文，
    除非 chunk B 明确属于另一个 heading。
    """

    structural_block_complete: bool = True
    """结构块（代码块、表格、引用块）必须保持完整。
    不可在代码块中间切断，不可把表格头行和 body 行分开。
    """

    discourse_locality: bool = True
    """话语单元（段落、列表项、定义-示例对）不可跨 chunk 分割。
    一个段落应该完整地出现在一个 chunk 中。
    """

    # extensible: more invariants may be added here
    # cross_reference_preservation: bool = True
    # footnote_locality: bool = True


# ======================================
# 2. Representation Policies (slimmed)
# ======================================

@dataclass
class BoundaryPreference:
    """Boundary preference — where chunks are split."""

    primary: BoundaryPrimary = "discourse_unit"
    """首选切分位置：
    - discourse_unit: 话语单元边界（段落、列表项）
    - heading_boundary: heading 之间
    - structural_block: 结构块边界（代码块、表格）
    - token_limit: token 限制（fallback）
    """

    fallback_chain: List[str] = field(default_factory=lambda: [
        "heading_boundary",
        "structural_block",
        "token_limit",
    ])
    """当首选不可行时的回退链。"""

    use_structural_relations: bool = True
    """是否调用 StructuralSemanticsAnalyzer 生成 merge constraints。

    False 时 _build_merge_constraints 直接返回空字典 —— 用于「机械基线」
    策略（不依赖任何结构语义关系，仅按 token 限制切分），与语义策略形成
    明显对比。
    """

    merge_short_sibling_sections: bool = False
    """是否在初始 section 分块后，合并同父路径下相邻的短 sibling sections。

    该选项只适用于 heading_boundary 策略。它保留各自标题与 heading_path，
    只取消“短章节必须独占一个 chunk”的机械边界；不会跨父路径、不会越过
    soft limit，也不会改变默认策略的行为。
    """

    short_section_ratio: float = 0.30
    """短章节资格阈值，占 size_soft_limit 的比例（默认 30%）。"""


@dataclass
class IntegrityRules:
    """Integrity rules — which structures must stay intact."""

    code_blocks: bool = True
    """ fenced code blocks 必须完整。 """

    tables: bool = True
    """ 表格必须完整（表头 + 至少一行数据）。 """

    blockquotes: bool = True
    """ 引用块必须完整。 """

    list_groups: bool = True
    """ 列表项组必须完整（一个列表的所有项）。 """


@dataclass
class ContextPropagation:
    """Context propagation — controls the semantic link between
    adjacent chunks."""

    mode: ContextPropagationMode = "selective"
    """传播模式：
    - none: 不传播，每个 chunk 独立
    - selective: 选择性传播（heading_path + 前一段落的最后一句）
    - heading_only: 只传播 heading_path
    - full: 完整传播（类似传统 overlap，但语义感知）
    """

    overlap_size: int = 0
    """相邻 chunk 之间的文本重叠大小（字符数）。

    语义：当前 chunk 的末尾 N 个字符会被复制到下一个 chunk 的头部，
    保证跨 chunk 检索时语义不断裂。

    默认为 0（不重叠）。当 mode = "full" 时建议设置为 50–200。
    mode = "none" / "heading_only" / "selective" 时此字段不生效。
    """

    heading_depth: int = 3
    """传播 heading 的最大深度（h1, h2, h3）。"""

    semantic_dependency_locality: bool = True
    """传播语义依赖局部性信息（如：前一段落的主题）。"""


@dataclass
class SemanticDensity:
    """Semantic density — a purely semantic concept, no token
    constraints.

    size_soft_limit / size_hard_limit moved to
    RuntimePackagingPolicy. Only structural-semantics-based density
    targets remain here.
    """

    discourse_unit_target: int = 5
    """目标 discourse unit 数（段落、列表项等）。"""

    paragraph_target: int = 3
    """目标段落数。"""


# ======================================
# 3. Runtime Packaging (extracted from SemanticDensity + deprecated fields)
# ======================================

@dataclass
class CoarseGranularityPolicy:
    """Standalone policy for coarse context packs; does not reuse
    the base packaging limits.

    Structure boundaries are the primary constraint; length
    relative to the whole document only supplies soft targets, and
    the absolute cap applies only when a degrading split becomes
    necessary. This object stays pure policy-layer configuration;
    concrete splitter settings are converted in the representation
    layer as needed, keeping base/coarse length semantics from
    bleeding into each other.
    """

    strategy: Literal["structure_adaptive"] = "structure_adaptive"
    relative_target_ratio: float = 0.35
    min_target_chars: int = 800
    max_target_chars: int = 6000
    hard_max_chars: int = 9000
    short_document_chars: int = 1200
    short_section_chars: int = 300
    merge_same_level: bool = True
    merge_deeper_level: bool = True
    cohesion_threshold: float = 0.0
    cohesion_topic_weight: float = 0.50
    cohesion_boundary_weight: float = 0.35
    cohesion_turbulence_weight: float = 0.15


@dataclass
class FineGranularityPolicy:
    """Standalone policy configuration for the fine layer
    (chunk_size-budget triggered).

    Per granularity_redefinition_spec_v1: chunk_size / overlap
    formally belong to the fine layer, which splits above the base
    (paragraph) layer by budget. This object stays pure
    policy-layer configuration; concrete splitter settings are
    converted by FineGrainedConfig.from_policy.

    Two configured modes: mode="adaptive" (default arm: sentence-
    count budget × document P75 sentence length, clamped to a
    [0.6×, 1.4×] band around the anchor) and mode="fixed" (control
    arm: one corpus-wide anchor).

    All constants are rough external estimates with no calibration
    on this project; tuning requires a separate preregistration.
    """

    mode: Literal["adaptive", "fixed"] = "adaptive"
    anchor_chars: int = 512
    """全局锚点（fixed 臂的 chunk_size；adaptive 臂的钳位带中心）。"""
    sentence_budget: int = 8
    """adaptive 臂句数预算 N：chunk_size_doc = clamp(N × P75 句长)。"""
    clamp_low_ratio: float = 0.6
    clamp_high_ratio: float = 1.4
    overlap_window_chars: int = 220
    """overlap 搜索窗 W 的绝对上限；W = min(220, ratio × chunk_size)。"""
    overlap_window_ratio: float = 0.5
    overlap_ratio: float = 0.18
    """三档比率带 / 四档 raw_chars 的重叠率（市面惯例）。"""
    ratio_band_ratio: float = 0.5
    """三档容差带半宽：重叠目标 L 的 [0.5L, 1.5L]。"""
    min_fine_chars: int = 30
    """细粒度块最小字符数（碎片合并阈值，沿用旧值）。"""


@dataclass
class RuntimePackagingPolicy:
    """Runtime Packaging Layer — token/size constraints.

    These parameters do not define representation truth, only
    constrain representation deployment. Embedding window /
    retrieval latency / model context always exist, but they are
    packaging constraints, not semantic primitives.

    Former SemanticDensity.size_soft_limit / size_hard_limit moved
    here. Former RepresentationPolicy.chunk_size / overlap /
    orphan_threshold moved here.
    """

    size_soft_limit: int = 500
    """token 软限制 —— 合并约束，非目标值（not a target）。
    当前 chunk 字符数低于此值时，不触发切分，继续合并后续段落；
    它不要求 chunk 达到该大小，只禁止在该阈值之下切分。
    base 层实际 chunk 大小由段落原子决定（中位远低于此值），这是
    设计本身而非参数脱节。
    """

    size_hard_limit: int = 800
    """token 硬限制 —— 分块的最大允许大小（max）。
    超过此值时强制切分，即使没有语义边界。
    structural block（代码块等）可例外保持完整。
    """

    # --- backward-compatible legacy fields ---
    chunk_size: Optional[int] = None
    """兼容字段：显式设置时优先于 size_hard_limit
    （见 get_effective_chunk_size 的优先级语义）。"""

    overlap: Optional[int] = None
    """旧字段兼容层：显式设置时优先于 context_propagation.overlap_size。
    建议新代码直接设置 context_propagation.overlap_size。
    """

    orphan_threshold: int = 120
    """碎片合并阈值（活跃运行时参数：表示层合并逻辑
    与质量评估 size 指标均读取此值）。"""

    @classmethod
    def from_chunk_size(cls, chunk_size: int) -> "RuntimePackagingPolicy":
        """Create a packaging policy from a legacy chunk_size
        (approximate mapping)."""
        return cls(
            size_soft_limit=int(chunk_size * 0.6),
            size_hard_limit=chunk_size,
        )

    def get_effective_chunk_size(self) -> int:
        """Return the effective chunk_size cap (the hard limit).

        Used for oversized-paragraph checks and the maximum
        threshold of forced flushes. Legacy compatibility: if
        chunk_size is explicitly set, use it directly.
        """
        if self.chunk_size is not None:
            return self.chunk_size
        return self.size_hard_limit

    def get_min_chunk_size(self) -> int:
        """Return the minimum target chunk size (the soft limit).

        While the current chunk is below this value, no split is
        triggered; keep merging.
        """
        return self.size_soft_limit

    def get_effective_overlap(self, context_propagation=None) -> int:
        """Return the effective overlap size (in characters).

        Priority:
            1. packaging.overlap (when the legacy field is set)
            2. context_propagation.overlap_size (new field)
            3. 0 (no overlap by default)

        Args:
            context_propagation: a ContextPropagation instance, used
                to read overlap_size.
        """
        if self.overlap is not None:
            return self.overlap
        if context_propagation is not None:
            return context_propagation.overlap_size
        return 0


# ======================================
# 4. Runtime Scale Policy (huge files / runtime scale)
# ======================================

@dataclass
class ScoreObservation:
    """One score observation for a single document.

    unit_count is used to compute weights under the per_chunk /
    capped modes; tags are used for stratified benchmark statistics
    (normal / huge / stress).
    """

    score: Optional[float]
    unit_count: int = 1
    tags: List[str] = field(default_factory=lambda: ["normal"])
    label: str = ""


@dataclass
class EvaluationWeighting:
    """Per-document weighting for quality / benchmark aggregation.

    The problem is not "the file is large" but "statistical meaning
    is polluted by huge files". A 10MB file producing 200
    low-quality chunks can drag the whole benchmark down.
    """

    mode: Literal["per_chunk", "per_document", "capped", "percentile"] = "per_document"
    """聚合模式：
    - per_chunk: 按 chunk 数统计（旧行为，会被大文件污染）
    - per_document: 每个文档等权贡献
    - capped: 每个文档贡献上限为 max_contribution
    - percentile: 取 percentile_aggregation 分位数
    """

    max_contribution_per_document: float = 0.05
    """capped 模式下，单文档最大贡献占比（5%）。"""

    percentile_aggregation: int = 50
    """percentile 模式下，取哪个分位数（50=中位数）。"""

    def aggregate(self, observations: List["ScoreObservation"]) -> Optional[float]:
        """Aggregate per-document observations under the current
        mode.

        Args:
            observations: one ScoreObservation per document (score +
                unit_count).

        Returns:
            The aggregated score; None when there is no valid
            observation.
        """
        obs = [
            (o.score, o.unit_count)
            for o in observations
            if o.score is not None
        ]
        if not obs:
            return None

        if self.mode == "per_chunk":
            # legacy behavior: weighted by unit count — huge files
            # dominate the statistics
            total_units = sum(max(unit_count, 1) for _, unit_count in obs)
            if total_units <= 0:
                return None
            return (
                sum(score * max(unit_count, 1) for score, unit_count in obs)
                / total_units
            )

        if self.mode == "percentile":
            scores = sorted(score for score, _ in obs)
            if len(scores) == 1:
                return scores[0]
            pos = (len(scores) - 1) * (self.percentile_aggregation / 100.0)
            low = int(pos)
            high = min(low + 1, len(scores) - 1)
            frac = pos - low
            return scores[low] * (1 - frac) + scores[high] * frac

        if self.mode == "capped":
            # each document's weight is capped at
            # max_contribution_per_document of the total weight
            n = len(obs)
            cap = self.max_contribution_per_document
            raw = [max(unit_count, 1) for _, unit_count in obs]
            total = sum(raw)
            weights = [min(r / total, cap) for r in raw] if total > 0 else [1.0 / n] * n
            wsum = sum(weights)
            if wsum <= 0:
                return sum(score for score, _ in obs) / n
            return (
                sum(score * w for (score, _), w in zip(obs, weights)) / wsum
            )

        # per_document (default): each document weighs equally
        return sum(score for score, _ in obs) / len(obs)


@dataclass
class RepresentationScaling:
    """Runtime representation scaling for large documents.

    Huge files should not fully expand all representation units,
    otherwise the UI would blow up.
    """

    lazy_expansion: bool = True
    """是否惰性展开（按需加载 unit 详情）。"""

    max_units_per_section: int = 50
    """超过此数量的 section 折叠为摘要层级。"""

    folded_visualization: bool = True
    """可视化折叠（只展示顶层 + 标记有子层级的节点）。"""

    sampled_preview: bool = True
    """采样预览（只展示前 N 个 unit 作为预览）。"""

    preview_sample_size: int = 10
    """采样预览的 unit 数量。"""

    def should_sample(
        self, benchmark_tags: List[str], unit_count: int,
    ) -> bool:
        """Whether this file needs a sampled preview."""
        if not self.sampled_preview:
            return False
        if {"huge", "stress"} & set(benchmark_tags or ["normal"]):
            return True
        return unit_count > self.max_units_per_section

    def sample_preview(self, items: list) -> tuple:
        """Sample uniformly, returning (sampled_items,
        metadata_dict).

        metadata contains total / sample_size / sampled /
        sample_indices, consumed by the serialization and reporting
        ends.
        """
        total = len(items)
        if total <= self.preview_sample_size:
            return items, {
                "sampled": False,
                "total": total,
                "sample_size": total,
            }
        step = total / self.preview_sample_size
        indices = [int(i * step) for i in range(self.preview_sample_size)]
        sampled = [items[i] for i in indices]
        return sampled, {
            "sampled": True,
            "total": total,
            "sample_size": len(sampled),
            "sample_indices": indices,
        }


@dataclass
class DensityAdaptation:
    """Semantic density adaptation for low-density documents.

    Log dumps / giant plain text are naturally unsuited to
    fine-grained representation.
    """

    mode: Literal["fine", "section_level", "windowed", "sampled"] = "fine"
    """密度模式：
    - fine: 细粒度（默认，正常 narrative）
    - section_level: 按 section 级别组织
    - windowed: 滑动窗口层级
    - sampled: 采样局部性
    """

    section_level_threshold_lines: int = 5000
    """超过此行数时自动切换到 section_level。"""

    window_size_lines: int = 500
    """windowed 模式的窗口大小（行数）。"""

    auto_detect: bool = True
    """是否自动检测低密度文档并切换模式。"""

    def detect_mode(self, line_count: int) -> str:
        """Auto-detect the density mode from the line count."""
        if not self.auto_detect:
            return self.mode
        if line_count >= self.section_level_threshold_lines:
            return "section_level"
        return "fine"

    def should_downscale(self, line_count: int) -> bool:
        """Whether quality / failure evaluation needs downgraded
        sampling."""
        return self.detect_mode(line_count) != "fine"


@dataclass
class BenchmarkTagging:
    """Benchmark taxonomy tagging for huge/stress/stream files.

    Huge files get their own benchmark taxonomy instead of being
    counted together with the normal narrative corpus.
    """

    tags: List[str] = field(default_factory=lambda: ["normal"])
    """当前文档的 benchmark 标签。"""

    huge_file_threshold_bytes: int = 5_000_000
    """超过此字节数的文件自动标记为 huge。"""

    stress_file_threshold_units: int = 500
    """超过此 unit 数的文件自动标记为 stress。"""

    auto_tag: bool = True
    """是否自动打标签。"""

    def classify(self, file_size_bytes: int, unit_count: int) -> List[str]:
        """Classify automatically from file size and unit count."""
        result = ["normal"]
        if file_size_bytes >= self.huge_file_threshold_bytes:
            result.append("huge")
        if unit_count >= self.stress_file_threshold_units:
            result.append("stress")
        return result


@dataclass
class RuntimeScalePolicy:
    """Runtime Scale Policy — huge files are a runtime concern, not
    a chunking strategy.

    Four sub-policies:
    1. evaluation_weighting: evaluation weighting (prevents
       benchmark skew)
    2. representation_scaling: representation scaling (prevents UI
       blowup)
    3. density_adaptation: density adaptation (prevents fine-grained
       explosion on low-density documents)
    4. benchmark_tagging: benchmark taxonomy (prevents statistical
       pollution)
    """

    evaluation_weighting: EvaluationWeighting = field(default_factory=EvaluationWeighting)
    representation_scaling: RepresentationScaling = field(default_factory=RepresentationScaling)
    density_adaptation: DensityAdaptation = field(default_factory=DensityAdaptation)
    benchmark_tagging: BenchmarkTagging = field(default_factory=BenchmarkTagging)

    def aggregate_scores(self, observations: List[ScoreObservation]) -> dict:
        """Aggregate scores, stratified by benchmark tag.

        Returns:
            {
              "overall": float | None,      # total score aggregated per the weighting mode
              "mode": str,                  # the aggregation mode used
              "by_tag": {tag: {"value": float, "count": int}},
              "excluded_from_normal": int,  # documents excluded by huge/stress tags
              "normal_only": float | None,  # aggregate over normal documents only
            }
        """
        weighting = self.evaluation_weighting
        overall = weighting.aggregate(observations)

        by_tag: dict = {}
        all_tags = {t for o in observations for t in (o.tags or ["normal"])}
        for tag in sorted(all_tags):
            subset = [o for o in observations if tag in (o.tags or ["normal"])]
            value = weighting.aggregate(subset)
            if value is not None:
                by_tag[tag] = {"value": round(value, 4), "count": len(subset)}

        normal_subset = [
            o for o in observations
            if not ({"huge", "stress"} & set(o.tags or ["normal"]))
        ]
        normal_only = weighting.aggregate(normal_subset)

        return {
            "overall": round(overall, 4) if overall is not None else None,
            "mode": weighting.mode,
            "by_tag": by_tag,
            "excluded_from_normal": len(observations) - len(normal_subset),
            "normal_only": round(normal_only, 4) if normal_only is not None else None,
        }


# ======================================
# 5. Optimization Config
# ======================================

@dataclass
class OptimizationConfig:
    """Runtime optimization configuration."""

    quality_feedback: bool = True
    """是否启用质量反馈调整。"""

    retry_on_invariant_violation: bool = True
    """如果 Semantic Invariant 被违反，是否重试。"""

    online_adjustment: bool = False
    """是否启用在线参数调整（实验性功能）。"""

    max_retry: int = 3
    """最大重试次数。"""
