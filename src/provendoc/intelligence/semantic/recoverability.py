"""3-Layer Recoverability Model — can the original document be recovered?

Recoverability is the ultimate quality question:
    "Given the chunks, can a reader (or an LLM) reconstruct the
     original document's semantic content?"

This is NOT about byte-level reconstruction (that's lossless compression).
It's about SEMANTIC recoverability — whether the chunks preserve enough
information for the document's meaning to be reconstructed.

==================================
Three Layers of Recoverability
==================================

Layer 1: Extraction Recoverability (ER)
    "Was everything extracted, or did the parser drop content?"
    This is about the parsing stage. If the parser silently dropped
    sections, no chunking strategy can recover them.

    Signals:
      - extraction_completeness (from shared_primitives)
      - parse_success
      - content coverage (sum of chunk text vs document text)

    This layer is DOMAIN-AGNOSTIC — it measures the ingestion pipeline,
    not the chunker.

Layer 2: Representation Stability (RS)
    "Is the representation stable across chunking strategies?"
    If different strategies produce wildly different invariant scores,
    the representation is unstable — meaning the chunking has a large
    effect on what information is preserved.

    Signals:
      - global_stability from StabilityScorer
      - per-invariant stability classifications
      - fragile invariant count

    This layer is computed from the StabilityScorer's output.

Layer 3: Reconstruction Recoverability (RR)
    "Can the document's semantic structure be reconstructed from chunks?"
    This is the chunker-specific layer. Even with perfect extraction
    and stable representation, the chunks might break the document's
    structural backbone (headings, sections, code blocks, tables).

    Signals:
      - hierarchy_preservation invariant score
      - discourse_locality invariant score
      - boundary_cleanliness invariant score
      - structural element integrity (code fences, tables)

    This layer uses domain-specific invariants.

==================================
Composite Recoverability Score
==================================

The three layers are sequential (waterfall model):

    ER × RS × RR = Composite Recoverability (geometric mean)

Geometric mean (not pure multiplication) because:
    - Pure multiplication systematically undershoots: three "okay"
      scores of 0.7 give 0.343, which looks "low" but isn't.
    - Geometric mean preserves the key property (any layer at 0
      kills the composite) while staying in the same numeric range
      as the layer scores.
    - Any layer being weak still drags down the composite, but
      proportionally, not catastrophically.

However, for diagnostic purposes, we also report each layer
independently so the user can see WHERE recoverability breaks.

==================================
Interpretation
==================================

    > 0.8  → high recoverability — chunks faithfully represent the document
    0.5-0.8 → moderate — some information may be lost or distorted
    < 0.5  → low — significant semantic loss likely

The composite score is conservative (multiplicative), so it will be
lower than any individual layer. This is intentional — we want to
flag any weakness in the pipeline.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from provendoc.intelligence.semantic.invariant import InvariantRegistry
from provendoc.intelligence.semantic.stability import StabilityReport


@dataclass
class LayerResult:
    """Result of a single recoverability layer.

    Attributes:
        layer_name:    "extraction" | "representation_stability" | "reconstruction"
        score:         layer score [0, 1]
        signals:       detailed signals contributing to the score
        issues:        problems detected at this layer
    """
    layer_name: str
    score: float
    signals: Dict = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize into a dict."""
        return {
            "layer_name": self.layer_name,
            "score": round(self.score, 3),
            "signals": self.signals,
            "issues": self.issues,
        }


@dataclass
class RecoverabilityReport:
    """Full 3-layer recoverability assessment.

    Attributes:
        extraction:               Layer 1 result
        representation_stability:  Layer 2 result
        reconstruction:           Layer 3 result
        composite:                geometric mean of 3 layer scores
        bottleneck:               which layer is the weakest link
        classification:           "high" | "moderate" | "low" | "critical"
    """
    extraction: Optional[LayerResult] = None
    representation_stability: Optional[LayerResult] = None
    reconstruction: Optional[LayerResult] = None
    composite: float = 0.0
    bottleneck: str = ""
    classification: str = "unknown"

    def to_dict(self) -> dict:
        """Serialize into a dict."""
        return {
            "extraction": self.extraction.to_dict() if self.extraction else None,
            "representation_stability": (
                self.representation_stability.to_dict()
                if self.representation_stability else None
            ),
            "reconstruction": (
                self.reconstruction.to_dict()
                if self.reconstruction else None
            ),
            "composite": round(self.composite, 3),
            "bottleneck": self.bottleneck,
            "classification": self.classification,
        }


class RecoverabilityModel:
    """3-layer recoverability assessment.

    Usage:
        model = RecoverabilityModel(registry)
        report = model.assess(
            document=document,
            chunks=chunks,
            strategy=strategy,
            invariant_results=inv_results,  # from registry.evaluate_all()
            stability_report=stab_report,   # optional, from StabilityScorer
            shared_signals=signals,         # from shared_primitives
        )
    """

    def __init__(self, registry: InvariantRegistry):
        """Inject the domain-invariant registry."""
        self.registry = registry

    def assess(
        self,
        document,
        chunks,
        strategy=None,
        invariant_results: Optional[Dict] = None,
        stability_report: Optional[StabilityReport] = None,
        shared_signals: Optional[Dict] = None,
        **kwargs,
    ) -> RecoverabilityReport:
        """Run the full 3-layer recoverability assessment.

        Args:
            document: the Document
            chunks: list of Chunk objects
            strategy: RepresentationPolicy (optional)
            invariant_results: pre-computed invariant results
            stability_report: pre-computed stability analysis
            shared_signals: shared primitive signals from extract_shared_signals()

        Returns:
            RecoverabilityReport with all 3 layers + composite
        """
        report = RecoverabilityReport()

        # ==================================
        # Layer 1: Extraction Recoverability
        # ==================================
        report.extraction = self._assess_extraction(
            document, chunks, shared_signals
        )

        # ==================================
        # Layer 2: Representation Stability
        # ==================================
        report.representation_stability = self._assess_stability(
            stability_report, invariant_results
        )

        # ==================================
        # Layer 3: Reconstruction Recoverability
        # ==================================
        report.reconstruction = self._assess_reconstruction(
            chunks, document, invariant_results
        )

        # ==================================
        # Composite (multiplicative)
        # ==================================
        scores = []
        for layer in [
            report.extraction,
            report.representation_stability,
            report.reconstruction,
        ]:
            if layer is not None:
                scores.append(layer.score)

        if scores:
            # Geometric mean instead of pure multiplication.
            #
            # Pure multiplication (ER * RS * RR) systematically undershoots:
            # three "okay" scores of 0.7 give 0.343 (classified "low"),
            # which is misleading — the document IS reconstructable, just
            # not perfectly. Geometric mean preserves the multiplicative
            # semantics (any layer at 0 kills the composite) but stays
            # in the same numeric range as the layer scores.
            #
            # gmean = (s1 * s2 * s3) ^ (1/n)
            product = 1.0
            for s in scores:
                product *= max(s, 0.001)  # avoid log(0)
            composite = product ** (1.0 / len(scores))
            report.composite = round(composite, 3)

            # identify bottleneck (lowest layer)
            layer_names = [
                "extraction",
                "representation_stability",
                "reconstruction",
            ]
            layer_scores = [
                (report.extraction.score if report.extraction else 1.0),
                (
                    report.representation_stability.score
                    if report.representation_stability else 1.0
                ),
                (
                    report.reconstruction.score
                    if report.reconstruction else 1.0
                ),
            ]
            min_idx = layer_scores.index(min(layer_scores))
            report.bottleneck = layer_names[min_idx]

            # classify (thresholds tuned for geometric mean range)
            if report.composite >= 0.75:
                report.classification = "high"
            elif report.composite >= 0.55:
                report.classification = "moderate"
            elif report.composite >= 0.35:
                report.classification = "low"
            else:
                report.classification = "critical"

        return report

    def _assess_extraction(
        self,
        document,
        chunks,
        shared_signals: Optional[Dict] = None,
    ) -> LayerResult:
        """Layer 1: Was everything extracted?

        Signals:
            - content coverage (chunk text vs document text)
            - parse_success (from shared signals)
            - extraction_completeness (from shared signals)
            - corruption signals
        """
        signals = {}
        issues = []
        score_factors = []

        # Content coverage
        doc_text = getattr(document, "text", "")
        chunk_text_total = sum(len(c.text) for c in chunks)
        doc_len = max(len(doc_text), 1)

        # Account for overlap (chunk text may exceed doc text with overlap)
        coverage = min(chunk_text_total / doc_len, 1.0) if doc_len > 0 else 0.0
        signals["content_coverage"] = round(coverage, 3)
        score_factors.append(coverage)

        if coverage < 0.5:
            issues.append(
                f"low content coverage: chunks contain {coverage:.1%} "
                f"of document text"
            )

        # Shared primitive signals
        if shared_signals:

            for signal_name in [
                "parse_success",
                "extraction_completeness",
                "corruption_signals",
            ]:
                signal = shared_signals.get(signal_name)
                if signal:
                    val = signal.value if hasattr(signal, 'value') else signal
                    signals[signal_name] = round(val, 3) if isinstance(val, float) else val
                    if isinstance(val, float):
                        score_factors.append(val)
                        if val < 0.5:
                            issues.append(f"{signal_name} is low: {val:.3f}")

        # Compute layer score
        if score_factors:
            layer_score = min(score_factors)  # conservative: worst factor
        else:
            layer_score = 0.5  # neutral when no signals available

        return LayerResult(
            layer_name="extraction",
            score=round(layer_score, 3),
            signals=signals,
            issues=issues,
        )

    def _assess_stability(
        self,
        stability_report: Optional[StabilityReport],
        invariant_results: Optional[Dict],
    ) -> LayerResult:
        """Layer 2: Is the representation stable?

        If we have a stability report (multi-strategy), use it.
        Otherwise, fall back to a single-strategy heuristic: use
        invariant confidence scores as a proxy.
        """
        signals: Dict[str, Any] = {}
        issues = []

        if stability_report and stability_report.invariant_stability:
            signals["global_stability"] = stability_report.global_stability
            signals["robust_count"] = len(stability_report.robust_invariants)
            signals["fragile_count"] = len(stability_report.fragile_invariants)

            for name, stab in stability_report.invariant_stability.items():
                signals[f"{name}_stability"] = stab.stability
                if stab.classification == "fragile":
                    issues.append(
                        f"invariant '{name}' is fragile "
                        f"(stability={stab.stability:.3f}, "
                        f"cv={stab.cv:.3f})"
                    )

            # Layer score = global stability
            # Penalize for fragile invariants
            n_total = len(stability_report.invariant_stability)
            n_fragile = len(stability_report.fragile_invariants)
            fragile_penalty = 1.0 - 0.2 * (n_fragile / max(n_total, 1))
            layer_score = stability_report.global_stability * fragile_penalty

        elif invariant_results:
            # Single-strategy fallback: use confidence scores as proxy.
            # Use weighted average (not min) for the same reason as L3:
            # one low-confidence invariant shouldn't collapse the layer.
            confidences = []
            for name, result in invariant_results.items():
                if result.applicable:
                    confidences.append(result.confidence)
                    signals[f"{name}_confidence"] = round(result.confidence, 3)

            if confidences:
                base = sum(confidences) / len(confidences)
                min_conf = min(confidences)
                if min_conf < 0.5:
                    layer_score = 0.7 * base + 0.3 * min_conf
                else:
                    layer_score = base
            else:
                layer_score = 0.5
            signals["note"] = "single-strategy mode (no stability report)"

        else:
            layer_score = 0.5
            signals["note"] = "no stability data available"

        return LayerResult(
            layer_name="representation_stability",
            score=round(layer_score, 3),
            signals=signals,
            issues=issues,
        )

    def _assess_reconstruction(
        self,
        chunks,
        document,
        invariant_results: Optional[Dict],
    ) -> LayerResult:
        """Layer 3: Can the document be reconstructed from chunks?

        Uses domain-specific invariant scores to assess whether
        the document's structural backbone is preserved.
        """
        signals = {}
        issues = []
        scores = []

        if invariant_results:
            for name, result in invariant_results.items():
                if result.applicable:
                    signals[f"{name}_score"] = round(result.score, 3)
                    scores.append(result.score)

                    # report specific issues
                    for issue_msg in result.issues:
                        issues.append(f"{name}: {issue_msg}")

                    # Flag low-scoring invariants
                    if result.score < 0.5:
                        issues.append(
                            f"invariant '{name}' score is low: "
                            f"{result.score:.3f}"
                        )
                else:
                    signals[f"{name}_score"] = "N/A"

        # Also check basic structural signals
        chunk_count = len(chunks)
        if chunk_count > 0:
            # Check for empty chunks
            empty_count = sum(1 for c in chunks if not c.text.strip())
            if empty_count > 0:
                signals["empty_chunks"] = empty_count
                issues.append(f"{empty_count} empty chunk(s) detected")
                scores.append(0.3)

            # Check for extreme size variance
            lengths = [len(c.text) for c in chunks if c.text.strip()]
            if len(lengths) >= 3:
                avg_len = sum(lengths) / len(lengths)
                if avg_len > 0:
                    import statistics as stats
                    cv = stats.pstdev(lengths) / avg_len
                    signals["size_cv"] = round(cv, 3)
                    if cv > 2.0:
                        issues.append(
                            f"extreme chunk size variance (cv={cv:.3f})"
                        )
                        scores.append(0.4)

        # Layer score: weighted average with floor penalty.
        #
        # Using min() was too aggressive: a single low-scoring invariant
        # (e.g. discourse_locality on a diverse-vocabulary doc) would
        # collapse the entire layer, even though the document's structure
        # is perfectly reconstructable. The proper question is "overall,
        # can we reconstruct?" not "what's the worst single signal?"
        #
        # Approach:
        #   - Base = weighted average of invariant scores
        #   - If ANY invariant is critically low (<0.3), apply a floor
        #     penalty so we don't give a false "all clear"
        if scores:
            base = sum(scores) / len(scores)

            # Floor penalty: if any score is critically low, pull base down
            min_score = min(scores)
            if min_score < 0.3:
                # blend: 70% base + 30% min (keeps sensitivity to
                # critical failures without collapsing entirely)
                layer_score = 0.7 * base + 0.3 * min_score
            else:
                layer_score = base
        else:
            # No applicable invariants — check if this is because
            # the document is trivially small (1 chunk) or genuinely
            # has no structural signals
            if len(chunks) <= 1:
                # Single chunk: structure is trivially preserved,
                # but check content quality
                total_text = sum(len(c.text) for c in chunks)
                if total_text > 0:
                    layer_score = 0.8  # structurally fine but uninformative
                    signals["note"] = "single chunk — structure trivially preserved"
                else:
                    layer_score = 0.0
            else:
                layer_score = 0.5

        return LayerResult(
            layer_name="reconstruction",
            score=round(layer_score, 3),
            signals=signals,
            issues=issues,
        )
