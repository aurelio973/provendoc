"""Runtime-layer data models.

FileResult (single-file processing facts) and RunSummary (run-level aggregate)
are the runtime layer's data carriers, consumed by the provendoc_run orchestration
layer, the run_artifacts persistence layer, the report layer, and tests.
_is_run_dir_name decides run-directory lifecycle (distinguishing run directories from analysis-artifact directories when cleaning old runs).

Split out of provendoc_run.py (structural convergence): data facts and pure
predicates only, no behavior.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from provendoc.runtime.config import RunConfig


@dataclass
class _SweepDoc:
    """Lightweight document proxy for the stability sweep."""
    text: str
    metadata: dict = field(default_factory=lambda: {"headings": []})


@dataclass
class FileResult:
    """Processing result for a single file."""
    file_path: str
    relative_path: str
    success: bool
    error: Optional[str] = None
    chunk_count: int = 0
    quality_overall: Optional[float] = None
    quality_confidence: Optional[float] = None
    # Quality-diagnostic issues (issue-first: carries suggested_actions for the observatory)
    quality_issues: Optional[List[Dict[str, Any]]] = None
    failure_modes: List[str] = field(default_factory=list)
    domain: str = "unknown"
    ingestion_mode: str = "unknown"
    processing_time_ms: int = 0

    # Runtime Scale Policy: benchmark tier label and scale metrics
    file_size_bytes: int = 0
    benchmark_tags: List[str] = field(default_factory=lambda: ["normal"])

    # Data for report display (never serialized into summary.json; runtime-memory use only)
    raw_text: Optional[str] = None              # parse snapshot (original_text: sanitized, pre-cleaning)
    source_text: Optional[str] = None           # file source text (decoded original, the editor view)
    # True total length recorded before parallel workers drop overly long source_text.
    # When the report cannot re-read the full original from the raw artifact (the huge
    # fast path persists nothing), this keeps source_truncated / source_char_count truthful.
    source_char_count: Optional[int] = None
    markdown_content: Optional[str] = None       # markdown output content
    raw_json_path: Optional[str] = None           # raw document path
    markdown_path: Optional[str] = None           # full markdown path
    representation_json_path: Optional[str] = None  # representation-layer path (parse + clean + multi-granularity)
    evaluation_json_path: Optional[str] = None           # evaluation result path (incl. the failure_modes top-level dimension)
    debug_json_path: Optional[str] = None        # debug-layer full intermediate-state path
    optimization_result: Optional[Dict] = None   # optimization result (per-file level)
    doc_quality_result: Optional[Dict] = None    # document-quality diagnostics

    # Quality dimension-score details (for the radar chart)
    quality_dimensions: Optional[Dict[str, float]] = None

    # Runtime Scale Policy: sampling and density-downgrade markers
    sampling_meta: Optional[Dict[str, Any]] = None
    density_adapted: bool = False

    # Corpus Stratification: corpus-tier classification
    corpus_tier: str = "normal_narrative"

    # RuntimeProfiler: per-file profile data
    profile: Optional[Dict[str, Any]] = None

    # PDF productionization: per-file runtime/reliability facts (for run-level aggregation)
    pdf_runtime_summary: Optional[Dict[str, Any]] = None


@dataclass
class RunSummary:
    """Run summary."""
    run_id: str
    timestamp: str
    config: RunConfig
    total_files: int = 0
    success_files: int = 0
    failed_files: int = 0
    avg_quality_overall: float = 0.0
    avg_quality_confidence: float = 0.0
    files: List[FileResult] = field(default_factory=list)

    # Runtime Scale Policy: weighted aggregation results (guards against benchmark skew)
    quality_aggregation: Dict[str, Any] = field(default_factory=dict)
    confidence_aggregation: Dict[str, Any] = field(default_factory=dict)

    # Corpus Stratification: corpus tiers + Overall/Filtered dual-track quality
    corpus_distribution: Dict[str, Any] = field(default_factory=dict)
    quality_filtered: Dict[str, Any] = field(default_factory=dict)  # quality over meaningful documents only
    quality_filtered_dimensions: Dict[str, float] = field(default_factory=dict)

    # Corpus Drift Snapshot: between-run comparable tier-composition snapshot
    # Shape: {run_id, timestamp, total, tiers: {tier: {count, pct}}, meaningful_count}
    corpus_tier_snapshot: Dict[str, Any] = field(default_factory=dict)

    # Semantic Stability Observer: stability landscape
    stability_result: Dict[str, Any] = field(default_factory=dict)

    # Strategy Optimizer: parameter-search advice (in-run cold path; advisory only, never written back)
    optimization_summary: Dict[str, Any] = field(default_factory=dict)

    # PDF productionization: run-level runtime shares / OCR page rates / layout success rates /
    # reliability three-state distribution / stage latency P50/P95 (pure observation; never enters any quality score)
    pdf_runtime_summary: Dict[str, Any] = field(default_factory=dict)

    # Archive expansion: collection-time archive facts (member/extract/skip counts
    # per archive; nothing silently dropped, all go into run-level artifacts)
    archive_expansion: Dict[str, Any] = field(default_factory=dict)

    # Code in-domain deepening: cross-file project-index artifact facts (opt-in cold path).
    # Shape: {enabled, skip_reason?, code_files, file_edges, symbol_edges,
    # unresolved, unresolved_symbols, resolution_rate?, artifact_path?}
    # A pure retrieval/reporting asset; participates in no ingestion decisions.
    project_index_summary: Dict[str, Any] = field(default_factory=dict)


def _is_run_dir_name(name: str) -> bool:
    """Whether a directory name is a ProvendocRun run directory (run_id naming).

    run_id = "%Y-%m-%d_%H-%M-%S" + "_" + config fingerprint; the timestamp prefix is 19
    characters. Analysis-artifact directories (regression reports, verification reports)
    may coexist under runs/; when cleaning old runs they must not count toward the
    "most recent N" window — non-run directories would be deleted by mistake and new run directories pushed out of the retention window.
    """
    if len(name) < 21 or name[19] != "_":
        return False
    try:
        datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return False
    return True
