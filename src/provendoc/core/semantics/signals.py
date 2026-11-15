"""Unified Signal Layer — format-level and discourse-level observable signals.

This is the SINGLE truth source for "what was observed" in text.
No other module should define signal types or detectors.

Two signal tiers:

1. Format-level signals (SignalType enum):
   HEADING_SIGNAL, CODE_SIGNAL, TABLE_SIGNAL, LIST_SIGNAL,
   DISCOURSE_CUE, STRUCTURAL_BOUNDARY

   These are format-agnostic surface features that any parser can observe.
   "I see a code block" ✓
   "This code explains the previous paragraph" ✗ (that's mapping, not observation)

2. Discourse-level cues (DiscourseCueType enum):
   EXPLANATION_MARKER, TRANSITION_PHRASE, TOPIC_SENTENCE,
   CONTINUATION, STRUCTURAL_BOUNDARY

   These are sub-types of SignalType.DISCOURSE_CUE, detected by
   DiscourseSignalDetector and lifted into SignalObservation.sub_type.

Pipeline:
    text → SignalExtractor.extract() → List[SignalObservation]
    → SignalSemanticMapper.map() → MappedSemantics
    → RepresentationPolicy.apply() → chunks
"""

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

# ==================================
# Format-level Signal Types
# ==================================


class SignalType(Enum):
    """Cross-format observable signal types.

    These are surface signals that parsers can directly observe
    without semantic reasoning.
    """
    HEADING_SIGNAL = auto()
    """Heading structure signal.
    Detection: markdown # heading, HTML <h1>-<h6>, logical heading patterns.
    Semantic mapping: → hierarchy_path, SemanticRole.REFERENCE
    """

    CODE_SIGNAL = auto()
    """Code block signal.
    Detection: ``` fenced blocks, 4-space indent blocks, <pre>/<code> tags.
    Semantic mapping: → locality_scope="code_block", integrity constraint
    """

    TABLE_SIGNAL = auto()
    """Table signal.
    Detection: markdown | tables, HTML <table>, TSV structure, ASCII tables.
    Semantic mapping: → locality_scope="table", integrity constraint
    """

    LIST_SIGNAL = auto()
    """List signal.
    Detection: -, *, + bullets, 1. numbered, <ul>/<ol> tags.
    Semantic mapping: → locality_scope="list_group", discourse cue
    """

    DISCOURSE_CUE = auto()
    """Discourse cue signal.
    Detection: "for example", "therefore", "however", "例如", "因此".
    Sub-types: provided by DiscourseCueType / DiscourseSignalDetector.
    Semantic mapping: → discourse_cues, SemanticRole (EXAMPLE/EXPLANATION/TRANSITION)
    """

    STRUCTURAL_BOUNDARY = auto()
    """Structural boundary signal.
    Detection: blank line + indent change + format switch (code→prose, list→paragraph).
    Semantic mapping: → split candidate, is_separable_from judgment
    """

    COHERENCE_SHIFT = auto()
    """Semantic coherence shift signal.
    Detection: TF-IDF cosine similarity valley between adjacent paragraphs (Phase 1),
               TextTiling lexical density change (Phase 1),
               C99 similarity matrix diagonal structure (Phase 3, not yet implemented).
    Semantic mapping: → boundary_pressure, weak split candidate.
    Note: confidence = boundary influence strength (0-1), not probability.
          Only fires in unstructured regions (no heading/code/table signals nearby).
    """


@dataclass(frozen=True)
class SignalObservation:
    """A single observable signal.

    Attributes:
        signal_type: signal type
        text: signal text (trigger word or structural marker)
        position: character offset in text
        span: (start, end) character range
        confidence: signal strength 0.0-1.0
        sub_type: optional sub-type (e.g. "explanation_marker" for DISCOURSE_CUE)
        context: surrounding text preview (for debugging)
    """
    signal_type: SignalType
    text: str
    position: int
    span: Tuple[int, int]
    confidence: float = 0.8
    sub_type: Optional[str] = None
    context: str = ""

    def __repr__(self) -> str:
        st = f":{self.sub_type}" if self.sub_type else ""
        return (
            f"SignalObservation({self.signal_type.name}{st}, "
            f"pos={self.position}, text='{self.text[:30]}')"
        )


# ==================================
# Discourse-level Cue Types
# ==================================


class DiscourseCueType(Enum):
    """Discourse cue sub-types — refinements of SignalType.DISCOURSE_CUE.

    These are discourse-level signals detected by pattern matching,
    not NLP models. They guide representation boundary decisions.
    """
    EXPLANATION_MARKER = "explanation_marker"
    TRANSITION_PHRASE = "transition_phrase"
    TOPIC_SENTENCE = "topic_sentence"
    CONTINUATION = "continuation"
    STRUCTURAL_BOUNDARY = "structural_boundary"


@dataclass
class DiscourseSignal:
    """A single discourse signal."""
    signal_type: DiscourseCueType
    text: str
    position: int
    span: Tuple[int, int]
    confidence: float
    context: str


# ==================================
# Discourse Pattern Tables
# ==================================

EXPLANATION_MARKERS = {
    "例如": 0.9,
    "比如": 0.9,
    "举例来说": 0.95,
    "如下示例": 0.95,
    "如下所示": 0.95,
    "如下": 0.8,
    "示例如下": 0.9,
    "具体实现如下": 0.9,
    "代码如下": 0.95,
    "for example": 0.9,
    "e.g.": 0.85,
    "for instance": 0.9,
    "such as": 0.8,
    "as shown below": 0.9,
    "see the following": 0.85,
}

TRANSITION_PHRASES = {
    "然而": 0.8,
    "但是": 0.8,
    "不过": 0.7,
    "尽管如此": 0.85,
    "此外": 0.75,
    "另外": 0.75,
    "除此之外": 0.8,
    "不仅如此": 0.85,
    "综上所述": 0.9,
    "总之": 0.85,
    "总而言之": 0.9,
    "最后": 0.7,
    "另一方面": 0.85,
    "相比之下": 0.85,
    "相反": 0.8,
    "however": 0.8,
    "therefore": 0.85,
    "thus": 0.8,
    "consequently": 0.85,
    "in conclusion": 0.9,
    "on the other hand": 0.85,
    "furthermore": 0.75,
    "moreover": 0.75,
}

TOPIC_SENTENCE_PATTERNS = [
    r'^(?:本文|本节|本章|本部分|本指南|本文档).*?(?:介绍|描述|说明|讨论|讲解)',
    r'^(?:以下|下面).*?(?:介绍|描述|说明|讨论|讲解)',
    r'^(?:什么是|什么是|何谓).*?\?',
    r'^[\w\s]+(?:是|指|表示|定义为).*?(?:一种|一个|一类)',
    r'^(?:This|This section|This chapter|This guide).*?(?:describes|explains|introduces|discusses)',
    r'^(?:What is|What are|How to).*?\?',
    r'^[A-Z][\w\s]+(?:is|are|refers to|means|defines)',
]

CONTINUATION_PATTERNS = {
    "pronouns": [
        r'\b(它|他|她|它们|他们|她们|这个|那个|这些|那些|上述|前者|后者)\b',
        r'\b(it|its|they|them|their|this|that|these|those|the former|the latter)\b',
    ],
    "coherent_verbs": [
        r'\b(继续|接着|然后|随后|接下来|最后|最终)\b',
        r'\b(continue|proceed|then|next|afterwards|finally|ultimately)\b',
    ],
    "ellipsis": [
        r'\b(同上|同前|如前所述|如前所述|详见上文|见上文)\b',
        r'\b(as above|as mentioned|as discussed|see above)\b',
    ],
}

STRUCTURAL_BOUNDARY_PATTERNS = [
    r'^\s*[-*+]\s+.*\n\s*\n',
    r'^\s*\d+\.\s+.*\n\s*\n',
    r'^\s*#{1,6}\s+',
    r'^\s*[-=]{3,}\s*$',
    r'^\s*```\w*\s*$',
    r'^\s*$',
]


# ==================================
# Discourse Signal Detector
# ==================================


class DiscourseSignalDetector:
    """Discourse signal detector.

    Detects discourse signals in text to guide representation
    boundary decisions. Rule-based, no NLP models.
    """

    def __init__(self):
        """Initialize the detector and pre-compile all discourse patterns."""
        self._compile_patterns()

    def _compile_patterns(self):
        self.topic_patterns = [re.compile(p, re.IGNORECASE) for p in TOPIC_SENTENCE_PATTERNS]
        self.continuation_patterns = {
            category: [re.compile(p, re.IGNORECASE) for p in patterns]
            for category, patterns in CONTINUATION_PATTERNS.items()
        }
        self.boundary_patterns = [re.compile(p, re.MULTILINE) for p in STRUCTURAL_BOUNDARY_PATTERNS]

    def detect(self, text: str) -> List[DiscourseSignal]:
        """Detect all discourse signals in text."""
        signals = []
        signals.extend(self._detect_explanation_markers(text))
        signals.extend(self._detect_transition_phrases(text))
        signals.extend(self._detect_topic_sentences(text))
        signals.extend(self._detect_continuation_signals(text))
        signals.extend(self._detect_structural_boundaries(text))
        signals.sort(key=lambda s: s.position)
        return signals

    def _detect_explanation_markers(self, text: str) -> List[DiscourseSignal]:
        signals = []
        for marker, confidence in EXPLANATION_MARKERS.items():
            for match in re.finditer(re.escape(marker), text, re.IGNORECASE):
                start = max(0, match.start() - 20)
                end = min(len(text), match.end() + 20)
                signals.append(DiscourseSignal(
                    signal_type=DiscourseCueType.EXPLANATION_MARKER,
                    text=marker,
                    position=match.start(),
                    span=(match.start(), match.end()),
                    confidence=confidence,
                    context=text[start:end],
                ))
        return signals

    def _detect_transition_phrases(self, text: str) -> List[DiscourseSignal]:
        signals = []
        for phrase, confidence in TRANSITION_PHRASES.items():
            for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
                start = max(0, match.start() - 20)
                end = min(len(text), match.end() + 20)
                signals.append(DiscourseSignal(
                    signal_type=DiscourseCueType.TRANSITION_PHRASE,
                    text=phrase,
                    position=match.start(),
                    span=(match.start(), match.end()),
                    confidence=confidence,
                    context=text[start:end],
                ))
        return signals

    def _detect_topic_sentences(self, text: str) -> List[DiscourseSignal]:
        signals = []
        paragraphs = text.split('\n\n')
        offset = 0
        for para in paragraphs:
            first_line = para.split('\n')[0].strip()
            for pattern in self.topic_patterns:
                match = pattern.search(first_line)
                if match:
                    signals.append(DiscourseSignal(
                        signal_type=DiscourseCueType.TOPIC_SENTENCE,
                        text=first_line[:50],
                        position=offset,
                        span=(offset, offset + len(first_line)),
                        confidence=0.8,
                        context=para[:100],
                    ))
                    break
            offset += len(para) + 2
        return signals

    def _detect_continuation_signals(self, text: str) -> List[DiscourseSignal]:
        signals = []
        for category, patterns in self.continuation_patterns.items():
            for pattern in patterns:
                for match in pattern.finditer(text):
                    start = max(0, match.start() - 15)
                    end = min(len(text), match.end() + 15)
                    signals.append(DiscourseSignal(
                        signal_type=DiscourseCueType.CONTINUATION,
                        text=match.group(),
                        position=match.start(),
                        span=(match.start(), match.end()),
                        confidence=0.75,
                        context=text[start:end],
                    ))
        return signals

    def _detect_structural_boundaries(self, text: str) -> List[DiscourseSignal]:
        signals = []
        for pattern in self.boundary_patterns:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 10)
                end = min(len(text), match.end() + 10)
                signals.append(DiscourseSignal(
                    signal_type=DiscourseCueType.STRUCTURAL_BOUNDARY,
                    text=match.group()[:20],
                    position=match.start(),
                    span=(match.start(), match.end()),
                    confidence=0.9,
                    context=text[start:end],
                ))
        return signals

    def get_no_split_regions(self, text: str, window_size: int = 50) -> List[Tuple[int, int]]:
        """Get 'no-split' regions based on discourse signals."""
        signals = self.detect(text)
        regions = []
        for signal in signals:
            if signal.signal_type in (DiscourseCueType.EXPLANATION_MARKER, DiscourseCueType.CONTINUATION):
                start = max(0, signal.position - window_size)
                end = min(len(text), signal.span[1] + window_size)
                regions.append((start, end))
        if not regions:
            return []
        regions.sort()
        merged = [regions[0]]
        for current in regions[1:]:
            last = merged[-1]
            if current[0] <= last[1]:
                merged[-1] = (last[0], max(last[1], current[1]))
            else:
                merged.append(current)
        return merged

    def get_split_candidates(self, text: str) -> List[Tuple[int, float]]:
        """Get recommended split positions based on structural boundaries."""
        signals = self.detect(text)
        candidates = []
        for signal in signals:
            if signal.signal_type == DiscourseCueType.STRUCTURAL_BOUNDARY:
                candidates.append((signal.position, signal.confidence))
        return candidates


# ==================================
# Signal Extractor (format-level)
# ==================================


class SignalExtractor:
    """Cross-format signal extractor.

    Extracts SignalType from surface features without semantic interpretation.
    Internally composes DiscourseSignalDetector for DISCOURSE_CUE signals.
    """

    def __init__(self):
        """Initialize the extractor and pre-compile format-level surface patterns."""
        self._compile_patterns()

    def _compile_patterns(self):
        # (pattern, level_rule): a markdown # heading takes its level
        # from the # run length; 【】 and 第X章/节/条 headings expose no
        # #-run — 【】 conservatively maps to h1, 第X章/节/条 maps by
        # unit word (章1/节2/条3). Heading text length is never used
        # as a level: SignalSemanticMapper.map reads sub_type "h<N>"
        # and takes N as an int heading_level — deriving level from
        # text length would produce h8/h12-style pollution.
        self._heading_patterns = [
            (re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE), "hashes"),
            (re.compile(r"^【(.+?)】\s*$", re.MULTILINE), "bracket"),
            (
                re.compile(
                    r"^第[一二三四五六七八九十百千万\d]+([条章节])\s*(.*)$",
                    re.MULTILINE,
                ),
                "cn_unit",
            ),
        ]
        self._fenced_code_start = re.compile(r"^```(\w*)\s*$", re.MULTILINE)
        self._fenced_code_end = re.compile(r"^```\s*$", re.MULTILINE)
        self._indented_code = re.compile(r"^(    |\t).+", re.MULTILINE)
        self._markdown_table = re.compile(r"^\|.+\|\s*$", re.MULTILINE)
        self._table_separator = re.compile(r"^\|[\s:|-]+\|\s*$", re.MULTILINE)
        self._bullet_list = re.compile(r"^\s*[-*+]\s+(.+)$", re.MULTILINE)
        self._numbered_list = re.compile(r"^\s*\d+[.)、]\s+(.+)$", re.MULTILINE)
        self._blank_line = re.compile(r"^\s*$", re.MULTILINE)
        self._separator_line = re.compile(r"^[-=]{3,}\s*$", re.MULTILINE)

    def extract(
        self,
        text: str,
        coherence_observations: Optional[List["SignalObservation"]] = None,
    ) -> List[SignalObservation]:
        """Extract all observable signals from text.

        Args:
            text: input text
            coherence_observations: pre-computed COHERENCE_SHIFT observations
                from CoherenceShiftDetector. If provided, merged into output.
                CoherenceShiftDetector is NOT called here to avoid circular
                imports and keep SignalExtractor format-level only.

        Returns:
            SignalObservation list, sorted by position
        """
        signals: List[SignalObservation] = []
        signals.extend(self._detect_headings(text))
        signals.extend(self._detect_code_blocks(text))
        signals.extend(self._detect_tables(text))
        signals.extend(self._detect_lists(text))
        signals.extend(self._detect_discourse_cues(text))
        signals.extend(self._detect_structural_boundaries(text))
        if coherence_observations:
            signals.extend(coherence_observations)

        seen = set()
        unique = []
        for s in signals:
            key = (s.signal_type, s.position, s.text[:20])
            if key not in seen:
                seen.add(key)
                unique.append(s)
        unique.sort(key=lambda s: s.position)
        return unique

    #: Observable level words for 第X章/节/条 headings (章>节>条).
    _CN_UNIT_LEVELS = {"章": 1, "节": 2, "条": 3}

    def _detect_headings(self, text: str) -> List[SignalObservation]:
        signals = []
        for pattern, level_rule in self._heading_patterns:
            for match in pattern.finditer(text):
                if level_rule == "hashes":
                    level = len(match.group(1))
                elif level_rule == "cn_unit":
                    level = self._CN_UNIT_LEVELS.get(match.group(1), 1)
                else:  # bracket 【…】: no observable level evidence, conservative h1
                    level = 1
                heading_text = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(0)
                signals.append(SignalObservation(
                    signal_type=SignalType.HEADING_SIGNAL,
                    text=heading_text.strip()[:50],
                    position=match.start(),
                    span=(match.start(), match.end()),
                    confidence=0.95,
                    sub_type=f"h{level}",
                    context=self._context(text, match.start(), match.end()),
                ))
        return signals

    def _detect_code_blocks(self, text: str) -> List[SignalObservation]:
        signals = []
        for match in self._fenced_code_start.finditer(text):
            signals.append(SignalObservation(
                signal_type=SignalType.CODE_SIGNAL,
                text=match.group(0).strip(),
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.95,
                sub_type="fence_start",
                context=self._context(text, match.start(), match.end()),
            ))
        for match in self._fenced_code_end.finditer(text):
            signals.append(SignalObservation(
                signal_type=SignalType.CODE_SIGNAL,
                text=match.group(0).strip(),
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.95,
                sub_type="fence_end",
                context=self._context(text, match.start(), match.end()),
            ))
        for match in self._indented_code.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line = text[line_start:match.end()]
            if re.match(r"^\s*[-*+]\s", line) or re.match(r"^\s*\d+[.)、]\s", line):
                continue
            signals.append(SignalObservation(
                signal_type=SignalType.CODE_SIGNAL,
                text=match.group(0).strip()[:50],
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.6,
                sub_type="indented",
                context=self._context(text, match.start(), match.end()),
            ))
        return signals

    def _detect_tables(self, text: str) -> List[SignalObservation]:
        signals = []
        for match in self._markdown_table.finditer(text):
            is_sep = bool(self._table_separator.match(match.group(0)))
            signals.append(SignalObservation(
                signal_type=SignalType.TABLE_SIGNAL,
                text=match.group(0).strip()[:50],
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.9,
                sub_type="separator" if is_sep else "row",
                context=self._context(text, match.start(), match.end()),
            ))
        return signals

    def _detect_lists(self, text: str) -> List[SignalObservation]:
        signals = []
        for match in self._bullet_list.finditer(text):
            signals.append(SignalObservation(
                signal_type=SignalType.LIST_SIGNAL,
                text=match.group(1).strip()[:50],
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.85,
                sub_type="bullet",
                context=self._context(text, match.start(), match.end()),
            ))
        for match in self._numbered_list.finditer(text):
            signals.append(SignalObservation(
                signal_type=SignalType.LIST_SIGNAL,
                text=match.group(1).strip()[:50],
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.85,
                sub_type="numbered",
                context=self._context(text, match.start(), match.end()),
            ))
        return signals

    def _detect_discourse_cues(self, text: str) -> List[SignalObservation]:
        signals = []
        try:
            detector = DiscourseSignalDetector()
            discourse_signals = detector.detect(text)
            for ds in discourse_signals:
                signals.append(SignalObservation(
                    signal_type=SignalType.DISCOURSE_CUE,
                    text=ds.text,
                    position=ds.position,
                    span=ds.span,
                    confidence=ds.confidence,
                    sub_type=ds.signal_type.value,
                    context=ds.context,
                ))
        # Skip on any single-signal build failure (the signal layer is
        # best-effort enhancement; it must not block the main flow).
        except Exception:
            pass
        return signals

    def _detect_structural_boundaries(self, text: str) -> List[SignalObservation]:
        signals = []
        for match in self._blank_line.finditer(text):
            if match.start() > 0 and match.end() < len(text):
                signals.append(SignalObservation(
                    signal_type=SignalType.STRUCTURAL_BOUNDARY,
                    text="<blank>",
                    position=match.start(),
                    span=(match.start(), match.end()),
                    confidence=0.7,
                    sub_type="blank_line",
                    context=self._context(text, match.start(), match.end()),
                ))
        for match in self._separator_line.finditer(text):
            signals.append(SignalObservation(
                signal_type=SignalType.STRUCTURAL_BOUNDARY,
                text=match.group(0).strip(),
                position=match.start(),
                span=(match.start(), match.end()),
                confidence=0.85,
                sub_type="separator",
                context=self._context(text, match.start(), match.end()),
            ))
        return signals

    @staticmethod
    def _context(text: str, start: int, end: int, window: int = 20) -> str:
        s = max(0, start - window)
        e = min(len(text), end + window)
        return text[s:e]


# ==================================
# Discourse-aware Boundary Policy
# ==================================


class DiscourseAwareBoundaryPolicy:
    """Discourse-aware boundary policy.

    Considers discourse signals during representation splitting
    to avoid cutting at semantically coherent points.
    """

    def __init__(self, window_size: int = 50):
        """Initialize the policy.

        Args:
            window_size: radius (in characters) for judging whether a
                boundary is close to a discourse signal.
        """
        self.detector = DiscourseSignalDetector()
        self.window_size = window_size

    def is_valid_boundary(self, text: str, position: int) -> Tuple[bool, str]:
        """Judge whether position is a splittable boundary.

        Inside a no-split region with a nearby explanation/
        continuation discourse signal it is not splittable (cutting at
        a semantically coherent point harms unit integrity).

        Args:
            text: full text.
            position: candidate split offset (characters).

        Returns:
            (is_valid, human-readable reason).
        """
        no_split_regions = self.detector.get_no_split_regions(text, self.window_size)
        for start, end in no_split_regions:
            if start <= position <= end:
                signals = self.detector.detect(text)
                for signal in signals:
                    if signal.signal_type in (DiscourseCueType.EXPLANATION_MARKER, DiscourseCueType.CONTINUATION):
                        if signal.span[0] - self.window_size <= position <= signal.span[1] + self.window_size:
                            return False, f"Near {signal.signal_type.value}: '{signal.text}'"
        return True, "Valid boundary"

    def find_best_boundary(self, text: str, preferred_position: int) -> Tuple[int, str]:
        """Find the best split point near a preferred position.

        If the preferred position itself is splittable it is used
        directly; otherwise the nearest structural-boundary candidate
        is taken; with no candidates the preferred position stands.

        Args:
            text: full text.
            preferred_position: preferred split offset (characters).

        Returns:
            (actual split position, human-readable reason).
        """
        valid, reason = self.is_valid_boundary(text, preferred_position)
        if valid:
            return preferred_position, reason
        candidates = self.detector.get_split_candidates(text)
        if not candidates:
            return preferred_position, "No structural boundaries found, using preferred"
        best_pos, best_conf = min(
            candidates,
            key=lambda c: abs(c[0] - preferred_position)
        )
        return best_pos, f"Adjusted to structural boundary (confidence={best_conf:.2f})"


# ==================================
# Convenience function
# ==================================


def analyze_discourse(text: str) -> Dict:
    """Analyze text discourse structure.

    Returns statistics and no-split regions.
    """
    detector = DiscourseSignalDetector()
    signals = detector.detect(text)
    stats: Dict[str, Any] = {
        "total_signals": len(signals),
        "by_type": {},
        "avg_confidence": sum(s.confidence for s in signals) / len(signals) if signals else 0,
    }
    for signal_type in DiscourseCueType:
        count = len([s for s in signals if s.signal_type == signal_type])
        if count > 0:
            stats["by_type"][signal_type.value] = count
    return {
        "signals": signals,
        "no_split_regions": detector.get_no_split_regions(text),
        "split_candidates": detector.get_split_candidates(text),
        "stats": stats,
    }
