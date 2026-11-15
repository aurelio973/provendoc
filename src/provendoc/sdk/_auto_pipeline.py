"""auto_pipeline implementation: full quality evaluation + auto-optimization.

Split from sdk/client.py (delegate style: run's first argument
is the Provendoc instance). The public API
Provendoc.auto_pipeline keeps its original signature and docs;
it only delegates here.
"""

from pathlib import Path

from provendoc.base.models.ingestion_result import IngestionResult
from provendoc.base.models.representation_result import (
    RepresentationResult,
)
from provendoc.core.domain.domain_registry import (
    classify_domain,
    get_domain_warnings,
)
from provendoc.core.pipeline.config_validation import (
    configuration_parameters_from_packaging,
    stamp_config_validation,
)
from provendoc.core.strategies.corpus_stratification import (
    CorpusStratifier,
)
from provendoc.core.strategies.representation_policy import (
    RepresentationPolicy,
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
from provendoc.intelligence.rse.rse_report import canonical_quality
from provendoc.sdk._common import (
    _chunk_count,
    _ingestion_mode,
    _is_narrative,
    _suffix_of,
    cleaning_applied_flag,
)


def run(self, file_path, stability_sweep=False, profile=False, *,
        version_source_uri=None, parent_manifest=None):
    """auto_pipeline body (orchestration documented on Provendoc.auto_pipeline)."""
    # RuntimeProfiler
    from provendoc.core.diagnostics.runtime_profiler import RuntimeProfiler
    _profiler = RuntimeProfiler(enabled=profile)
    _profiler.set_label(str(file_path))

    # step 1: parse
    with _profiler.stage("parse"):
        parsed = self.parse(file_path)
    document = parsed.document
    suffix = _suffix_of(file_path)

    # step 2: domain-specific cleaning. Code uses source-preserving
    # whitespace normalization instead of narrative semantic cleaning.
    with _profiler.stage("cleaning"):
        self._clean_document_for_domain(document, suffix)

    # step 3: representation (always built)
    default_strategy = RepresentationPolicy()
    semantic_repr = self._get_representation(suffix, default_strategy)
    with _profiler.stage("representation"):
        semantic_result = semantic_repr.build(document)
        # B12 evidence-only provenance pass (additive keys, idempotent)
        from provendoc.core.representations.provenance import (
            apply_representation_provenance,
        )
        apply_representation_provenance(
            semantic_result,
            document,
            parser=parsed.metadata.get("parser"),
            representation=semantic_repr.__class__.__name__,
            policy_name=default_strategy.name,
            cleaning_level=self._cleaning_level.value,
        )
        # doc/chunk identity (additive; failures degrade silently)
        from provendoc.core.representations.doc_identity import (
            stamp_representation_identity,
        )
        stamp_representation_identity(
            semantic_result, document, file_path=file_path,
        )
        # version manifest (source-anchored version lineage;
        # additive, failures degrade silently)
        from provendoc.core.representations.version_manifest import (
            compute_configuration_hash,
            stamp_version_manifest,
        )
        # processing binding: parser/chunker code versions + the
        # effective chunking config fingerprint; None when
        # unavailable (honest absence, never fabricated).
        try:
            from provendoc import __version__ as _provendoc_version
        except Exception:
            _provendoc_version = None
        try:
            _config_hash = compute_configuration_hash(
                chunk_size=default_strategy.get_effective_chunk_size(),
                overlap=default_strategy.get_effective_overlap(),
            )
        except Exception:
            _config_hash = None
        version_manifest = stamp_version_manifest(
            document, file_path,
            source_uri=version_source_uri,
            parent_manifest=parent_manifest,
            parser_version=_provendoc_version,
            chunker_version=_provendoc_version,
            configuration_hash=_config_hash,
        )
        # config validation contract: effective chunking params vs
        # frozen caps — violations warn, never fail (additive,
        # idempotent).
        try:
            stamp_config_validation(
                document,
                configuration_parameters_from_packaging(
                    getattr(default_strategy, "packaging", None)
                ),
            )
        # On packaging-param derivation failure, fall back to the
        # default strategy's params (SDK safety net).
        except Exception:
            pass

    # ==================================
    # structured files: stop here
    # semantic flattening is the final step.
    # evaluation and optimization are not applicable.
    # ==================================
    if not _is_narrative(suffix):
        domain_name = classify_domain(suffix)
        domain_warnings = get_domain_warnings(suffix)
        _file_size = Path(file_path).stat().st_size
        _chunk_total = _chunk_count(semantic_result)
        _tier = CorpusStratifier().classify(
            file_size_bytes=_file_size,
            chunk_count=_chunk_total,
            relative_path=str(file_path),
            success=True,
        )
        _struct_meta = {
            "parser": parsed.metadata["parser"],
            "file_type": suffix,
            "representation": semantic_repr.__class__.__name__,
            "chunk_count": _chunk_total,
            "ingestion_mode": _ingestion_mode(suffix),
            "ingestion_domain": domain_name,
            "domain_warnings": domain_warnings,
            # derived from the cleaning trace (same source of
            # truth as chunk()/evaluate()).
            "cleaning_applied": cleaning_applied_flag(document),
            "cleaning_level": self._cleaning_level.value,
            "corpus_tier": _tier.value,
            # version lineage (content-addressed version_id; see
            # the version_manifest module)
            "version_id": (version_manifest or {}).get("version_id"),
        }
        if profile:
            _profiler.record("file_size_bytes", _file_size)
            _profiler.record("char_count", len(document.text) if document.text else 0)
            _profiler.record("base_units", _chunk_total)
            _struct_meta["profile"] = _profiler.to_dict()
            print(_profiler.format_text())
        return IngestionResult(
            document=document,
            representations={"semantic": semantic_result},
            metadata=_struct_meta,
        )

    # ==================================
    # narrative files: full evaluation + optimization
    # ==================================

    # step 3: benchmark
    with _profiler.stage("evaluation"):
        dqr = self._doc_quality.evaluate(document)
        evaluation = self._evaluator.evaluate(
            document,
            semantic_result,
            default_strategy,
            dqr,
        )

    # MultiGranularityOutput compatibility
    _raw_content = semantic_result.content
    if hasattr(_raw_content, "base"):
        _base_chunks = _raw_content.base
    elif hasattr(_raw_content, "all_units"):
        _base_chunks = _raw_content.all_units()
    else:
        _base_chunks = _raw_content

    benchmark = {
        "file": str(file_path),
        "strategy": default_strategy.name,
        "strategy_params": {
            # native semantic/packaging params (semantic intent)
            "size_soft_limit": (
                default_strategy.packaging.size_soft_limit
            ),
            "size_hard_limit": (
                default_strategy.packaging.size_hard_limit
            ),
            "boundary_primary": (
                default_strategy.boundary_preference.primary
            ),
            "context_mode": (
                default_strategy.context_propagation.mode
            ),
            "overlap_size": (
                default_strategy.context_propagation.overlap_size
            ),
            "discourse_unit_target": (
                default_strategy.semantic_density.discourse_unit_target
            ),
            "paragraph_target": (
                default_strategy.semantic_density.paragraph_target
            ),
            # legacy view (derived; for old consumers)
            "chunk_size": default_strategy.get_effective_chunk_size(),
            "overlap": default_strategy.get_effective_overlap(),
            "orphan_threshold": (
                default_strategy.packaging.orphan_threshold
            ),
            "paragraph_merge": (
                default_strategy.paragraph_merge
            ),
            "collapse_blank_lines": (
                default_strategy.collapse_blank_lines
            ),
        },
        "chunk_count": len(_base_chunks),
        "document_quality": dqr,
        "evaluation": evaluation,
        **self._pdf_quality_context(document, suffix),
    }

    # RE three-layer envelope (additive; see _re_envelope)
    _envelope = self._re_envelope(
        suffix, evaluation, semantic_result,
        strategy=default_strategy, document=document,
    )
    if _envelope:
        benchmark.update(_envelope)

    # step 4: optimization
    with _profiler.stage("optimization"):
        optimizer = StrategyOptimizer()
        opt_result = optimizer.optimize_document(
            document,
            suffix,
            mode="controlled",
            file_label=str(file_path),
        )

    # step 5: build optimized semantic representation
    optimized_semantic = None
    optimized_benchmark = None

    opt_output = opt_result.get("optimized_output")
    if opt_output and opt_output.get("chunks"):
        optimized_semantic = RepresentationResult(
            representation_type="semantic",
            content=opt_output["chunks"],
            metadata={
                "chunk_count": opt_output.get(
                    "chunk_count"
                ),
                "avg_chunk_length": opt_output.get(
                    "avg_chunk_length"
                ),
                "strategy": opt_output.get("strategy"),
                "optimized_score": opt_output.get(
                    "optimized_score"
                ),
                "improvement": opt_output.get(
                    "improvement"
                ),
            },
        )

        # optimized benchmark
        best_evaluation = opt_result.get("best_evaluation")
        optimized_benchmark = {
            "file": str(file_path),
            "strategy": "optimized",
            "strategy_params": opt_output.get(
                "strategy"
            ),
            "chunk_count": opt_output.get(
                "chunk_count"
            ),
            "document_quality": dqr,
            "evaluation": best_evaluation,
            **self._pdf_quality_context(document, suffix),
            "baseline": opt_output.get("baseline"),
            "optimized_score": opt_output.get(
                "optimized_score"
            ),
            "improvement": opt_output.get("improvement"),
        }

        # RE envelope for the optimized benchmark (additive;
        # strategy object not retained by the optimizer, so the
        # csc candidate is honestly not_applicable here)
        if best_evaluation:
            _opt_repr = (
                optimized_semantic
                if optimized_semantic is not None
                else semantic_result
            )
            _opt_env = self._re_envelope(
                suffix, best_evaluation, _opt_repr,
                document=document,
            )
            if _opt_env:
                optimized_benchmark.update(_opt_env)

    representations = {"semantic": semantic_result}
    if optimized_semantic:
        representations["optimized_semantic"] = (
            optimized_semantic
        )

    metadata = {
        "parser": parsed.metadata["parser"],
        "file_type": suffix,
        "representation": semantic_repr.__class__.__name__,
        "chunk_count": _chunk_count(semantic_result),
        "benchmark": benchmark,
        "optimization": {
            "best_strategy": opt_result.get(
                "best_strategy"
            ),
            "best_score": opt_result.get("best_score"),
            # the narrative invariant track emits only
            # invariant_composite, no overall -- read the baseline
            # via canonical_quality, else improvement collapses
            # into best_score and regressions are masked.
            "improvement": round(
                opt_result.get("best_score", 0)
                - (canonical_quality(evaluation) or 0),
                3,
            ),
            "sanity_flags": opt_result.get(
                "sanity_flags"
            ),
            "phases_trace": opt_result.get(
                "phases_trace"
            ),
            "total_tested": opt_result.get(
                "total_tested"
            ),
            "optimized_output": opt_output,
        },
        # derived from the cleaning trace (same source of truth
        # as chunk()/evaluate()).
        "cleaning_applied": cleaning_applied_flag(document),
        "cleaning_level": self._cleaning_level.value,
        # document semantic identity (see the doc_identity module)
        "doc_id": (
            (document.metadata or {}).get("doc_identity") or {}
        ).get("doc_id"),
        # version lineage (content-addressed version_id; see
        # the version_manifest module)
        "version_id": (version_manifest or {}).get("version_id"),
    }

    if optimized_benchmark:
        metadata["optimized_benchmark"] = (
            optimized_benchmark
        )

    # structure analysis + metadata enrichment (semantic persistence)
    with _profiler.stage("structure_analysis"):
        _analyzer = StructureAnalyzer()
        _tree = _analyzer.analyze(document)
        if _tree.total_nodes > 1:
            _smap = StructureMap(_tree)
            _enricher = ChunkMetadataEnricher(_smap)
            _enricher.enrich(_base_chunks, document.text)
            metadata["structure"] = _tree.to_summary()

    # corpus tier classification
    with _profiler.stage("corpus_stratification"):
        _file_size = Path(file_path).stat().st_size
        _stratifier = CorpusStratifier()
        _tier = _stratifier.classify(
            file_size_bytes=_file_size,
            chunk_count=len(_base_chunks),
            relative_path=str(file_path),
            quality_overall=canonical_quality(evaluation),
            success=True,
        )
        metadata["corpus_tier"] = _tier.value

    # RuntimeProfiler: complexity metrics + output
    _profiler.record("file_size_bytes", _file_size)
    _profiler.record("char_count", len(document.text) if document.text else 0)
    _profiler.record("line_count", len(document.text.splitlines()) if document.text else 0)
    _profiler.record("base_units", len(_base_chunks))
    if profile:
        metadata["profile"] = _profiler.to_dict()
        print(_profiler.format_text())

    # optional stability sweep
    if stability_sweep:
        _observer = SemanticStabilityObserver()
        _sweep = _observer.sweep_document(
            raw_text=document.text,
            document=document,
            quality_evaluator=self._evaluator,
            strategy=default_strategy,
            doc_quality=dqr,
            current_soft_limit=(
                default_strategy.packaging.size_soft_limit
            ),
        )
        metadata["stability"] = _sweep.to_user_friendly_dict()

    return IngestionResult(
        document=document,
        representations=representations,
        metadata=metadata,
    )
