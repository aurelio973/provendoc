"""
Semantic Stability Observer — semantic stability observation

A paradigm shift from point evaluation to landscape mapping.

Instead of asking "what is the quality at this parameter value",
it sweeps a parameter range, draws the quality landscape, and
finds stable plateaus and degradation cliffs.

Core Concepts:
    - Sweep: re-chunk + quality-evaluate at multiple soft_limit
      sample points
    - Degradation Onset: the soft_limit where a dimension starts
      degrading noticeably
    - Stable Region: the parameter interval where all applicable
      dimensions are undegraded
    - Confidence Margin: distance from the current parameter to
      the nearest degradation cliff
    - Health Status: stable / weakening / degrading

Output:
    StabilitySweepResult, containing:
    - overall + per-dimension scores at each sample point
    - the degradation onset of each dimension
    - stable region + confidence margin
    - current health status

Usage:
    observer = SemanticStabilityObserver()
    result = observer.sweep_document(
        raw_text, document, quality_evaluator, strategy, doc_quality,
        current_soft_limit=300,
    )
    print(result.health_status)  # "stable"
    print(result.stable_region)  # (200, 600)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ======================================
# Proxy types for sweep evaluation
# ======================================

@dataclass
class _SweepChunk:
    """Lightweight chunk proxy for quality evaluation during sweep."""
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class _SweepRepresentation:
    """Lightweight representation proxy for quality evaluation."""
    content: List[_SweepChunk]
    representation_type: str = "narrative"


@dataclass
class _SweepStrategy:
    """Strategy proxy for sweep evaluation: exposes the current
    sample point's soft_limit through the chunk-size interface.

    Note: get_effective_chunk_size() here returns soft_limit, not
    the hard limit. That is the special semantics of the sweep
    scenario: each sample point uses its own soft_limit as the
    chunking target, so there is no need to distinguish the
    soft/hard thresholds.
    """
    soft_limit: int = 300

    def get_effective_chunk_size(self) -> int:
        """Return the chunking target size of the current sweep
        sample point (i.e. soft_limit)."""
        return self.soft_limit

    def get_effective_overlap(self) -> int:
        """Proportional overlap: 10% of soft_limit, at least 10."""
        return max(int(self.soft_limit * 0.1), 10)

    @property
    def orphan_threshold(self) -> int:
        """Orphan-chunk threshold: 40% of soft_limit, at least
        50."""
        return max(int(self.soft_limit * 0.4), 50)


# ======================================
# Sweep Result
# ======================================

@dataclass
class StabilitySweepResult:
    """Single-document stability sweep result."""
    soft_limits: List[int] = field(default_factory=list)
    overall_scores: List[float] = field(default_factory=list)
    dimension_scores: Dict[str, List[float]] = field(default_factory=dict)
    chunk_counts: List[int] = field(default_factory=list)
    degradation_onsets: Dict[str, Optional[int]] = field(default_factory=dict)
    stable_region: Optional[Tuple[int, int]] = None
    confidence_margin: int = 0
    current_point: int = 300
    health_status: str = "insufficient_data"

    def to_user_friendly_dict(self) -> Dict[str, Any]:
        """Convert into a user-friendly explanation dict."""
        status_map = {
            "stable": ("稳定", "当前参数处于质量稳定区间，无需调整。"),
            "weakening": ("弱化", "当前参数接近稳定边界，小幅调整可能改善质量。"),
            "degrading": ("退化", "当前参数已超出稳定区间，建议调整到稳定范围内。"),
            "insufficient_data": ("数据不足", "文档过短或采样点不足，无法判断稳定性。"),
        }
        status_label, status_advice = status_map.get(
            self.health_status, ("未知", "")
        )

        # find the best soft_limit (the point with the highest
        # overall score)
        best_sl = None
        best_score = 0.0
        if self.soft_limits and self.overall_scores:
            best_idx = max(
                range(len(self.overall_scores)),
                key=lambda i: self.overall_scores[i],
            )
            best_sl = self.soft_limits[best_idx]
            best_score = self.overall_scores[best_idx]

        # recommended actions
        actions = []
        if self.health_status == "degrading":
            if self.stable_region:
                actions.append(
                    f"建议将 soft_limit 调整到稳定区间 "
                    f"[{self.stable_region[0]}, {self.stable_region[1]}]"
                )
            elif best_sl is not None:
                actions.append(
                    f"建议尝试 soft_limit={best_sl}（该点质量分最高: {best_score:.3f}）"
                )
        elif self.health_status == "weakening":
            if self.stable_region:
                mid = (self.stable_region[0] + self.stable_region[1]) // 2
                actions.append(
                    f"当前参数可用，但可尝试 soft_limit={mid} 获得更稳定的质量"
                )
        elif self.health_status == "stable":
            actions.append("当前参数设置良好，保持即可。")
            if best_sl and best_sl != self.current_point:
                actions.append(
                    f"若追求更高质量，可尝试 soft_limit={best_sl}"
                    f"（质量分: {best_score:.3f}）"
                )

        return {
            "health_status": self.health_status,
            "status_label": status_label,
            "status_advice": status_advice,
            "current_point": self.current_point,
            "stable_region": self.stable_region,
            "confidence_margin": self.confidence_margin,
            "best_point": {"soft_limit": best_sl, "quality": round(best_score, 3)}
            if best_sl is not None
            else None,
            "degradation_onsets": self.degradation_onsets,
            "recommended_actions": actions,
            "raw": {
                "soft_limits": self.soft_limits,
                "overall_scores": [
                    round(s, 3) for s in self.overall_scores
                ],
                "chunk_counts": self.chunk_counts,
            },
        }


@dataclass
class AggregateStabilityResult:
    """Aggregate stability across multiple documents."""
    soft_limits: List[int] = field(default_factory=list)
    overall_scores: List[float] = field(default_factory=list)
    dimension_scores: Dict[str, List[float]] = field(default_factory=dict)
    chunk_counts: List[float] = field(default_factory=list)
    degradation_onsets: Dict[str, Optional[int]] = field(default_factory=dict)
    stable_region: Optional[Tuple[int, int]] = None
    confidence_margin: int = 0
    current_point: int = 300
    health_status: str = "insufficient_data"
    document_count: int = 0


# ======================================
# Observer
# ======================================

class SemanticStabilityObserver:
    """Semantic stability observer.

    Sweeps the soft_limit parameter range, mapping the quality
    stability landscape.

    Attributes:
        sweep_points: list of sample points (soft_limit values)
        degradation_threshold: how far below the peak counts as
            "degradation" (default 10%)
        max_eval_per_file: max evaluations per file (keeps huge
            documents from slowing the sweep)
    """

    DEFAULT_SWEEP_POINTS: List[int] = [100, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200]

    def __init__(
        self,
        sweep_points: Optional[List[int]] = None,
        degradation_threshold: float = 0.10,
        max_eval_per_file: int = 11,
    ):
        """Initialize the observer.

        Args:
            sweep_points: soft_limit sample points (None uses
                DEFAULT_SWEEP_POINTS).
            degradation_threshold: fractional drop from the peak
                counted as "degradation" (default 10%).
            max_eval_per_file: max evaluations per file (keeps
                huge documents from slowing the sweep).
        """
        self.sweep_points = sweep_points or self.DEFAULT_SWEEP_POINTS
        self.degradation_threshold = degradation_threshold
        self.max_eval = max_eval_per_file

    # ------------------------------------------------------------------
    # Single-document sweep
    # ------------------------------------------------------------------

    def sweep_document(
        self,
        raw_text: str,
        document: Any,
        quality_evaluator: Any,
        strategy: Any = None,
        doc_quality: Any = None,
        current_soft_limit: int = 300,
    ) -> StabilitySweepResult:
        """Run a stability sweep on a single document.

        Args:
            raw_text: the document's raw text
            document: document object (needs .text)
            quality_evaluator: NarrativeQuality instance
            strategy: strategy object (used to extract
                doc_headings etc.)
            doc_quality: document quality report (optional)
            current_soft_limit: the currently running soft_limit

        Returns:
            StabilitySweepResult
        """
        if not raw_text or len(raw_text) < 50:
            return StabilitySweepResult(
                current_point=current_soft_limit,
                health_status="insufficient_data",
            )

        result = StabilitySweepResult(
            soft_limits=[],
            overall_scores=[],
            dimension_scores={},
            chunk_counts=[],
            current_point=current_soft_limit,
        )

        for soft_limit in self.sweep_points:
            # Re-chunk at this soft_limit
            chunks = self._rechunk(raw_text, soft_limit)
            if not chunks:
                continue

            # Evaluate quality
            repr_proxy = _SweepRepresentation(content=[_SweepChunk(text=c) for c in chunks])
            sweep_strategy = _SweepStrategy(soft_limit=soft_limit)

            try:
                evaluation_result = quality_evaluator.evaluate(
                    document=document,
                    semantic_representation=repr_proxy,
                    strategy=sweep_strategy,
                    document_quality=doc_quality,
                )
            except Exception:
                continue

            if not evaluation_result:
                continue

            result.soft_limits.append(soft_limit)
            result.overall_scores.append(evaluation_result.get("overall", 0.0))
            result.chunk_counts.append(len(chunks))

            profile = evaluation_result.get("profile") or {}
            for dim, val in profile.items():
                if isinstance(val, (int, float)):
                    result.dimension_scores.setdefault(dim, []).append(float(val))

        # Detect degradation onsets
        result.degradation_onsets = self._detect_onsets(result)
        result.stable_region = self._find_stable_region(result)
        result.confidence_margin = self._compute_margin(result)
        result.health_status = self._health_status(result)

        return result

    # ------------------------------------------------------------------
    # Aggregate sweep across multiple files
    # ------------------------------------------------------------------

    def sweep_batch(
        self,
        files: List[Dict[str, Any]],
        quality_evaluator: Any,
        current_soft_limit: int = 300,
    ) -> AggregateStabilityResult:
        """Aggregate sweep: sweep multiple documents and average.

        Args:
            files: list of files; each must provide:
                raw_text, document, doc_quality
            quality_evaluator: NarrativeQuality instance
            current_soft_limit: the current soft_limit

        Returns:
            AggregateStabilityResult
        """
        if not files:
            return AggregateStabilityResult(current_point=current_soft_limit)

        # Collect per-point scores across all files
        point_overall: Dict[int, List[float]] = {}
        point_dims: Dict[int, Dict[str, List[float]]] = {}
        point_chunks: Dict[int, List[int]] = {}
        doc_count = 0

        for f in files:
            raw_text = f.get("raw_text", "")
            document = f.get("document")
            if not raw_text or not document:
                continue

            single = self.sweep_document(
                raw_text=raw_text,
                document=document,
                quality_evaluator=quality_evaluator,
                strategy=f.get("strategy"),
                doc_quality=f.get("doc_quality"),
                current_soft_limit=current_soft_limit,
            )

            if single.health_status == "insufficient_data":
                continue

            doc_count += 1
            for i, sl in enumerate(single.soft_limits):
                point_overall.setdefault(sl, []).append(
                    single.overall_scores[i] if i < len(single.overall_scores) else 0
                )
                point_chunks.setdefault(sl, []).append(
                    single.chunk_counts[i] if i < len(single.chunk_counts) else 0
                )
                for dim, scores in single.dimension_scores.items():
                    if i < len(scores):
                        point_dims.setdefault(sl, {}).setdefault(dim, []).append(scores[i])

        if doc_count == 0:
            return AggregateStabilityResult(
                current_point=current_soft_limit,
                health_status="insufficient_data",
            )

        # Aggregate: average at each point
        soft_limits = sorted(point_overall.keys())
        agg = AggregateStabilityResult(
            soft_limits=soft_limits,
            overall_scores=[sum(point_overall[sl]) / len(point_overall[sl]) for sl in soft_limits],
            chunk_counts=[sum(point_chunks[sl]) / len(point_chunks[sl]) for sl in soft_limits],
            current_point=current_soft_limit,
            document_count=doc_count,
        )

        for dim in sorted(set(d for sl in point_dims.values() for d in sl)):
            agg.dimension_scores[dim] = [
                sum(point_dims[sl].get(dim, [0])) / len(point_dims[sl].get(dim, [0]))
                if point_dims[sl].get(dim) else 0
                for sl in soft_limits
            ]

        agg.degradation_onsets = self._detect_onsets_agg(agg)
        agg.stable_region = self._find_stable_region_agg(agg)
        agg.confidence_margin = self._compute_margin_agg(agg)
        agg.health_status = self._health_status_agg(agg)

        return agg

    # ------------------------------------------------------------------
    # Re-chunking (simple paragraph-based, like frontend previewSplit)
    # ------------------------------------------------------------------

    def _rechunk(self, text: str, soft_limit: int) -> List[str]:
        """Simple paragraph-based re-chunking at a given token limit.

        This is an approximation of the real chunking pipeline,
        sufficient for observing stability trends.
        """
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paras:
            return [text] if text else []

        def est(s: str) -> int:  # ~4 chars per token
            """Estimate the token count (about 4 chars per token,
            minimum 1)."""
            return max(len(s) // 4, 1)

        chunks: List[str] = []
        buf: List[str] = []
        buf_tokens = 0

        for p in paras:
            t = est(p)
            # Single paragraph exceeds limit → independent chunk
            if t > soft_limit * 1.5:
                if buf:
                    chunks.append("\n\n".join(buf))
                    buf, buf_tokens = [], 0
                chunks.append(p)
                continue
            # Adding would exceed limit → flush
            if buf_tokens + t > soft_limit and buf:
                chunks.append("\n\n".join(buf))
                buf, buf_tokens = [], 0
            buf.append(p)
            buf_tokens += t

        if buf:
            chunks.append("\n\n".join(buf))

        return chunks

    # ------------------------------------------------------------------
    # Degradation detection
    # ------------------------------------------------------------------

    def _detect_onsets(self, result: StabilitySweepResult) -> Dict[str, Optional[int]]:
        """Detect degradation onset for each dimension."""
        onsets: Dict[str, Optional[int]] = {}
        for dim, scores in result.dimension_scores.items():
            onsets[dim] = self._find_onset(scores, result.soft_limits)
        return onsets

    def _detect_onsets_agg(self, result: AggregateStabilityResult) -> Dict[str, Optional[int]]:
        onsets: Dict[str, Optional[int]] = {}
        for dim, scores in result.dimension_scores.items():
            onsets[dim] = self._find_onset(scores, result.soft_limits)
        return onsets

    def _find_onset(self, scores: List[float], soft_limits: List[int]) -> Optional[int]:
        """Find the soft_limit where score starts degrading.

        Algorithm:
        1. Find peak score
        2. Define "stable" as within degradation_threshold of peak
        3. Scan right from peak for first drop below threshold
        4. Return the soft_limit at that point
        """
        if not scores or len(scores) < 3:
            return None

        peak = max(scores)
        if peak <= 0:
            return None
        threshold = peak * (1 - self.degradation_threshold)

        peak_idx = scores.index(peak)

        # Scan right from peak for degradation
        for i in range(peak_idx + 1, len(scores)):
            if scores[i] < threshold:
                return soft_limits[i]

        return None  # No degradation detected in scanned range

    # ------------------------------------------------------------------
    # Stable region computation
    # ------------------------------------------------------------------

    def _find_stable_region(self, result: StabilitySweepResult) -> Optional[Tuple[int, int]]:
        return self._compute_stable_region(
            result.dimension_scores, result.soft_limits
        )

    def _find_stable_region_agg(self, result: AggregateStabilityResult) -> Optional[Tuple[int, int]]:
        return self._compute_stable_region(
            result.dimension_scores, result.soft_limits
        )

    def _compute_stable_region(
        self,
        dimension_scores: Dict[str, List[float]],
        soft_limits: List[int],
    ) -> Optional[Tuple[int, int]]:
        """Find the parameter range where all applicable dimensions are stable.

        A point is "stable" if all dimensions with scores at that point
        are within degradation_threshold of their respective peaks.

        Returns:
            (left_bound, right_bound) or None if no stable region found.
        """
        if not soft_limits or not dimension_scores:
            return None

        n = len(soft_limits)
        # Compute per-dimension peaks
        peaks: Dict[str, float] = {}
        for dim, scores in dimension_scores.items():
            if scores and len(scores) == n:
                peaks[dim] = max(scores)

        if not peaks:
            return None

        # Find all points where all dimensions are within threshold of peak
        stable_indices: List[int] = []
        for i in range(n):
            all_stable = True
            for dim, peak in peaks.items():
                scores = dimension_scores[dim]
                if i < len(scores) and scores[i] > 0:
                    if scores[i] < peak * (1 - self.degradation_threshold):
                        all_stable = False
                        break
            if all_stable:
                stable_indices.append(i)

        if not stable_indices:
            return None

        return (soft_limits[stable_indices[0]], soft_limits[stable_indices[-1]])

    # ------------------------------------------------------------------
    # Confidence margin
    # ------------------------------------------------------------------

    def _compute_margin(self, result: StabilitySweepResult) -> int:
        return self._compute_margin_impl(
            result.degradation_onsets, result.current_point, result.soft_limits
        )

    def _compute_margin_agg(self, result: AggregateStabilityResult) -> int:
        return self._compute_margin_impl(
            result.degradation_onsets, result.current_point, result.soft_limits
        )

    def _compute_margin_impl(
        self,
        onsets: Dict[str, Optional[int]],
        current: int,
        soft_limits: List[int],
    ) -> int:
        """Distance from current point to nearest degradation onset."""
        nearest_onset: Optional[int] = None
        for onset in onsets.values():
            if onset is not None:
                dist = abs(onset - current)
                if nearest_onset is None or dist < nearest_onset:
                    nearest_onset = dist

        if nearest_onset is None:
            # No degradation detected → margin is full range
            if soft_limits:
                return max(abs(soft_limits[0] - current), abs(soft_limits[-1] - current))
            return 0

        return nearest_onset

    # ------------------------------------------------------------------
    # Health status
    # ------------------------------------------------------------------

    def _health_status(self, result: StabilitySweepResult) -> str:
        return self._compute_health(
            result.degradation_onsets, result.current_point,
            result.stable_region, len(result.soft_limits)
        )

    def _health_status_agg(self, result: AggregateStabilityResult) -> str:
        return self._compute_health(
            result.degradation_onsets, result.current_point,
            result.stable_region, len(result.soft_limits)
        )

    def _compute_health(
        self,
        onsets: Dict[str, Optional[int]],
        current: int,
        stable_region: Optional[Tuple[int, int]],
        n_points: int,
    ) -> str:
        if n_points < 3:
            return "insufficient_data"

        if stable_region is None:
            return "degrading"

        left, right = stable_region
        if left <= current <= right:
            return "stable"

        # Current is outside stable region
        any_onset = any(o is not None for o in onsets.values())
        if any_onset:
            return "degrading"

        return "weakening"
