"""Narrative domain semantic invariants.

4 invariants that capture the semantic properties a narrative document's
chunking MUST preserve, regardless of chunking strategy.

Invariants:
    1. HierarchyPreservation — heading/fence/table structure not broken
    2. DiscourseLocality     — topical coherence within and across chunks
    3. GranularityAppropriateness — chunk sizes match document's natural rhythm
    4. BoundaryCleanliness   — cuts at natural break points, overlap correct

==================================
Positioning (after rse_naming_spec_v1 batch 3)
==================================

These invariants are the narrative domain's only scoring track (invariant_v1).
The old six-dimension production line (NarrativeCQI's 6-dimension profile /
overall assembly layer) retired with batch 3; the four-dimension Quality Profile (structural_preservation /
semantic_continuity / granularity_appropriateness / boundary_cleanliness)
is assembled by rse_report._quality_profile as a 1:1 projection
from the invariant scores (csc candidates are reported separately
by candidate_profile, not scored).

The metric algorithms are delegated to narrative_metrics.metrics_* (single implementation).

==================================
Weight Design (historical note)
==================================

The weight values once restated here (0.35/0.25/0.25/0.15) were
superseded by the 2026-10-02 rescale (8-sample LLM audit across
txt/md/docx/pdf): the invariant registry now carries 0.30/0.15/
0.35/0.20 internally, and the single scored surface is
rse_report.QUALITY_DIMENSIONS — see the admission rationale in
the comment block there. This header intentionally no longer
restates the numbers: a second hand-synced weight surface is how
the old drift happened.
"""

import importlib
from typing import Any, Dict, Optional

from provendoc.base.models.representation_unit import UnitType

# ==================================
# Shared primitives — single source of truth
# ==================================
# Consumed from narrative_checks (shared with the metric modules).
# Historical note: this file originally carried
# COPIES of these primitives, which drifted out of sync with the
# metric modules. Importing the shared module
# makes future drift structurally impossible.
#
# The same anti-drift principle is applied one level up: the
# metric ALGORITHMS are delegated to narrative_metrics.metrics_*
# (pure-function modules) instead of being
# re-implemented here. See _mm() below.
# ==================================
from provendoc.intelligence.rse.narrative_checks import (
    FENCE_MARKS as _FENCE_MARKS,
)
from provendoc.intelligence.rse.narrative_checks import (
    HEADING_PATTERNS as _HEADING_PATTERNS,
)

# The four aliases below are a tested compat surface: the
# contract test asserts identity with narrative_checks
# (single-source guarantee) and calls them directly. Keep them.
from provendoc.intelligence.rse.narrative_checks import (
    SENTENCE_ENDS as _SENTENCE_ENDS,  # noqa: F401
)
from provendoc.intelligence.rse.narrative_checks import (
    check_ending as _check_ending,  # noqa: F401
)
from provendoc.intelligence.rse.narrative_checks import (
    check_starting as _check_starting,  # noqa: F401
)
from provendoc.intelligence.rse.narrative_checks import (
    strip_context_prefix as _strip_context_prefix,
)
from provendoc.intelligence.rse.narrative_checks import (
    tokenize as _tokenize,  # noqa: F401
)
from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    InvariantResult,
    SemanticInvariant,
)

# ==================================
# Metric algorithms — delegated, never copied
# ==================================
# The invariant track consumes the metric implementations from
# narrative_metrics.metrics_* — metric-level drift is
# structurally impossible.
# Imported lazily at first call to stay cycle-safe.
# ==================================
_METRIC_MODULES: Dict[str, Any] = {}


def _mm(name: str):
    """Lazy-load a narrative_metrics.metrics_* module (cycle-safe)."""
    mod = _METRIC_MODULES.get(name)
    if mod is None:
        mod = importlib.import_module(
            "provendoc.intelligence.rse.narrative_metrics.metrics_"
            + name
        )
        _METRIC_MODULES[name] = mod
    return mod

# ==================================
# Invariant → Quality Profile projection
# ==================================
# The four-dimension Quality Profile is assembled as a 1:1 projection
# from the invariant scores; the single source of truth for weights
# and signal ownership is rse_report._INVARIANT_TO_QUALITY + rse_report.compute_quality_score — no mirror table here.
#
# Known semantic notes (documented, not accidental):
# hierarchy_preservation's documented adaptation: see its method
# docstring. discourse_locality is pure lexical cohesion since
# 2026-10-04 (see that invariant's §split-attribution record): the
# structural-boundary term was removed — boundary evidence is billed once, in boundary_cleanliness.
# ==================================


# ==================================
# Invariant 1: HierarchyPreservation
# ==================================

class HierarchyPreservation(SemanticInvariant):
    """Heading/code-fence/table structural hierarchy is preserved.

    This invariant asks: "Does the chunking respect the document's
    structural backbone?"

    It reuses the old _structural_integrity() logic from the
    retired six-dimension evaluator, reorganized as a semantic
    assertion rather than a quality score.
    """

    #: evaluate()'s diagnostics overflow slot: the orphan-heading
    #: count from the last _compute_hierarchy_score run (written
    #: then read on every evaluation; reset to 0 on the empty-chunks early exit).
    _last_orphan_headings: int = 0

    @property
    def name(self) -> str:
        """Invariant name."""
        return "hierarchy_preservation"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Document structural elements (headings, code fences, "
            "tables) are not fragmented across chunks in ways that "
            "break their hierarchical relationships."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Lewis et al. (2020) RAG — chunk integrity affects "
            "retrieval precision. Structural preservation is a "
            "necessary condition for semantic recoverability."
        )

    @property
    def weight(self) -> float:
        """Mounting weight.

        2026-10-02 weight rescale (8-sample LLM audit, synced to
        the invariant_v1 weight table): 0.35 → 0.30. The structural
        dimension is robust but clearly ceilinged; yields to granularity.
        """
        return 0.30

    def applicable(self, chunks, document=None, strategy=None, **kwargs) -> tuple:
        """Judge whether the invariant applies (returns a reason when not)."""
        if not chunks:
            return False, "no_chunks"
        # 2026-10-04 N/A semantics fix: this invariant measures cross-chunk
        # fragmentation of structural elements (orphan headings / broken
        # fences / tables). A single chunk has no cross-chunk axis — its
        # structure is trivially intact, yet the hollow 1.0 once pumped
        # degenerate tiny docs (13-56 chars) into bad-band highs, inverting
        # the hierarchy calibration (bad 0.917 > good 0.897). Fix: honest
        # N/A; oversize-single-chunk granularity is billed to granularity_appropriateness.
        if len(chunks) <= 1:
            return False, "single_chunk"
        return True, ""

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant and return the result."""
        # Adapted structural scoring — divergences from
        # metrics_structural documented on the method
        doc_headings = []
        if document and hasattr(document, 'metadata') and document.metadata:
            doc_headings = document.metadata.get('headings', [])
        doc_text = getattr(document, 'text', '') if document else ''

        score = self._compute_hierarchy_score(chunks, doc_headings, doc_text)

        # Build diagnostics
        diagnostics = {
            "has_document_headings": bool(doc_headings),
            "has_fences": any(f in doc_text for f in _FENCE_MARKS) if doc_text else False,
            "has_tables": doc_text.count("|") >= 4 if doc_text else False,
            "chunk_count": len(chunks),
            "orphan_heading_count": self._last_orphan_headings,
        }

        # Build issues
        issues = []
        if self._last_orphan_headings > 0:
            issues.append(
                "orphan heading chunks detected (%d): heading split "
                "from its section body — retrieval hits an empty "
                "shell" % self._last_orphan_headings
            )
        if score < 0.5:
            issues.append("significant structural fragmentation detected")
        elif score < 0.7:
            issues.append("minor structural issues in some chunks")

        # Confidence: higher with more structural signals
        confidence = 0.9
        if not diagnostics["has_document_headings"] and not diagnostics["has_fences"] and not diagnostics["has_tables"]:
            confidence = 0.6  # less confident when no structural markers

        return InvariantResult(
            name=self.name,
            score=score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )

    def _compute_hierarchy_score(self, chunks, doc_headings, doc_text) -> float:
        """Hierarchy score — adapted from metrics_structural.structural_integrity().

        Deliberately NOT delegated to metrics_structural; three
        documented divergences:

        1. Table-fragmentation guard: this method checks
           ``chunk.unit_type == UnitType.NARRATIVE`` (live — real
           narrative chunks carry UnitType.NARRATIVE). The legacy
           track checks ``unit_type.value == "text"``, which no
           UnitType member has, so that check is dead there.
           Delegating would silently disable table-fragmentation
           detection in this track.
        2. No structure_provenance input: the legacy track's
           w1-c three-tier penalty (declared / inferred / none)
           requires summarize_structure_provenance(document),
           which this invariant does not compute; the middle tier
           would never fire here anyway.
        3. Orphan-heading detection, v2 (2026-10-01): the legacy
           check tests raw ``chunk.text`` — with live chunks the
           runtime-packaging "[Context: ...]" prefix inflates the
           stripped length past the 50-char bar and shifts the
           heading pattern off anchor 0, so real orphan headings
           escaped undetected (hierarchy stuck at 1.0 while
           retrieval hit empty shells). This track strips the
           prefix first and additionally treats
           ``unit_type == UnitType.HEADING`` short chunks as
           orphan signals; the violation weight is raised
           0.2 → 0.5 (empty-shell retrieval severity: between
           broken fence 1.0 and table-edge 0.3).

        Everything else (fence parity, table edges, prose
        structure-absence penalty x0.7/x0.8/x0.3/x0.5) is
        algorithm-identical to metrics_structural.
        """
        if not chunks:
            self._last_orphan_headings = 0
            return 0.0

        score = 1.0
        violations = 0.0
        orphan_headings = 0
        has_any_structure = bool(doc_headings and len(doc_headings) > 0)

        if doc_text:
            if any(f in doc_text for f in _FENCE_MARKS):
                has_any_structure = True
            if doc_text.count("|") >= 4:
                has_any_structure = True

        for i, chunk in enumerate(chunks):
            text = chunk.text

            # code fence completeness
            if chunk.unit_type == UnitType.CODE:
                fence_count = sum(text.count(f) for f in _FENCE_MARKS)
                if fence_count % 2 != 0:
                    violations += 1.0

            # table fragmentation
            if chunk.unit_type == UnitType.NARRATIVE:
                lines = text.split("\n")
                table_lines = [
                    line for line in lines
                    if "|" in line.strip() and len(line.strip()) > 3
                ]
                if i > 0 and table_lines:
                    first_tl = table_lines[0].strip()
                    if not first_tl.startswith("|") and first_tl.count("|") >= 2:
                        violations += 0.3
                if i < len(chunks) - 1 and table_lines:
                    last_tl = table_lines[-1].strip()
                    if last_tl.count("|") >= 2 and not last_tl.endswith("|"):
                        violations += 0.3

            # orphan heading (v2: strip the context prefix first —
            # see divergence 3 in the method docstring)
            if i < len(chunks) - 1:
                body = _strip_context_prefix(text).strip()
                heading_like = (
                    any(p.match(body) for p in _HEADING_PATTERNS)
                    or chunk.unit_type == UnitType.HEADING
                )
                if heading_like and len(body) < 50:
                    violations += 0.5
                    orphan_headings += 1

            # detect structural elements in chunk text
            if any(p.match(text.strip()) for p in _HEADING_PATTERNS):
                has_any_structure = True
            if any(f in text for f in _FENCE_MARKS):
                has_any_structure = True
            if "|" in text and text.strip().count("|") >= 2:
                has_any_structure = True

        score -= violations / max(len(chunks), 1)

        # structure absence penalty
        total_text = sum(len(c.text) for c in chunks)
        if not has_any_structure:
            has_prose_structure = False
            if doc_text:
                raw_paras = [p.strip() for p in doc_text.split("\n\n") if p.strip()]
                if len(raw_paras) >= 3:
                    avg_para_len = sum(len(p) for p in raw_paras) / len(raw_paras)
                    if avg_para_len >= 50:
                        has_prose_structure = True
                # v4 calibration (rse_audit_25, synced with metrics_structural):
                # Clean prose in one or very few paragraphs is NOT
                # pathological fragmentation — short news is routinely
                # 1-2 long paragraphs, no blank-line splits (thucnews:
                # a 1793-char single-line prose landed in the ×0.3 heavy
                # band). Prose with ≤3 non-blank lines and avg line length ≥50 takes the light prose band; the ×0.3 heavy band is reserved for true fragment text (indented tables / stacked short lines).
                if not has_prose_structure:
                    nonblank = [
                        ln for ln in doc_text.splitlines() if ln.strip()
                    ]
                    if (
                        0 < len(nonblank) <= 3
                        and len(doc_text.strip()) / len(nonblank) >= 50
                    ):
                        has_prose_structure = True
            if has_prose_structure:
                if total_text > 1000:
                    score *= 0.7
                elif total_text > 500:
                    score *= 0.8
            else:
                if total_text > 1000:
                    score *= 0.3
                elif total_text > 500:
                    score *= 0.5

        self._last_orphan_headings = orphan_headings
        return round(max(score, 0.0), 3)


# ==================================
# Invariant 2: DiscourseLocality
# ==================================

class DiscourseLocality(SemanticInvariant):
    """Topical coherence is preserved within and across chunks.

    This invariant asks: "Does each chunk contain a coherent topic,
    and do boundaries correspond to topical transitions?"

It composes (all delegated to narrative_metrics.metrics_* — no
private copies):
- within-chunk cohesion (half-split overlap coefficient,
  penalties judged INTO the score — metrics_cohesion.
  within_chunk_cohesion v7 + doc_ttr_discount)
- cross-chunk cohesion dip (boundary transition RELATIVE to
  flank internal continuity, evidence-volume weighted —
  metrics_cohesion.cross_boundary_scores v8)

v8 redesign record (2026-10-04, 34-doc x 7-strategy
calibration): v6's absolute dip (1-jaccard) was a guaranteed
false positive on random text — every lorem window pair is
dissimilar, so fragmented bad-band docs scored "perfect
transitions" and the band means inverted (bad 0.474 > good
0.289). The dip is now normalized against each flank's own
internal continuity (TextTiling depth semantics); post-fix
band means are monotone: bad 0.112 < acceptable 0.134 <
good 0.144. See metrics_cohesion docstrings for the formula
and evidence-validity guards.

    ==================================
    Split-attribution record (2026-10-04, owner-session approved)
    ==================================

    This invariant once additionally mixed in a 0.3 share of
    metrics_boundary.boundary_quality — the same sub-metric was
    simultaneously billed by boundary_cleanliness (0.6 share):
    double billing. Boundary evidence's real weight in the
    composite was 0.15×0.3 + 0.20×0.6 = 0.165 — higher than any
    single dimension itself. This fix deletes that share;
    discourse_locality converges to pure lexical cohesion,
    aligned with the responsibility boundaries (lexical continuity vs structural surface) declared in the metrics_cohesion / metrics_boundary docstrings.

    Deliberately opposite to the guardrails §2.1 reserved direction
    of "sentence completeness as discourse evidence": a mid-sentence
    cut is both a lexical-continuity break and a boundary-position
    error — single-point attribution (boundary evidence billed once,
    in boundary_cleanliness) avoids renamed double billing. 2026-10-01 thresholds recalibrated (see rse_report
    §QUALITY_DIMENSION_THRESHOLDS).
    """

    @property
    def name(self) -> str:
        """Invariant name."""
        return "discourse_locality"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Each chunk represents a coherent topical unit, and chunk "
            "boundaries correspond to natural discourse transitions."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Halliday & Hasan (1976) — lexical cohesion theory. "
            "Hearst (1997) TextTiling — boundary detection via "
            "lexical cohesion dip. Jaccard similarity as cohesion proxy."
        )

    @property
    def weight(self) -> float:
        """Mounting weight.

        2026-10-02 weight rescale (8-sample LLM audit): 0.25 → 0.15.
        The lexical proxy is the weakest discriminator on long literary
        text (half-split Jaccard floor saturation + cross constant);
        still a weak proxy after the v6 rebuild — down-weighted.
        """
        return 0.15

    def applicable(self, chunks, document=None, strategy=None, **kwargs) -> tuple:
        """Judge whether the invariant applies (returns a reason when not)."""
        n = len(chunks)
        total_text = sum(len(c.text) for c in chunks)
        if n <= 1:
            return False, "single_chunk"
        if total_text < 40:
            return False, "insufficient_text"
        return True, ""

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant and return the result."""
        cohesion_mod = _mm("cohesion")
        within = cohesion_mod.within_chunk_cohesion(chunks)
        boundary_scores = cohesion_mod.cross_boundary_scores(chunks)

        # v6 composition (2026-10-02 audit fix): the cross share
        # flexes with evidence volume. The old formula 0.6*within +
        # 0.4*cross banked a free 0.4×1.0 constant when all cuts
        # were structural (measured 0.94-1.0, no discrimination); below 3 non-structural cuts the share scales down and the weight returns to within.
        if boundary_scores:
            cross = round(
                sum(boundary_scores) / len(boundary_scores), 3
            )
            cross_w = 0.4 * min(1.0, len(boundary_scores) / 3.0)
            cohesion_score = round(
                (0.6 * within + cross_w * cross) / (0.6 + cross_w), 3
            )
        else:
            # v5 contract: fully structural cuts have nothing to judge,
            # no penalty; since v6 within alone carries the cohesion verdict — no auto-inherited perfect score
            cross = None
            cohesion_score = within

        # 2026-10-04 double-billing fix: the boundary_quality share is
        # deleted — discourse_locality = pure lexical cohesion (within
        # + evidence-volume-weighted cross). Structural boundary
        # evidence is billed once, in boundary_cleanliness (that metric's own formula carries it at a 0.6 share).
        final_score = cohesion_score

        diagnostics = {
            "within_chunk_cohesion": within,
            "cross_chunk_cohesion": cross,
            "cross_boundary_pairs": len(boundary_scores),
            "cohesion_composite": cohesion_score,
        }

        issues = []
        if within < 0.2:
            issues.append("very low within-chunk cohesion — chunks may mix topics")
        if cross is not None and cross < 0.3:
            issues.append("cross-chunk boundaries not at topical transitions")

        # Confidence: lower for very short chunks or repetitive text
        avg_len = sum(len(c.text) for c in chunks) / len(chunks)
        confidence = 1.0
        if avg_len < 50:
            confidence = 0.6
        elif avg_len < 100:
            confidence = 0.85

        return InvariantResult(
            name=self.name,
            score=final_score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )


# ==================================
# Invariant 3: GranularityAppropriateness
# ==================================

class GranularityAppropriateness(SemanticInvariant):
    """Chunk sizes match the document's natural granularity.

    This invariant asks: "Are chunks sized appropriately — not too
    small (orphan fragments) and not too large (losing granularity)?"

    It composes (all delegated to narrative_metrics.metrics_*):
      - intrinsic granularity (chunk sizes vs document paragraph
        structure — metrics_size.intrinsic_granularity)
      - operational alignment (chunk sizes vs configured target —
        metrics_size.operational_alignment)
      - distribution uniformity (Shannon entropy —
        metrics_distribution.distribution_uniformity)
    """

    @property
    def name(self) -> str:
        """Invariant name."""
        return "granularity_appropriateness"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Chunk sizes align with the document's natural semantic "
            "unit sizes and the configured operational target, with "
            "a balanced distribution."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Manning & Schütze (1999) — text segmentation theory. "
            "Shannon (1948) — entropy for distribution analysis. "
            "LangChain/LlamaIndex chunk size recommendations."
        )

    @property
    def weight(self) -> float:
        """Mounting weight.

        2026-10-02 weight rescale (8-sample LLM audit): 0.25 → 0.35.
        The strongest discriminator in the audit — fragmentation /
        over-granularity cuts were consistently captured; promoted to the top weight.
        """
        return 0.35

    def applicable(self, chunks, document=None, strategy=None, **kwargs) -> tuple:
        """Judge whether the invariant applies (returns a reason when not)."""
        if not chunks:
            return False, "no_chunks"
        # 2026-10-04 N/A semantics fix: when the document's total text
        # is ≤ target and the output is a single chunk, that single
        # chunk IS the only correct answer for this scale band — there
        # is no granularity decision to judge. The hollow compliance
        # score (size_compliance_short is always 1.0 for 13-56-char
        # tiny docs) once pumped the bad band into high scores,
        # inverting the granularity calibration (bad 0.931 > good 0.763).
        # Two judgeable classes remain: fragmented multi-chunk (orphan fragments still billed) and oversize single chunks (operational_alignment measurable once target_size is wired).
        total_text = sum(len(c.text) for c in chunks)
        target_size = 300
        if strategy is not None:
            _get_cs = getattr(strategy, "get_effective_chunk_size", None)
            if callable(_get_cs):
                target_size = _get_cs()
            else:
                target_size = getattr(strategy, "chunk_size", 300)
        if len(chunks) == 1 and total_text <= target_size:
            return False, "doc_fits_target_chunk"
        return True, ""

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant and return the result."""
        # 2026-10-04 wiring fix: RepresentationPolicy has no top-level
        # chunk_size field — the old getattr always fell to the
        # default 300, so the scale bands (small/large hard limits
        # 200/700) never entered scoring. Now same basis as narrative_quality.evaluate: prefer the packaging layer's get_effective_chunk_size().
        target_size = 300
        if strategy is not None:
            _get_cs = getattr(strategy, "get_effective_chunk_size", None)
            if callable(_get_cs):
                target_size = _get_cs()
            else:
                target_size = getattr(strategy, "chunk_size", 300)
        # orphan_threshold semi-retired: read the packaging layer first; the top-level field is only a legacy-object fallback
        orphan_threshold = 120
        if strategy is not None:
            packaging = getattr(strategy, "packaging", None)
            orphan_threshold = getattr(
                packaging if packaging is not None else strategy,
                "orphan_threshold", 120
            )
        total_text_length = sum(len(c.text) for c in chunks)
        short_doc_mode = total_text_length < target_size * 0.5

        # Compute sub-scores
        size_mod = _mm("size")
        if short_doc_mode:
            granularity_score = size_mod.size_compliance_short(
                chunks, total_text_length
            )
            operational_score = granularity_score  # same in short mode
            uniformity_score = 1.0  # trivially uniform for short docs
        else:
            granularity_score = size_mod.intrinsic_granularity(
                chunks, document
            )
            operational_score = size_mod.operational_alignment(
                chunks, target_size, orphan_threshold
            )
            uniformity_score = _mm(
                "distribution"
            ).distribution_uniformity(chunks)

        # Compose: 0.4 intrinsic + 0.3 operational + 0.3 uniformity
        final_score = round(
            0.4 * granularity_score
            + 0.3 * operational_score
            + 0.3 * uniformity_score,
            3,
        )

        diagnostics = {
            "intrinsic_granularity": granularity_score,
            "operational_alignment": operational_score,
            "distribution_uniformity": uniformity_score,
            "short_document_mode": short_doc_mode,
            "target_size": target_size,
            "avg_chunk_length": round(
                sum(len(c.text) for c in chunks) / len(chunks), 2
            ),
        }

        issues = []
        if not short_doc_mode:
            if granularity_score < 0.5:
                issues.append("chunk sizes don't match document's natural granularity")
            if operational_score < 0.5:
                issues.append("chunk sizes deviate from configured target")
        if uniformity_score < 0.4:
            issues.append("chunk size distribution is very uneven")

        # Confidence
        n = len(chunks)
        if n >= 5:
            confidence = 1.0
        elif n >= 3:
            confidence = 0.9
        else:
            confidence = 0.7

        return InvariantResult(
            name=self.name,
            score=final_score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )


# ==================================
# Invariant 4: BoundaryCleanliness
# ==================================

class BoundaryCleanliness(SemanticInvariant):
    """Chunk boundaries are at natural break points, and overlap is consistent.

    This invariant asks: "Do chunk boundaries fall at clean structural
    break points, and does the overlap actually work as configured?"

    It composes (both delegated to narrative_metrics.metrics_*):
      - sentence-level boundary quality
        (metrics_boundary.boundary_quality)
      - overlap consistency
        (metrics_overlap.overlap_consistency — inherits the v4
        contract: non-triggered overlap is N/A → 1.0, never a
        quality penalty)
    """

    @property
    def name(self) -> str:
        """Invariant name."""
        return "boundary_cleanliness"

    @property
    def description(self) -> str:
        """Invariant semantic description."""
        return (
            "Chunk boundaries align with sentence/paragraph/heading "
            "break points, and overlap between consecutive chunks "
            "matches the configured strategy."
        )

    @property
    def theoretical_basis(self) -> str:
        """Theoretical basis."""
        return (
            "Hearst (1997) TextTiling — boundaries should align with "
            "transitions. RAG best practices: chunk overlap should "
            "preserve sentence boundaries for context continuity."
        )

    @property
    def weight(self) -> float:
        """Mounting weight.

        2026-10-02 weight rescale (8-sample LLM audit): 0.15 → 0.20.
        Hard misses of mid-sentence / mid-paragraph cuts were
        consistently caught in the audit — real discrimination, moderately up-weighted.
        """
        return 0.20

    def applicable(self, chunks, document=None, strategy=None, **kwargs) -> tuple:
        """Judge whether the invariant applies (returns a reason when not)."""
        n = len(chunks)
        if n <= 1:
            return False, "single_chunk"
        return True, ""

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant and return the result."""
        # 2026-10-30 wiring fix (a same-family sweep of the
        # granularity_appropriateness chunk_size fix): RepresentationPolicy
        # has no top-level overlap field — the old getattr always fell
        # to the default 30, so overlap_consistency (0.4 share) was
        # forever scored against a ghost config: with the user setting
        # context_propagation.overlap_size=0 (mode "none") it still got
        # scored, and diagnostics.overlap_configured lied. Now aligned
        # with granularity: prefer packaging.get_effective_overlap()
        # (contains context_propagation.overlap_size and the
        # legacy-field compat layer); the top-level overlap field is
        # only a legacy flat-object fallback; with no strategy context, honestly treat as "no overlap configured" (composition = boundary_only; overlap_consistency stays out of the score).
        overlap = 0
        if strategy is not None:
            _get_ov = getattr(strategy, "get_effective_overlap", None)
            if callable(_get_ov):
                overlap = _get_ov()
            else:
                overlap = getattr(strategy, "overlap", 0)

        boundary_score = _mm("boundary").boundary_quality(chunks)
        overlap_score = _mm("overlap").overlap_consistency(
            chunks, overlap
        )

        # Compose: if overlap is 0, just use boundary quality
        if overlap == 0:
            final_score = boundary_score
            composition = "boundary_only (no overlap configured)"
        else:
            final_score = round(0.6 * boundary_score + 0.4 * overlap_score, 3)
            composition = "boundary(0.6) + overlap(0.4)"

        diagnostics = {
            "boundary_quality": boundary_score,
            "overlap_consistency": overlap_score,
            "overlap_configured": overlap,
            "composition": composition,
        }

        issues = []
        if boundary_score < 0.5:
            issues.append("many chunk boundaries cut mid-sentence or mid-paragraph")
        if overlap > 0 and overlap_score < 0.5:
            issues.append("overlap between chunks is missing or inconsistent")

        # Confidence
        n = len(chunks)
        avg_len = sum(len(c.text) for c in chunks) / max(n, 1)
        confidence = 1.0
        if avg_len < 50:
            confidence = 0.7
        if overlap > 0 and overlap < 10:
            confidence *= 0.8  # very short overlap is unreliable

        return InvariantResult(
            name=self.name,
            score=final_score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )


# ==================================
# Registry factory
# ==================================

def create_narrative_registry() -> InvariantRegistry:
    """Create and populate the narrative domain invariant registry."""
    registry = InvariantRegistry("narrative")
    registry.register(HierarchyPreservation())
    registry.register(DiscourseLocality())
    registry.register(GranularityAppropriateness())
    registry.register(BoundaryCleanliness())
    return registry

