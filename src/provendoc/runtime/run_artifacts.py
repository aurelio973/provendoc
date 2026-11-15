"""Run-level artifact persistence layer.

RunArtifactStore owns the run side of the seven-dimension artifact contract: run-directory
initialization (manifest.json), old-run cleanup, summary/config persistence, the report
entry, the debug run_trace, the three official optimization-layer artifacts, and the
code-domain project-index artifact. run_strategy_optimization is the in-run cold-path parameter search (a pure function).

Split out of provendoc_run.py (structural convergence): this module only persists and cleans;
it makes no ingestion decisions — all facts are passed in by the caller (the ProvendocRun orchestrator).
"""

import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from provendoc.core.diagnostics.runtime_profiler import ProfileAggregator
from provendoc.runtime.config import RunConfig
from provendoc.runtime.run_models import _is_run_dir_name


def write_json_sync(data, path_str: str):
    """Write JSON to file (used for async export submission)."""
    with open(path_str, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def run_strategy_optimization(files):
    """Run a controlled parameter search over a representative file subset and aggregate advice.

    Controlled search (decided 2026-09-07): a profile-first baseline + enumerated
    neighborhood + hard-constraint rejection (never split protected blocks / never
    cross unrelated sections / never exceed the hard limit / structure integrity never below the threshold).

    Sampling: stratified by category, taking the 2 longest successful files per category, capped at 8 total.
    Aggregation: best_strategy per category + a cross-file median as the global recommendation.

    Domain filtering (optimizer_code_bypass_prereg_v1 P2, D2=B): the optimizer is
    narrative-only by design; before sampling, non-narrative files are filtered out
    by the facade whitelist (_PARSER_MAP) and counted as skipped_non_narrative —
    defense in depth, so the official artifact doesn't depend on the caller passing the right file set.
    """
    def _cat_of(f):
        # Same bucketing as the report/aggregate scripts: first two directory levels after separator normalization
        parts = str(f.relative_path).replace("\\", "/").split("/")
        return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "root")

    from provendoc.intelligence.optimization.strategy_optimizer import (
        StrategyOptimizer,
    )

    # facade whitelist (single source of truth: StrategyOptimizer._PARSER_MAP)
    _supported_suffixes = StrategyOptimizer._PARSER_MAP

    by_category = dict()
    skipped_non_narrative = 0
    for f in files:
        if not f.success:
            continue
        if Path(f.file_path).suffix.lower() not in _supported_suffixes:
            skipped_non_narrative += 1
            continue
        by_category.setdefault(_cat_of(f), []).append(f)

    sampled = []
    for cat, group in sorted(by_category.items()):
        group.sort(key=lambda f: getattr(f, "file_size_bytes", 0) or 0, reverse=True)
        sampled.extend(group[:2])
    sampled = sampled[:8]
    if not sampled:
        # An empty set is registered honestly too (skip counts are never lost): when
        # everything is filtered out or fails, the artifact explicitly says why there is no advice instead of a bare empty dict.
        return {
            "sampled_files": 0,
            "skipped_non_narrative": skipped_non_narrative,
            "mode": "controlled",
            "per_category": {},
            "errors": [],
            "global_recommendation": {
                "boundary_mode": None,
                "boundary_votes": {},
                "scale_median": None,
                "files_sampled": 0,
            },
            "note": (
                "no eligible narrative files to sample "
                "(optimizer is narrative-only by design)"
            ),
        }

    optimizer = StrategyOptimizer()
    per_category = {}
    boundary_votes = []
    scale_values = []

    for f in sampled:
        try:
            result = optimizer.optimize(f.file_path, mode="controlled")
        except Exception as e:
            per_category.setdefault("_errors", []).append({"file": f.relative_path, "error": str(e)})
            continue
        best = result.get("best_strategy") or {}
        entry = {
            "file": f.relative_path,
            "profile": result.get("profile"),
            "best_strategy": best,
            "best_score": result.get("best_score"),
            "baseline_score": result.get("baseline_score"),
            "better_than_baseline": len(result.get("better_than_baseline") or []),
            "total_tested": result.get("total_tested"),
            "sanity_flags": result.get("sanity_flags") or [],
        }
        cat = _cat_of(f)
        per_category.setdefault(cat, []).append(entry)
        if "boundary" in best:
            boundary_votes.append(best["boundary"])
        if "scale" in best:
            scale_values.append(best["scale"])

    _SCALE_ORDER = {"small": 0, "medium": 1, "large": 2}

    def _mode(vals):
        if not vals:
            return None
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def _median_scale(vals):
        if not vals:
            return None
        s = sorted(vals, key=lambda v: _SCALE_ORDER.get(v, 1))
        m = len(s) // 2
        return s[m]

    return {
        "sampled_files": len(sampled),
        "skipped_non_narrative": skipped_non_narrative,
        "mode": "controlled",
        "per_category": {k: v for k, v in per_category.items() if k != "_errors"},
        "errors": per_category.get("_errors", []),
        "global_recommendation": {
            "boundary_mode": _mode(boundary_votes),
            "boundary_votes": {b: boundary_votes.count(b) for b in sorted(set(boundary_votes))},
            "scale_median": _median_scale(scale_values),
            "files_sampled": len(boundary_votes),
        },
        "note": "Advisory only — adopt between runs; never written back to the current run config (cold-path discipline).",
    }


class RunArtifactStore:
    """Run-directory artifact persister (run_dir + config injected)."""

    def __init__(self, run_dir: Path, config: RunConfig):
        """Inject the run directory and run configuration."""
        self.run_dir = run_dir
        self.config = config

    def init_run_dir(self, run_id: str):
        """Initialize the run directory.

        Runtime Artifact Contract (seven dimensions + optional debug):
            - core artifacts (kept long-term): raw, representation, markdown, evaluation, optimization
            - run entry (always kept): manifest.json
            - report entry (kept long-term): reports/index.html
            - debug artifacts (optional; can be disabled in production): debug/ and debug/diagnostics/

        Cleanup policy: old runs are cleaned immediately after the new run directory is
        created (keep_runs entries), so runs/ never holds more than keep_runs directories
        — even if this run crashes midway, no excess directories remain.
        """
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Core artifact directories (seven dimensions; manifest is the root file; debug is optional)
        for subdir in ["raw", "representation", "markdown", "evaluation", "optimization"]:
            (self.run_dir / subdir).mkdir(exist_ok=True)

        # debug-layer artifacts (optional): covering parse → representation → evaluation → global full-chain
        if getattr(self.config, "enable_debug", False):
            (self.run_dir / "debug").mkdir(exist_ok=True)
            (self.run_dir / "debug" / "diagnostics").mkdir(exist_ok=True)

        # Report directory
        reports_dir = self.run_dir / "reports"
        reports_dir.mkdir(exist_ok=True)
        (reports_dir / "visualizations").mkdir(exist_ok=True)

        # Clean old runs (including the new directory; keep the most recent keep_runs).
        # Done here rather than at the end of execute: the directory count converges
        # immediately after creation, so a mid-run crash leaves no excess directories.
        self.cleanup_old_runs(keep=self.config.keep_runs)

        # manifest.json — runtime metadata root
        manifest = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": asdict(self.config),
            "artifact_semantics": {
                "core": ["raw", "representation", "markdown", "evaluation", "optimization"],
                "run_root": ["manifest.json", "summary.json"],
                "report": ["reports/index.html"],
                "debug": ["debug", "debug/diagnostics"] if getattr(self.config, 'enable_debug', False) else [],
                "images": ["images"] if getattr(self.config, "enable_image_asset_export", True) else [],
            },
            "entry_point": str(self.run_dir / "reports" / "index.html"),
        }
        manifest_path = self.run_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)

    def cleanup_old_runs(self, keep: int = 10):
        """Clean old run directories, keeping only the most recent ``keep``."""
        runs_root = self.run_dir.parent
        if not runs_root.exists():
            return
        runs = sorted(
            [
                d
                for d in runs_root.iterdir()
                if d.is_dir() and _is_run_dir_name(d.name)
            ],
            key=lambda d: d.name,
            reverse=True,
        )
        for old_run in runs[keep:]:
            shutil.rmtree(old_run, ignore_errors=True)
        removed = len(runs) - min(len(runs), keep)
        if removed > 0:
            print(f"[ProvendocRun] Cleaned {removed} old run(s), kept latest {keep}")

    def save_summary(self, summary):
        """Save the summary and configuration."""
        # config
        config_path = self.run_dir / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self.config), f, indent=2, ensure_ascii=False)

        # summary — drop report-only heavy fields to keep summary.json light
        heavy_fields = {"raw_text", "markdown_content", "source_text"}
        summary_dict = asdict(summary)
        for file_entry in summary_dict.get("files", []):
            for key in heavy_fields:
                file_entry.pop(key, None)

        summary_path = self.run_dir / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_dict, f, indent=2, ensure_ascii=False, default=str)

    def generate_report(self, summary, elapsed_total: float = 0.0):
        """Generate the unified report HTML + the run-level consolidated report."""
        from provendoc.runtime.report_generator import ReportGenerator

        generator = ReportGenerator(self.run_dir, summary)
        generator.generate()
        # Run-level consolidated report (run_report.md): a full fact snapshot of the run,
        # including archive-expansion facts. Wrapped in its own try — a report failure never blocks the main run flow.
        try:
            from provendoc.runtime.run_report import generate_run_report
            generate_run_report(self.run_dir, summary, elapsed_total)
        except Exception as e:
            print(f"[ProvendocRun] Run report (run_report.md) skipped: {e}")

    def write_run_trace(self, summary, elapsed_total: float,
                        cache_before: Optional[dict], cache_after: Optional[dict]):
        """Write the run-level global debug trace.

        Covers: run overview, aggregate quality evaluation, corpus stratification, stability landscape,
        optimization advice, cache hits — i.e. the global side of the "parsing to global" chain.
        Together with the per-file debug/{base}_debug.json (parse→representation→evaluation)
        it forms the complete debug layer.
        """
        s = summary
        run_trace = {
            "layer": "debug",
            "scope": "run",
            "run": {
                "run_id": s.run_id,
                "timestamp": s.timestamp,
                "wall_time_seconds": round(elapsed_total, 2),
                "total_files": s.total_files,
                "success_files": s.success_files,
                "failed_files": s.failed_files,
                "failed_details": [
                    {"file": f.relative_path, "error": f.error}
                    for f in s.files if not f.success
                ][:50],
            },
            "aggregation": {
                "quality": s.quality_aggregation,
                "confidence": s.confidence_aggregation,
                "quality_filtered": s.quality_filtered,
                "quality_filtered_dimensions": s.quality_filtered_dimensions,
                "corpus_distribution": s.corpus_distribution,
                "corpus_tier_snapshot": s.corpus_tier_snapshot,
            },
            "stability": s.stability_result,
            "optimization": s.optimization_summary,
            "cache": {
                "before": cache_before,
                "after": cache_after,
                "enabled": self.config.enable_parse_cache,
            },
        }
        trace_path = self.run_dir / "debug" / "run_trace.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(run_trace, f, indent=2, ensure_ascii=False, default=str)

    def write_optimization_artifacts(self, summary, elapsed_total: float,
                                      cache_before: Optional[dict],
                                      cache_after: Optional[dict],
                                      profile_aggregator: Optional[ProfileAggregator]):
        """Write the official optimization/ artifacts (seven-dimension contract §2.6).

        Three files, all official publishable run-level data (not gated by the debug switch):
          - optimization/optimization_summary.json — strategy-search conclusion (numeric trials + references;
            full per-trial evaluations are not stored — they belong to the debug layer)
          - optimization/profile_summary.json — RuntimeProfiler aggregate report
          - optimization/run_trace.json — run-level global trace (performance/stability)
        """
        opt_dir = self.run_dir / "optimization"
        opt_dir.mkdir(parents=True, exist_ok=True)

        # 1. optimization summary
        opt_data = {
            "layer": "optimization",
            "run_id": summary.run_id,
            "optimization_summary": summary.optimization_summary,
        }
        opt_path = opt_dir / "optimization_summary.json"
        with open(opt_path, "w", encoding="utf-8") as f:
            json.dump(opt_data, f, indent=2, ensure_ascii=False, default=str)

        # 2. profile summary
        if profile_aggregator is not None:
            profile_summary = profile_aggregator.summary()
            profile_path = opt_dir / "profile_summary.json"
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile_summary, f, indent=2, ensure_ascii=False)

        # 3. run trace (copy of debug run_trace, but in optimization/)
        s = summary
        run_trace = {
            "layer": "optimization",
            "scope": "run",
            "run": {
                "run_id": s.run_id,
                "timestamp": s.timestamp,
                "wall_time_seconds": round(elapsed_total, 2),
                "total_files": s.total_files,
                "success_files": s.success_files,
                "failed_files": s.failed_files,
                "failed_details": [
                    {"file": f.relative_path, "error": f.error}
                    for f in s.files if not f.success
                ][:50],
            },
            "aggregation": {
                "quality": s.quality_aggregation,
                "confidence": s.confidence_aggregation,
                "quality_filtered": s.quality_filtered,
                "quality_filtered_dimensions": s.quality_filtered_dimensions,
                "corpus_distribution": s.corpus_distribution,
                "corpus_tier_snapshot": s.corpus_tier_snapshot,
            },
            "stability": s.stability_result,
            "optimization": s.optimization_summary,
            "cache": {
                "before": cache_before,
                "after": cache_after,
                "enabled": self.config.enable_parse_cache,
            },
        }
        trace_path = opt_dir / "run_trace.json"
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(run_trace, f, indent=2, ensure_ascii=False, default=str)

    def export_project_index(self, summary, dataset_dir: Path) -> None:
        """Code in-domain deepening cold path: the cross-file project-index artifact (opt-in).

        After all files are processed, builds a read-only project graph over dataset_dir
        (P2 file import edges + P5 verified cross-file symbol edges + unresolved records with reasons);
        the artifact lands in run_dir/project_index.json (lists capped by module-export limits).

        Discipline and boundaries:
          - never writes back to or annotates persisted chunks (the SDK's
            chunk(project_root=...) handles chunk-level evidence injection);
          - participates in no ingestion decisions; a pure retrieval/reporting asset;
          - uses the full index() (no incremental cache) — run_dir gets cleaned per
            keep_runs and writing to the corpus directory would pollute the user's dataset;
            both cache options lose more than they gain; the cost of repeated runs is borne explicitly by the user via the opt-in switch.
          - records an honest skip_reason when there are no code-domain files; never emits an empty index.
        """
        code_files = [
            f for f in summary.files
            if f.success and f.domain == "code"
        ]
        if not code_files:
            summary.project_index_summary = {
                "enabled": True,
                "skip_reason": "no_code_files",
            }
            print("[ProvendocRun] Project index: skipped (no code files)")
            return

        from provendoc.core.parsers.code.project_indexer import ProjectIndexer

        index = ProjectIndexer().index(dataset_dir)
        metadata = index.to_metadata()
        artifact_path = self.run_dir / "project_index.json"
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)

        summary.project_index_summary = {
            "enabled": True,
            "code_files": len(metadata.get("files", [])),
            "file_edges": len(metadata.get("file_edges", [])),
            "symbol_edges": len(metadata.get("symbol_edges", [])),
            "unresolved": len(metadata.get("unresolved", [])),
            "unresolved_symbols": len(metadata.get("unresolved_symbols", [])),
            "index_stats": metadata.get("stats") or {},
            "artifact_path": str(artifact_path),
        }
        print(
            "[ProvendocRun] Project index: "
            f"{len(metadata.get('files', []))} code files, "
            f"{len(metadata.get('file_edges', []))} file edges, "
            f"{len(metadata.get('symbol_edges', []))} symbol edges "
            f"→ {artifact_path.name}"
        )
