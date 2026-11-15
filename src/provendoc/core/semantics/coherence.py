"""Coherence Shift Detector — Phase 1 implementation.

Detects semantic coherence valleys between adjacent paragraphs using
TF-IDF cosine similarity and TextTiling lexical overlap. Outputs
COHERENCE_SHIFT SignalObservations that feed into RepresentationPolicy
as weak boundary pressure signals.

Design principles:
    - coherence shift = weak evidence, not hard boundary rule
    - confidence = boundary influence strength (0-1), not probability
    - only fires in unstructured regions (caller responsibility to filter)
    - no NLP models, no tokenizers, no external dependencies beyond stdlib

Phase roadmap:
    Phase 1 (this file): TF-IDF cosine similarity + TextTiling valleys
    Phase 2 (future):    Slope accumulation for gradual drift detection
    Phase 3 (future):    C99 global similarity matrix (see coherence_segmentation.md)

Pipeline:
    text → CoherenceShiftDetector.detect() → List[SignalObservation]
    → SignalExtractor.extract(coherence_observations=...) → merged signals
    → SignalSemanticMapper.map() → MappedSemantics (with coherence_pressure)
    → RepresentationPolicy.apply() → chunks
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from provendoc.core.semantics.signals import SignalObservation, SignalType

# ==================================
# Configuration
# ==================================


@dataclass
class CoherenceConfig:
    """Configuration for CoherenceShiftDetector.

    Attributes:
        min_paragraphs: Minimum paragraph count to run detection.
            Below this threshold, returns empty (statistics unreliable).
        texttiling_min_paragraphs: Minimum paragraph count to run TextTiling.
            Below this, only TF-IDF is used.
        texttiling_window: Number of paragraphs on each side of a candidate
            boundary for TextTiling block comparison. Auto-adjusted for short docs.
        valley_threshold_k: Valley detection sensitivity. A position is a valley
            if its score < mean - k * std. Higher k = fewer, more confident valleys.
        tfidf_weight: Weight for TF-IDF similarity in confidence calculation.
        texttiling_weight: Weight for TextTiling score in confidence calculation.
        min_confidence: Minimum confidence to emit a SignalObservation.
            Valleys below this threshold are silently dropped.
    """
    min_paragraphs: int = 4
    texttiling_min_paragraphs: int = 6
    texttiling_window: int = 2
    valley_threshold_k: float = 0.8
    tfidf_weight: float = 0.5
    texttiling_weight: float = 0.5
    min_confidence: float = 0.3


# ==================================
# Text utilities
# ==================================


def _tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase word tokens.

    Strips punctuation, splits on whitespace and CJK character boundaries.
    No stemming, no stopword removal — keeps it fast and tokenizer-agnostic.
    """
    # Split CJK characters individually (each character is a token)
    # Split Latin words on non-alphanumeric boundaries
    tokens = []
    # Normalize whitespace
    text = text.lower().strip()
    # Extract CJK characters and Latin words
    for match in re.finditer(r'[\u4e00-\u9fff\u3400-\u4dbf]|[a-z0-9]+', text):
        tokens.append(match.group())
    return tokens


def _split_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs by blank lines.

    Filters out empty paragraphs and paragraphs that are purely whitespace.
    """
    raw = re.split(r'\n\s*\n', text)
    return [p.strip() for p in raw if p.strip()]


# ==================================
# TF-IDF similarity
# ==================================


def _build_tfidf_vectors(paragraphs: List[str]) -> List[Dict[str, float]]:
    """Build TF-IDF vectors for a list of paragraphs.

    Uses log-normalized TF and standard IDF. No external libraries.
    """
    n = len(paragraphs)
    if n == 0:
        return []

    # Tokenize all paragraphs
    token_lists = [_tokenize(p) for p in paragraphs]

    # Document frequency
    # Counter (not dict): update(set(tokens)) counts keys once per
    # document — exactly document frequency semantics (each document
    # contributes 1 per term).
    df: Counter[str] = Counter()
    for tokens in token_lists:
        df.update(set(tokens))

    # Build TF-IDF vectors
    vectors = []
    for tokens in token_lists:
        tf = Counter(tokens)
        total = len(tokens) or 1
        vec: Dict[str, float] = {}
        for term, count in tf.items():
            tf_val = count / total
            idf_val = math.log((n + 1) / (df[term] + 1)) + 1.0
            vec[term] = tf_val * idf_val
        vectors.append(vec)

    return vectors


def _cosine_similarity(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
    """Compute cosine similarity between two TF-IDF vectors."""
    if not vec_a or not vec_b:
        return 0.0

    dot = sum(vec_a.get(t, 0.0) * vec_b.get(t, 0.0) for t in vec_a)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tfidf_similarity_curve(paragraphs: List[str]) -> List[float]:
    """Compute adjacent-paragraph TF-IDF cosine similarity curve.

    Returns a list of length (n-1) where result[i] = similarity(p_i, p_{i+1}).
    """
    vectors = _build_tfidf_vectors(paragraphs)
    return [
        _cosine_similarity(vectors[i], vectors[i + 1])
        for i in range(len(vectors) - 1)
    ]


# ==================================
# TextTiling
# ==================================


def _lexical_overlap(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Compute lexical overlap between two token lists.

    Uses Jaccard-like overlap: |intersection| / |union|.
    """
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def _texttiling_score_curve(
    paragraphs: List[str],
    window: int = 2,
) -> List[float]:
    """Compute TextTiling boundary scores.

    For each candidate boundary position i (between p_{i-1} and p_i),
    compare the left window [i-window : i] with the right window [i : i+window].
    Score = lexical overlap of the two windows.

    Returns a list of length (n-1) where result[i] = score at boundary i+1
    (i.e., between paragraph i and i+1).

    Lower score = more likely topic boundary.
    """
    n = len(paragraphs)
    token_lists = [_tokenize(p) for p in paragraphs]
    scores = []

    for i in range(1, n):
        left_start = max(0, i - window)
        right_end = min(n, i + window)

        left_tokens: List[str] = []
        for j in range(left_start, i):
            left_tokens.extend(token_lists[j])

        right_tokens: List[str] = []
        for j in range(i, right_end):
            right_tokens.extend(token_lists[j])

        scores.append(_lexical_overlap(left_tokens, right_tokens))

    return scores


# ==================================
# Valley detection
# ==================================


def _find_valleys(
    scores: List[float],
    k: float = 0.8,
) -> List[Tuple[int, float]]:
    """Find valley positions in a score curve.

    Primary strategy: statistical threshold.
        A position i is a valley if scores[i] < mean - k * std
        AND it is a local minimum.

    Fallback strategy (sparse curves): when std is near zero (most values identical,
    common with short-paragraph TF-IDF), fall back to rank-based detection:
        Find positions where scores[i] is among the bottom 25% of non-max values
        AND is a local minimum relative to its neighbors.

    Returns list of (position, depth) where depth = mean - scores[i].
    Higher depth = stronger valley = more confident boundary.
    """
    if not scores:
        return []

    n = len(scores)
    mean = sum(scores) / n
    variance = sum((s - mean) ** 2 for s in scores) / n
    std = math.sqrt(variance)

    # Primary: statistical threshold
    if std > 1e-6:
        threshold = mean - k * std
        valleys = []
        for i in range(n):
            if scores[i] >= threshold:
                continue
            left_ok = (i == 0) or (scores[i] <= scores[i - 1])
            right_ok = (i == n - 1) or (scores[i] <= scores[i + 1])
            if left_ok and right_ok:
                depth = mean - scores[i]
                valleys.append((i, depth))
        if valleys:
            return valleys

    # Fallback: rank-based detection for sparse/flat curves
    # Find local minima that are strictly below their neighbors
    # (handles the case where most similarities are 0 but some are non-zero)
    #
    # Hard constraint: a true valley must have a low absolute score.
    # High-similarity positions (>= 0.6) are never valleys regardless of
    # local shape — they represent strong coherence, not topic boundaries.
    VALLEY_MAX_SCORE = 0.6

    valleys = []
    for i in range(n):
        # Absolute score guard: skip high-similarity positions unconditionally
        if scores[i] >= VALLEY_MAX_SCORE:
            continue

        left_val = scores[i - 1] if i > 0 else scores[i]
        right_val = scores[i + 1] if i < n - 1 else scores[i]

        # Strict interior local minimum: lower than both neighbors
        if i > 0 and i < n - 1:
            if scores[i] < left_val and scores[i] < right_val:
                depth = ((left_val + right_val) / 2) - scores[i]
                if depth > 1e-6:  # Non-trivial depth
                    valleys.append((i, depth))
        # Edge positions: only fire when the drop is substantial (>= 0.15)
        # to avoid spurious edge valleys from minor fluctuations
        elif i == 0 and n > 1 and scores[0] < scores[1]:
            depth = scores[1] - scores[0]
            if depth >= 0.15:
                valleys.append((0, depth))
        elif i == n - 1 and n > 1 and scores[-1] < scores[-2]:
            depth = scores[-2] - scores[-1]
            if depth >= 0.15:
                valleys.append((n - 1, depth))

    return valleys


# ==================================
# Confidence calculation
# ==================================


def _normalize_depth(depth: float, max_depth: float) -> float:
    """Normalize valley depth to [0, 1]."""
    if max_depth <= 0:
        return 0.5
    return min(1.0, depth / max_depth)


def _merge_valley_signals(
    tfidf_valleys: List[Tuple[int, float]],
    texttiling_valleys: Optional[List[Tuple[int, float]]],
    tfidf_weight: float,
    texttiling_weight: float,
) -> List[Tuple[int, float, List[str]]]:
    """Merge TF-IDF and TextTiling valley signals.

    Voting strategy:
        - Both agree on a boundary → confidence = weighted average, sources = both
        - Only one fires → confidence × 0.5 (single-source penalty)

    Returns list of (boundary_idx, confidence, sources).
    boundary_idx is the gap index: 0 = between p0 and p1, etc.
    """
    # Normalize depths within each algorithm
    tfidf_max = max((d for _, d in tfidf_valleys), default=1.0) or 1.0
    tt_max = max((d for _, d in texttiling_valleys), default=1.0) if texttiling_valleys else 1.0

    tfidf_map: Dict[int, float] = {
        idx: _normalize_depth(depth, tfidf_max)
        for idx, depth in tfidf_valleys
    }
    tt_map: Dict[int, float] = {}
    if texttiling_valleys:
        tt_map = {
            idx: _normalize_depth(depth, tt_max)
            for idx, depth in texttiling_valleys
        }

    all_positions = set(tfidf_map) | set(tt_map)
    results = []

    for pos in sorted(all_positions):
        has_tfidf = pos in tfidf_map
        has_tt = pos in tt_map

        if has_tfidf and has_tt:
            # Both agree: weighted average, full confidence
            conf = tfidf_map[pos] * tfidf_weight + tt_map[pos] * texttiling_weight
            sources = ["tfidf", "texttiling"]
        elif has_tfidf:
            # Only TF-IDF: single-source penalty
            conf = tfidf_map[pos] * tfidf_weight * 0.5
            sources = ["tfidf"]
        else:
            # Only TextTiling: single-source penalty
            conf = tt_map[pos] * texttiling_weight * 0.5
            sources = ["texttiling"]

        results.append((pos, conf, sources))

    return results


# ==================================
# Paragraph position mapping
# ==================================


def _paragraph_char_offsets(text: str, paragraphs: List[str]) -> List[int]:
    """Map paragraph indices to character offsets in the original text.

    Returns a list of start offsets, one per paragraph.
    Uses simple forward search to handle edge cases robustly.
    """
    offsets = []
    search_start = 0
    for para in paragraphs:
        idx = text.find(para, search_start)
        if idx == -1:
            # Fallback: use last known position
            idx = search_start
        offsets.append(idx)
        search_start = idx + len(para)
    return offsets


def _boundary_char_offset(
    offsets: List[int],
    paragraphs: List[str],
    boundary_idx: int,
) -> int:
    """Get character offset for a boundary between paragraphs.

    boundary_idx = gap between p_{boundary_idx} and p_{boundary_idx+1}.
    Returns the offset at the END of p_{boundary_idx} (i.e., the gap position).
    """
    if boundary_idx >= len(offsets):
        return offsets[-1] + len(paragraphs[-1]) if offsets else 0
    para_end = offsets[boundary_idx] + len(paragraphs[boundary_idx])
    return para_end


# ==================================
# CoherenceShiftDetector
# ==================================


class CoherenceShiftDetector:
    """Detect semantic coherence shifts in unstructured text.

    Phase 1 implementation: TF-IDF cosine similarity + TextTiling.

    Usage:
        detector = CoherenceShiftDetector()
        observations = detector.detect(text)
        # Pass to SignalExtractor:
        extractor = SignalExtractor()
        signals = extractor.extract(text, coherence_observations=observations)

    The detector is stateless and thread-safe. Create once, call detect() many times.
    """

    def __init__(self, config: Optional[CoherenceConfig] = None):
        """Initialize the detector; defaults to CoherenceConfig when None."""
        self.config = config or CoherenceConfig()

    def detect(self, text: str) -> List[SignalObservation]:
        """Detect coherence shift signals in text.

        Args:
            text: Input text (plain text or lightly formatted).

        Returns:
            List of SignalObservation with signal_type=COHERENCE_SHIFT.
            Empty list if text is too short or no valleys found.
            Sorted by position.
        """
        paragraphs = _split_paragraphs(text)
        n = len(paragraphs)

        if n < self.config.min_paragraphs:
            return []

        # Compute similarity curves
        tfidf_curve = _tfidf_similarity_curve(paragraphs)

        use_texttiling = n >= self.config.texttiling_min_paragraphs
        texttiling_curve: Optional[List[float]] = None
        if use_texttiling:
            window = self._adaptive_window(paragraphs)
            texttiling_curve = _texttiling_score_curve(paragraphs, window=window)

        # Find valleys
        tfidf_valleys = _find_valleys(tfidf_curve, k=self.config.valley_threshold_k)
        texttiling_valleys: Optional[List[Tuple[int, float]]] = None
        if texttiling_curve is not None:
            texttiling_valleys = _find_valleys(
                texttiling_curve, k=self.config.valley_threshold_k
            )

        # Merge signals
        merged = _merge_valley_signals(
            tfidf_valleys=tfidf_valleys,
            texttiling_valleys=texttiling_valleys,
            tfidf_weight=self.config.tfidf_weight,
            texttiling_weight=self.config.texttiling_weight,
        )

        # Map to character offsets and build SignalObservations
        offsets = _paragraph_char_offsets(text, paragraphs)
        observations = []

        for boundary_idx, confidence, sources in merged:
            if confidence < self.config.min_confidence:
                continue

            char_pos = _boundary_char_offset(offsets, paragraphs, boundary_idx)
            span_end = min(char_pos + 1, len(text))

            # Context: last 30 chars of p_i + first 30 chars of p_{i+1}
            context = self._boundary_context(paragraphs, boundary_idx)

            observations.append(SignalObservation(
                signal_type=SignalType.COHERENCE_SHIFT,
                text=f"coherence_valley@p{boundary_idx}→p{boundary_idx + 1}",
                position=char_pos,
                span=(char_pos, span_end),
                confidence=round(confidence, 3),
                sub_type="+".join(sources),
                context=context,
            ))

        observations.sort(key=lambda o: o.position)
        return observations

    def detect_with_curve(self, text: str) -> "CoherenceAnalysis":
        """Detect coherence shifts and return full curve data for visualization.

        Returns CoherenceAnalysis containing both the SignalObservations and
        the raw similarity curves for rendering in the Dashboard coherence panel.
        """
        paragraphs = _split_paragraphs(text)
        n = len(paragraphs)

        if n < self.config.min_paragraphs:
            return CoherenceAnalysis(
                observations=[],
                paragraph_count=n,
                tfidf_similarities=[],
                texttiling_scores=[],
                valley_positions=[],
                valley_confidences=[],
            )

        tfidf_curve = _tfidf_similarity_curve(paragraphs)

        use_texttiling = n >= self.config.texttiling_min_paragraphs
        texttiling_curve: Optional[List[float]] = None
        if use_texttiling:
            window = self._adaptive_window(paragraphs)
            texttiling_curve = _texttiling_score_curve(paragraphs, window=window)

        tfidf_valleys = _find_valleys(tfidf_curve, k=self.config.valley_threshold_k)
        texttiling_valleys: Optional[List[Tuple[int, float]]] = None
        if texttiling_curve is not None:
            texttiling_valleys = _find_valleys(
                texttiling_curve, k=self.config.valley_threshold_k
            )

        merged = _merge_valley_signals(
            tfidf_valleys=tfidf_valleys,
            texttiling_valleys=texttiling_valleys,
            tfidf_weight=self.config.tfidf_weight,
            texttiling_weight=self.config.texttiling_weight,
        )

        offsets = _paragraph_char_offsets(text, paragraphs)
        observations = []
        valley_positions = []
        valley_confidences = []

        for boundary_idx, confidence, sources in merged:
            if confidence < self.config.min_confidence:
                continue
            char_pos = _boundary_char_offset(offsets, paragraphs, boundary_idx)
            span_end = min(char_pos + 1, len(text))
            context = self._boundary_context(paragraphs, boundary_idx)
            observations.append(SignalObservation(
                signal_type=SignalType.COHERENCE_SHIFT,
                text=f"coherence_valley@p{boundary_idx}→p{boundary_idx + 1}",
                position=char_pos,
                span=(char_pos, span_end),
                confidence=round(confidence, 3),
                sub_type="+".join(sources),
                context=context,
            ))
            valley_positions.append(boundary_idx)
            valley_confidences.append(round(confidence, 3))

        observations.sort(key=lambda o: o.position)

        return CoherenceAnalysis(
            observations=observations,
            paragraph_count=n,
            tfidf_similarities=[round(s, 3) for s in tfidf_curve],
            texttiling_scores=[round(s, 3) for s in (texttiling_curve or [])],
            valley_positions=valley_positions,
            valley_confidences=valley_confidences,
        )

    def _adaptive_window(self, paragraphs: List[str]) -> int:
        """Compute adaptive TextTiling window size.

        Short paragraphs → larger window (need more context).
        Long paragraphs → smaller window (each paragraph is already rich).
        """
        avg_len = sum(len(p) for p in paragraphs) / len(paragraphs)
        if avg_len < 100:
            return min(4, len(paragraphs) // 2)
        elif avg_len < 300:
            return self.config.texttiling_window
        else:
            return max(1, self.config.texttiling_window - 1)

    @staticmethod
    def _boundary_context(paragraphs: List[str], boundary_idx: int) -> str:
        """Build context string around a boundary for debugging."""
        left = paragraphs[boundary_idx][-40:] if boundary_idx < len(paragraphs) else ""
        right = paragraphs[boundary_idx + 1][:40] if boundary_idx + 1 < len(paragraphs) else ""
        return f"...{left} | {right}..."


# ==================================
# CoherenceAnalysis (visualization data)
# ==================================


@dataclass
class CoherenceAnalysis:
    """Full coherence analysis result, including curve data for visualization.

    Attributes:
        observations: COHERENCE_SHIFT SignalObservations (boundary pressure signals)
        paragraph_count: Total paragraph count in the analyzed text
        tfidf_similarities: Adjacent-paragraph TF-IDF cosine similarity curve.
            Length = paragraph_count - 1. Index i = similarity(p_i, p_{i+1}).
        texttiling_scores: TextTiling lexical overlap score curve.
            Length = paragraph_count - 1. Empty if TextTiling was not run.
            Lower score = more likely topic boundary.
        valley_positions: Boundary gap indices where valleys were detected.
            Index i means the gap between p_i and p_{i+1}.
        valley_confidences: Confidence values for each valley (parallel to valley_positions).
    """
    observations: List[SignalObservation]
    paragraph_count: int
    tfidf_similarities: List[float]
    texttiling_scores: List[float]
    valley_positions: List[int]
    valley_confidences: List[float]

    def to_json_dict(self) -> dict:
        """Serialize to JSON-compatible dict for Dashboard embedding."""
        return {
            "paragraph_count": self.paragraph_count,
            "tfidf_similarities": self.tfidf_similarities,
            "texttiling_scores": self.texttiling_scores,
            "valley_positions": self.valley_positions,
            "valley_confidences": self.valley_confidences,
            "boundary_pressures": [
                {
                    "paragraph_idx": pos,
                    "confidence": conf,
                    "sources": obs.sub_type.split("+") if obs.sub_type else [],
                    # Actual similarity scores at this valley position (for display)
                    "tfidf_score": self.tfidf_similarities[pos] if pos < len(self.tfidf_similarities) else None,
                    "tt_score": self.texttiling_scores[pos] if self.texttiling_scores and pos < len(self.texttiling_scores) else None,
                }
                for pos, conf, obs in zip(
                    self.valley_positions,
                    self.valley_confidences,
                    self.observations,
                )
            ],
        }
