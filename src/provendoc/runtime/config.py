"""Run configuration layer.

RunConfig holds every tunable knob of a run (seven-dimension artifact switches / image
assets / archive expansion / huge fast path / PDF guards / parse cache / parallelism /
async export / run-directory retention / in-domain deepening), plus the path-anchoring
rule anchor_cwd_relative (a consistent anchor for CWD-relative artifacts like runs/ and .cache/).

Split out of provendoc_run.py (structural convergence): configuration facts only, no behavior.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional


def anchor_cwd_relative(rel):
    """Anchor relative-path artifacts: re-anchor to the repo root when CWD is inside the package source tree.

    - absolute paths (explicitly configured by the user): returned unchanged;
    - normal invocation (project root / any directory): CWD-relative, legacy behavior;
    - CWD inside the provendoc package directory (e.g. an IDE session launched
      from src/provendoc): writing there would pollute the source tree and hide
      artifacts from the user (runs/ and .cache/ scattered) → anchor to the dev repo root;
    - an installed site-packages copy: no pyproject.toml above the package,
      so the redirect condition fails and CWD semantics hold (never write into site-packages).
    """
    p = Path(rel)
    if p.is_absolute():
        return p
    cwd = Path.cwd().resolve()
    pkg_root = Path(__file__).resolve().parent.parent  # .../provendoc
    if cwd == pkg_root or pkg_root in cwd.parents:
        repo_root = pkg_root.parent.parent  # src/.. → repo root
        if (repo_root / "pyproject.toml").exists():
            return repo_root / p
    return cwd / p


def _discover_project_tessdata() -> Optional[str]:
    """Locate a project-local ``.tessdata`` directory, else None.

    The system Tesseract install (``C:\\Program Files\\Tesseract-OCR\\
    tessdata``) ships ``eng`` only, so Chinese scans never resolved a model
    there.  The repository carries ``.tessdata`` with ``chi_sim``, ``eng``
    and ``osd``; pointing the default at it is what makes CJK OCR actually
    work without every caller editing a config file.

    Returns the path only when a Chinese model is present — a ``.tessdata``
    directory without ``chi_sim`` adds nothing over the system default and
    would only mask the real resolution path.
    """
    try:
        candidate = anchor_cwd_relative(".tessdata")
        if (candidate / "chi_sim.traineddata").is_file():
            return str(candidate)
    except OSError:
        return None
    return None


def _default_ocr_language() -> str:
    """Default Tesseract language string, widened when CJK models exist.

    ``chi_sim`` is preferred over ``chi_sim+eng``: measured on the
    ``annotated_forms_zh`` scans, adding ``eng`` *lowered* ideograph yield
    (190→173, 934→870, 136→121 on the first three cases) because the model
    splits confidence between scripts, and it also emitted junk such as
    ``#*#*~*;™!O`` after an email address.  It additionally makes Tesseract
    try to load ``chi_sim_vert``, which is not shipped and logs an error on
    every call.

    Built from installed models rather than guessed, so a machine without
    the Chinese pack keeps the historical ``eng`` behaviour.
    """
    tessdata = _discover_project_tessdata()
    if tessdata and (Path(tessdata) / "chi_sim.traineddata").is_file():
        return "chi_sim"
    return "eng"


@dataclass
class RunConfig:
    """Run configuration."""
    policy_name: str = "semantic_baseline"
    # 2026-10-01 defaults 300/500 → 384/640: a hard cap of 500 bisects ~600-char NQ
    # paragraphs mid-sentence (miss dissection in the round-2 diagnostics); on the frozen
    # competitor corpus the default pipeline scores recall@1 0.8006 vs cs640 0.8131
    # (= the frozen baseline). 640 aligns with ParameterSpace's legacy chunk_size=640
    # (the effective ceiling, get_effective_chunk_size()=640); soft=384 comes from the
    # RuntimePackagingPolicy.from_chunk_size(640) mapping (0.6×hard);
    # note the evaluate(chunk_size=640) path derives soft=640 via from_legacy.
    # The round-2 preregistered grid (cs500/640/800) tied across the board; picking 640 among ties is a product decision.
    packaging_size_soft_limit: int = 384
    packaging_size_hard_limit: int = 640
    context_propagation_mode: str = "selective"
    # RepresentationPolicy extension knobs
    boundary_preference: str = "discourse_unit"       # discourse_unit | heading_boundary | structural_block | token_limit
    discourse_unit_target: int = 5                      # target semantic units (1-15)
    integrity_mode: str = "all"                         # all | code_table | code_only | none
    run_optimization: bool = True
    dataset_dir: str = "./tests/datasets/txt"
    description: str = ""

    # Runtime mode — preset config groups for quick mode switching
    # "analysis" (default): full quality evaluation + markdown/json export + reports
    # "benchmark": minimal export, no debug, no reports, fast data runs
    # "debug": everything on, including detailed traces/granularity
    runtime_mode: str = "analysis"

    # Runtime Artifact Semantics switches
    enable_debug: bool = True        # debug/ artifacts (on during development; set False for releases)

    # Export control — per-artifact switches (less unnecessary I/O)
    # seven-dimension artifact contract: raw / representation / markdown / evaluation / optimization + optional debug
    enable_raw_export: bool = True             # raw/{base}_raw.json (original_text + parser metadata + DQR)
    enable_markdown_export: bool = True        # markdown/{base}.md (presentation rendering of base units)
    enable_representation_export: bool = True  # representation/{base}_repr.json (cleaned full text + three-granularity views)
    enable_evaluation_export: bool = True             # evaluation/{base}_evaluation.json (unified evaluation incl. the failure_modes top-level dimension)
    enable_optimization_export: bool = True    # optimization/ (numeric trials + references) + profile_summary.json
    # Legacy config names kept as placeholders so old TOML files don't error
    enable_chunked_export: bool = True         # deprecated → chunks folded into representation

    # Image asset export — auto-triggered (on by default)
    # Only acts when images are detected: a natural no-op for image-free documents; docx/pdf images
    # are written as original bytes to images/{base}/original/, plus a manifest.json
    # (asset_id ← first 12 hex chars of sha256, carrying page number/context/caption anchoring,
    # pixel size and effective_dpi facts); when OpenCV is available, derived/ is also written
    # (deskew/autocrop/thumbnail pipeline with a parameter fingerprint). The body gains
    # [Image: ...] placeholder blocks. False = explicitly off.
    enable_image_asset_export: bool = True

    # Archive expansion — expanding zip/tar/gz-family archives at collection time
    # Expansion ≠ parsing: archives are not a parse domain; member files flow into their own
    # domain's parser as usual; virtual paths like "bundle.zip/docs/chapter.md" preserve origin.
    # Defensive caps: nesting depth / members per archive / per-member extraction / total extraction.
    enable_archive_expansion: bool = True
    archive_max_depth: int = 3
    archive_max_members: int = 1000
    archive_max_member_mb: int = 200
    archive_max_total_gb: int = 4

    # Huge file fast path — large files skip heavy exports
    # Layering principle: the content layer (representation) and chunk-quality evaluation (evaluation/failure_modes)
    # are not downgraded by default — large files are exactly where chunking degrades most; the
    # bigger the file, the more it needs evaluation. evaluation/failure cost is already bounded
    # to constant scale by DensityAdaptation (≥5000 lines auto-samples ~20 units), so no
    # whole-file skip is needed. Downgrades apply only to the presentation (markdown) and debug layers (debug), plus full-text doc_quality.
    huge_file_threshold: int = 524_288    # 512 KB — medium files like Gutenberg books also take the fast path
    huge_file_skip_markdown: bool = True   # no full markdown export for huge files (presentation layer, degradable)
    huge_file_skip_debug: bool = True      # no debug JSON export for huge files (debug layer, degradable)
    huge_file_skip_representation: bool = False  # still export representation for huge files (content layer, kept by default)
    huge_file_skip_evaluation: bool = False        # still evaluate quality for huge files (chunk quality is core; not skipped by default)
    huge_file_skip_failure_modes: bool = False  # still detect failure modes for huge files (same sampling basis as quality evaluation)
    huge_file_skip_doc_quality: bool = True     # skip doc-quality diagnostics for huge files (O(full text), diagnostic info only)

    # PDF Runtime Limits — resource guards (guards, not filters):
    # exceeding a cap never drops a document; the reason is recorded honestly and the cheap native path runs.
    # Nothing is limited by default (valid large files like books are processed in full);
    # configure in TOML/CLI when a per-run resource bound is needed. None = no cap.
    pdf_max_pages: Optional[int] = None              # page-count cap
    pdf_max_file_bytes: Optional[int] = None         # file-size cap
    pdf_max_page_pixels: Optional[int] = None       # per-page rendered-pixel cap (oversized pages auto-downscale DPI)
    pdf_max_image_asset_bytes: Optional[int] = 64_000_000  # total byte budget for a document's image-asset payload (default 64MB, G11). metadata["image_asset_payloads"] carries each image's original bytes (can reach hundreds of MB — the main source of cross-file RSS bloat); past the budget, remaining figure layouts register not_extracted_reason="budget_exhausted" while already-extracted ones are unaffected; the truncation fact is recorded in pdf_image_assets.counts and payloads_truncated. None = no limit.
    pdf_ocr_time_budget_seconds: Optional[float] = None   # per-document OCR time budget; past it, remaining pages are marked budget_exhausted
    pdf_mineru_timeout_seconds: Optional[float] = None    # per MinerU layout-run cap (library default 300s)
    pdf_ocr_parallel_workers: int = 4                     # page-level process parallelism for scanned-doc OCR; 1 = serial (benchmarks: ~2.4x with 4 processes)
    pdf_ocr_language: str = field(default_factory=_default_ocr_language)  # Tesseract language string, "+" combines languages e.g. "chi_sim+eng"; only installed models are actually passed to Tesseract (see resolve_ocr_language)
    pdf_ocr_tessdata: Optional[str] = field(               # explicit tessdata directory; by default the project's .tessdata is auto-discovered (Chinese scans need chi_sim present); when absent, None = system-default resolution
        default_factory=_discover_project_tessdata
    )

    # Table Runtime Limits — resource guards (guards, not filters, prereg
    # oversize_structure_limits_prereg_v1): wired only for openpyxl-engine
    # .xlsx/.xlsm and .csv/.tsv/.psv. Exceeding a cap never drops a document:
    # skeleton/truncation facts go into metadata["resource_guard"], ingestion_status is PARTIAL.
    # Nothing is limited by default (None = no cap, historical behavior).
    table_max_sheets: Optional[int] = None             # sheet-count cap (over-cap sheets register only a skeleton; sheet_count stays truthful)
    table_max_rows_per_sheet: Optional[int] = None     # per-sheet row cap (over-cap rows not extracted)
    table_max_columns_per_sheet: Optional[int] = None  # per-sheet column cap (over-cap columns not extracted)
    table_max_cells_per_sheet: Optional[int] = None    # per-sheet total-cell cap (if the declared shape product exceeds it, the sheet is skipped entirely)

    # Docx Runtime Limits — cap on direct body children, wired only for .docx;
    # over-cap truncates body traversal and records into resource_guard. None = no cap.
    docx_max_body_elements: Optional[int] = None

    # Pptx Runtime Limits — resource guards (guards, not filters, prereg
    # pptx_limits_prereg_v1 / pptx_evidence_limits_prereg_v1, batch 9):
    # wired only for .pptx. Over-cap slides skip XML parsing/notes capture and are
    # registered per slide; once a slide's shape-level objects hit the cap, one entry
    # covers the whole slide; package-level evidence channels (charts/smartart/formulas)
    # share an action budget — hitting it early-stops that channel; facts go into
    # metadata["resource_guard"] and the existing pptx_unresolved keys; ingestion_status is PARTIAL.
    # Nothing is limited by default (None = no cap, zero-diff from historical behavior).
    pptx_max_slides: Optional[int] = None             # declared slide-count cap (over-cap slides not extracted; slide_count still reports the declared total; evidence channels reuse the declared order)
    pptx_max_shapes_per_slide: Optional[int] = None  # per-slide shape-object cap (objects after the cap are not processed)
    pptx_max_evidence_parts: Optional[int] = None    # shared action budget for package-level evidence extraction (chart part reads / smartart part reads / oMath conversion; hitting it early-stops)

    # Parse cache — cache parse results so repeated benchmark runs don't re-parse
    enable_parse_cache: bool = True
    parse_cache_dir: str = ".cache/parse"
    cache_ttl_days: int = 7          # cache entries older than N days are auto-cleaned
    cache_max_size_mb: int = 2048    # max cache-directory size (2GB)

    # Parallel processing — process files in parallel
    enable_parallel: bool = True
    parallel_workers: int = 0       # 0 = auto (os.cpu_count())

    # Async export — background thread pool writes files, overlapping I/O with CPU
    enable_async_export: bool = True
    async_export_workers: int = 2     # I/O threads (2 is plenty; the disk is the bottleneck)

    # run-directory retention: keep only the most recent N runs (older ones auto-cleaned)
    keep_runs: int = 3
    enable_report: bool = True       # reports/index.html report entry
    enable_profiling: bool = True     # RuntimeProfiler per-stage timing + complexity metrics
    progress_interval: int = 50      # print progress every N files

    # Record-stream in-domain deepening — ".log" incident context window
    # The default session keeps zero drift; after switching to "incident": events within the
    # window around an error/traceback anchor merge into one incident unit (the window is
    # defined in event counts; timestamps are metadata facts only), and background events
    # still cluster by session. Window parameters take effect only under the incident strategy.
    log_grouping_strategy: str = "session"
    log_incident_context_before: int = 5
    log_incident_context_after: int = 3

    # Code in-domain deepening — cross-file project-index artifact (opt-in, off by default)
    # When on and the run contains code-domain files, a cold path at the end of execute()
    # builds the project graph with ProjectIndexer and writes run_dir/project_index.json
    # (file edges + verified symbol edges + unresolved, lists are capped). It never writes
    # back to or annotates persisted chunks and participates in no ingestion decisions (a pure
    # retrieval/reporting asset). Indexing re-parses code files with tree-sitter and uses the
    # full index() without incremental caching (run_dir is cleaned per keep_runs, and writing
    # to the corpus directory would pollute the user's dataset); the cost of repeated runs is borne explicitly by the user via the opt-in switch.
    enable_project_index_export: bool = False

    def fingerprint(self) -> str:
        """Generate the config fingerprint."""
        data = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        """Create RunConfig from a dict, ignoring unknown keys.

        Mode presets provide defaults for omitted options.  Values explicitly
        supplied by a config file take precedence over those presets.
        """
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        config = cls(**filtered)
        for name, value in filtered.items():
            setattr(config, name, value)
        return config

    @classmethod
    def from_toml(cls, path: str) -> "RunConfig":
        """Load RunConfig from a TOML file.

        The file may contain a [provendoc] section (preferred) or
        top-level keys.  Unknown keys are silently ignored — including
        sections named after pre-release project names ([anchordoc] /
        [chainmark] / [omni]): the project was renamed before its first
        public release, so no compatibility alias is carried (naming
        history is recorded in CHANGELOG.md).

        Example TOML::

            [provendoc]
            dataset_dir = "./data"
            runtime_mode = "benchmark"
            parallel_workers = 4
            huge_file_threshold = 1048576
            pdf_ocr_time_budget_seconds = 600.0
            pdf_max_pages = 500
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                raise ImportError(
                    "TOML config requires Python 3.11+ (tomllib) or 'pip install tomli'"
                )
        with open(p, "rb") as f:
            data = tomllib.load(f)
        # Section rule: [provendoc] if present, else flat top-level keys.
        section = data["provendoc"] if "provendoc" in data else data
        return cls.from_dict(section)

    def __post_init__(self):
        """Apply the runtime-mode preset."""
        if self.runtime_mode == "benchmark":
            self.enable_parallel = False
            self.parallel_workers = 1
            self.enable_debug = False
            self.enable_raw_export = False
            self.enable_markdown_export = False
            self.enable_evaluation_export = False
            self.enable_report = False
            self.huge_file_threshold = 524_288
        elif self.runtime_mode == "development":
            self.enable_parallel = False
            self.parallel_workers = 1
            self.enable_debug = True
            self.enable_raw_export = True
            self.enable_markdown_export = True
            self.enable_representation_export = True
            self.enable_evaluation_export = True
            self.enable_optimization_export = True
            self.enable_report = True
        elif self.runtime_mode == "batch":
            self.enable_parallel = True
            self.enable_debug = False
            self.enable_raw_export = True
            self.enable_markdown_export = True
            self.enable_representation_export = True
            self.enable_evaluation_export = True
            self.enable_optimization_export = True
            self.enable_report = False
        elif self.runtime_mode == "debug":
            self.enable_debug = True
            self.enable_raw_export = True
            self.enable_markdown_export = True
            self.enable_representation_export = True
            self.enable_evaluation_export = True
            self.enable_optimization_export = True
            self.enable_report = True
            # debug mode skips no files: disable every huge-file skip flag
            # (threshold must not be 0 — every file would satisfy size>=0 and count as huge)
            self.huge_file_skip_evaluation = False
            self.huge_file_skip_failure_modes = False
            self.huge_file_skip_doc_quality = False
            self.huge_file_skip_markdown = False
            self.huge_file_skip_debug = False
        elif self.runtime_mode == "analysis":
            # analysis is the default mode; the defaults already cover it
            pass
        else:
            raise ValueError(
                f"Unknown runtime_mode: {self.runtime_mode!r}. "
                f"Must be 'benchmark', 'development', 'batch', 'analysis', or 'debug'."
            )
