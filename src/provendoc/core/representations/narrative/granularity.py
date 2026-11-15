"""Three-granularity splitter — coarse and fine implementations.

Produced alongside the base granularity (existing discourse-unit
chunking); each layer independently emits RepresentationUnits.

Design: the three layers are stored side by side, not as
replacements. Coarse merges adjacent sections across headings for
macro retrieval; fine splits over-budget paragraphs at sentence
boundaries for precise retrieval, carrying a leading overlap (a
four-tier fallback ladder) for context continuity. All outputs
have the granularity field set.

Coarse merge strategy: adjacent sections merge when heading levels
are equal or deeper; an optional cohesion check (topic-word Jaccard
overlap + last/first-sentence TF-IDF similarity, weighted) can
reject low-cohesion merges; merges are capped by an absolute hard
max; text without headings joins the nearest section.

Fine split strategy: triggered only when a paragraph exceeds the
chunk_size budget (nothing is emitted for in-budget paragraphs —
the base layer already holds that text); cut points fall only on
sentence boundaries; an over-budget single sentence becomes its
own flagged block instead of being cut; chunk_size is fixed
(uniform anchor) or adaptive (sentence budget × document P75
sentence length, clamped); adaptivity reads only observable
document facts (sentence-length distribution) and is
deterministic per input.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    StructuralInfo,
    UnitType,
)
from provendoc.core.representations.narrative.sentence_boundaries import (
    find_sentence_boundary,
)
from provendoc.core.semantics.coherence import (
    _build_tfidf_vectors,
    _cosine_similarity,
    _tokenize,
)


def _split_sentences(text: str) -> List[str]:
    """Split paragraph text into sentences (fine chunking's sentence rule).

    Splits on CJK and ASCII sentence-final punctuation (。！？.!?):
    CJK punctuation splits with or without following whitespace;
    ASCII punctuation requires whitespace (avoiding decimals,
    abbreviations, URLs). Empty and too-short fragments are dropped.
    """
    # split on newlines first, then sentence-final punctuation
    raw = re.split(r'(?<=[。！？])\s*|(?<=[.!?])\s+|(?<=\n)\s*', text)
    sentences = []
    for s in raw:
        s = s.strip()
        if s and len(s) > 3:  # 过滤过短的碎片
            sentences.append(s)
    return sentences

# ==================================
# coarse-grained configuration
# ==================================


@dataclass
class CoarseGrainedConfig:
    """Structure-first, adaptive coarse-granularity configuration.

    Coarse does not reuse the base layer's runtime packaging limits.
    Document structure is the primary boundary; a soft target is
    computed from the whole-document size: short documents keep
    full context, consecutive short sibling sections may merge; only
    the absolute hard max forces a degraded split inside a section.

    ``coarse_token_limit`` is kept for legacy callers only. When
    passed explicitly it overrides ``hard_max_chars`` but plays no
    part in normal grouping decisions.
    """
    strategy: str = "structure_adaptive"
    relative_target_ratio: float = 0.35
    min_target_chars: int = 800
    max_target_chars: int = 6000
    hard_max_chars: int = 9000
    short_document_chars: int = 1200
    short_section_chars: int = 300
    merge_same_level: bool = True
    merge_deeper_level: bool = True
    # legacy-API compat; None means use the structure-adaptive hard_max_chars.
    coarse_token_limit: Optional[int] = None
    # container locality barrier (pptx slide / epub spine chapter):
    # when True, coarse merging never crosses the container identity
    # declared in unit metadata (container_kind + container_index) —
    # chapters/slides are native semantic boundaries of binary
    # containers, and a cross-container pack would fuse two chapters
    # or two slides into one retrieval unit. Off by default: formats
    # without container semantics (txt/md/docx) are unaffected, and
    # container-aware representation layers enable it explicitly.
    container_locality: bool = False
    # cohesion is an optional guard and must not override the
    # short-sibling structural merge rule.
    cohesion_threshold: float = 0.0
    cohesion_topic_weight: float = 0.50
    cohesion_boundary_weight: float = 0.35
    # the turbulence signal is retired; the field stays for policy
    # round-trip compat and takes no part in scoring.
    cohesion_turbulence_weight: float = 0.15

    @classmethod
    def from_policy(cls, policy) -> "CoarseGrainedConfig":
        """Convert RepresentationPolicy.coarse (pure policy config) into splitter config.

        All narrative-structure text representations
        (txt/markdown/html/docx) funnel through this entry point so
        run-level tuning applies to every text format; base
        packaging length limits do not enter coarse.
        """
        from provendoc.core.strategies.representation_policy import CoarseGranularityPolicy

        if not isinstance(policy, CoarseGranularityPolicy):
            policy = CoarseGranularityPolicy()
        return cls(
            strategy=policy.strategy,
            relative_target_ratio=policy.relative_target_ratio,
            min_target_chars=policy.min_target_chars,
            max_target_chars=policy.max_target_chars,
            hard_max_chars=policy.hard_max_chars,
            short_document_chars=policy.short_document_chars,
            short_section_chars=policy.short_section_chars,
            merge_same_level=policy.merge_same_level,
            merge_deeper_level=policy.merge_deeper_level,
            cohesion_threshold=policy.cohesion_threshold,
            cohesion_topic_weight=policy.cohesion_topic_weight,
            cohesion_boundary_weight=policy.cohesion_boundary_weight,
            cohesion_turbulence_weight=policy.cohesion_turbulence_weight,
        )

    def effective_hard_max_chars(self) -> int:
        """Return the effective absolute max (legacy coarse_token_limit compat)."""
        return max(1, self.coarse_token_limit or self.hard_max_chars)

    def target_chars_for(self, document_chars: int) -> int:
        """Compute the coarse soft target from document size, clamped to absolute bounds."""
        target = round(max(document_chars, 1) * self.relative_target_ratio)
        target = max(self.min_target_chars, target)
        target = min(self.max_target_chars, target)
        return min(target, self.effective_hard_max_chars())


# ==================================
# fine-grained configuration
# ==================================


@dataclass
class FineGrainedConfig:
    """Fine-grained split configuration (chunk_size budget trigger).

    Fine is budget-triggered on top of base (paragraphs); overlap
    follows a "repeat the previous sentence" four-tier fallback
    ladder. All constants are rough estimates, not calibrated for
    this project.

    Attributes:
        mode: "adaptive" (default arm: sentence budget × document
            P75 sentence length, clamped to
            [clamp_low_ratio ×, clamp_high_ratio ×] anchor) or
            "fixed" (control arm: one uniform anchor for the corpus).
        anchor_chars: global anchor (fixed-arm chunk_size /
            adaptive-arm clamp-band center).
        sentence_budget: adaptive-arm sentence budget N.
        clamp_low_ratio / clamp_high_ratio: clamp band bounds
            (anchor multiples).
        overlap_window_chars / overlap_window_ratio: overlap search
            window W = min(220, ratio × chunk_size).
        overlap_ratio: overlap ratio for the ratio band / raw_chars
            tiers (18%).
        ratio_band_ratio: half-width of the ratio-band tolerance
            ([0.5L, 1.5L] around the overlap target L).
        min_fine_chars: minimum fine block size; smaller blocks
            merge into the previous block (no fragments).
    """

    mode: str = "adaptive"
    anchor_chars: int = 512
    sentence_budget: int = 8
    clamp_low_ratio: float = 0.6
    clamp_high_ratio: float = 1.4
    overlap_window_chars: int = 220
    overlap_window_ratio: float = 0.5
    overlap_ratio: float = 0.18
    ratio_band_ratio: float = 0.5
    min_fine_chars: int = 30

    @classmethod
    def from_policy(cls, policy) -> "FineGrainedConfig":
        """Convert RepresentationPolicy.fine (pure policy config) into splitter config."""
        from provendoc.core.strategies.representation_policy import FineGranularityPolicy

        if not isinstance(policy, FineGranularityPolicy):
            policy = FineGranularityPolicy()
        return cls(
            mode=policy.mode,
            anchor_chars=policy.anchor_chars,
            sentence_budget=policy.sentence_budget,
            clamp_low_ratio=policy.clamp_low_ratio,
            clamp_high_ratio=policy.clamp_high_ratio,
            overlap_window_chars=policy.overlap_window_chars,
            overlap_window_ratio=policy.overlap_window_ratio,
            overlap_ratio=policy.overlap_ratio,
            ratio_band_ratio=policy.ratio_band_ratio,
            min_fine_chars=policy.min_fine_chars,
        )


# ==================================
# Cohesion pressure helpers
# ==================================


def _topic_word_overlap(text_a: str, text_b: str, top_n: int = 15) -> float:
    """Compute the topic-word overlap (Jaccard) of two texts.

    Topic words are each text's top_n by term frequency.
    Returns |intersection| / |union| in [0, 1].
    """
    def top_words(text: str) -> Set[str]:
        """Return the text's top_n high-frequency words."""
        tokens = _tokenize(text)
        if not tokens:
            return set()
        freq = Counter(tokens)
        return {w for w, _ in freq.most_common(top_n)}

    words_a = top_words(text_a)
    words_b = top_words(text_b)
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _boundary_sentence_similarity(text_a: str, text_b: str) -> float:
    """Compute the TF-IDF cosine similarity of text_a's last sentence and text_b's first.

    Used to check coherence at the semantic boundary between two
    sections.
    """
    # last/first sentence: simple split on sentence-final punctuation
    def last_sentence(text: str) -> str:
        """Return the last sentence (or the final 100 chars if no split)."""
        parts = re.split(r'(?<=[。！？.!?\n])\s*', text.strip())
        parts = [p.strip() for p in parts if p.strip()]
        return parts[-1] if parts else text[-100:]

    def first_sentence(text: str) -> str:
        """Return the first sentence (or the first 100 chars if no split)."""
        parts = re.split(r'(?<=[。！？.!?\n])\s*', text.strip())
        parts = [p.strip() for p in parts if p.strip()]
        return parts[0] if parts else text[:100]

    sent_a = last_sentence(text_a)
    sent_b = first_sentence(text_b)

    if not sent_a or not sent_b:
        return 0.0

    vecs = _build_tfidf_vectors([sent_a, sent_b])
    if len(vecs) < 2:
        return 0.0
    return _cosine_similarity(vecs[0], vecs[1])


def _cohesion_score(
    unit_a: RepresentationUnit,
    unit_b: RepresentationUnit,
    topic_w: float,
    boundary_w: float,
) -> float:
    """Compute the semantic cohesion score [0, 1] of two adjacent units.

    Weighted fusion of two signals:
        - topic_overlap: topic-word overlap (lexical, structure-free)
        - boundary_similarity: TF-IDF similarity of the boundary sentences
    """
    topic = _topic_word_overlap(unit_a.text, unit_b.text)
    boundary = _boundary_sentence_similarity(unit_a.text, unit_b.text)
    return topic * topic_w + boundary * boundary_w


# ==================================
# coarse-grained splitter
# ==================================


@dataclass
class _CoarseSection:
    """A structural section: consecutive base units under one heading path."""

    units: List[RepresentationUnit]
    heading_path: List[str]
    heading_level: int


class CoarseGrainedSplitter:
    """Produce coarse context packs from sections, adaptive to document size.

    Structure beats length: only same-parent siblings or
    parent-child sections merge; the relative target only decides
    when to stop growing. Short documents keep whole-document
    context, consecutive short siblings may slightly exceed the
    target, and only the absolute hard max forces a split inside a
    section.
    """

    def __init__(self, config: Optional[CoarseGrainedConfig] = None):
        """Initialize the coarse splitter; None uses the default CoarseGrainedConfig."""
        self.config = config or CoarseGrainedConfig()

    def merge(self, units: List[RepresentationUnit]) -> List[RepresentationUnit]:
        """Produce structure-first coarse units from ordered base units."""
        if not units:
            return []

        sections = self._build_sections(units)
        document_chars = sum(len(unit.text) for unit in units)
        target_chars = self.config.target_chars_for(document_chars)
        groups = self._pack_sections(sections, document_chars, target_chars)

        coarse_units: List[RepresentationUnit] = []
        for group, reasons in groups:
            coarse_units.extend(
                self._make_group_units(group, reasons, document_chars, target_chars)
            )
        return coarse_units

    @staticmethod
    def _heading_path(unit: RepresentationUnit) -> List[str]:
        """Support both legacy storage locations of heading_path."""
        return list(unit.heading_path or unit.structural_info.heading_path or [])

    @staticmethod
    def _container_key(unit: RepresentationUnit):
        """The unit's declared container identity (container_kind, container_index).

        Returns None when undeclared; two Nones are the same (plain
        text without container semantics), while None vs a declared
        identity differ (unknown never merges with known).
        """
        metadata = unit.metadata or {}
        if "container_index" not in metadata:
            return None
        return (metadata.get("container_kind"), metadata.get("container_index"))

    def _build_sections(self, units: List[RepresentationUnit]) -> List[_CoarseSection]:
        """Collapse consecutive paragraphs under one heading path into indivisible section atoms.

        With container_locality on, a container identity change is
        also a section boundary (defensive self-consistency for the
        splitter).
        """
        sections: List[_CoarseSection] = []
        current_units: List[RepresentationUnit] = []
        current_path: List[str] = []
        current_level = 0

        for unit in units:
            path = self._heading_path(unit)
            level = unit.structural_info.heading_level or len(path)
            container_changed = (
                self.config.container_locality
                and current_units
                and self._container_key(unit)
                != self._container_key(current_units[-1])
            )
            if current_units and (path != current_path or container_changed):
                sections.append(_CoarseSection(current_units, current_path, current_level))
                current_units = []
            if not current_units:
                current_path = path
                current_level = level
            current_units.append(unit)

        if current_units:
            sections.append(_CoarseSection(current_units, current_path, current_level))
        return sections

    @staticmethod
    def _section_text(section: _CoarseSection) -> str:
        """Emit the section heading once instead of repeating it per paragraph."""
        body = "\n\n".join(unit.text.strip() for unit in section.units if unit.text.strip())
        if section.heading_path:
            return f"[{section.heading_path[-1]}]\n{body}" if body else f"[{section.heading_path[-1]}]"
        return body

    def _related_by_structure(self, left: _CoarseSection, right: _CoarseSection) -> bool:
        """Allow only enabled sibling or parent-child continuations to merge across sections.

        With container_locality on, sections with different container
        identities (slide/chapter boundaries) are never related —
        the container barrier overrides all structure.
        """
        if self.config.container_locality and (
            self._container_key(left.units[0])
            != self._container_key(right.units[0])
        ):
            return False
        left_path = tuple(left.heading_path)
        right_path = tuple(right.heading_path)
        if not left_path and not right_path:
            return True
        if not left_path or not right_path:
            return False
        same_parent = (
            self.config.merge_same_level
            and left_path[:-1] == right_path[:-1]
        )
        parent_child = (
            self.config.merge_deeper_level
            and right_path[:len(left_path)] == left_path
        )
        return same_parent or parent_child

    @staticmethod
    def _is_short_sibling(left: _CoarseSection, right: _CoarseSection, limit: int) -> bool:
        if not left.heading_path or not right.heading_path:
            return False
        return (
            left.heading_path[:-1] == right.heading_path[:-1]
            and len(CoarseGrainedSplitter._section_text(left)) <= limit
            and len(CoarseGrainedSplitter._section_text(right)) <= limit
        )

    def _passes_cohesion(self, left: _CoarseSection, right: _CoarseSection) -> bool:
        """Optional semantic guard; off by default so scores never override explicit structure."""
        if self.config.cohesion_threshold <= 0.0:
            return True
        prev_unit = left.units[-1]
        next_unit = right.units[0]
        if len(prev_unit.text) < 20 or len(next_unit.text) < 20:
            return True
        score = _cohesion_score(
            prev_unit,
            next_unit,
            topic_w=self.config.cohesion_topic_weight,
            boundary_w=self.config.cohesion_boundary_weight,
        )
        return score >= self.config.cohesion_threshold

    def _pack_sections(
        self,
        sections: List[_CoarseSection],
        document_chars: int,
        target_chars: int,
    ) -> List[Tuple[List[_CoarseSection], List[str]]]:
        """Pack sections by structural relation; target is soft, hard max is protective."""
        if not sections:
            return []
        if document_chars <= self.config.short_document_chars:
            if self.config.container_locality:
                # the container barrier beats whole-document merging:
                # a short deck/book must not pack across slides/chapters
                packs: List[Tuple[List[_CoarseSection], List[str]]] = []
                for section in sections:
                    if packs and (
                        self._container_key(section.units[0])
                        == self._container_key(packs[-1][0][-1].units[0])
                    ):
                        packs[-1][0].append(section)
                    else:
                        packs.append(([section], ["short_document_container"]))
                return packs
            return [(sections, ["short_document_whole"])]

        hard_max = self.config.effective_hard_max_chars()
        sibling_slack = min(hard_max, target_chars + self.config.short_section_chars)
        packed: List[Tuple[List[_CoarseSection], List[str]]] = []
        current: List[_CoarseSection] = []
        current_len = 0
        reasons: List[str] = []

        for section in sections:
            section_len = len(self._section_text(section))
            if not current:
                current = [section]
                current_len = section_len
                continue

            previous = current[-1]
            related = self._related_by_structure(previous, section)
            combined_len = current_len + 2 + section_len
            short_sibling = self._is_short_sibling(
                previous, section, self.config.short_section_chars,
            )
            can_use_cohesion = not short_sibling and not (
                tuple(section.heading_path[:len(previous.heading_path)])
                == tuple(previous.heading_path)
            )
            cohesion_ok = not can_use_cohesion or self._passes_cohesion(previous, section)

            if related and combined_len <= hard_max and cohesion_ok:
                if combined_len <= target_chars:
                    current.append(section)
                    current_len = combined_len
                    reasons.append(
                        "short_sibling_same_parent" if short_sibling else "within_relative_target"
                    )
                    continue
                if short_sibling and combined_len <= sibling_slack:
                    current.append(section)
                    current_len = combined_len
                    reasons.append("short_sibling_same_parent")
                    continue

            packed.append((current, reasons or ["structural_section_boundary"]))
            current = [section]
            current_len = section_len
            reasons = []

        if current:
            packed.append((current, reasons or ["structural_section_boundary"]))
        return packed

    def _make_group_units(
        self,
        sections: List[_CoarseSection],
        reasons: List[str],
        document_chars: int,
        target_chars: int,
    ) -> List[RepresentationUnit]:
        """Build coarse units; split inside a section only when the absolute hard max is hit."""
        text = "\n\n".join(self._section_text(section) for section in sections).strip()
        source_group = [unit for section in sections for unit in section.units]
        hard_max = self.config.effective_hard_max_chars()
        if len(text) <= hard_max:
            return [self._make_coarse_unit(
                text, sections, source_group, reasons, document_chars, target_chars,
            )]

        fragments = self._split_at_hard_max(text, hard_max)
        return [
            self._make_coarse_unit(
                fragment,
                sections,
                source_group,
                list(dict.fromkeys(reasons + ["hard_max_split"])),
                document_chars,
                target_chars,
            )
            for fragment in fragments
        ]

    @staticmethod
    def _split_at_hard_max(text: str, hard_max: int) -> List[str]:
        """Hard-max degraded split preferring paragraph, sentence, and whitespace boundaries; never leaves an over-limit block.

        Cut priority matches the base layer's ``_split_at_hard_fallback``:
        \n\n > \n > sentence end > space > hard cut; when a sentence
        boundary exists, spaces drop out of contention (sentences are
        never truncated); sentence detection shares the
        sentence_boundaries module.
        """
        fragments: List[str] = []
        remaining = text.strip()
        while len(remaining) > hard_max:
            sentence_cut = find_sentence_boundary(remaining, hard_max)
            if sentence_cut > 0:
                boundary = max(
                    remaining.rfind("\n\n", 0, hard_max + 1),
                    remaining.rfind("\n", 0, hard_max + 1),
                    sentence_cut,
                )
            else:
                boundary = max(
                    remaining.rfind("\n\n", 0, hard_max + 1),
                    remaining.rfind("\n", 0, hard_max + 1),
                    remaining.rfind(" ", 0, hard_max + 1),
                )
            if boundary <= 0:
                boundary = hard_max
            fragment = remaining[:boundary].strip()
            if fragment:
                fragments.append(fragment)
            remaining = remaining[boundary:].lstrip()
        if remaining:
            fragments.append(remaining)
        return fragments

    def _make_coarse_unit(
        self,
        text: str,
        sections: List[_CoarseSection],
        source_group: List[RepresentationUnit],
        reasons: List[str],
        document_chars: int,
        target_chars: int,
    ) -> RepresentationUnit:
        """Build the formal coarse representation with policy, span, and merge reasons."""
        first = source_group[0]
        heading_path = list(sections[0].heading_path)
        all_cues: List[str] = []
        for unit in source_group:
            all_cues.extend(unit.semantic_primitives.discourse_cues)
        sequence_indices = [unit.sequence_index for unit in source_group]

        # container provenance aggregation: which slides/chapters the
        # coarse unit's base units came from (with container_locality
        # the span is always a single container; recorded faithfully
        # for downstream provenance/quality consumers)
        container_keys = [
            key for key in (self._container_key(u) for u in source_group)
            if key is not None
        ]
        container_metadata: Dict[str, Any] = {}
        if container_keys:
            container_metadata["container_kind"] = container_keys[0][0]
            indices = [k[1] for k in container_keys if isinstance(k[1], int)]
            if indices:
                container_metadata["container_span"] = [
                    min(indices), max(indices),
                ]

        return RepresentationUnit(
            text=text,
            unit_type=UnitType.NARRATIVE,
            granularity="coarse",
            sequence_index=min(sequence_indices),
            component_count=len(source_group),
            heading=first.heading,
            heading_path=heading_path,
            structural_info=StructuralInfo(
                heading_level=sections[0].heading_level,
                heading_path=heading_path,
                parent_section=first.structural_info.parent_section,
            ),
            source_context=first.source_context,
            semantic_primitives=SemanticPrimitives(
                hierarchy_path=heading_path,
                locality_scope="section",
                semantic_role="body",
                discourse_cues=list(dict.fromkeys(all_cues)),
            ),
            metadata={
                "granularity": "coarse",
                "coarse_policy": self.config.strategy,
                "coarse_document_chars": document_chars,
                "coarse_target_chars": target_chars,
                "coarse_hard_max_chars": self.config.effective_hard_max_chars(),
                "coarse_actual_chars": len(text),
                "source_unit_count": len(source_group),
                "source_section_count": len(sections),
                "source_base_range": [min(sequence_indices), max(sequence_indices)],
                "merge_reasons": list(dict.fromkeys(reasons)),
                **container_metadata,
            },
        )


# ==================================
# fine-grained splitter
# ==================================


class FineGrainedSplitter:
    """Split over-budget base units into sentence-budget blocks.

    Strategy:
        1. chunk_size depends on mode: fixed (uniform anchor) or
           adaptive (sentence budget × document P75 sentence length,
           clamped; document-level stats are computed in split_all;
           a standalone split_unit call degrades to the unit's own
           stats)
        2. paragraphs within chunk_size produce nothing (same text
           as base, no information gain)
        3. sentences accumulate from the block start; cut at the
           nearest sentence boundary once >= chunk_size
        4. an over-budget single sentence is never hard-cut — it
           becomes its own block flagged oversized_sentence
        5. non-first blocks carry a leading overlap (four-tier
           fallback ladder, see _overlap_before)

    Usage:
        splitter = FineGrainedSplitter()
        fine_units = splitter.split_all(base_units)
    """

    def __init__(self, config: Optional[FineGrainedConfig] = None):
        """Initialize the fine splitter; None uses the default FineGrainedConfig."""
        self.config = config or FineGrainedConfig()

    # ==================================
    # document-level sentence-length stats (the adaptive arm's only dynamic input)
    # ==================================

    def _sentence_stats(self, units: List[RepresentationUnit]) -> Dict[str, Any]:
        """Compute the sentence-length distribution over all (non-passthrough) units.

        Pure observable document facts; deterministic per input. P75
        uses the nearest upper neighbor (at least 75% of sentence
        lengths <= that value).
        """
        lens: List[int] = []
        for u in units:
            if (u.metadata or {}).get("chunking") == "structural_ast":
                continue
            lens.extend(len(s) for s in _split_sentences(u.text))
        if not lens:
            return {"count": 0, "p75": 0, "mean": 0.0, "median": 0.0}
        ordered = sorted(lens)
        n = len(ordered)
        p75_idx = min(n - 1, max(0, math.ceil(0.75 * n) - 1))
        return {
            "count": n,
            "p75": ordered[p75_idx],
            "mean": round(sum(ordered) / n, 2),
            "median": round(
                (ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2), 2
            ),
        }

    def _resolve_chunk_size(self, stats: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Resolve chunk_size and its size-explanation metadata.

        Returns:
            (chunk_size, size_meta) — size_meta lands in every fine
            block, recording "why am I this size"; mean/median are
            diagnostics only and take part in no decision.
        """
        cfg = self.config
        meta: Dict[str, Any] = {
            "fine_mode": cfg.mode,
            "anchor_chars": cfg.anchor_chars,
            "sentence_count": stats["count"],
            "sentence_mean_chars": stats["mean"],
            "sentence_median_chars": stats["median"],
        }
        if cfg.mode == "fixed":
            return cfg.anchor_chars, meta
        if stats["count"] == 0:
            # no observable sentence lengths → fall back to the anchor; meta records count=0
            meta.update({"chunk_size_doc": cfg.anchor_chars, "clamped": "none", "p75_sentence_chars": 0})
            return cfg.anchor_chars, meta
        p75 = stats["p75"]
        raw = round(cfg.sentence_budget * p75)
        low = max(1, round(cfg.clamp_low_ratio * cfg.anchor_chars))
        high = max(low, round(cfg.clamp_high_ratio * cfg.anchor_chars))
        clamped = "none"
        if raw < low:
            raw, clamped = low, "low"
        elif raw > high:
            raw, clamped = high, "high"
        meta.update({
            "chunk_size_doc": raw,
            "p75_sentence_chars": p75,
            "sentence_budget": cfg.sentence_budget,
            "clamped": clamped,
        })
        return raw, meta

    def split_all(self, units: List[RepresentationUnit]) -> List[RepresentationUnit]:
        """Fine-split all base units (document-level stats).

        Args:
            units: base-granularity RepresentationUnit list.

        Returns:
            Fine-granularity RepresentationUnit list, granularity="fine".
            Units within the chunk_size budget produce no fine block.
        """
        doc_stats = self._sentence_stats(units)
        fine_units = []
        for unit in units:
            fine_units.extend(self.split_unit(unit, doc_stats=doc_stats))
        return fine_units

    def split_unit(
        self,
        unit: RepresentationUnit,
        doc_stats: Optional[Dict[str, Any]] = None,
    ) -> List[RepresentationUnit]:
        """Fine-split a single base-granularity unit.

        Args:
            unit: a single base-granularity RepresentationUnit.
            doc_stats: document-level sentence stats (passed by
                split_all). None degrades to the unit's own stats
                (standalone-use behavior).

        Returns:
            Fine-granularity RepresentationUnit list. Within-budget
            paragraphs return an empty list; structural_ast code
            units pass through whole.
        """
        if (unit.metadata or {}).get("chunking") in (
            "structural_ast", "display_math"
        ):
            # structured code units (embedded code blocks / code
            # domains): sentence-splitting would cut source code —
            # integrity beats granularity, so they pass through whole
            # (unit_type/text/metadata unchanged, only the granularity
            # flag changes) and fine-only consumers lose no code.
            # display_math (transcribed TeX math atoms): splitting
            # would also cut formula blocks mid-stream — same family,
            # same whole-block passthrough.
            return [self._make_fine_passthrough_unit(unit)]

        sentences = _split_sentences(unit.text)
        if not sentences:
            return []

        if doc_stats is None:
            doc_stats = self._sentence_stats([unit])
        chunk_size, size_meta = self._resolve_chunk_size(doc_stats)

        # in-budget paragraphs produce no fine output — the blocks
        # would duplicate base text with no information gain
        if len(unit.text) <= chunk_size:
            return []

        groups = self._pack_sentences(sentences, chunk_size)
        if len(groups) <= 1:
            # a single block == the parent's full text (including an
            # over-budget lone sentence) → same text as base, no
            # information gain, produce nothing
            return []

        fine_units = []
        for group_idx, (start_idx, group, oversized) in enumerate(groups):
            if group_idx == 0:
                overlap_text, overlap_tier = "", "none"
            else:
                tail = " ".join(sentences[:start_idx])
                overlap_text, overlap_tier = self._overlap_before(
                    sentences, start_idx, chunk_size, tail,
                )
            text = " ".join(([overlap_text] if overlap_text else []) + group)
            if text == unit.text:
                # degraded block: reassembles into the parent's full
                # text — same text as base, no information gain, skip
                continue

            fine_unit = self._make_fine_unit(
                text=text,
                parent_unit=unit,
                group_idx=group_idx,
                overlap_text=overlap_text,
                overlap_tier=overlap_tier,
                size_meta=size_meta,
                oversized=oversized,
            )
            fine_units.append(fine_unit)

        return fine_units

    def _pack_sentences(
        self,
        sentences: List[str],
        chunk_size: int,
    ) -> List[Tuple[int, List[str], bool]]:
        """Pack sentences into budget groups at sentence boundaries.

        Returns:
            List of (start_idx, group_sentences, oversized).
            A tail block under min_fine_chars merges into the
            previous block (no fragments); an over-budget single
            sentence becomes its own block (sentence integrity beats
            budget, no hard cut).
        """
        groups: List[Tuple[int, List[str], bool]] = []
        cur: List[str] = []
        cur_start = 0
        cur_len = 0
        for i, s in enumerate(sentences):
            if len(s) > chunk_size:
                if cur:
                    groups.append((cur_start, cur, False))
                    cur, cur_len = [], 0
                groups.append((i, [s], True))
                cur_start = i + 1
                continue
            if not cur:
                cur_start = i
            cur.append(s)
            cur_len += len(s)
            if cur_len >= chunk_size:
                groups.append((cur_start, cur, False))
                cur, cur_len = [], 0
        if cur:
            if groups and cur_len < self.config.min_fine_chars:
                prev_start, prev_group, prev_over = groups[-1]
                groups[-1] = (prev_start, prev_group + cur, prev_over)
            else:
                groups.append((cur_start, cur, False))
        return groups

    def _overlap_before(
        self,
        sentences: List[str],
        start_idx: int,
        chunk_size: int,
        tail: str,
    ) -> Tuple[str, str]:
        """Four-tier leading-overlap fallback ladder (all leading, look-back only).

        Tier sentence: the previous sentence fits the W window →
            repeat it whole (periods and newlines both end
            sentences);
        Tier semicolon: find the last semicolon in the previous
            sentence's W-sized tail → repeat the clause after it;
        Tier ratio_band: overlap target L = 18% × chunk_size with a
            tolerance band [0.5L, 1.5L]; scan back from the block
            start for the nearest comma/period interval;
        Tier raw_chars: take 18% × chunk_size raw characters (the
            only tier allowed to cut a semantic unit; auditable via
            metadata).

        Returns:
            (overlap_text, tier).
        """
        cfg = self.config
        W = max(
            1,
            min(
                cfg.overlap_window_chars,
                round(cfg.overlap_window_ratio * chunk_size),
            ),
        )

        prev = sentences[start_idx - 1]
        if len(prev) <= W:
            return prev, "sentence"

        window = prev[-W:]
        semi = max(window.rfind("；"), window.rfind(";"))
        if semi >= 0:
            clause = window[semi + 1:].strip()
            if clause:
                return clause, "semicolon"

        target = max(1, round(cfg.overlap_ratio * chunk_size))
        low = max(1, round(target * (1.0 - cfg.ratio_band_ratio)))
        high = max(low, round(target * (1.0 + cfg.ratio_band_ratio)))
        n = len(tail)
        for i in range(1, n + 1):
            if i > high:
                break
            if tail[n - i] in "，,。！？.!?":
                if i >= low:
                    return tail[n - i:], "ratio_band"
                # punctuation too close (overlap < low) → keep looking further back

        raw_len = max(1, round(cfg.overlap_ratio * chunk_size))
        return tail[-raw_len:], "raw_chars"

    @staticmethod
    def _make_fine_passthrough_unit(
        unit: RepresentationUnit,
    ) -> RepresentationUnit:
        """Whole-block fine-layer passthrough for structural_ast code units.

        Unlike _make_fine_unit: text verbatim (no sentence
        re-packing, no overlap padding), unit_type stays CODE, and
        the source metadata (chunking/language/symbols, ...) travels
        whole so fine-only consumers see the same code facts.
        """
        return RepresentationUnit(
            text=unit.text,
            unit_type=unit.unit_type,
            granularity="fine",
            heading_path=unit.heading_path,
            structural_info=unit.structural_info,
            source_context=unit.source_context,
            semantic_primitives=unit.semantic_primitives,
            metadata={
                **(unit.metadata or {}),
                "granularity": "fine",
                "fine_passthrough": "structural_ast",
            },
        )

    def _make_fine_unit(
        self,
        text: str,
        parent_unit: RepresentationUnit,
        group_idx: int,
        overlap_text: str,
        overlap_tier: str,
        size_meta: Dict[str, Any],
        oversized: bool,
    ) -> RepresentationUnit:
        """Build a fine-granularity RepresentationUnit (budget-triggered contract).

        metadata carries the size explanation (chunk_size_doc /
        p75 / clamped, ...) and the overlap audit fields
        (overlap_tier / overlap_chars).
        """
        return RepresentationUnit(
            text=text,
            unit_type=UnitType.NARRATIVE,
            granularity="fine",
            heading_path=parent_unit.heading_path,
            structural_info=StructuralInfo(
                heading_level=parent_unit.structural_info.heading_level,
                heading_path=parent_unit.structural_info.heading_path,
                parent_section=parent_unit.structural_info.parent_section,
            ),
            source_context=parent_unit.source_context,
            semantic_primitives=SemanticPrimitives(
                hierarchy_path=parent_unit.semantic_primitives.hierarchy_path,
                locality_scope="sentence",
                semantic_role=parent_unit.semantic_primitives.semantic_role,
                # the turbulence signal is retired
                discourse_cues=[],
            ),
            metadata={
                "granularity": "fine",
                "fine_group_idx": group_idx,
                "has_overlap_before": bool(overlap_text),
                "has_overlap_after": False,
                "overlap_tier": overlap_tier,
                "overlap_chars": len(overlap_text),
                "oversized_sentence": oversized,
                "parent_heading_path": parent_unit.heading_path,
                **size_meta,
                # container provenance passthrough (pptx slide /
                # epub chapter): fine-only consumers see the same
                # container facts as base/coarse
                **{
                    key: (parent_unit.metadata or {})[key]
                    for key in ("container_kind", "container_index")
                    if key in (parent_unit.metadata or {})
                },
            },
        )


# ==================================
# three-granularity unified entry point
# ==================================


@dataclass
class MultiGranularityOutput:
    """Three-granularity output container.

    Attributes:
        coarse: coarse RepresentationUnit list (granularity="coarse")
        base: base RepresentationUnit list (granularity="base", the existing output)
        fine: fine RepresentationUnit list (granularity="fine")
        fine_enabled: whether fine granularity is enabled. When False, fine is always [].
    """
    coarse: List[RepresentationUnit] = field(default_factory=list)
    base: List[RepresentationUnit] = field(default_factory=list)
    fine: List[RepresentationUnit] = field(default_factory=list)
    fine_enabled: bool = False

    def all_units(self) -> List[RepresentationUnit]:
        """Return all units across granularities, ordered coarse → base → fine."""
        return self.coarse + self.base + self.fine

    def by_granularity(self, granularity: str) -> List[RepresentationUnit]:
        """Filter by granularity."""
        if granularity == "coarse":
            return self.coarse
        elif granularity == "base":
            return self.base
        elif granularity == "fine":
            return self.fine
        else:
            raise ValueError(f"未知粒度: {granularity!r}，应为 'coarse'/'base'/'fine'")

    def stats(self) -> Dict[str, Any]:
        """Return three-granularity statistics."""
        return {
            "coarse_count": len(self.coarse),
            "base_count": len(self.base),
            "fine_count": len(self.fine),
            "fine_enabled": self.fine_enabled,
            "total_count": len(self.coarse) + len(self.base) + len(self.fine),
            "coarse_avg_chars": _avg_chars(self.coarse),
            "base_avg_chars": _avg_chars(self.base),
            "fine_avg_chars": _avg_chars(self.fine),
        }


def _avg_chars(units: List[RepresentationUnit]) -> float:
    if not units:
        return 0.0
    return round(sum(len(u.text) for u in units) / len(units), 1)


class MultiGranularitySplitter:
    """Unified entry point for the three granularities.

    Takes base-granularity units, emits coarse and (optionally)
    fine, and returns a MultiGranularityOutput.

    Contract: coarse / base / fine are all formal representation
    views, controlled uniformly by
    RepresentationPolicy.enable_fine_grained (policy default
    True: all three granularities are persisted).

    This class's enable_fine_grained constructor default of False
    only keeps the low-level API neutral — every representation in
    the main pipeline passes the flag through from the policy and
    should not rely on this default.

    Usage:
        # main pipeline: decided by the policy (all three on by default)
        splitter = MultiGranularitySplitter(
            enable_fine_grained=policy.enable_fine_grained
        )
        output = splitter.process(base_units)

        # standalone: enable fine explicitly
        splitter = MultiGranularitySplitter(enable_fine_grained=True)
        output = splitter.process(base_units)
        # output.fine_enabled == True
        # output.fine: the fine-granularity block list
    """

    def __init__(
        self,
        coarse_config: Optional[CoarseGrainedConfig] = None,
        fine_config: Optional[FineGrainedConfig] = None,
        enable_fine_grained: bool = False,
    ):
        """Initialize the multi-granularity splitter.

        Args:
            coarse_config: coarse config (None for defaults).
            fine_config: fine config (None for defaults; the main
                pipeline should pass policy.fine converted through
                FineGrainedConfig.from_policy).
            enable_fine_grained: whether fine splitting is enabled.
                When False, FineGrainedSplitter is not created (no
                wasted resources); the main pipeline passes this
                through from the policy.
        """
        self.coarse_splitter = CoarseGrainedSplitter(coarse_config)
        # FineGrainedSplitter is created only when fine is enabled,
        # so unused runs cost nothing
        self.enable_fine_grained = enable_fine_grained
        self._fine_splitter: Optional[FineGrainedSplitter] = (
            FineGrainedSplitter(fine_config) if enable_fine_grained else None
        )

    def process(self, base_units: List[RepresentationUnit]) -> MultiGranularityOutput:
        """Process base-granularity units into three-granularity output.

        Args:
            base_units: base-granularity RepresentationUnit list
                (already in document order). Their granularity field
                is set to "base" in place.

        Returns:
            MultiGranularityOutput with all three granularities.
            The fine layer is non-empty only when
            enable_fine_grained=True.
        """
        # make sure base units carry the correct granularity field
        for unit in base_units:
            if unit.granularity != "base":
                unit.granularity = "base"

        coarse_units = self.coarse_splitter.merge(base_units)

        fine_units: List[RepresentationUnit] = []
        if self.enable_fine_grained and self._fine_splitter is not None:
            fine_units = self._fine_splitter.split_all(base_units)

        return MultiGranularityOutput(
            coarse=coarse_units,
            base=base_units,
            fine=fine_units,
            fine_enabled=self.enable_fine_grained,
        )
