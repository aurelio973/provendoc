"""ProvendocRun — unified runtime orchestration layer.

One run = one complete ingestion journey, producing a self-contained run/ directory.

Runtime Artifact Contract (seven dimensions; see the artifact-contract documentation):
    - distinguish "core run artifacts" from "development/debug artifacts"
    - core artifacts are kept long-term; debug artifacts are configurable
    - unified entry: reports/index.html threads the whole journey together

Directory layout (seven-dimension artifact contract):
    runs/
        2026-08-11_14-32-51_abc123/
            ├── manifest.json            # run-level metadata + document artifact index (frontend entry)
            ├── summary.json             # aggregated summary
            │
            ├── raw/                     # parser raw snapshot (original_text, pre-cleaning) + DQR
            ├── representation/          # final structured output: cleaned full text + coarse/base/fine
            │   ├── {base}_repr.json             # baseline (three-granularity views)
            │   └── {base}_repr.optimized.json   # optimization run (best-strategy artifacts)
            ├── markdown/                # pure presentation layer: base-unit rendering (baseline + optimized)
            ├── evaluation/              # unified evaluation: continuous metrics + failure_modes top-level dimension
            │   ├── {base}_evaluation.json             # full baseline evaluation
            │   └── {base}_evaluation.optimized.json   # optimization run
            ├── optimization/            # strategy-search conclusion (numeric trials + references)
            │
            ├── debug/                   # [optional] cross-dimension correlation index + process drilldown (references only, no copies)
            │   ├── {base}_debug.json    # single-file full-pipeline intermediate state
            │   ├── run_trace.json       # run-level global trace (aggregation/optimization/stability)
            │   ├── diagnostics/         # error diagnostics
            │   └── profile_summary.json # RuntimeProfiler aggregation
            │
            └── reports/                 # unified reporting entry
                ├── index.html           # the single main entry
                └── visualizations/      # runtime-facing charts

    (the legacy chunks/ directory has been folded into representation's chunks field —
     coarse/base/fine are chunking-strategy granularities, not directory levels;
     the failure report has been merged into evaluation's failure_modes top-level dimension)

Module division of labor (structural convergence, split out of this file):
    - config.py        RunConfig (run configuration) + anchor_cwd_relative (path anchoring)
    - run_models.py    FileResult / RunSummary / _SweepDoc / _is_run_dir_name
    - parse_cache.py   parser code fingerprint + ParseCache (parse-result cache)
    - image_export.py  ImageAssetExporter (independent image-asset storage)
    - run_artifacts.py RunArtifactStore (run-level artifact persistence) + run_strategy_optimization
    - provendoc_run.py      this module: ProvendocRun orchestration (execute / _process_file /
                       _save_file_outputs / _compute_summary / component wiring)
"""

import json
import logging
import os
import shutil
import tempfile
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.core.diagnostics.runtime_profiler import ProfileAggregator, RuntimeProfiler
from provendoc.core.domain.domain_registry import (
    get_all_supported_suffixes,
)
from provendoc.core.exporters.json_exporter import JsonExporter
from provendoc.core.exporters.markdown_exporter import MarkdownExporter
from provendoc.core.exporters.raw_document_exporter import RawDocumentExporter
from provendoc.core.exporters.representation_exporter import RepresentationExporter
from provendoc.core.pipeline.log_hygiene import sanitize_log_value
from provendoc.core.pipeline.universal_parser import UniversalParser
from provendoc.core.strategies.corpus_stratification import CorpusStratifier, CorpusTier
from provendoc.core.strategies.representation_policy import (
    BoundaryPreference,
    BoundaryPrimary,
    ContextPropagation,
    ContextPropagationMode,
    IntegrityRules,
    RepresentationPolicy,
    RuntimePackagingPolicy,
    ScoreObservation,
    SemanticDensity,
)
from provendoc.core.strategies.stability_observer import SemanticStabilityObserver
from provendoc.integrity.failure_modes import (
    RetrievalFailureBenchmark,
)
from provendoc.intelligence.diagnostics.document_quality import DocumentQualityReport
from provendoc.intelligence.rse.context_invariants import (
    evaluate_context_self_containment,
)
from provendoc.intelligence.rse.domain_rse_report import (
    DOMAIN_QUALITY_CONTRACTS,
    build_domain_rse_report,
)
from provendoc.intelligence.rse.narrative_quality import NarrativeQuality
from provendoc.intelligence.rse.rse_report import (
    build_provenance_fact,
    build_re_report,
    canonical_quality,
    policy_version_string,
)
from provendoc.intelligence.rse.unified_result import build_unified_evaluation
from provendoc.runtime.config import RunConfig, anchor_cwd_relative
from provendoc.runtime.image_export import ImageAssetExporter
from provendoc.runtime.parse_cache import ParseCache, compute_parser_fingerprint
from provendoc.runtime.run_artifacts import (
    RunArtifactStore,
    run_strategy_optimization,
    write_json_sync,
)
from provendoc.runtime.run_models import FileResult, RunSummary, _SweepDoc

_log = logging.getLogger(__name__)


class ProvendocRun:
    """Provendoc unified runtime.

    Usage:
        config = RunConfig(dataset_dir="./data", description="test run")
        run = ProvendocRun(config)
        summary = run.execute()
        print(f"Run {summary.run_id} done: {summary.success_files}/{summary.total_files}")
        # then open runs/{run_id}/index.html to view the full report
    """

    def __init__(self, config: Optional[RunConfig] = None):
        """Initialize a run: use the default RunConfig if none is given; generate the run_id and run directory."""
        self.config = config or RunConfig()
        self.run_id = self._generate_run_id()
        # Runtime artifacts belong to the invocation context (CWD). This
        # keeps an installed SDK/CLI from writing into its site-packages
        # directory. Exception: a session started from inside the
        # provendoc source tree (IDE console CWD=src/provendoc)
        # would write runs/ into the package source and hide artifacts
        # from the repo-root runs/ users actually watch — redirect to
        # the dev repo root instead (mirrors app.py dataset fallback).
        self.run_dir = self._resolve_runs_root() / self.run_id
        self.summary = RunSummary(
            run_id=self.run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            config=self.config,
        )

        # Component initialization
        self._init_components()

    @classmethod
    def _resolve_runs_root(cls) -> Path:
        """runs root directory: CWD/runs by default; redirected to the repo root when CWD is inside the package source tree."""
        return anchor_cwd_relative("runs")

    def _init_components(self):
        """Initialize processing components (also called by parallel workers)."""
        self.policy = self._build_policy()
        self.parser = UniversalParser(
            representation_policy=self.policy,
            pdf_limits=self._build_pdf_limits(),
            pdf_ocr_language=self.config.pdf_ocr_language,
            pdf_ocr_tessdata=self.config.pdf_ocr_tessdata,
            table_limits=self._build_table_limits(),
            docx_limits=self._build_docx_limits(),
            pptx_limits=self._build_pptx_limits(),
            enable_image_assets=getattr(
                self.config, "enable_image_asset_export", True
            ),
            log_grouping=self.config.log_grouping_strategy,
            log_incident_context=(
                self.config.log_incident_context_before,
                self.config.log_incident_context_after,
            ),
        )
        self.quality_evaluator = NarrativeQuality()
        # Domain quality evaluators (RE envelope generalization §10/§11): the four domains
        # record_stream / structured_config / spreadsheet / code, driven by a contract table
        self.domain_quality_evaluators = {
            name: contract["evaluator"]()
            for name, contract in DOMAIN_QUALITY_CONTRACTS.items()
        }
        self.doc_quality_evaluator = DocumentQualityReport()
        self.json_exporter = JsonExporter()
        self.markdown_exporter = MarkdownExporter()
        self.raw_exporter = RawDocumentExporter()
        self.representation_exporter = RepresentationExporter()
        self.corpus_stratifier = CorpusStratifier()
        self.stability_observer = SemanticStabilityObserver()

        # Async export thread pool — write files in the background, overlapping I/O with CPU
        self._export_executor = None
        self._export_futures: list = []
        if getattr(self.config, 'enable_async_export', False):
            from concurrent.futures import ThreadPoolExecutor
            self._export_executor = ThreadPoolExecutor(
                max_workers=getattr(self.config, 'async_export_workers', 2)
            )

        # Satellite-module wiring (parse cache / image-asset export / run-level artifact persistence)
        self.parse_cache = ParseCache(self.config, self.parser)
        self.image_asset_exporter = ImageAssetExporter(
            self.run_dir, self._submit_export
        )
        self.artifacts = RunArtifactStore(self.run_dir, self.config)

    def _build_pdf_limits(self):
        """Build PDFRuntimeLimits from RunConfig (guards, not filters).

        When PdfParser is available (pymupdf present), return config-driven limits;
        otherwise return None (preserving current behavior when PDF parsing is unavailable).
        All-None defaults = no caps; valid large files such as books are processed in full.
        """
        try:
            from provendoc.core.parsers.narrative.pdf.contracts import (
                PDFRuntimeLimits,
            )
        except ImportError:
            return None
        return PDFRuntimeLimits(
            max_pages=self.config.pdf_max_pages,
            max_file_bytes=self.config.pdf_max_file_bytes,
            max_page_pixels=self.config.pdf_max_page_pixels,
            max_image_asset_bytes=getattr(
                self.config, "pdf_max_image_asset_bytes", None
            ),
            ocr_time_budget_seconds=self.config.pdf_ocr_time_budget_seconds,
            mineru_timeout_seconds=self.config.pdf_mineru_timeout_seconds,
            ocr_parallel_workers=self.config.pdf_ocr_parallel_workers,
        )

    def _build_table_limits(self):
        """Build TableRuntimeLimits from RunConfig (guards, not filters).

        Returns None when every field is None (the default) — zero-diff semantics: no caps
        means not wired in, so no facts are claimed. Setting any field returns limits.
        """
        fields = (
            "table_max_sheets",
            "table_max_rows_per_sheet",
            "table_max_columns_per_sheet",
            "table_max_cells_per_sheet",
        )
        if all(getattr(self.config, field, None) is None for field in fields):
            return None
        from provendoc.core.parsers.table.contracts import (
            TableRuntimeLimits,
        )
        return TableRuntimeLimits(
            max_sheets=self.config.table_max_sheets,
            max_rows_per_sheet=self.config.table_max_rows_per_sheet,
            max_columns_per_sheet=self.config.table_max_columns_per_sheet,
            max_cells_per_sheet=self.config.table_max_cells_per_sheet,
        )

    def _build_docx_limits(self):
        """Build DocxRuntimeLimits from RunConfig (guards, not filters)."""
        if getattr(self.config, "docx_max_body_elements", None) is None:
            return None
        from provendoc.core.parsers.narrative.docx_limits import (
            DocxRuntimeLimits,
        )
        return DocxRuntimeLimits(
            max_body_elements=self.config.docx_max_body_elements,
        )

    def _build_pptx_limits(self):
        """Build PptxRuntimeLimits from RunConfig (guards, not filters).

        All-None returns None and skips wiring (precedent: _build_docx_limits) —
        no fake "throttled" fact surface is produced.
        """
        max_slides = getattr(self.config, "pptx_max_slides", None)
        max_shapes = getattr(
            self.config, "pptx_max_shapes_per_slide", None,
        )
        max_evidence = getattr(
            self.config, "pptx_max_evidence_parts", None,
        )
        if (
            max_slides is None
            and max_shapes is None
            and max_evidence is None
        ):
            return None
        from provendoc.core.parsers.narrative.pptx_limits import (
            PptxRuntimeLimits,
        )
        return PptxRuntimeLimits(
            max_slides=max_slides,
            max_shapes_per_slide=max_shapes,
            max_evidence_parts=max_evidence,
        )

    @classmethod
    def for_worker(cls, config: RunConfig, run_dir: str):
        """Create a parallel-worker instance (reuses the main process's run_dir; no new run_id)."""
        instance = cls.__new__(cls)
        instance.config = config
        instance.run_dir = Path(run_dir)
        instance.run_id = Path(run_dir).name
        # Workers reuse the main process's run_dir and produce no reports themselves;
        # give them a real empty summary so no downstream path ever sees None (same shape as the main-process construction path).
        instance.summary = RunSummary(
            run_id=instance.run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            config=config,
        )
        instance._init_components()
        return instance

    # ── Async export helpers ───────────────────────────────

    def _submit_export(self, fn, *args):
        """Submit a file write to the async export thread pool, or run sync if disabled."""
        if self._export_executor is not None:
            future = self._export_executor.submit(fn, *args)
            self._export_futures.append(future)
        else:
            fn(*args)

    def _flush_exports(self):
        """Wait for all pending async export writes to complete."""
        if not self._export_futures:
            return
        for future in self._export_futures:
            try:
                future.result()
            except Exception as e:
                # Log hygiene: exception text may carry document-derived content.
                _log.debug("Async export error: %s", sanitize_log_value(e))
        self._export_futures.clear()

    def _generate_run_id(self) -> str:
        """Generate the run ID: timestamp + config fingerprint."""
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
        fingerprint = self.config.fingerprint()
        return f"{timestamp}_{fingerprint}"

    def _build_policy(self) -> RepresentationPolicy:
        """Build the RepresentationPolicy from config.

        L3 Representation Policies: boundary / integrity / context / density
        L4 Runtime Packaging: size_soft_limit / size_hard_limit
        """
        # integrity_mode → IntegrityRules mapping
        im = self.config.integrity_mode
        if im == "all":
            integrity = IntegrityRules(code_blocks=True, tables=True, blockquotes=True, list_groups=True)
        elif im == "code_table":
            integrity = IntegrityRules(code_blocks=True, tables=True, blockquotes=False, list_groups=False)
        elif im == "code_only":
            integrity = IntegrityRules(code_blocks=True, tables=False, blockquotes=False, list_groups=False)
        else:  # none
            integrity = IntegrityRules(code_blocks=False, tables=False, blockquotes=False, list_groups=False)

        # The config layer is free-form str; values are validated and narrowed to Literal-legal ones
        # before entering the policy layer — invalid config falls back to the default (consistent with the documented value range).
        boundary_primary = self.config.boundary_preference
        if boundary_primary not in ("discourse_unit", "heading_boundary", "structural_block", "token_limit"):
            boundary_primary = "discourse_unit"
        context_mode = self.config.context_propagation_mode
        if context_mode not in ("none", "selective", "heading_only", "full"):
            context_mode = "selective"

        return RepresentationPolicy(
            name=self.config.policy_name,
            boundary_preference=BoundaryPreference(
                primary=cast(BoundaryPrimary, boundary_primary),
            ),
            integrity_rules=integrity,
            context_propagation=ContextPropagation(
                mode=cast(ContextPropagationMode, context_mode),
            ),
            semantic_density=SemanticDensity(
                discourse_unit_target=self.config.discourse_unit_target,
                paragraph_target=max(2, self.config.discourse_unit_target // 2),
            ),
            packaging=RuntimePackagingPolicy(
                size_soft_limit=self.config.packaging_size_soft_limit,
                size_hard_limit=self.config.packaging_size_hard_limit,
            ),
        )

    def _process_file(self, file_path: Path, dataset_dir: Path,
                     relative_override: Optional[Path] = None) -> FileResult:
        """Process a single file.

        Args:
            file_path: real path (possibly a temp file expanded during collection)
            dataset_dir: dataset root (used to compute the default relative path)
            relative_override: virtual relative path (archive members use it to preserve
                their origin, e.g. "bundle.zip/docs/chapter.md"; None for regular files)
        """
        relative_path = self._resolve_relative_path(
            file_path, dataset_dir, relative_override
        )
        start_time = datetime.now(timezone.utc)

        # Make sure the run directory is initialized (for callers invoking _process_file standalone)
        if not self.run_dir.exists():
            self.artifacts.init_run_dir(self.run_id)

        profiler = RuntimeProfiler(enabled=self.config.enable_profiling)
        profiler.set_label(str(relative_path))

        try:
            # parse (with cache)
            with profiler.stage("parse"):
                result = self.parse_cache.parse(file_path)
            with profiler.stage("representation"):
                # Preserve the original semantic representation (may contain
                # MultiGranularityOutput with coarse/base/fine) for the
                # representation/ export layer. The normalized base-only
                # version is used for evaluation, failure modes, and chunks.
                raw_semantic_repr = result.get_representation("semantic")
                semantic_repr = self._normalize_semantic_representation(
                    raw_semantic_repr
                )

            # domain info
            ingestion_mode = result.metadata.get("ingestion_mode", "narrative")
            ingestion_domain = result.metadata.get("ingestion_domain", "narrative")
            is_narrative = (ingestion_mode == "narrative")

            # ── Image assets (opt-in) ──────────────────────────
            image_asset_info = self._export_image_assets(
                result, relative_path, profiler
            )

            # Runtime Scale Policy: precompute the benchmark label and scale metrics
            chunk_count = len(semantic_repr.content) if semantic_repr else 0
            try:
                file_size_bytes = file_path.stat().st_size
            except OSError:
                file_size_bytes = 0
            benchmark_tags = self.policy.scale.benchmark_tagging.classify(
                file_size_bytes, chunk_count,
            )

            # ── Huge file fast-path ─────────────────────────────────
            # Large files skip expensive quality evaluation / failure_modes / doc_quality.
            # representation still runs (0.05ms, essentially free).
            # save_outputs has a matching huge-file skip path.
            is_huge = file_size_bytes >= self.config.huge_file_threshold

            # Quality-evaluation cold path: doc_quality / density downgrade / narrative evaluation /
            # failure modes / domain evaluation (discipline documented in _run_quality_evals).
            line_count = len(result.document.text.splitlines()) if result.document.text else 0
            (
                doc_quality,
                evaluation_result,
                failure_report,
                domain_quality_result,
                eval_repr,
            ) = self._run_quality_evals(
                result=result,
                semantic_repr=semantic_repr,
                is_narrative=is_narrative,
                is_huge=is_huge,
                ingestion_domain=ingestion_domain,
                relative_path=relative_path,
                line_count=line_count,
                profiler=profiler,
            )

            # save outputs
            with profiler.stage("save_outputs"):
                artifacts = self._save_file_outputs(
                    result=result,
                    semantic_repr=semantic_repr,
                    raw_semantic_repr=raw_semantic_repr,
                    evaluation_result=evaluation_result,
                    domain_quality_result=domain_quality_result,
                    failure_report=failure_report,
                    relative_path=relative_path,
                    benchmark_tags=benchmark_tags,
                    file_size_bytes=file_size_bytes,
                    doc_quality=doc_quality,
                    profiler=profiler,
                )
            if image_asset_info is not None:
                artifacts["image_assets"] = image_asset_info
                profiler.record(
                    "image_assets_unique",
                    image_asset_info.get("unique_assets", 0),
                )

            end_time = datetime.now(timezone.utc)
            processing_time = int((end_time - start_time).total_seconds() * 1000)

            # Extract failure modes (compatible with dict and FailureReport objects)
            failure_mode_list = self._extract_failure_mode_list(failure_report)

            # Corpus Stratification: classify document tiers
            corpus_tier, quality_dims, active_evaluation = (
                self._classify_corpus_tier(
                    artifacts=artifacts,
                    evaluation_result=evaluation_result,
                    domain_quality_result=domain_quality_result,
                    file_size_bytes=file_size_bytes,
                    chunk_count=chunk_count,
                    benchmark_tags=benchmark_tags,
                    relative_path=relative_path,
                )
            )

            # RuntimeProfiler: complexity metrics
            self._record_complexity_metrics(
                profiler=profiler,
                result=result,
                semantic_repr=semantic_repr,
                file_size_bytes=file_size_bytes,
                line_count=line_count,
                chunk_count=chunk_count,
            )

            # PDF productionization: extract single-file runtime/reliability facts for run-level aggregation
            pdf_metrics = self._extract_pdf_metrics(file_path, result)

            return FileResult(
                file_path=str(file_path),
                relative_path=relative_path.as_posix(),
                success=True,
                chunk_count=chunk_count,
                file_size_bytes=file_size_bytes,
                benchmark_tags=benchmark_tags,
                quality_overall=canonical_quality(active_evaluation),
                quality_confidence=active_evaluation.get("confidence") if active_evaluation else None,
                quality_issues=(active_evaluation or {}).get("issues") or [],
                failure_modes=failure_mode_list,
                domain=ingestion_domain,
                ingestion_mode=ingestion_mode,
                processing_time_ms=processing_time,
                # Reporting-chain contract:
                #   raw_text    = original_text (parse snapshot: sanitized, pre-semantic-cleaning)
                #   source_text = the file's source text (decoded original, the "PyCharm view":
                #                 blank lines/zero-width chars/original newlines preserved; only text-like formats carry a value)
                raw_text=result.document.original_text or result.document.text,
                source_text=getattr(
                    result.document, "source_text", None
                ),
                markdown_content=artifacts.get("markdown_content"),
                markdown_path=artifacts.get("markdown"),
                representation_json_path=artifacts.get("representation_json"),
                raw_json_path=artifacts.get("raw_json"),
                evaluation_json_path=artifacts.get("evaluation_json"),
                debug_json_path=artifacts.get("debug_json"),
                doc_quality_result=self._extract_doc_quality(doc_quality),
                quality_dimensions=quality_dims,
                sampling_meta=artifacts.get("sampling_meta"),
                density_adapted=eval_repr is not semantic_repr,
                corpus_tier=corpus_tier.value,
                profile=profiler.to_dict(),
                pdf_runtime_summary=pdf_metrics,
            )

        except Exception as e:
            end_time = datetime.now(timezone.utc)
            processing_time = int((end_time - start_time).total_seconds() * 1000)

            # save error diagnostic (to run debug/ if enabled, else skip)
            self._save_error_diagnostic(file_path, relative_path, e)

            return FileResult(
                file_path=str(file_path),
                relative_path=relative_path.as_posix(),
                success=False,
                error=str(e),
                processing_time_ms=processing_time,
                corpus_tier=CorpusTier.MINIMAL.value,
                profile=profiler.to_dict(),
            )

    # ---- _process_file() stage methods (2026-10-06 structural convergence: split out of the
    # orchestration body with line-for-line equivalent behavior; profiler stage timing
    # boundaries stay in the orchestrator; cold-path stage timers migrated with each evaluator) ----

    @staticmethod
    def _resolve_relative_path(
        file_path: Path,
        dataset_dir: Path,
        relative_override: Optional[Path],
    ) -> Path:
        """Compute a FileResult relative path (virtual-path contract in the comments below)."""
        if relative_override is not None:
            # Virtual-path contract: posix separators ("bundle.zip/docs/chapter.md");
            # str(Path) on Windows would produce backslashes and break the origin
            # contract — the FileResult layer normalizes explicitly with as_posix().
            return Path(str(relative_override).replace("\\", "/"))
        return file_path.relative_to(dataset_dir)

    def _export_image_assets(
        self, result, relative_path: Path, profiler: RuntimeProfiler
    ) -> Optional[Dict[str, Any]]:
        """Image-asset export (opt-in).

        Image bytes travel in memory with the metadata up to this point: unconditionally
        popped so that no downstream JSON serialization path (raw export / debug /
        summary) ever touches bytes; when the switch is on they are written to separate storage.
        """
        image_payloads = (result.document.metadata or {}).pop(
            "image_asset_payloads", None
        )
        image_asset_info = None
        if self.config.enable_image_asset_export and image_payloads:
            with profiler.stage("image_assets"):
                image_asset_info = self.image_asset_exporter.export(
                    result=result,
                    relative_path=relative_path,
                    payloads=image_payloads,
                )
        return image_asset_info

    def _run_quality_evals(
        self,
        *,
        result,
        semantic_repr,
        is_narrative: bool,
        is_huge: bool,
        ingestion_domain: str,
        relative_path: Path,
        line_count: int,
        profiler: RuntimeProfiler,
    ):
        """Quality-evaluation cold path: doc_quality → density downgrade → narrative evaluation →
        failure modes → domain quality evaluation.

        Returns (doc_quality, evaluation_result, failure_report,
        domain_quality_result, eval_repr); eval_repr is the post-density-downgrade
        evaluation-basis representation (the same object as semantic_repr when not
        downgraded, so the orchestrator can detect density_adapted via ``is not``).

        ── Quality-evaluation cold-path discipline ─────────────────
        Quality evaluation is observational/diagnostic and never participates in ingestion decisions.
        Structural guarantee: semantic_repr is already built by the time this runs, and
        quality evaluation executes after it — quality scores cannot affect chunking /
        representation / output or any other ingestion behavior. Same for corpus_tier: it feeds
        only corpus-stratification metadata and report grouping, never the pipeline.
        If quality-score-driven adaptation is ever needed (e.g. auto-tuning), it must go through an
        explicit feedback loop executed between runs, not gate within the same run.
        ──────────────────────────────────────────────────────────────
        """
        # document quality
        doc_quality = None
        if not (is_huge and self.config.huge_file_skip_doc_quality):
            with profiler.stage("doc_quality"):
                doc_quality = self.doc_quality_evaluator.evaluate(result.document)

        # DensityAdaptation: downgrade quality/failure evaluation for large documents
        eval_repr = self._density_adapted_repr(
            semantic_repr, is_narrative, line_count
        )

        assert semantic_repr is not None or not is_narrative, \
            "Quality cold-path violation: semantic_repr must be built before quality eval"
        evaluation_result = None
        if eval_repr and is_narrative and not (is_huge and self.config.huge_file_skip_evaluation):
            with profiler.stage("evaluation"):
                evaluation_result = self.quality_evaluator.evaluate(
                    document=result.document,
                    semantic_representation=eval_repr,
                    strategy=self.policy,
                    document_quality=doc_quality,
                )

        # failure modes
        failure_report = None
        if eval_repr and is_narrative and not (is_huge and self.config.huge_file_skip_failure_modes):
            with profiler.stage("failure_modes"):
                failure_benchmark = RetrievalFailureBenchmark()
                failure_report = failure_benchmark.run(
                    chunks=eval_repr.content,
                    document_id=str(relative_path),
                    document_text=result.document.text,
                )

        # ── Domain quality evaluation (RE envelope generalization §10/§11) ─────
        # Covers the four domains record_stream / structured_config / spreadsheet /
        # code (contract-table driven). Domain determination uses ingestion_domain
        # (ingestion_mode is not an authoritative signal for non-narrative domains).
        # Cold-path discipline matches narrative: evaluation runs after representation
        # and never participates in decisions. When the huge fast path skips it,
        # domain_quality_result=None and the three-layer envelope honestly downgrades
        # in save_outputs (skipped/not_evaluated).
        domain_quality_result = None
        domain_evaluator = self.domain_quality_evaluators.get(
            ingestion_domain
        )
        if (
            eval_repr
            and domain_evaluator is not None
            and not (is_huge and self.config.huge_file_skip_evaluation)
        ):
            with profiler.stage(f"{ingestion_domain}_evaluation"):
                domain_quality_result = domain_evaluator.evaluate(
                    document=result.document,
                    semantic_representation=eval_repr,
                    strategy=self.policy,
                    document_quality=doc_quality,
                )

        return (
            doc_quality,
            evaluation_result,
            failure_report,
            domain_quality_result,
            eval_repr,
        )

    def _density_adapted_repr(
        self, semantic_repr, is_narrative: bool, line_count: int
    ):
        """DensityAdaptation: downgrade quality/failure evaluation for large documents.

        Returns semantic_repr unchanged when no downgrade triggers (preserving object
        identity for the ``density_adapted = eval_repr is not semantic_repr`` check).
        """
        density = self.policy.scale.density_adaptation
        if semantic_repr and is_narrative and density.should_downscale(line_count):
            units = semantic_repr.content
            total = len(units)
            sample_n = min(total, max(
                self.policy.scale.representation_scaling.preview_sample_size,
                20,
            ))
            if total > sample_n:
                step = total / sample_n
                sampled = [units[int(i * step)] for i in range(sample_n)]
                return RepresentationResult(
                    representation_type=semantic_repr.representation_type,
                    content=sampled,
                    metadata={
                        **(semantic_repr.metadata or {}),
                        "density_adapted": True,
                        "density_mode": density.detect_mode(line_count),
                        "full_unit_count": total,
                        "sampled_unit_count": len(sampled),
                    },
                )
        return semantic_repr

    @staticmethod
    def _extract_failure_mode_list(failure_report) -> list:
        """Extract failure modes (compatible with dict and FailureReport objects)."""
        failure_mode_list = []
        if failure_report:
            if hasattr(failure_report, 'failures'):
                failure_mode_list = [f.mode.value for f in failure_report.failures]
            elif isinstance(failure_report, dict):
                failure_mode_list = failure_report.get("failure_modes", [])
        return failure_mode_list

    def _classify_corpus_tier(
        self,
        *,
        artifacts: Dict[str, Any],
        evaluation_result,
        domain_quality_result,
        file_size_bytes: int,
        chunk_count: int,
        benchmark_tags,
        relative_path: Path,
    ):
        """Corpus Stratification: classify document tiers.

        Dimension basis: the four invariant_v1 dimensions (rse.quality_profile.dimensions,
        canonical; csc has been demoted back to candidate and scores nothing); falls back to
        domain-dimension extraction when the rse layer is missing or its dimensions
        are empty (skipped/partial). record_stream (RE envelope generalization):
        rse.quality_profile.dimensions holds the event_quality_v2.0 domain dimensions —
        _extract_re_dimensions is generic over the {dim: score} shape; no domain branch needed.

        Returns (corpus_tier, quality_dims, active_evaluation).
        """
        re_dims = self._extract_re_dimensions(artifacts.get("rse"))
        quality_dims = re_dims or self._extract_quality_dimensions(evaluation_result)
        # narrative and four-domain evaluation results are mutually exclusive (domain
        # determination); the dual-track switch picks one for FileResult / corpus-stratification consumption
        active_evaluation = evaluation_result or domain_quality_result
        corpus_tier = self.corpus_stratifier.classify(
            file_size_bytes=file_size_bytes,
            chunk_count=chunk_count,
            benchmark_tags=benchmark_tags,
            quality_overall=canonical_quality(active_evaluation),
            quality_dimensions=quality_dims,
            relative_path=str(relative_path),
            success=True,
        )
        return corpus_tier, quality_dims, active_evaluation

    @staticmethod
    def _record_complexity_metrics(
        *,
        profiler: RuntimeProfiler,
        result,
        semantic_repr,
        file_size_bytes: int,
        line_count: int,
        chunk_count: int,
    ) -> None:
        """RuntimeProfiler: complexity-metric registration."""
        profiler.record("file_size_bytes", file_size_bytes)
        profiler.record("char_count", len(result.document.text) if result.document.text else 0)
        profiler.record("line_count", line_count)
        profiler.record("base_units", chunk_count)
        _fine: float = 0
        if semantic_repr and semantic_repr.metadata:
            gs = semantic_repr.metadata.get("granularity_stats")
            if isinstance(gs, dict):
                _f = gs.get("fine")
                if isinstance(_f, dict):
                    _fine = _f.get("count", 0)
                elif isinstance(_f, (int, float)):
                    _fine = _f
        profiler.record("fine_units", _fine)

    @staticmethod
    def _extract_pdf_metrics(file_path: Path, result) -> Optional[Dict[str, Any]]:
        """PDF productionization: extract single-file runtime/reliability facts for run-level aggregation."""
        if file_path.suffix.lower() != ".pdf":
            return None
        # Honest-failure documents (format mismatch / missing optional
        # backend) have no PDF runtime facts to extract — the parser
        # never ran, return None; run_metrics depends on the pdf module
        # chain (top-level import of pymupdf), which cannot be imported
        # without the optional dependency — likewise honestly returns
        # None. Neither may drag down already-obtained parse results
        # (README/NOTICE auditable degradation, never a crash
        # contract).
        if (result.document.metadata or {}).get("parse_failed"):
            return None
        try:
            from provendoc.core.parsers.narrative.pdf.run_metrics import (
                extract_pdf_run_metrics,
            )
        except ImportError:
            return None
        return extract_pdf_run_metrics(result.document.metadata)

    def _save_error_diagnostic(
        self, file_path: Path, relative_path: Path, error: Exception
    ) -> None:
        """save error diagnostic (to run debug/ if enabled, else skip)."""
        if not getattr(self.config, 'enable_debug', False):
            return
        debug_dir = self.run_dir / "debug" / "diagnostics"
        debug_dir.mkdir(parents=True, exist_ok=True)
        error_path = debug_dir / f"{relative_path.parent / relative_path.stem}_error.json".replace("\\", "_")
        with open(error_path, "w", encoding="utf-8") as f:
            json.dump({
                "file": str(file_path),
                "error": str(error),
                "traceback": traceback.format_exc(),
            }, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _normalize_semantic_representation(semantic_repr):
        """Normalize multi-granularity containers to the default base-granularity representation.

        Narrative representation may return MultiGranularityOutput (coarse/base/fine).
        Runtime quality evaluation, failure benchmarking, export, and reporting all observe
        the base layer — container types never leak downstream, and the representation the parser produced is unchanged.
        """
        if semantic_repr is None:
            return None
        content = getattr(semantic_repr, "content", None)
        base_units = getattr(content, "base", None)
        if base_units is None:
            return semantic_repr

        metadata = dict(getattr(semantic_repr, "metadata", None) or {})
        if hasattr(content, "stats"):
            metadata["granularity_stats"] = content.stats()
        metadata["runtime_granularity"] = "base"
        return RepresentationResult(
            representation_type=semantic_repr.representation_type,
            content=base_units,
            metadata=metadata,
        )

    @staticmethod
    def _extract_re_dimensions(re_layer) -> Optional[Dict[str, float]]:
        """Extract the four RE quality_profile dimension scores (radar-chart data source).

        The four invariant_v1 dimensions are the canonical basis
        (re.quality_profile.dimensions; csc candidates never enter scores),
        preferred over domain-dimension extraction; when dimensions are missing
        (huge skipped / partial) return None and the caller falls back.
        """
        if not re_layer:
            return None
        quality = re_layer.get("quality_profile") or {}
        dimensions = quality.get("dimensions") or {}
        values = {
            k: float(v)
            for k, v in dimensions.items()
            if isinstance(v, (int, float))
        }
        return values or None

    @staticmethod
    def _extract_quality_dimensions(evaluation_result) -> Optional[Dict[str, float]]:
        """Extract legacy quality-profile dimension scores (fallback basis)."""
        if not evaluation_result:
            return None
        profile = evaluation_result.get("profile") or {}
        return {k: float(v) for k, v in profile.items() if isinstance(v, (int, float))}

    @staticmethod
    def _extract_doc_quality(doc_quality) -> Optional[Dict[str, Any]]:
        """Extract the document-quality diagnostics digest (diagnostics-panel data source)."""
        if not doc_quality:
            return None
        return {
            "overall": doc_quality.get("overall"),
            "profile": doc_quality.get("profile") or {},
            "issues": doc_quality.get("issues") or [],
            "metadata": doc_quality.get("metadata") or {},
        }

    def _save_file_outputs(self, result, semantic_repr, evaluation_result, relative_path,
                           benchmark_tags=None, file_size_bytes=0, doc_quality=None, profiler=None,
                           raw_semantic_repr=None, domain_quality_result=None,
                           failure_report=None):
        """Save all outputs for a single file (the six file layers of the seven-dimension artifact contract).

        Runtime Artifact Semantics (seven-dimension contract):
            - raw/             — source text + original_text + DQR (document_quality)
            - markdown/        — pure presentation rendering of base units
            - representation/  — cleaned_text + coarse/base/fine three-granularity views
            - evaluation/      — unified evaluation (failure_modes as a top-level dimension)
            - optimization/    — run-level strategy-search conclusion (official, publishable artifacts)
            - debug/ (optional) — full-chain intermediate state, parse → representation → evaluation
            - (the legacy standalone chunks/ directory has been folded into representation.chunks)

        Returns:
            dict: artifact metadata (relative paths + markdown content) for report consumption
        """
        base_name = str(relative_path.parent / relative_path.stem).replace("\\", "_")
        artifacts = {
            "base_name": base_name,
            "raw_json": None,
            "markdown": None,
            "markdown_content": None,
            "representation_json": None,
            "chunks_json": None,
            "evaluation_json": None,
            "failure_json": None,
            "debug_json": None,
        }

        # Huge file fast-path
        is_huge = file_size_bytes >= self.config.huge_file_threshold

        # Debug execution-trace availability: RE provenance.execution_trace and the
        # L5 debug export share the same expression (single source of truth; defined
        # before all consuming blocks, so the debug block stays usable when evaluation is off).
        exec_trace_available = bool(
            getattr(self.config, "enable_debug", False)
        ) and not (
            is_huge and self.config.huge_file_skip_debug
        )

        # RepresentationScaling: sampled preview for large files to avoid serialization blowup
        scaling = self.policy.scale.representation_scaling
        repr_for_output = semantic_repr
        sampling_meta = None
        if semantic_repr and benchmark_tags:
            unit_count = len(semantic_repr.content) if hasattr(semantic_repr, "content") else 0
            if scaling.should_sample(benchmark_tags, unit_count):
                sampled_content, sampling_meta = scaling.sample_preview(semantic_repr.content)
                repr_for_output = RepresentationResult(
                    representation_type=semantic_repr.representation_type,
                    content=sampled_content,
                    metadata={
                        **(semantic_repr.metadata or {}),
                        "scaling_sampled": True,
                        "scaling_meta": sampling_meta,
                    },
                )
                artifacts["sampling_meta"] = sampling_meta

        # ── raw: parser raw snapshot (original_text, pre-cleaning) + DQR ──
        # skip for huge files (serialization is O(n) expensive)
        if self.config.enable_raw_export and not (is_huge and self.config.huge_file_skip_debug):
            raw_path = self.run_dir / "raw" / f"{base_name}_raw.json"
            self._submit_export(
                self.raw_exporter.export,
                result.document, str(raw_path), doc_quality,
            )
            artifacts["raw_json"] = f"raw/{base_name}_raw.json"

        if repr_for_output:
            # ── representation: cleaned full text + three-granularity views (contract schema) ──
            # Export the original full semantic representation (including the coarse/base/fine
            # of MultiGranularityOutput), not just the normalized base units.
            # selected_granularity defaults to "base" (the observation basis for quality evaluation/failure).
            # The legacy standalone chunks/ export has been folded into representation.chunks (seven-dimension contract).
            # representation is the primary content artifact: huge files still export it by default
            # (previously mis-gated by skip_debug, leaving book-sized files with no representation artifact at all).
            # Serialization cost is affordable (~3x text volume); in benchmark mode the
            # RepresentationScaling sampled preview still applies to huge/stress.
            if self.config.enable_representation_export and not (
                is_huge and self.config.huge_file_skip_representation
            ):
                repr_export_target = raw_semantic_repr if raw_semantic_repr is not None else repr_for_output
                repr_path = self.run_dir / "representation" / f"{base_name}_repr.json"
                self._submit_export(
                    self.representation_exporter.export,
                    result.document, repr_export_target, str(repr_path),
                    self.policy, "base",
                )
                artifacts["representation_json"] = f"representation/{base_name}_repr.json"

            # ── markdown: pure presentation rendering of base units (not a source of truth) ──
            # For huge files, skip both generation and disk write to avoid O(n) cost.
            skip_md_disk = (
                not self.config.enable_markdown_export
                or (is_huge and self.config.huge_file_skip_markdown)
            )
            if not skip_md_disk:
                md_content = self.markdown_exporter.to_markdown(repr_for_output.content)
                artifacts["markdown_content"] = md_content
                md_path = self.run_dir / "markdown" / f"{base_name}.md"
                self._submit_export(self.markdown_exporter.export, repr_for_output.content, str(md_path))
                artifacts["markdown"] = f"markdown/{base_name}.md"
            elif not is_huge:
                # disabled by config but not huge: still generate content for report
                artifacts["markdown_content"] = self.markdown_exporter.to_markdown(
                    repr_for_output.content
                )
            else:
                # huge file: placeholder instead of generating full markdown
                artifacts["markdown_content"] = f"[Huge file ({file_size_bytes} bytes) — markdown skipped]"

        # ── evaluation: unified evaluation (continuous metrics + failure_modes top-level dimension) ──
        # The failure report is no longer persisted separately: it is merged into {base}_evaluation.json's failure_modes dimension
        # (artifact contract §3). risk_score does not mix into overall.
        # Step 4 (RE→RSE migration, constraint 4): the huge lightweight gate — when quality
        # is skipped (huge_file_skip_evaluation) but failure modes still run, the same RSE
        # assembly runs with a quality-missing stub (quality_evaluation="skipped"): the gate
        # switches to lightweight mode (structure checks kept, quality_score=None as honest absence).
        if self.config.enable_evaluation_export and (evaluation_result or failure_report):
            if evaluation_result:
                unified_evaluation = build_unified_evaluation(
                    evaluation_result,
                    failure_report=failure_report,
                    document_id=str(relative_path),
                )
            else:
                # huge lightweight stub: metadata.chunk_count drives gate density normalization
                # (_gate_status falls back to the representation chunk count when unavailable)
                _stub_chunks = None
                if repr_for_output is not None:
                    _stub_content = getattr(repr_for_output, "content", None)
                    if hasattr(_stub_content, "__len__"):
                        _stub_chunks = len(_stub_content)
                unified_evaluation = build_unified_evaluation(
                    {
                        "quality_evaluation": "skipped",
                        "metadata": {
                            "chunk_count": _stub_chunks,
                            "note": (
                                "huge fast-path stub: quality skipped, "
                                "gate lightweight on failure modes"
                            ),
                        },
                    },
                    failure_report=failure_report,
                    document_id=str(relative_path),
                )
            # RE→RSE migration steps 0/1: three-layer envelope (rse / compatibility;
            # the cqi_version field was removed with the RSE version bump)
            # merged incrementally into the unified output — keys are only added, no existing field or score changes.
            # Step 1: provenance's three facts (parser fingerprint / policy fingerprint /
            # this file's debug execution-trace availability). Contract:
            # re_migration_plan.md §2.3/§3.
            # Step 3: context_self_containment candidate dimension — a standalone invariant
            # (not in the narrative registry, invariant_composite untouched);
            # results land in re.candidate_profile, physically isolated from quality_score.
            # 2026-10-01: demoted back to candidate per the admission preregistration
            # (the batch evidence was judged insufficient for step-4 promotion); pending a second human review batch.
            # try/except backstop: candidate evaluation must not become a new runtime single point of failure.
            # The huge lightweight stub skips csc (quality is already skipped; no double cost).
            context_candidate = None
            if evaluation_result is not None:
                try:
                    context_candidate = evaluate_context_self_containment(
                        repr_for_output,
                        document=result.document,
                        strategy=self.policy,
                    )
                except Exception:
                    context_candidate = None
            re_envelope = build_re_report(
                unified_evaluation,
                representation=repr_for_output,
                provenance=build_provenance_fact(
                    compute_parser_fingerprint(),
                    policy_version_string(self.policy),
                    exec_trace_available,
                ),
                context_candidate=context_candidate,
                document_text=getattr(result.document, "text", None),
                policy=self.policy,
                # scenario visibility block (config_scenario_capability_prereg_v1):
                # L2 payload passthrough; missing payload → None → zero drift in the capability key set.
                scenario=(result.document.metadata or {}).get("scenario"),
            )
            unified_evaluation["rse"] = re_envelope["rse"]
            unified_evaluation["compatibility"] = re_envelope["compatibility"]
            # The rse layer feeds FileResult dimension extraction (four-dimension canonical basis);
            # it stays out of the artifacts contract surface (in-memory only, never persisted).
            artifacts["rse"] = re_envelope["rse"]
            evaluation_path = self.run_dir / "evaluation" / f"{base_name}_evaluation.json"
            self._submit_export(write_json_sync, unified_evaluation, str(evaluation_path))
            artifacts["evaluation_json"] = f"evaluation/{base_name}_evaluation.json"

        # ── Domain quality evaluation: the common three-layer RSE envelope for the four domains (§10/§11 generalization) ──────
        # record_stream / structured_config / spreadsheet / code get domain-specific
        # assembly and do not ride narrative's build_unified_evaluation track (these
        # domains have no invariant/failure_modes benchmark). Merged incrementally: keys are
        # only added (rse / compatibility); no existing field changes.
        # The huge fast path (domain_quality_result=None) does not export — consistent with
        # narrative's behavior when both tracks are empty.
        elif self.config.enable_evaluation_export and domain_quality_result is not None:
            domain_name = (result.metadata or {}).get(
                "ingestion_domain", "narrative"
            )
            re_envelope = build_domain_rse_report(
                domain_name,
                domain_quality_result,
                representation=repr_for_output,
                provenance=build_provenance_fact(
                    compute_parser_fingerprint(),
                    policy_version_string(self.policy),
                    exec_trace_available,
                ),
            )
            domain_unified = {
                **domain_quality_result,
                "rse": re_envelope["rse"],
                "compatibility": re_envelope["compatibility"],
            }
            # The rse layer feeds FileResult dimension extraction (domain-dimension canonical basis);
            # it stays out of the artifacts contract surface (in-memory only, never persisted).
            artifacts["rse"] = re_envelope["rse"]
            evaluation_path = self.run_dir / "evaluation" / f"{base_name}_evaluation.json"
            self._submit_export(write_json_sync, domain_unified, str(evaluation_path))
            artifacts["evaluation_json"] = f"evaluation/{base_name}_evaluation.json"

        # ── L5 debug: full-chain intermediate state, parse → representation → evaluation (optional) ─────────
        # Availability uses the same expression as RE provenance.execution_trace
        # (single source of truth; the capability layer never pretends a trace exists).
        if exec_trace_available:
            debug_payload = self._build_debug_trace(
                result=result,
                semantic_repr=semantic_repr,
                repr_for_output=repr_for_output,
                evaluation_result=evaluation_result,
                domain_quality_result=domain_quality_result,
                failure_report=failure_report,
                doc_quality=doc_quality,
                profiler=profiler,
                relative_path=relative_path,
                file_size_bytes=file_size_bytes,
                benchmark_tags=benchmark_tags,
            )
            debug_path = self.run_dir / "debug" / f"{base_name}_debug.json"
            self._submit_export(write_json_sync, debug_payload, str(debug_path))
            artifacts["debug_json"] = f"debug/{base_name}_debug.json"

        return artifacts

    def _build_debug_trace(self, result, semantic_repr, repr_for_output, evaluation_result,
                           failure_report, doc_quality, profiler, relative_path,
                           file_size_bytes=0, benchmark_tags=None, domain_quality_result=None):
        """Build the single-file full-pipeline debug trace (parse → representation → evaluation).

        Debug-layer contract: the complete intermediate state from parser input
        to quality evaluation, covering every pipeline stage, so any stage
        is easy to troubleshoot. (Run-level global info lives in debug/run_trace.json, written by execute().)
        """
        meta = result.metadata or {}
        doc_meta = result.document.metadata or {}

        # parse stage: document + parser metadata
        parse_stage = {
            "document": {
                "text": result.document.text,
                "original_text_included": getattr(result.document, "original_text", None) is not None,
                "metadata": {
                    k: v for k, v in doc_meta.items()
                    if k not in ("text", "original_text")
                },
            },
            "ingestion_metadata": meta,
        }

        # representation stage: semantic representation units + granularity stats
        repr_stage = None
        if repr_for_output is not None:
            units = repr_for_output.content or []
            repr_stage = {
                "representation_type": repr_for_output.representation_type,
                "unit_count": len(units),
                "metadata": repr_for_output.metadata or {},
                "units": units,
            }

        # evaluation stage: quality evaluation + domain evaluation + failure + doc_quality
        eval_stage = {
            "evaluation": evaluation_result,
            "domain_quality": domain_quality_result,
            "failure_report": (
                asdict(failure_report)
                if failure_report is not None and hasattr(failure_report, "__dataclass_fields__")
                else failure_report
            ),
            "doc_quality": doc_quality,
        }

        return {
            "layer": "debug",
            "file": {
                "relative_path": str(relative_path),
                "file_size_bytes": file_size_bytes,
                "benchmark_tags": benchmark_tags or ["normal"],
            },
            "config": asdict(self.config),
            "pipeline_trace": profiler.to_dict() if profiler is not None else None,
            "stages": {
                "parse": parse_stage,
                "representation": repr_stage,
                "evaluation": eval_stage,
            },
        }

    def _compute_summary(self):
        """Compute the run summary — with Overall/Filtered quality dual-track aggregation and corpus-stratification distribution."""
        files = self.summary.files
        self.summary.total_files = len(files)
        self.summary.success_files = sum(1 for f in files if f.success)
        self.summary.failed_files = sum(1 for f in files if not f.success)

        # Runtime Scale Policy: per-document weighting so large files don't pollute statistics
        scale = self.policy.scale

        # === Overall Quality (all documents — the raw, mixed reality of real input) ===
        quality_obs = [
            ScoreObservation(
                score=f.quality_overall,
                unit_count=max(f.chunk_count, 1),
                tags=f.benchmark_tags or ["normal"],
                label=f.relative_path,
            )
            for f in files if f.quality_overall is not None
        ]
        conf_obs = [
            ScoreObservation(
                score=f.quality_confidence,
                unit_count=max(f.chunk_count, 1),
                tags=f.benchmark_tags or ["normal"],
                label=f.relative_path,
            )
            for f in files if f.quality_confidence is not None
        ]

        self.summary.quality_aggregation = scale.aggregate_scores(quality_obs)
        self.summary.confidence_aggregation = scale.aggregate_scores(conf_obs)

        # Scalar fields kept for downstream compatibility (report / summary.json consumers)
        self.summary.avg_quality_overall = self.summary.quality_aggregation.get("overall") or 0.0
        self.summary.avg_quality_confidence = (
            self.summary.confidence_aggregation.get("overall") or 0.0
        )

        # === Corpus Stratification: corpus-tier distribution ===
        tiers = [
            CorpusTier(f.corpus_tier) if f.corpus_tier else CorpusTier.NORMAL_NARRATIVE
            for f in files
        ]
        self.summary.corpus_distribution = self.corpus_stratifier.distribution(tiers)

        # === Corpus Drift Snapshot: between-run tier-composition snapshot ===
        # Records this run's tier distribution for cross-run diffing to detect corpus drift
        from collections import Counter
        tier_counts = Counter(t.value for t in tiers)
        total_count = sum(tier_counts.values())
        snapshot_tiers = {}
        for tier_name, count in sorted(tier_counts.items()):
            snapshot_tiers[tier_name] = {
                "count": count,
                "pct": round(count / total_count * 100, 1) if total_count else 0.0,
            }
        self.summary.corpus_tier_snapshot = {
            "run_id": self.summary.run_id,
            "timestamp": self.summary.timestamp,
            "total": total_count,
            "tiers": snapshot_tiers,
            "meaningful_count": sum(
                c for t, c in tier_counts.items()
                if CorpusTier(t) in self.corpus_stratifier.meaningful_tiers()
            ),
        }

        # === Filtered Quality (meaningful documents only = Normal Narrative + Robustness) ===
        meaningful_tiers = self.corpus_stratifier.meaningful_tiers()
        meaningful_files = [
            f for f in files
            if CorpusTier(f.corpus_tier or "normal_narrative") in meaningful_tiers
        ]

        filtered_quality_obs = [
            ScoreObservation(
                score=f.quality_overall,
                unit_count=max(f.chunk_count, 1),
                tags=f.benchmark_tags or ["normal"],
                label=f.relative_path,
            )
            for f in meaningful_files if f.quality_overall is not None
        ]
        filtered_conf_obs = [
            ScoreObservation(
                score=f.quality_confidence,
                unit_count=max(f.chunk_count, 1),
                tags=f.benchmark_tags or ["normal"],
                label=f.relative_path,
            )
            for f in meaningful_files if f.quality_confidence is not None
        ]

        self.summary.quality_filtered = scale.aggregate_scores(filtered_quality_obs)
        # Also aggregate confidence
        self.summary.quality_filtered["confidence"] = (
            scale.aggregate_scores(filtered_conf_obs).get("overall")
        )
        self.summary.quality_filtered["meaningful_count"] = len(meaningful_files)

        # Filtered dimension means (for radar-chart comparison)
        filtered_dims: Dict[str, List[float]] = {}
        for f in meaningful_files:
            for dim, val in (f.quality_dimensions or {}).items():
                filtered_dims.setdefault(dim, []).append(val)
        self.summary.quality_filtered_dimensions = {
            k: sum(v) / len(v) for k, v in filtered_dims.items() if v
        }

        # === PDF productionization: run-level runtime/reliability aggregation (pure observation) ===
        pdf_metrics = [f.pdf_runtime_summary for f in files if f.pdf_runtime_summary]
        if pdf_metrics:
            from provendoc.core.parsers.narrative.pdf.run_metrics import (
                aggregate_pdf_run_metrics,
            )

            self.summary.pdf_runtime_summary = aggregate_pdf_run_metrics(pdf_metrics)

        # === Semantic Stability Observer: stability landscape ===
        # Sweep meaningful documents to find stable plateaus and degradation cliffs
        # Cap sweep samples to control cost (at most 40 representative documents)
        sweep_candidates = [
            f for f in meaningful_files
            if f.raw_text and len(f.raw_text) > 200 and f.quality_overall is not None
        ]
        sweep_sample = sweep_candidates[:40] if len(sweep_candidates) > 40 else sweep_candidates

        if sweep_sample:
            # Build the document-object list (lightweight proxies)

            sweep_files = [
                {
                    "raw_text": f.raw_text,
                    "document": self._make_sweep_document(f),
                    "doc_quality": None,
                }
                for f in sweep_sample
            ]

            current_sl = self.config.packaging_size_soft_limit
            agg_result = self.stability_observer.sweep_batch(
                sweep_files, self.quality_evaluator, current_soft_limit=current_sl,
            )

            self.summary.stability_result = {
                "soft_limits": agg_result.soft_limits,
                "overall_scores": agg_result.overall_scores,
                "dimension_scores": agg_result.dimension_scores,
                "chunk_counts": agg_result.chunk_counts,
                "degradation_onsets": agg_result.degradation_onsets,
                "stable_region": list(agg_result.stable_region) if agg_result.stable_region else None,
                "confidence_margin": agg_result.confidence_margin,
                "current_point": agg_result.current_point,
                "health_status": agg_result.health_status,
                "document_count": agg_result.document_count,
            }

    @staticmethod
    def _make_sweep_document(file_result):
        """Build lightweight document objects for the stability sweep."""
        return _SweepDoc(
            text=file_result.raw_text or "",
        )

    def execute(self) -> RunSummary:
        """Execute a complete run.

        Returns:
            RunSummary: the run summary
        """
        # init
        self.artifacts.init_run_dir(self.run_id)

        # cache cleanup (TTL + size limit) at start of run
        cache_before = self.parse_cache.stats()
        if self.config.enable_parse_cache:
            cleanup_info = self.parse_cache.cleanup()
            if cleanup_info["removed_ttl"] or cleanup_info["removed_size"]:
                print(f"[ProvendocRun] Cache cleanup: removed {cleanup_info['removed_ttl']} expired + {cleanup_info['removed_size']} over-size entries")
            cache_before = {"entries": cleanup_info["entries"], "size_mb": cleanup_info["size_mb"]}

        # process files
        dataset_dir = Path(self.config.dataset_dir)
        if not dataset_dir.exists():
            raise ValueError(f"Dataset directory not found: {dataset_dir}")

        # Planned domains may be present in the taxonomy, but only active
        # domains are discoverable by this runtime.
        supported_suffixes = get_all_supported_suffixes(active_only=True)
        from provendoc.core.parsers.archive import is_archive

        expand_archives = self.config.enable_archive_expansion
        files = [
            p for p in dataset_dir.rglob("*")
            if p.is_file() and (
                p.suffix.lower() in supported_suffixes
                or (expand_archives and is_archive(p))
            )
        ]

        # ── Archive expansion: expand archives at collection time ─────────
        # archives are not a parse domain: expanded member files flow into their
        # own domain's parser as usual; virtual paths preserve origin (archive.zip/inner.md).
        # entries = [(real path, virtual relative path)]; for regular files the virtual path is just the real relative path.
        archive_work_dir = None
        if expand_archives and any(is_archive(p) for p in files):
            from provendoc.core.parsers.archive import (
                ArchiveExpander,
                ExpansionLimits,
            )

            archive_work_dir = Path(
                tempfile.mkdtemp(prefix="provendoc_archive_")
            )
            limits = ExpansionLimits(
                max_depth=self.config.archive_max_depth,
                max_members_per_archive=self.config.archive_max_members,
                max_member_bytes=self.config.archive_max_member_mb * 1024 * 1024,
                max_total_bytes=self.config.archive_max_total_gb * 1024 * 1024 * 1024,
            )
            expander = ArchiveExpander(archive_work_dir, sorted(supported_suffixes), limits)
            regular = [
                (p, p.relative_to(dataset_dir))
                for p in files if not is_archive(p)
            ]
            archives = [
                (p, p.relative_to(dataset_dir).as_posix())
                for p in files if is_archive(p)
            ]
            expanded, archive_infos = expander.expand(archives)
            entries = regular + expanded
            self.summary.archive_expansion = {
                "archives": archive_infos,
                "extracted_members": len(expanded),
                "depth_stops": expander.depth_stops,
            }
            if archive_infos:
                total_members = sum(a["extracted"] for a in archive_infos)
                print(
                    f"[ProvendocRun] Archive expansion: {len(archives)} archive(s) "
                    f"→ {total_members} member(s) extracted"
                )
        else:
            entries = [(p, p.relative_to(dataset_dir)) for p in files]
            self.summary.archive_expansion = {"archives": [], "extracted_members": 0}

        total = len(entries)

        # ── Config summary ───────────────────────────────────
        mode = self.config.runtime_mode.upper()
        cache = "on" if self.config.enable_parse_cache else "off"
        thresh = self.config.huge_file_threshold
        thresh_str = f"{thresh // 1024}KB" if thresh >= 1024 else f"{thresh}B"
        async_label = "off"
        if getattr(self.config, 'enable_async_export', False):
            async_label = f"on({self.config.async_export_workers})"
        workers_label = "seq"
        if self.config.enable_parallel and total > 1:
            w = self.config.parallel_workers
            if w <= 0:
                w = os.cpu_count() or 4
            workers_label = f"{w} proc"
        print(f"[ProvendocRun] {mode:10s} | files={total} | workers={workers_label} | cache={cache} | threshold={thresh_str} | async_export={async_label}")
        print(f"[ProvendocRun] Dataset: {dataset_dir}")
        if total == 0:
            print("[ProvendocRun] No supported files found, skipping.")
            return self.summary

        import time as _time
        _t0 = _time.perf_counter()

        # RuntimeProfiler: cross-file aggregation
        profile_aggregator = ProfileAggregator() if self.config.enable_profiling else None
        interval = self.config.progress_interval

        def _print_progress(done: int):
            if interval <= 0 or done <= 0:
                return
            pct = done / total * 100
            elapsed = _time.perf_counter() - _t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  [{done}/{total}] {pct:.0f}% — {elapsed:.1f}s elapsed, ETA ~{eta:.0f}s")

        # ── Parallel file processing ──────────────────────────
        # ProcessPoolExecutor bypasses the GIL for true CPU parallelism.
        # Each worker process creates its own parser/evaluator/exporter instances.
        # raw_text stays in FileResult — the report generator needs it to render document content and compute the coherence curve.
        if self.config.enable_parallel and len(entries) > 1:
            workers = self.config.parallel_workers
            if workers <= 0:
                workers = os.cpu_count() or 4
            workers = min(workers, len(entries))
            args_list = [
                (str(fp), str(dataset_dir), self.config, str(self.run_dir), str(vp))
                for fp, vp in entries
            ]
            from concurrent.futures import as_completed
            with ProcessPoolExecutor(max_workers=workers) as executor:
                # submit all tasks; as_completed yields in completion order
                # so a single slow file never blocks faster ones
                futures = {executor.submit(_parallel_process_file, args): args
                           for args in args_list}
                done = 0
                next_checkpoint = interval
                for future in as_completed(futures):
                    result = future.result()
                    done += 1
                    if done == next_checkpoint and done < total:
                        _print_progress(done)
                        next_checkpoint = min(next_checkpoint + interval, total)
                    self.summary.files.append(result)
                    if profile_aggregator is not None and result.profile:
                        profile_aggregator.add(str(result.relative_path), result.profile)
            # sort by relative_path for deterministic output
            self.summary.files.sort(key=lambda r: r.relative_path)
        else:
            next_checkpoint = interval
            for idx, (file_path, virtual_path) in enumerate(entries):
                result = self._process_file(file_path, dataset_dir, relative_override=virtual_path)
                self.summary.files.append(result)
                if profile_aggregator is not None and result.profile:
                    profile_aggregator.add(str(result.relative_path), result.profile)
                done = idx + 1
                if done == next_checkpoint and done < total:
                    _print_progress(done)
                    next_checkpoint = min(next_checkpoint + interval, total)

        # ── Archive work-dir cleanup: members were consumed during processing; delete temp dirs immediately ──
        if archive_work_dir is not None:
            shutil.rmtree(archive_work_dir, ignore_errors=True)

        # flush any pending async exports before report
        self._flush_exports()

        elapsed_total = _time.perf_counter() - _t0
        # Explicitly print 100% when the file-processing phase finishes so the last line doesn't stall mid-progress.
        print(f"  [{total}/{total}] 100% — {elapsed_total:.1f}s elapsed, file processing complete")

        # finalize
        self._compute_summary()

        # Strategy Optimizer: cold-path parameter-search advice (never writes back to config)
        if getattr(self.config, "run_optimization", False):
            try:
                self.summary.optimization_summary = run_strategy_optimization(self.summary.files)
            except Exception as e:
                print(f"[ProvendocRun] Strategy optimization skipped: {e}")
                self.summary.optimization_summary = {"error": str(e)}

        # ── L6 optimization: official, publishable run-level optimization + observation artifacts ──
        # The optimization/ layer is not gated by enable_debug because it holds official, outward-facing data:
        #   - optimization_summary.json: strategy parameter-search advice
        #   - profile_summary.json: RuntimeProfiler aggregate report
        #   - run_trace.json: run-level global trace (aggregation/optimization/stability/cache)
        if getattr(self.config, "enable_optimization_export", True):
            try:
                cache_after = self.parse_cache.stats()
                self.artifacts.write_optimization_artifacts(
                    self.summary, elapsed_total, cache_before, cache_after,
                    profile_aggregator,
                )
            except Exception as e:
                print(f"[ProvendocRun] Optimization artifacts write skipped: {e}")

        # ── debug: run-level global trace (debug mode only) ──
        # Complements the per-file debug/{base}_debug.json, covering run-level global info.
        # Kept for development; switch off wholesale in production via enable_debug=False.
        if getattr(self.config, "enable_debug", False):
            try:
                cache_after = self.parse_cache.stats()
                self.artifacts.write_run_trace(
                    self.summary, elapsed_total, cache_before, cache_after,
                )
            except Exception as e:
                print(f"[ProvendocRun] Run trace write skipped: {e}")

        # ── In-domain deepening for code: cross-file project index (opt-in cold path) ──
        # Build the project graph after all files are processed (P2 file edges + P5 verified symbol edges);
        # the artifact lands in run_dir/project_index.json. It never writes back to or annotates
        # persisted chunks and participates in no ingestion decisions (pure retrieval/reporting asset).
        if getattr(self.config, "enable_project_index_export", False):
            try:
                self.artifacts.export_project_index(self.summary, dataset_dir)
            except Exception as e:
                self.summary.project_index_summary = {
                    "enabled": True,
                    "error": str(e),
                }
                print(f"[ProvendocRun] Project index export skipped: {e}")

        self.artifacts.save_summary(self.summary)
        if getattr(self.config, 'enable_report', True):
            self.artifacts.generate_report(self.summary, elapsed_total)

        # RuntimeProfiler: print the aggregate report (files already saved under optimization/)
        if profile_aggregator is not None:
            print()
            print(profile_aggregator.format_text())

        # single summary line — no per-file printing
        s = self.summary
        agg = s.quality_aggregation or {}
        filt = s.quality_filtered or {}
        quality_str = ""
        if s.avg_quality_overall:
            quality_str = f" | quality={s.avg_quality_overall:.2f}({agg.get('mode', 'per_document')})"
            normal_only = agg.get("normal_only")
            excluded = agg.get("excluded_from_normal", 0)
            if normal_only is not None and excluded:
                quality_str += f" | normal={normal_only:.2f}(-{excluded} huge/stress)"
            # Filtered quality (meaningful subset)
            filt_overall = filt.get("overall")
            filt_count = filt.get("meaningful_count", 0)
            if filt_overall is not None:
                quality_str += f" | filtered={filt_overall:.2f}({filt_count} meaningful)"
        print(f"[ProvendocRun] Done: {s.success_files}/{s.total_files} success{quality_str} | wall={elapsed_total:.1f}s")

        # cache summary
        if self.config.enable_parse_cache:
            cache_after = self.parse_cache.stats()
            new_entries = cache_after["entries"] - cache_before["entries"]
            miss_estimate = max(0, new_entries)
            hit_estimate = max(0, s.total_files - miss_estimate)
            hit_rate = (hit_estimate / s.total_files * 100) if s.total_files > 0 else 0
            print(f"[ProvendocRun] Cache: {cache_after['entries']} entries ({cache_after['size_mb']:.1f} MB) | est. hit rate: {hit_rate:.0f}% ({hit_estimate}/{s.total_files})")

        # Clickable file:/// links (Ctrl+Click opens directly in the terminal)
        report_path = (self.run_dir / "reports" / "index.html").resolve()
        report_url = report_path.as_uri()
        print(f"[ProvendocRun] Report: {report_url}")

        # Stale-run cleanup already happened in init_run_dir (cleanup-on-create);
        # nothing to repeat here.

        return self.summary


# ── Module-level worker for ProcessPoolExecutor ──────────────────
# Must be defined at module level for Windows spawn-mode pickling

def _parallel_process_file(args):
    """Process a single file in a subprocess (bypasses GIL).

    Each worker creates its own ProvendocRun instance with the same config and run_dir,
    processes the file, and returns a FileResult.

    Note: Artifacts are written to disk directly in the worker process.
    To prevent Windows IPC pipe deadlocks, large in-memory text buffers are
    capped at 20,000 characters before IPC transfer (matching HTML viewer limits),
    and heavy internal diagnostic structures are cleared.
    """
    file_path_str, dataset_dir_str, config, run_dir_str, relative_override_str = args
    try:
        run = ProvendocRun.for_worker(config, run_dir_str)
        override = Path(relative_override_str) if relative_override_str else None
        result = run._process_file(Path(file_path_str), Path(dataset_dir_str),
                                   relative_override=override)
        # flush async exports before returning to main process
        run._flush_exports()

        # IPC bandwidth optimization:
        # Cap raw_text / markdown to avoid Windows pipe buffer
        # saturation.  Full strings remain safely persisted in raw artifacts.
        if result.raw_text and len(result.raw_text) > 20000:
            result.raw_text = result.raw_text[:20000]
        # source_text must not be truncated without provenance metadata: the
        # Source Document panel uses it to compute source_truncated / count.
        # Normal files have a raw artifact, so drop the IPC copy and let
        # ReportGenerator recover the complete source_text from disk. Huge
        # fast-path files intentionally have no raw artifact; keep their exact
        # decoded source prefix in memory so the preview remains PyCharm-view
        # faithful instead of falling back to the sanitised parser snapshot.
        if result.source_text and len(result.source_text) > 20000:
            result.source_char_count = len(result.source_text)
            result.source_text = (
                None
                if result.raw_json_path
                else result.source_text[:20000]
            )
        if result.markdown_content and len(result.markdown_content) > 20000:
            result.markdown_content = result.markdown_content[:20000]
        if result.quality_issues:
            result.quality_issues = result.quality_issues[:20]
        if len(result.failure_modes) > 100:
            result.failure_modes = result.failure_modes[:100]
        # doc_quality_result can be large; trim to summary only
        if result.doc_quality_result:
            dq = result.doc_quality_result
            result.doc_quality_result = {
                "overall": dq.get("overall"),
                "profile": dq.get("profile") or {},
                "issue_count": len(dq.get("issues") or []),
            }
        result.optimization_result = None
        # KEEP profile for ProfileAggregator — it's small (stages + metrics dict)
        # and is the sole source of runtime profiling data in parallel mode.
        # Previously this was set to None, causing "Aggregated (0 files)".

        return result
    except Exception as e:
        rel = Path(file_path_str)
        try:
            rel = rel.relative_to(Path(dataset_dir_str))
        except ValueError:
            pass
        return FileResult(
            file_path=file_path_str,
            relative_path=rel.as_posix(),
            success=False,
            error=str(e),
        )
