"""evaluate() support layer: RE three-layer envelope, recommendation
engines, PDF quality context, and the evaluate orchestration stages.

Split from sdk/client.py (delegate style: instance methods became
module functions taking self first). client.Provendoc keeps thin
same-named delegates. The evaluate orchestration body is split
into build_semantic_representation / chunks_from_result /
narrative_benchmark / domain_benchmark / attach_re_envelope /
enrich_structure / attach_corpus_tier / stability_sweep so
client.evaluate keeps only readable orchestration.
"""

from pathlib import Path

from provendoc.core.domain.domain_registry import (
    classify_domain,
    get_domain_benchmark_spec,
    get_domain_warnings,
)
from provendoc.core.strategies.corpus_stratification import (
    CorpusStratifier,
)
from provendoc.core.strategies.stability_observer import (
    SemanticStabilityObserver,
)
from provendoc.core.structure import (
    ChunkMetadataEnricher,
    StructureAnalyzer,
    StructureMap,
)
from provendoc.intelligence.optimization.strategy_optimizer import (
    StrategyOptimizer,
)
from provendoc.intelligence.recommendation import (
    RecommendationContext,
    RuleRecommender,
    build_document_profile,
    strategy_params_from_policy,
    summarize_failure_report,
)
from provendoc.intelligence.rse.context_invariants import (
    evaluate_context_self_containment,
)
from provendoc.intelligence.rse.domain_rse_report import (
    build_domain_rse_report,
)
from provendoc.intelligence.rse.rse_report import (
    build_re_report,
    canonical_quality,
)
from provendoc.intelligence.rse.unified_result import (
    build_unified_evaluation,
)
from provendoc.sdk._common import _ingestion_mode, _is_code, _is_narrative


# ==================================
# RE three-layer envelope (shared by evaluate / auto_pipeline)
# ==================================
def re_envelope(self, suffix, evaluation, semantic_result,
                  strategy=None, document=None):
    """Assemble the RE three-layer envelope from a domain evaluation result.

    Pure assembly — no re-evaluation, no score changes. Mirrors the
    run-level evaluation.json envelope (quality_profile + gate_status +
    capability_profile) so SDK / API / tuning-UI consumers see one
    shape across all five domains:

        narrative  → build_unified_evaluation + build_re_report
                    (csc candidate included when strategy + document
                    are supplied; failure_modes honestly not_run —
                    the SDK path runs no retrieval benchmark)
        other      → build_domain_rse_report (domain contract table)

    provenance is intentionally None (honest unavailable): the SDK
    path has no parser fingerprint / policy digest / execution
    trace facts; capability marks it unavailable rather than
    fabricating placeholders.

    Returns None on any assembly failure — the envelope must never
    become a new single point of failure (same discipline as
    provendoc_run's candidate evaluation guard).
    """
    try:
        if _is_narrative(suffix):
            unified = build_unified_evaluation(evaluation, None)
            context_candidate = None
            if strategy is not None and document is not None:
                try:
                    context_candidate = (
                        evaluate_context_self_containment(
                            semantic_result,
                            document=document,
                            strategy=strategy,
                        )
                    )
                except Exception:
                    context_candidate = None
            return build_re_report(
                unified,
                representation=semantic_result,
                provenance=None,
                context_candidate=context_candidate,
                document_text=(
                    getattr(document, "text", None)
                    if document is not None else None
                ),
                policy=strategy,
                # scenario visibility block: L2 payload passed
                # through; a missing payload -> None -> the
                # capability key set stays unchanged.
                scenario=(
                    (getattr(document, "metadata", None) or {}).get(
                        "scenario"
                    )
                    if document is not None else None
                ),
            )
        return build_domain_rse_report(
            classify_domain(suffix),
            evaluation,
            representation=semantic_result,
            provenance=None,
        )
    except Exception:
        return None


# recommendation modes accepted by evaluate()
RECOMMEND_MODES = ("rules", "compare", "search", "none")
# compare mode candidate cap (each candidate re-runs the full
# parse → chunk → evaluation pipeline, so the fan-out is bounded)
COMPARE_MAX_CANDIDATES = 5


# ==================================
# 3b. recommendations — rules (default) / compare / search
# ==================================
def recommendations(self, mode, *, file_path, suffix, document,
                     chunks, strategy, evaluation, dqr, compare_strategies,
                     search_mode):
    """Build the recommendations payload for evaluate().

    Returns (payload, optimization_metadata_or_None). Additive and
    failure-tolerant: any internal error degrades to an honest
    ``unavailable`` payload instead of breaking evaluate().
    """
    try:
        if not _is_narrative(suffix):
            return {
                "schema_version": 1,
                "mode": mode,
                "status": "not_applicable",
                "reason": (
                    "recommendations tune narrative-domain size/boundary "
                    "parameters; the "
                    f"{classify_domain(suffix)} domain's representation "
                    "is size-parameter-independent by design"
                ),
            }, None

        if mode == "search":
            return search_recommendations(self,
                file_path, suffix, document, evaluation, search_mode,
            )

        payload = rules_recommendations(self,
            file_path=file_path,
            document=document,
            chunks=chunks,
            strategy=strategy,
            evaluation=evaluation,
            dqr=dqr,
        )
        if mode == "compare" and payload.get("status") in (
            "available", "no_recommendations",
        ):
            payload["mode"] = "compare"
            payload["comparison"] = compare_candidates(self,
                file_path=file_path,
                strategy=strategy,
                base_evaluation=evaluation,
                rules_payload=payload,
                compare_strategies=compare_strategies,
            )
        return payload, None
    except Exception as exc:  # recommendations never break evaluate()
        return {
            "schema_version": 1,
            "mode": mode,
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }, None

def rules_recommendations(self, *, file_path, document, chunks,
                           strategy, evaluation, dqr):
    """Deterministic rule-engine recommendations (default path)."""
    context = RecommendationContext(
        strategy_params=strategy_params_from_policy(strategy),
        evaluation=evaluation if isinstance(evaluation, dict) else {},
        failure_summary=failure_summary(
            chunks, document, file_path
        ),
        document_profile=build_document_profile(document, dqr),
    )
    return RuleRecommender().recommend(context)

def failure_summary(chunks, document, file_path):
    """Run the retrieval failure detectors; compact summary or {}.

    Detection feeds only the rule engine — it is intentionally NOT
    folded into the RE envelope (which honestly reports the SDK
    retrieval benchmark as not_run; that envelope contract is
    unchanged).
    """
    try:
        from provendoc.integrity.failure_modes import (
            RetrievalFailureBenchmark,
        )

        report = RetrievalFailureBenchmark().run(
            chunks,
            document_id=str(file_path),
            document_text=getattr(document, "text", "") or "",
        )
    except Exception:
        return {}
    return summarize_failure_report(report)

def compare_candidates(self, *, file_path, strategy, base_evaluation,
                        rules_payload, compare_strategies):
    """Verify suggestions by re-evaluating quality per candidate.

    Candidates: top rule suggestions first, then user-supplied
    ``compare_strategies`` dicts; capped at _COMPARE_MAX_CANDIDATES.
    Each candidate is applied over the current effective native
    params and re-run through evaluate(recommend="none").
    """
    candidates = []
    for item in (rules_payload.get("items") or [])[:3]:
        candidates.append({
            "label": f"rule:{item['rule_id']}",
            "overrides": dict(
                item["verification"]["suggested_params"]
            ),
        })
    for index, overrides in enumerate(compare_strategies or []):
        if isinstance(overrides, dict):
            candidates.append({
                "label": f"user:{index}",
                "overrides": dict(overrides),
            })
    candidates = candidates[: COMPARE_MAX_CANDIDATES]

    base_evaluation = base_evaluation if isinstance(base_evaluation, dict) else {}
    base_native = {
        key: value
        for key, value in strategy_params_from_policy(strategy).items()
        if key not in ("chunk_size", "overlap")
    }
    base_overall = canonical_quality(base_evaluation)
    base_invariants = base_evaluation.get("invariants") or {}
    base_scores = {
        name: view.get("score")
        for name, view in base_invariants.items()
        if isinstance(view, dict)
    }

    comparison = [{
        "label": "current",
        "params": "(as called)",
        "quality_overall": base_overall,
        "issue_count": len(base_evaluation.get("issues") or []),
        "delta_overall": 0.0,
    }]

    for candidate in candidates:
        merged = {**base_native, **candidate["overrides"]}
        entry = {
            "label": candidate["label"],
            "params": candidate["overrides"],
        }
        try:
            result = self.evaluate(
                file_path, recommend="none", **merged
            )
            cand_evaluation = (
                (result.metadata.get("benchmark") or {}).get("evaluation")
                or {}
            )
            cand_overall = canonical_quality(cand_evaluation)
            cand_invariants = cand_evaluation.get("invariants") or {}
            cand_scores = {
                name: view.get("score")
                for name, view in cand_invariants.items()
                if isinstance(view, dict)
            }
            entry.update({
                "status": "evaluated",
                "quality_overall": cand_overall,
                "issue_count": len(cand_evaluation.get("issues") or []),
                "delta_overall": (
                    round(cand_overall - base_overall, 3)
                    if isinstance(cand_overall, (int, float))
                    and isinstance(base_overall, (int, float))
                    else None
                ),
                "delta_invariants": {
                    dim: round(cand_scores[dim] - base_scores[dim], 3)
                    for dim in cand_scores
                    if isinstance(cand_scores.get(dim), (int, float))
                    and isinstance(base_scores.get(dim), (int, float))
                    and abs(cand_scores[dim] - base_scores[dim]) > 1e-9
                },
            })
        except Exception as exc:
            entry.update({
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        comparison.append(entry)

    return {
        "candidates_evaluated": len(comparison) - 1,
        "candidate_cap": COMPARE_MAX_CANDIDATES,
        "entries": comparison,
        "note": (
            "deltas are quality-score comparisons on this one document — "
            "directional evidence, not a retrieval-quality guarantee"
        ),
    }

def search_recommendations(self, file_path, suffix, document, evaluation,
                            search_mode):
    """Heavyweight StrategyOptimizer exploration (explicit only).

    Returns (payload, optimization_metadata) — the optimization
    block mirrors auto_pipeline's shape so consumers see one
    structure.
    """
    try:
        optimizer = StrategyOptimizer()
        opt_result = optimizer.optimize_document(
            document,
            suffix,
            mode=search_mode,
            file_label=str(file_path),
        )
    except Exception as exc:
        return {
            "schema_version": 1,
            "mode": "search",
            "status": "unavailable",
            "search_mode": search_mode,
            "reason": f"{type(exc).__name__}: {exc}",
        }, None

    optimization = {
        "best_strategy": opt_result.get("best_strategy"),
        "best_score": opt_result.get("best_score"),
        "improvement": round(
            opt_result.get("best_score", 0)
            - (canonical_quality(evaluation) or 0),
            3,
        ),
        "sanity_flags": opt_result.get("sanity_flags"),
        "phases_trace": opt_result.get("phases_trace"),
        "total_tested": opt_result.get("total_tested"),
        "optimized_output": opt_result.get("optimized_output"),
    }
    payload = {
        "schema_version": 1,
        "mode": "search",
        "status": "delegated",
        "search_mode": search_mode,
        "see": "metadata.optimization",
        "note": (
            "search explores the parameter space directly; its result "
            "is an optimization report, not rule-backed advice — "
            "adopting the best strategy still warrants a re-evaluation"
        ),
    }
    return payload, optimization


def pdf_quality_context(document, suffix):
    """Expose PDF input reliability beside, never inside, the narrative evaluation."""
    if suffix != ".pdf":
        return {}
    context = document.metadata.get("pdf_quality_for_evaluation")
    return {"pdf_quality_for_evaluation": context} if isinstance(context, dict) else {}


# ==================================
# 3c. evaluate() orchestration stages (split from
# client.evaluate; behavior preserved line by line)
# ==================================
def build_semantic_representation(self, parsed, file_path, suffix,
                                  strategy, *, version_source_uri,
                                  parent_manifest, profiler):
    """evaluate step 4: representation + provenance/identity/version stamping.

    Returns (semantic_repr, semantic_result, doc_identity,
    version_manifest).
    """
    semantic_repr = self._get_representation(suffix, strategy)
    with profiler.stage("representation"):
        semantic_result = semantic_repr.build(parsed.document)
        # B12 evidence-only provenance pass (additive keys, idempotent)
        from provendoc.core.representations.provenance import (
            apply_representation_provenance,
        )
        apply_representation_provenance(
            semantic_result,
            parsed.document,
            parser=parsed.metadata.get("parser"),
            representation=semantic_repr.__class__.__name__,
            cleaning_level=self._cleaning_level.value,
        )
        # doc/chunk identity (additive; failures degrade silently)
        from provendoc.core.representations.doc_identity import (
            stamp_representation_identity,
        )
        doc_identity = stamp_representation_identity(
            semantic_result, parsed.document, file_path=file_path,
        )
        # version manifest (source-anchored version lineage;
        # additive, failures degrade silently)
        from provendoc.core.representations.version_manifest import (
            stamp_version_manifest,
        )
        version_manifest = stamp_version_manifest(
            parsed.document, file_path,
            source_uri=version_source_uri,
            parent_manifest=parent_manifest,
        )
    return semantic_repr, semantic_result, doc_identity, version_manifest


def chunks_from_result(semantic_result):
    """evaluate step 4b: MultiGranularityOutput (new narrative
    shape) and legacy shape compatibility."""
    raw_content = semantic_result.content
    if hasattr(raw_content, "base"):
        return raw_content.base
    if hasattr(raw_content, "all_units"):
        return raw_content.all_units()
    return raw_content


def narrative_benchmark(self, file_path, suffix, document, semantic_result,
                        strategy, chunks, profiler):
    """evaluate step 5 (narrative): DQR + NarrativeQuality + param echo.

    Returns (dqr, benchmark dict). PDF extraction/layout reliability
    is an external input context. It never becomes a sixth quality
    dimension or changes the canonical quality profile or scores.
    """
    with profiler.stage("evaluation"):
        dqr = self._doc_quality.evaluate(document)
        evaluation = self._evaluator.evaluate(
            document,
            semantic_result,
            strategy,
            dqr,
        )
    benchmark = {
        "file": str(file_path),
        "strategy": strategy.name,
        "strategy_params": {
            # native semantic/packaging params (semantic intent)
            "size_soft_limit": strategy.packaging.size_soft_limit,
            "size_hard_limit": strategy.packaging.size_hard_limit,
            "boundary_primary": (
                strategy.boundary_preference.primary
            ),
            "context_mode": strategy.context_propagation.mode,
            "overlap_size": (
                strategy.context_propagation.overlap_size
            ),
            "discourse_unit_target": (
                strategy.semantic_density.discourse_unit_target
            ),
            "paragraph_target": (
                strategy.semantic_density.paragraph_target
            ),
            # legacy view (derived; for old consumers)
            "chunk_size": strategy.get_effective_chunk_size(),
            "overlap": strategy.get_effective_overlap(),
            "orphan_threshold": strategy.packaging.orphan_threshold,
            "paragraph_merge": strategy.paragraph_merge,
            "collapse_blank_lines": (
                strategy.collapse_blank_lines
            ),
        },
        "chunk_count": len(chunks),
        "document_quality": dqr,
        "evaluation": evaluation,
        **pdf_quality_context(document, suffix),
    }
    return dqr, benchmark


def domain_benchmark(self, suffix, file_path, document, semantic_result,
                     strategy, metadata, profiler):
    """evaluate step 5 (non-narrative domains): registry-driven benchmark
    assembly (writes metadata in place).

    All four non-narrative domains assemble the benchmark the same
    way (stage -> evaluator -> same-shape dict); the single source
    of the spec is domain_registry.DOMAIN_BENCHMARK_SPECS.
    narrative is deliberately not in the table (DQR + strategy
    param echo + PDF input-reliability context — a different
    shape; it goes through narrative_benchmark). A newly
    registered domain joins by adding one spec entry.
    """
    domain = classify_domain(suffix)
    metadata["ingestion_mode"] = _ingestion_mode(suffix)
    metadata["ingestion_domain"] = domain
    domain_warnings = get_domain_warnings(suffix)
    if domain_warnings:
        metadata["domain_warnings"] = domain_warnings
    spec = get_domain_benchmark_spec(domain)
    if spec is not None:
        evaluator = getattr(self, spec.evaluator_attr)
        with profiler.stage(spec.stage):
            domain_quality = spec.invoke(
                evaluator, document, semantic_result, strategy,
            )
        metadata["benchmark"] = {
            "file": str(file_path),
            "evaluator": spec.evaluator_name,
            "evaluation": domain_quality,
        }


def attach_re_envelope(self, suffix, metadata, semantic_result, strategy,
                       document):
    """evaluate step 5b: RSE three-layer envelope — additive benchmark keys
    (rse / compatibility), same shape as the run-level evaluation.json.
    Assembly only; None on failure (no new SPOF)."""
    benchmark = metadata.get("benchmark")
    if isinstance(benchmark, dict) and benchmark.get("evaluation"):
        envelope = re_envelope(
            self, suffix, benchmark["evaluation"], semantic_result,
            strategy=strategy, document=document,
        )
        if envelope:
            benchmark.update(envelope)


def enrich_structure(suffix, document, chunks, metadata, profiler):
    """evaluate step 6: StructureAnalyzer tree enriches chunk metadata
    (writes metadata in place).

    StructureAnalyzer is a narrative/config text analyzer. Code already
    carries tree-sitter symbols, comments and diagnostics in metadata["code"].
    """
    if _is_code(suffix):
        return
    with profiler.stage("structure_analysis"):
        analyzer = StructureAnalyzer()
        tree = analyzer.analyze(document)
        if tree.total_nodes > 1:
            smap = StructureMap(tree)
            enricher = ChunkMetadataEnricher(smap)
            enricher.enrich(chunks, document.text)
            metadata["structure"] = tree.to_summary()


def attach_corpus_tier(suffix, file_path, chunks, metadata, profiler):
    """evaluate step 7: corpus tier classification (writes metadata in
    place); returns file_size_bytes for the profiler's complexity
    metrics."""
    with profiler.stage("corpus_stratification"):
        file_size = Path(file_path).stat().st_size
        stratifier = CorpusStratifier()
        tier = stratifier.classify(
            file_size_bytes=file_size,
            chunk_count=len(chunks),
            relative_path=str(file_path),
            quality_overall=(
                canonical_quality(
                    metadata.get("benchmark", {}).get("evaluation")
                )
                if _is_narrative(suffix)
                else None
            ),
            success=True,
        )
        metadata["corpus_tier"] = tier.value
    return file_size


def stability_sweep(self, document, strategy, dqr):
    """evaluate step 8 (optional): SemanticStabilityObserver sweep
    (user-friendly stability analysis output)."""
    observer = SemanticStabilityObserver()
    sweep = observer.sweep_document(
        raw_text=document.text,
        document=document,
        quality_evaluator=self._evaluator,
        strategy=strategy,
        doc_quality=dqr,
        current_soft_limit=strategy.packaging.size_soft_limit,
    )
    return sweep.to_user_friendly_dict()
