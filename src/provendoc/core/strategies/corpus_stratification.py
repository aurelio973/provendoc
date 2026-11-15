"""
Corpus Stratification — the corpus tier classification system

Splits the test corpus into four tiers, keeping pathological
documents from polluting quality assessment of normal documents.

Four tiers:
    1. Normal Narrative  — ordinary narrative documents (the main
       corpus, core target of quality assessment)
    2. Robustness        — robustness test documents (dirty data /
       encoding problems / format anomalies, but with semantic
       content)
    3. Stress            — stress test documents (huge files / huge
       chunk counts, for performance validation)
    4. Minimal           — tiny documents (<100 bytes or a single
       chunk; most quality dimensions do not apply)

"Meaningful documents" = Normal Narrative + Robustness
    → these participate in Filtered quality
    → Stress and Minimal do not (they exist to verify no crashes,
      not whether splitting is good)

Classification Signals:
    - file_size_bytes: file size
    - chunk_count: number of chunks/units
    - benchmark_tags: existing huge/stress tags
    - quality_dimensions: per-dimension quality scores (used to
      judge structural diversity)
    - category: file path classification (e.g. dirty_*,
      encoding_*, simple_*)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class CorpusTier(str, Enum):
    """Corpus tiers."""
    NORMAL_NARRATIVE = "normal_narrative"
    ROBUSTNESS = "robustness"
    STRESS = "stress"
    MINIMAL = "minimal"

    @property
    def label_cn(self) -> str:
        """Chinese label of the tier (for report display)."""
        return _TIER_LABELS[self]

    @property
    def is_meaningful(self) -> bool:
        """Whether this is a "meaningful document" tier —
        participates in Filtered quality."""
        return self in (CorpusTier.NORMAL_NARRATIVE, CorpusTier.ROBUSTNESS)


_TIER_LABELS = {
    CorpusTier.NORMAL_NARRATIVE: "正常叙事",
    CorpusTier.ROBUSTNESS: "鲁棒性",
    CorpusTier.STRESS: "压力测试",
    CorpusTier.MINIMAL: "极小文档",
}

_TIER_COLORS = {
    CorpusTier.NORMAL_NARRATIVE: "#4a9eff",  # blue
    CorpusTier.ROBUSTNESS: "#ffb020",        # amber
    CorpusTier.STRESS: "#ff5c5c",            # red
    CorpusTier.MINIMAL: "#6b7280",           # gray
}

# path keywords → robustness classification hints
_ROBUSTNESS_PATH_KEYWORDS: Set[str] = {
    "dirty", "malformed", "encoding", "unicode", "broken",
    "edge", "weird", "chaos", "noise", "corrupt",
}

# path keywords → tiny-document hints
_MINIMAL_PATH_KEYWORDS: Set[str] = {
    "empty", "tiny", "single", "minimal", "one_line", "short",
}


@dataclass
class CorpusStratifier:
    """Corpus tier stratifier.

    Classifies documents into the four tiers from multi-dimensional
    signals, for the dual-track Overall/Filtered quality
    computation.

    Attributes:
        minimal_size_bytes: below this byte count → Minimal candidate
        minimal_chunk_count: chunk count ≤ this value → Minimal
            candidate
        stress_size_bytes: above this byte count → Stress candidate
        stress_chunk_count: chunk count ≥ this value → Stress
            candidate
        meaningful_quality_threshold: quality overall below this and
            the document is tiny → Minimal
    """

    minimal_size_bytes: int = 100
    minimal_chunk_count: int = 1
    stress_size_bytes: int = 5_000_000
    stress_chunk_count: int = 500
    meaningful_quality_threshold: float = 0.3

    def classify(
        self,
        file_size_bytes: int = 0,
        chunk_count: int = 0,
        benchmark_tags: Optional[List[str]] = None,
        quality_overall: Optional[float] = None,
        quality_dimensions: Optional[Dict[str, float]] = None,
        relative_path: str = "",
        success: bool = True,
    ) -> CorpusTier:
        """Classify a single document into its corpus tier.

        Classification priority (high to low):
        1. Stress: benchmark_tags contain huge/stress, or the file
           is huge / has a huge chunk count
        2. Minimal: tiny file (<100 bytes) with ≤1 chunk, or very
           low quality and tiny
        3. Robustness: path contains dirty/malformed/encoding-like
           keywords
        4. Normal Narrative: all other successful documents
        5. Failed documents → Minimal (not assessable)
        """
        tags = set(benchmark_tags or ["normal"])
        path_lower = relative_path.lower()

        # failed document → Minimal
        if not success:
            return CorpusTier.MINIMAL

        # 1. stress detection
        if "huge" in tags or "stress" in tags:
            return CorpusTier.STRESS
        if file_size_bytes >= self.stress_size_bytes:
            return CorpusTier.STRESS
        if chunk_count >= self.stress_chunk_count:
            return CorpusTier.STRESS

        # 2. minimal detection
        is_tiny = file_size_bytes > 0 and file_size_bytes < self.minimal_size_bytes
        is_single_chunk = chunk_count <= self.minimal_chunk_count
        if is_tiny and is_single_chunk:
            return CorpusTier.MINIMAL
        # tiny with a quality score that is extremely low (almost
        # no assessable structure)
        if is_tiny and quality_overall is not None and quality_overall < self.meaningful_quality_threshold:
            return CorpusTier.MINIMAL
        # path hints at minimal
        if any(kw in path_lower for kw in _MINIMAL_PATH_KEYWORDS) and is_single_chunk:
            return CorpusTier.MINIMAL

        # 3. robustness detection
        if any(kw in path_lower for kw in _ROBUSTNESS_PATH_KEYWORDS):
            return CorpusTier.ROBUSTNESS

        # 4. Normal Narrative (default)
        return CorpusTier.NORMAL_NARRATIVE

    def classify_batch(
        self,
        files: List[Dict[str, Any]],
    ) -> List[CorpusTier]:
        """Classify a batch of documents.

        Args:
            files: list of file info dicts; each dict must contain:
                file_size_bytes, chunk_count, benchmark_tags,
                quality_overall, quality_dimensions, relative_path, success
        """
        return [
            self.classify(
                file_size_bytes=f.get("file_size_bytes", 0),
                chunk_count=f.get("chunk_count", 0),
                benchmark_tags=f.get("benchmark_tags"),
                quality_overall=f.get("quality_overall"),
                quality_dimensions=f.get("quality_dimensions"),
                relative_path=f.get("relative_path", ""),
                success=f.get("success", True),
            )
            for f in files
        ]

    def distribution(self, tiers: List[CorpusTier]) -> Dict[str, Dict[str, Any]]:
        """Compute the tier distribution.

        Returns:
            {
                "normal_narrative": {"count": N, "percentage": P, "color": "#..."},
                "robustness": {...},
                "stress": {...},
                "minimal": {...},
            }
        """
        total = len(tiers) or 1
        result = {}
        for tier in CorpusTier:
            count = sum(1 for t in tiers if t == tier)
            result[tier.value] = {
                "count": count,
                "percentage": round(count / total * 100, 1),
                "color": _TIER_COLORS[tier],
                "label": tier.label_cn,
                "is_meaningful": tier.is_meaningful,
            }
        return result

    def meaningful_tiers(self) -> Set[CorpusTier]:
        """Return the set of "meaningful" tiers."""
        return {CorpusTier.NORMAL_NARRATIVE, CorpusTier.ROBUSTNESS}


def tier_color(tier: CorpusTier) -> str:
    """Return the color for a tier."""
    return _TIER_COLORS.get(tier, "#6b7280")
