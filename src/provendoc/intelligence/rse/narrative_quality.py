"""NarrativeQuality evaluator: a thin orchestration shell for the invariant track (invariant_v1).

rse_naming_spec_v1 batch 3: the old six-dimension production line (NarrativeCQI's
profile / overall / confidence / metric_definitions assembly layer)
is retired; this class only orchestrates the current invariant track (the
narrative_invariants registry) and the structural-evidence export. The metric algorithms live in the narrative_metrics
package (metrics_*), consumed by the invariant classes via lazy import.

Output structure (invariant-track convention):
    invariant_version    — "narrative_invariant_v1"
    invariants           — registry.evaluate_all's per-invariant
                           dict (None when degraded)
    invariant_composite  — weighted composite score (None when degraded)
    issues               — execution-trace diagnostics (audit-only, never scored)
    metadata             — chunk statistics + structural evidence
                           （structure_provenance / navigation_evidence
                           / single_chunk_suitability）

The four-dimension Quality Profile (structural_preservation /
semantic_continuity / granularity_appropriateness /
boundary_cleanliness) is assembled from this output by rse_report._quality_profile and
the optimization layer's compute_quality_score
(invariant → quality name projection); this class does not duplicate that layer.
csc candidates never enter scores (admission preregistration returned them to candidate status).
"""

import statistics

from provendoc.core.structure.structure_provenance import (
    summarize_navigation_evidence,
    summarize_structure_provenance,
)
from provendoc.intelligence.rse.narrative_invariants import (
    create_narrative_registry,
)
from provendoc.intelligence.rse.narrative_metrics import (
    diagnostics,
)


class NarrativeQuality:
    """Narrative-domain quality evaluator (invariant track, invariant_v1).

    Evaluates chunk quality via the four semantic invariants
    (hierarchy_preservation / discourse_locality /
    granularity_appropriateness / boundary_cleanliness) plus the
    separately-composed context_self_containment candidate. Document
    quality (encoding noise, hidden unicode, etc.) is handled
    separately by DocumentQualityReport.
    """

    def evaluate(
        self,
        document,
        semantic_representation,
        strategy=None,
        document_quality=None,
    ):
        """Evaluate chunk quality on the invariant track.

        Args:
            document: Document with text and metadata
            semantic_representation: RepresentationResult with chunks
            strategy: RepresentationPolicy (optional, for scoring context)
            document_quality: DocumentQualityReport result
                (optional; kept for call-site compatibility, the
                invariant track does not consume it)

        Returns:
            dict with invariants, invariant_composite, issues,
                metadata
        """
        raw_content = semantic_representation.content

        # Compatible with MultiGranularityOutput (the narrative representation's new output format)
        # and the old format (a plain list of RepresentationUnit)
        if hasattr(raw_content, "base"):
            chunks = raw_content.base
        elif hasattr(raw_content, "all_units"):
            chunks = raw_content.all_units()
        else:
            chunks = raw_content

        if not chunks:
            return diagnostics.empty_result()

        # extract strategy params (scoring context for the registry)
        target_size = 300
        orphan_threshold = 120

        if strategy is not None:
            if hasattr(strategy, "get_effective_chunk_size"):
                # RepresentationPolicy
                target_size = strategy.get_effective_chunk_size()
                # orphan_threshold is semi-retired: its new home is the packaging layer
                orphan_threshold = getattr(
                    getattr(strategy, "packaging", None),
                    "orphan_threshold", 120
                )
            else:
                # a flat-field compatibility object (chunk_size directly readable)
                target_size = getattr(strategy, "chunk_size", 300)
                orphan_threshold = getattr(
                    strategy, "orphan_threshold", 120
                )

        total_text_length = sum(len(c.text) for c in chunks)
        chunk_count = len(chunks)

        # ==================================
        # structural evidence (exported, never scored)
        # ==================================
        # Structural-evidence source summary: distinguishes author-declared structure from parser-guessed structure.
        # Exported with metadata; the RSE envelope uses it to explain structural trustworthiness to consumers.
        structure_provenance = summarize_structure_provenance(
            document
        )

        # PDF bookmark navigation coverage: an evidence fact that never enters scores, exported with
        # metadata for the RSE envelope to render as navigation_evidence. Non-PDF
        # documents have no pdf_navigation key → None, not exported.
        navigation_evidence = summarize_navigation_evidence(
            document
        )

        # ==================================
        # invariant track (sole scoring authority, invariant_v1)
        # ==================================
        # The old six-dimension production line retired in batch 3; the invariant track is the only scoring track.
        # Degradation defence retained: on invariant-evaluation failure it honestly degrades to
        # an invariant-free output (composite=None); the consumer
        # (rse_report._quality_profile) treats it as partial.
        invariant_results = None
        invariant_composite = None
        try:
            inv_registry = create_narrative_registry()
            invariant_results = inv_registry.evaluate_all(
                chunks, document, strategy,
            )
            invariant_composite = inv_registry.composite_score(
                invariant_results, weighted=True
            )
        except Exception:
            # Never break the evaluation pipeline itself.
            invariant_results = None
            invariant_composite = None

        invariant_dict = None
        if invariant_results is not None:
            invariant_dict = {
                name: result.to_dict()
                for name, result in invariant_results.items()
            }

        # ==================================
        # policy execution evidence (diagnostics only)
        # ==================================
        representation_metadata = getattr(
            semantic_representation, "metadata", {}
        ) or {}
        policy_execution = representation_metadata.get("policy_execution")
        representation_execution = representation_metadata.get(
            "representation_execution"
        )
        issues = diagnostics.execution_diagnostics(
            policy_execution, representation_execution,
        )

        # ==================================
        # single-chunk suitability (legitimacy check)
        # ==================================
        # A single block is a legal end state decided jointly by capacity, compactness and independently addressable structure.
        # Exported for the optimizer's sanity check and consumer judgement.
        doc_headings = []
        if hasattr(document, 'metadata') and document.metadata:
            doc_headings = document.metadata.get('headings', [])
        doc_text = getattr(document, 'text', '')

        single_chunk_suitability = None
        if chunk_count == 1:
            single_chunk_suitability = (
                diagnostics.single_chunk_suitability(
                    chunks, doc_headings, doc_text,
                    total_text_length, target_size, strategy,
                    policy_execution,
                )
            )
            if not single_chunk_suitability["suitable"]:
                issues.insert(0, {
                    "dimension": "single_chunk_suitability",
                    "score": 0.0,
                    "threshold": 1.0,
                    "message": (
                        "single chunk is NOT a legitimate terminal "
                        "state for this document: "
                        + "; ".join(
                            single_chunk_suitability["reasons"]
                        )
                    ),
                    "suggested_actions": [
                        "reduce hard_limit so oversized content is split",
                        "enable hierarchy_continuity so top-level "
                        "sections each open their own chunk",
                    ],
                    "classification": "primary",
                })

        # metadata
        lengths = [len(c.text) for c in chunks]
        avg = sum(lengths) / max(len(lengths), 1)
        cv = (
            statistics.pstdev(lengths) / max(avg, 1)
            if len(lengths) > 1 else 0
        )
        orphan_count = sum(
            1 for l_len in lengths if l_len < orphan_threshold
        )

        return {
            # invariant track (sole scoring authority)
            "invariant_version": "narrative_invariant_v1",
            "invariants": invariant_dict,
            "invariant_composite": invariant_composite,
            # audit trail (never scored)
            "issues": issues,
            "metadata": {
                "chunk_count": chunk_count,
                "avg_chunk_length": round(avg, 2),
                "min_chunk_length": min(lengths),
                "max_chunk_length": max(lengths),
                "total_length": total_text_length,
                "cv": round(cv, 3),
                "orphan_chunks": orphan_count,
                "orphan_threshold": orphan_threshold,
                "target_chunk_size": target_size,
                "utilization": round(avg / target_size, 3),
                "single_chunk_suitability": (
                    single_chunk_suitability
                ),
                "policy_execution_available": policy_execution is not None,
                "representation_execution": representation_execution,
                # Structural-evidence composition (declared/inferred counts + source methods):
                # The RSE envelope uses it to explain this batch's chunk structural trustworthiness to consumers.
                "structure_provenance": structure_provenance,
                # PDF bookmark navigation coverage (PDF documents only): an evidence fact,
                # rendered by the RSE envelope as navigation_evidence.
                "navigation_evidence": navigation_evidence,
            },
        }

    def _empty_result(self):
        """Empty result when no blocks are evaluable (delegates to diagnostics.empty_result)."""
        return diagnostics.empty_result()
