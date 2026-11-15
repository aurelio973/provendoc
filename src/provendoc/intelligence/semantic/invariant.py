"""SemanticInvariant — abstract base for domain quality invariants.

A semantic invariant is a property that SHOULD hold regardless of how
the text is chunked. Example for narrative text:

    "Heading hierarchy is preserved — no chunk breaks a parent-child
     heading relationship."

This is fundamentally different from the old CQI "quality score" approach:

    Old CQI: "How good is this chunking?" (score 0-1, weighted sum)
    Invariant: "Is this semantic property preserved?" (binary / degree)

Invariants are closer to "assertions" or "contracts" than to "scores".
The degree (0.0-1.0) measures how well the invariant is satisfied,
not a subjective quality judgment.

==================================
Design Principles
==================================

1. Each invariant is self-contained: it knows its own applicability
   conditions, scoring logic, and issue messages.

2. Invariants are registered per-domain. The registry is a simple
   dict, not a global singleton. Each domain evaluator owns its registry.

3. Invariants can compose: one invariant may call another as a sub-signal.
   But the composition must be explicit in the invariant's evaluate().

4. The result includes a "diagnostics" dict for introspection — this
   is what makes the system debuggable and calibratable.

==================================
Invariant vs. Old CQI Dimension Mapping
==================================

The retired six-dimension profile mapped to invariants as:

    hierarchy_preservation       ← structural_integrity (heading/fence/table)
    discourse_locality           ← semantic_cohesion + boundary (semantic part)
    granularity_appropriateness  ← size_compliance + distribution_uniformity
    boundary_cleanliness         ← boundary_quality (sentence) + overlap_consistency

This mapping is NOT 1:1. Invariants reorganize the signal space around
semantic properties, not measurement techniques.

==================================
Academic Basis
==================================

- Invariant theory in program verification (Hoare logic, contracts)
- Semantic preservation in compilation (Morrisson & Clarke, 1982)
- Text segmentation theory (Hearst, 1997; Choi, 2000)
- The key insight: "what must be true after chunking" is a stronger
  question than "how good is the chunking"
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class InvariantResult:
    """Result of evaluating a single semantic invariant.

    Attributes:
        name:           invariant identifier (e.g. "hierarchy_preservation")
        score:          degree of satisfaction [0.0, 1.0]
                        1.0 = fully preserved, 0.0 = violated
        applicable:     whether this invariant was applicable
        applicability_reason: why not applicable (if not)
        diagnostics:    detailed signals for debugging/calibration
        issues:         list of specific violations found
        confidence:     how reliable this invariant's score is
    """
    name: str
    score: float
    applicable: bool = True
    applicability_reason: str = ""
    diagnostics: Dict = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> dict:
        """Serialize into a dict."""
        return {
            "name": self.name,
            "score": round(self.score, 3),
            "applicable": self.applicable,
            "applicability_reason": self.applicability_reason,
            "diagnostics": self.diagnostics,
            "issues": self.issues,
            "confidence": round(self.confidence, 3),
        }


class SemanticInvariant(ABC):
    """Abstract base for a semantic invariant.

    Subclasses must implement:
        - name: unique invariant identifier
        - description: human-readable explanation
        - applicable(): whether this invariant applies to the input
        - evaluate(): compute the invariant's satisfaction degree

    The evaluate() method receives the same arguments as the old CQI
    evaluate(), plus an optional "context" dict that may carry
    pre-computed signals (to avoid redundant computation across
    multiple invariants in the same evaluation pass).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique invariant identifier."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this invariant checks."""
        ...

    @property
    def theoretical_basis(self) -> str:
        """Academic grounding for this invariant."""
        return ""

    @property
    def weight(self) -> float:
        """Weight in the domain's invariant composite (if used).
        Default 1.0 (equal weighting). Domains may override."""
        return 1.0

    @abstractmethod
    def applicable(
        self,
        chunks,
        document=None,
        strategy=None,
        **kwargs,
    ) -> tuple:
        """Check if this invariant is applicable.

        Returns:
            (is_applicable: bool, reason: str)
        """
        ...

    @abstractmethod
    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Evaluate the invariant's satisfaction degree.

        Args:
            chunks: list of Chunk objects
            document: original Document (optional)
            strategy: RepresentationPolicy (optional)
            context: shared computation cache (optional)

        Returns:
            InvariantResult with score, diagnostics, and issues
        """
        ...

    def _not_applicable_result(self, reason: str) -> InvariantResult:
        """Helper: construct a not-applicable result."""
        return InvariantResult(
            name=self.name,
            score=1.0,  # N/A defaults to neutral
            applicable=False,
            applicability_reason=reason,
        )


class InvariantRegistry:
    """Per-domain registry of semantic invariants.

    Each domain (narrative, config, code, table, event) owns its own
    registry. The registry is a simple ordered dict — no global state.

    Usage:
        registry = InvariantRegistry("narrative")
        registry.register(HierarchyPreservation())
        registry.register(DiscourseLocality())
        results = registry.evaluate_all(chunks, document, strategy)
    """

    def __init__(self, domain: str):
        """Create an empty registry for the specified domain."""
        self.domain = domain
        self._invariants: Dict[str, SemanticInvariant] = {}

    def register(self, invariant: SemanticInvariant):
        """Register an invariant. Overwrites if name exists."""
        self._invariants[invariant.name] = invariant

    def get(self, name: str) -> Optional[SemanticInvariant]:
        """Get an invariant by name (returns None when absent)."""
        return self._invariants.get(name)

    @property
    def invariant_names(self) -> List[str]:
        """List of registered invariant names."""
        return list(self._invariants.keys())

    @property
    def invariants(self) -> List[SemanticInvariant]:
        """List of registered invariants."""
        return list(self._invariants.values())

    def evaluate_all(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> Dict[str, InvariantResult]:
        """Evaluate all registered invariants.

        Returns:
            dict of invariant_name → InvariantResult
        """
        if context is None:
            context = {}

        results = {}
        for name, inv in self._invariants.items():
            is_applicable, reason = inv.applicable(
                chunks, document, strategy, **kwargs
            )
            if not is_applicable:
                results[name] = inv._not_applicable_result(reason)
            else:
                results[name] = inv.evaluate(
                    chunks, document, strategy,
                    context=context, **kwargs
                )

        return results

    def composite_score(
        self,
        results: Dict[str, InvariantResult],
        weighted: bool = True,
    ) -> float:
        """Compute a weighted composite from invariant results.

        Only applicable invariants contribute. N/A invariants are
        excluded (not counted as 1.0 or 0.0).
        """
        total_weight = 0.0
        weighted_sum = 0.0

        for name, result in results.items():
            if not result.applicable:
                continue
            inv = self._invariants.get(name)
            w = inv.weight if (inv and weighted) else 1.0
            weighted_sum += result.score * w
            total_weight += w

        if total_weight == 0:
            return 0.0
        return round(weighted_sum / total_weight, 3)


class ContentShim:
    """Minimal ``.content`` shim for domain quality evaluators.

    Domain quality evaluators (ConfigQuality / CodeQuality / TableQuality) accept a
    ``semantic_representation`` object and read ``.content`` (plus
    optional multi-granularity views). Invariants receive a plain
    chunk list; this shim adapts one to the other without
    constructing a full RepresentationResult.
    """

    def __init__(self, content):
        """Wrap content to fit the domain quality evaluators."""
        self.content = content


class QualityBackedInvariant(SemanticInvariant):
    """Base for domain invariants that compose domain-quality dimensions.

    While narrative invariants compute directly from chunk text
    (via shared primitives in narrative_checks), the config / code /
    table domains have fully fact-based quality evaluators (contract
    layers: config_entry_v1.1, code_structure_v1, table metadata).
    Re-implementing those facts inside invariants would duplicate
    logic and drift — the exact failure mode narrative_invariants.py
    documents from its own history.

    Therefore this base COMPOSES: each subclass declares which quality
    dimensions feed its semantic question (``dimension_weights``),
    and the score is a sub-weighted average over the dimensions that
    are APPLICABLE for the current document (N/A dimensions are
    excluded, never counted as 1.0 or 0.0 — same canonical policy
    as the domain quality overall).

    The domain quality evaluation runs ONCE per evaluate_all pass: the
    result dict is cached in the shared ``context`` under
    ``"evaluation_result::<domain>"``. When an invariant track is invoked
    from inside the domain quality evaluator itself, the evaluator
    pre-seeds that context key with its own result — zero recomputation.

Contract with the domain quality result dict:
profile        {dim: float | None}   None marks N/A
observations   {dim: float | None, ...} (optional, v1.2 config)
               fallback source for dims demoted out of the
               scoring layer (included_in_overall=False);
               QualityBackedInvariant reads dim scores with
               profile → observations fallback so invariant-tracks
               stay zero-drift across scoring-narrowing upgrades
applicability  {dim: {"applicable": bool, "reason": str}}
(optional — missing means "all applicable")
confidence     float (optional)
issues         list of {"dimension", "severity", "message"}
metadata       {"fact_source": str, ...} (optional)
"""

    #: Subclass hooks -------------------------------------------------
    domain: str = ""
    """Domain name — cache key fragment and registry label."""

    dimension_weights: Dict[str, float] = {}
    """Source quality dimensions feeding this invariant, with sub-weights.

    Sub-weights are relative within the invariant and need not sum
    to 1 (normalized over active dimensions at evaluation time).
    """

    quality_factory: Optional[Callable[[], Any]] = None
    """Zero-arg callable returning a fresh domain quality evaluator.

    Declared ``Optional`` on the base only for declaration purposes —
    every quality-backed subclass MUST override it with its domain
    evaluator class; ``_evaluation_result`` fails fast if it is missing.
    """

    confidence_cap: Optional[float] = None
    """Optional hard cap on confidence (e.g. skeleton consumers)."""

    # ------------------------------------------------------------------

    def _evaluation_result(self, chunks, document, strategy, context):
        """Return the (cached) domain quality evaluation result dict."""
        if context is None:
            context = {}
        cache_key = f"evaluation_result::{self.domain}"
        cached = context.get(cache_key)
        if cached is None:
            factory = self.quality_factory
            if factory is None:
                raise TypeError(
                    f"{type(self).__name__} must define quality_factory "
                    "(domain quality evaluator class)."
                )
            evaluator = factory()
            cached = evaluator.evaluate(
                document, ContentShim(list(chunks)), strategy,
            )
            context[cache_key] = cached
        return cached if isinstance(cached, dict) else {}

    def applicable(self, chunks, document=None, strategy=None, **kwargs) -> tuple:
        """Applies when both chunks and a document are present."""
        if not chunks:
            return False, "no_chunks"
        if document is None:
            return False, "no_document"
        return True, ""

    def _dim_value(self, evaluation_result, dim):
        """Read a single dimension score: profile first, with a fallback to observations for v1.2 demoted dimensions.

        From config_quality_v1.2 the scoring layer narrows to four dimensions, and the demoted
        dimensions (dependency_integrity / source_anomaly_handling) keep their scores
        in the ``observations`` section. From v1.3 path_integrity demotes the same way
        on constant evidence (live-value measurement: 144 samples always 1.0, negatives structurally unconstructible);
        the profile → observations fallback here guarantees
        zero score drift for the quality-backed invariant tracks
        (e.g. the config composite invariant ReferenceTraceability;
        the PathAddressability invariant likewise reads path_integrity through the fallback).
        """
        profile = evaluation_result.get("profile", {}) or {}
        if dim in profile:
            return profile.get(dim)
        observations = evaluation_result.get("observations", {}) or {}
        return observations.get(dim, None)

    def _dimension_active(self, evaluation_result, dim) -> bool:
        if self._dim_value(evaluation_result, dim) is None:
            return False
        applicability = evaluation_result.get("applicability", {}) or {}
        dim_info = applicability.get(dim) if isinstance(applicability, dict) else None
        if isinstance(dim_info, dict) and not dim_info.get("applicable", True):
            return False
        return True

    def evaluate(
        self,
        chunks,
        document=None,
        strategy=None,
        context: Optional[Dict] = None,
        **kwargs,
    ) -> InvariantResult:
        """Compose the invariant score by the declared quality-dimension weights."""
        evaluation_result = self._evaluation_result(chunks, document, strategy, context)

        active = {
            dim: weight
            for dim, weight in self.dimension_weights.items()
            if self._dimension_active(evaluation_result, dim)
        }
        inactive = {
            dim: self.dimension_weights[dim]
            for dim in self.dimension_weights
            if dim not in active
        }

        if not active:
            applicability = evaluation_result.get("applicability", {}) or {}
            reasons = []
            for dim in self.dimension_weights:
                info = applicability.get(dim) if isinstance(applicability, dict) else None
                if isinstance(info, dict) and not info.get("applicable", True):
                    reasons.append(f"{dim}: {info.get('reason', 'not applicable')}")
                else:
                    reasons.append(f"{dim}: no score in profile/observations")
            return InvariantResult(
                name=self.name,
                score=1.0,
                applicable=False,
                applicability_reason="; ".join(reasons) or "no source dimensions",
            )

        total_w = sum(active.values())
        score = sum(
            float(self._dim_value(evaluation_result, dim)) * w
            for dim, w in active.items()
        ) / max(total_w, 1e-9)
        score = round(max(0.0, min(1.0, score)), 3)

        # Propagate quality issues scoped to this invariant's source dims.
        # dimension_taxonomy_refactor_prereg_v1: a merged-dimension issue's
        # dimension is relabelled with the composite dimension name, the atomic check name stays in the subcheck field
        # — both keys participate in matching (zero issue-content drift).
        issues = []
        for issue in evaluation_result.get("issues", []) or []:
            if not isinstance(issue, dict):
                continue
            if (
                issue.get("dimension") in active
                or issue.get("subcheck") in active
            ):
                issues.append(
                    f"[{issue.get('severity', 'info')}] {issue.get('message', '')}"
                )
        # Invariant-level threshold issues.
        if score < 0.5:
            issues.append(
                f"{self.name} violated: composite {score} over "
                f"{sorted(active)}"
            )

        metadata = evaluation_result.get("metadata", {}) or {}
        diagnostics = {
            "sub_scores": {
                dim: self._dim_value(evaluation_result, dim)
                for dim in active
            },
            "active_dimensions": sorted(active),
            "na_dimensions": sorted(inactive),
            "fact_source": metadata.get("fact_source", "unknown")
            if isinstance(metadata, dict) else "unknown",
            "composition": "sub-weighted mean over applicable dimensions",
        }

        confidence = float(evaluation_result.get("confidence", 1.0) or 0.0)
        if self.confidence_cap is not None:
            confidence = min(confidence, self.confidence_cap)
        confidence = round(max(0.0, min(1.0, confidence)), 3)

        return InvariantResult(
            name=self.name,
            score=score,
            diagnostics=diagnostics,
            issues=issues,
            confidence=confidence,
        )
