
"""Ingestion Domain Taxonomy — single source of truth.

Five domains, each with its own:

    - representation contract (how sources become semantic units)
    - evaluation criteria (RSE quality profile per domain)
    - optimization policy (only where parameter tuning applies)
    - diagnostics layer

The registry is the ONE place that defines which suffix
belongs to which domain, what pipeline stages apply, and
what warnings to emit when a domain is only partially
supported.


Domain Overview
===============

1. narrative
   ---------------
   Continuous natural language with hierarchy, context
   flow, and semantic boundaries.

   Pipeline:  parse → representation → NarrativeQuality → optimization
   Quality:    yes (NarrativeQuality, invariant_v1)
   Formats:   txt, md, markdown, pdf, docx, tex, pptx, epub, rst

   "Good chunk" = semantic continuity

2. structured_config
   ---------------
   Key-value hierarchy with tree/path semantics.

   Pipeline:  parse → representation (semantic flattening)
   Quality:    yes (ConfigQuality, profile config_quality_v1.3)
   Formats:   json, jsonc, yaml, yml, toml, ini, xml,
              properties, env, plist,
              cfg + INI-dialect dotfiles (gitconfig, gitmodules,
              editorconfig, npmrc, coveragerc, pylintrc)
   Future:    —

   "Good chunk" = semantic path integrity

3. spreadsheet
   ---------------
   Two-dimensional canvases with sheets, regions, tables, cells and relations.

   Pipeline:  parse → representation (row / region / sheet views)
   Quality:    yes (TableQuality v1.1 — wired into the SDK evaluate() path)
   Formats:   csv, tsv, psv, xlsx, xls, xlsm, xlsb, ods,
              parquet (pyarrow extra)

   "Good unit" = source-traceable, boundary-preserving spreadsheet context

4. code
   ---------------
   AST-based, symbol graph, executable semantics.

   Pipeline:  parse → representation (maximal non-overlapping AST /
              component boundaries plus module fragments)
   Quality:    yes (CodeQuality — structural quality index)
   Formats:   py, js, ts, jsx, tsx, vue, css, scss, java,
              go, php, c, cpp, cs, and more

   "Good chunk" = executable semantic boundaries

5. record_stream
   ---------------
   Line-oriented, append-only, event semantics.

   Pipeline:  parse → representation (event grouping)
   Quality:    yes (EventQuality v1.0 — operational locality index)
   Formats:   log, jsonl, ndjson

   "Good chunk" = incident locality


Evaluation Architecture
=========================

    Evaluation Framework (RSE = Representation Stack Evaluation)
        ├── Narrative Quality (NarrativeQuality, invariant_v1)  ✓ implemented
        ├── Config Quality Index (ConfigQuality)        ✓ implemented
        ├── Table Quality Index (TableQuality v1.1)    ✓ implemented, in SDK path
        ├── Code Quality Index (CodeQuality)           ✓ implemented
        ├── Event Quality Index (EventQuality v1.0)    ✓ implemented, in SDK path

Shared evaluation primitives (all domains):
    - structural integrity
    - fragmentation
    - recoverability
    - retrieval readiness

Domain-specific metrics layered on top.

Directory Layout (canonical mapping)
====================================

The registry name is the canonical authority; directory names are
layout conventions inherited from earlier naming (e.g. the "tabular"
era). When the two ever disagree, the registry wins.

    registry name          code directory            parsers / representations
    ---------------------  ----------------------  ----------------------------
    narrative              narrative/              txt, md, pdf, docx, tex,
                                                    pptx, epub, rst
    structured_config      config/                 json, yaml, toml, ini, xml,
                                                    properties, env, plist,
                                                    cfg + INI-dialect dotfiles
    spreadsheet            table/                  csv, tsv, psv, xlsx, xls,
                                                    xlsm, xlsb, ods, parquet
    code                   code/                   py, js, ts, vue, html, ...
    record_stream          event/                 log, jsonl, ndjson

Every domain module documents its registry name in its docstring
(``Domain: <registry name>``). tests/test_domain_taxonomy.py
machine-checks this mapping, so the taxonomy cannot drift silently.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ==================================
# domain constants
# ==================================

DOMAIN_NARRATIVE = "narrative"
DOMAIN_STRUCTURED_CONFIG = "structured_config"
DOMAIN_SPREADSHEET = "spreadsheet"
DOMAIN_CODE = "code"
DOMAIN_RECORD_STREAM = "record_stream"


# ==================================
# domain definition
# ==================================

@dataclass(frozen=True)
class DomainInfo:
    """Immutable definition of an ingestion domain.

    Attributes:
        name:           domain identifier
        suffixes:       file extensions belonging to this domain
        quality_applicable: whether the narrative quality track
                        applies (narrative domain only; ConfigQuality
                        and CodeQuality run via their own evaluator
                        paths in the SDK, not via this flag)
        pipeline_stages: ordered list of pipeline stages
                         that run for this domain
        representation:  representation contract description
        evaluation:     evaluation criteria description (RSE)
        optimization:   optimization policy description
        status:          "active" or "planned" (all registered domains
                         are currently active; the field is kept so a
                         future domain can be staged before activation)
        investment:      investment direction per domain_investment
                         policy v1 — "core" (active scenario-era
                         development) or "frozen" (maintenance mode:
                         existing parsers/RSE keep running, no new
                         suffixes, no new features; frozen domains
                         are NOT removed and their runtime routing is
                         unaffected). Distinct from ``status``, which
                         governs runtime suffix selection
                         (``get_all_supported_suffixes(active_only=``
                         ``True)``) and must stay "active".
        warnings:        domain-specific warning messages
                        emitted when the domain is only
                        partially supported
    """

    name: str
    suffixes: Set[str]
    quality_applicable: bool
    pipeline_stages: List[str]
    representation: str
    evaluation: str
    optimization: str
    status: str
    investment: str
    warnings: List[str] = field(default_factory=list)


# ==================================
# registry — the single source of truth
# ==================================

_DOMAINS: Dict[str, DomainInfo] = {

    # ==================================
    # 1. narrative
    # ==================================

    DOMAIN_NARRATIVE: DomainInfo(
        name=DOMAIN_NARRATIVE,
        suffixes={
            ".txt", ".md", ".markdown",
            ".mdown", ".mkdn", ".mdwn", ".mdx", ".rmd", ".qmd",
            ".pdf", ".docx",
            ".doc", ".docm", ".dot", ".dotx", ".dotm",
            ".tex", ".pptx",
            # PowerPoint-family extensions
            ".pptm", ".ppsx", ".ppsm", ".potx", ".potm",
            ".ppt", ".pps", ".pot",
            ".epub", ".rst", ".rtf",
            # .rtx: disguised suffix — routed by content sniffing
            ".rtx",
            # .rtfd: directory bundle — main doc TXT.rtf via the RTF channel
            ".rtfd",
            # ODP family
            ".odp",
            # ODP family: .fodp flat file + .otp/.sti templates
            ".fodp", ".otp", ".sti",
            # Image inputs: one parser for all eight suffixes;
            # magic-byte check + OCR routing (no domain_signals)
            ".png", ".jpg", ".jpeg", ".gif", ".bmp",
            ".tif", ".tiff", ".webp",
        },
        quality_applicable=True,
        pipeline_stages=[
            "parse",
            "representation",
            "evaluation",
            "optimization",
        ],
        representation=(
            "context segmentation — split continuous "
            "text into semantically coherent chunks"
        ),
        evaluation=(
            "NarrativeQuality — four semantic invariants measuring "
            "narrative chunk quality (structure, discourse, "
            "granularity, boundary)"
        ),
        optimization=(
            "strategy optimization — search chunk_size / "
            "overlap / orphan_threshold for best quality score"
        ),
        status="active",
        investment="core",
        warnings=[],
    ),

    # ==================================
    # 2. structured_config
    # ==================================

    DOMAIN_STRUCTURED_CONFIG: DomainInfo(
        name=DOMAIN_STRUCTURED_CONFIG,
        suffixes={
            ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml",
            ".ini", ".xml",
            ".properties", ".env", ".plist",
            # direnv .envrc (identify-only parser)
            ".envrc",
            # INI dialects: .cfg + dotfiles whose canonical name is
            # the bare filename (see parser_registry.DOTFILE_CANONICAL_NAMES)
            ".cfg", ".gitconfig", ".gitmodules",
            ".editorconfig", ".npmrc", ".coveragerc", ".pylintrc",
            # Cargo.lock (canonical name routing, TOML family)
            "cargo.lock",
            # Terraform HCL native parser; .hcl generic syntax stays
            # in the code domain
            ".tf", ".tfvars",
            # HCL JSON variants + tftest compound suffixes
            ".tf.json", ".tfvars.json",
            ".tftest.hcl", ".tftest.json",
            # .conf: content-gated ConfParser (generic syntax +
            # zero-IO includes)
            ".conf",
            # eslintrc/babelrc dotfile family (bare name + three
            # extension variants)
            ".eslintrc", ".babelrc",
            ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml",
            ".babelrc.json", ".babelrc.yml", ".babelrc.yaml",
            # .cnf: content-gated CnfParser, five-profile routing +
            # zero-IO includes
            ".cnf",
            # systemd units: .service/.timer (INI-like lexer +
            # systemd semantics; zero execution, zero IO)
            ".service", ".timer",
            # .config: content-gated DotConfigParser (delegates to
            # the matching core parser; falls back to ConfParser
            # generic when uncertain)
            ".config",
        },
        quality_applicable=False,
        pipeline_stages=[
            "parse",
            "representation",
        ],
        representation=(
            "semantic flattening — extract key-value "
            "pairs as path-addressable chunks "
            "(semantic_path is the core value)"
        ),
        evaluation=(
            "ConfigQuality (config_quality_v1.3) — 3 scoring "
            "dimensions: hierarchy continuity, sibling "
            "locality, value fidelity (dependency/source-anomaly "
            "demoted to observations in v1.2; path integrity "
            "demoted — structural constant — in v1.3)"
        ),
        optimization=(
            "not applicable — chunk_size / overlap / "
            "orphan_threshold do not affect semantic "
            "flattening"
        ),
        status="active",
        investment="frozen",
        warnings=[],
    ),

    # ==================================
    # 3. spreadsheet
    # ==================================

    DOMAIN_SPREADSHEET: DomainInfo(
        name=DOMAIN_SPREADSHEET,
        suffixes={
            ".csv", ".tsv", ".psv",
            ".xlsx", ".xls", ".xlsm", ".xlsb", ".ods",
            ".parquet",
        },
        quality_applicable=False,
        pipeline_stages=[
            "parse",
            "representation",
        ],
        representation=(
            "canvas-aware structural rendering — preserve cells, rows, "
            "regions, tables, sheets, charts (first-class base units with "
            "series/axis/legend facts), formulas and source relations "
            "(spreadsheet_canvas_v2_prereg_v1)"
        ),
        evaluation=(
            "TableQuality (table_quality_v2.0) — evaluator + "
            "invariants implemented and wired "
            "into the SDK evaluate() path"
        ),
        optimization=(
            "not applicable — Spreadsheet representation is driven by "
            "canvas structure rather than chunk_size / overlap tuning"
        ),
        status="active",
        investment="core",
        warnings=[
            "Spreadsheet Domain — representation preserves canvas, region, "
            "row and sheet boundaries; charts are first-class base units "
            "and cross-sheet relations (formula ranges, defined names, "
            "chart series) are rendered into retrievable units; "
            "TableQuality (v1.1) is wired into the SDK evaluation path",
        ],
    ),

    # ==================================
    # 4. code
    # ==================================

    DOMAIN_CODE: DomainInfo(
        name=DOMAIN_CODE,
        # Every suffix has its own parser entry; language_map stays
        # in sync with this set.
        suffixes={
            ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx",
            ".ts", ".tsx", ".vue", ".html", ".htm",
            ".css", ".scss", ".less",
            ".java", ".go", ".php",
            ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
            ".cs", ".rs", ".rb", ".swift", ".kt", ".scala",
            ".sh", ".bash", ".sql",
            ".ps1", ".psm1", ".psd1",
            ".dockerfile", ".makefile", ".mk", ".mak",
            ".proto", ".cmake",
            ".lua", ".dart", ".r", ".pl", ".pm", ".zig",
            # .tf/.tfvars moved to the config domain; .hcl generic
            # HCL syntax stays here
            ".hcl",
        },
        quality_applicable=False,
        pipeline_stages=[
            "parse",
            "representation",
        ],
        representation=(
            "AST / component structural chunks — functions, classes, "
            "interfaces, rules and SFC/HTML blocks; remaining top-level "
            "source is retained as module fragments"
        ),
        evaluation=(
            "CodeQuality — structural quality index over "
            "AST/component chunks (integrity, symbol "
            "retrieval, dependency extraction)"
        ),
        optimization=(
            "not applicable — code chunking is "
            "structure-driven, not parameter-driven"
        ),
        status="active",
        investment="frozen",
        warnings=[
            "code domain — tolerant Tree-sitter parsing with "
            "symbol/comment/diagnostic extraction and AST/component "
            "structural chunks; unresolved dependency graphs and "
            "cross-file semantic analysis remain future work",
            "optimization not applicable for code structures "
            "(structure-driven, not parameter-driven)",
        ],
    ),

    # ==================================
    # 5. record_stream
    # ==================================

    DOMAIN_RECORD_STREAM: DomainInfo(
        name=DOMAIN_RECORD_STREAM,
        suffixes={
            ".log", ".jsonl", ".ndjson",
        },
        quality_applicable=False,
        pipeline_stages=[
            "parse",
            "representation",
        ],
        representation=(
            "event grouping — aggregate multiline records, "
            "preserve request_id / session correlation, "
            "extract incident context windows"
        ),
        evaluation=(
            "EventQuality (event_quality_v2.0) — "
            "evaluator over event-group chunks, facts from "
            "the log/jsonl parse contracts, wired into the "
            "SDK evaluate() path"
        ),
        optimization=(
            "not applicable — event grouping is "
            "correlation-driven, not parameter-driven"
        ),
        status="active",
        investment="frozen",
        warnings=[
            "record_stream domain — EventQuality (v1.0) evaluates "
            "multiline event integrity, boundary correctness, "
            "stacktrace completeness, timestamp continuity, "
            "session grouping and parse success; incident "
            "context windows and correlation-aware chunk "
            "optimisation remain future work",
        ],
    ),
}


# ==================================
# reverse lookup suffix → domain
# ==================================

_SUFFIX_TO_DOMAIN: Dict[str, str] = {}

for _domain_name, _domain_info in _DOMAINS.items():
    for _suffix in _domain_info.suffixes:
        _SUFFIX_TO_DOMAIN[_suffix] = _domain_name


# ==================================
# public API
# ==================================

def classify_domain(suffix: str) -> str:
    """Classify a file suffix into its ingestion domain.

    Args:
        suffix: file extension, e.g. ".yaml"

    Returns:
        domain name string.  Returns DOMAIN_NARRATIVE
        as fallback for unknown suffixes (best-effort
        parsing as plain text).
    """
    return _SUFFIX_TO_DOMAIN.get(
        suffix.lower(),
        DOMAIN_NARRATIVE,
    )


def get_domain_info(domain_name: str) -> Optional[DomainInfo]:
    """Get full domain definition by name."""
    return _DOMAINS.get(domain_name)


def get_all_domains() -> Dict[str, DomainInfo]:
    """Return all registered domains."""
    return dict(_DOMAINS)


def get_suffixes_for_domain(domain_name: str) -> Set[str]:
    """Return all suffixes for a given domain."""
    info = _DOMAINS.get(domain_name)
    if info:
        return set(info.suffixes)
    return set()


def is_quality_applicable(suffix: str) -> bool:
    """Check whether NarrativeQuality applies to this file type.

    NarrativeQuality is narrative-only.  It assumes chunks have
    natural language continuity and that cohesion is
    a meaningful signal.
    """
    domain_name = classify_domain(suffix)
    info = _DOMAINS.get(domain_name)
    if info:
        return info.quality_applicable
    return False


def get_domain_warnings(suffix: str) -> List[str]:
    """Return domain-specific warnings for a file type.

    These are emitted when a domain is only partially
    supported (e.g. parse works but representation
    or evaluation is not yet implemented).
    """
    domain_name = classify_domain(suffix)
    info = _DOMAINS.get(domain_name)
    if info:
        return list(info.warnings)
    return []


def get_all_supported_suffixes(active_only: bool = False) -> Set[str]:
    """Return registered suffixes from the product taxonomy.

    Pass ``active_only=True`` when selecting files for runtime execution; this
    omits planned domains that do not yet have a concrete parser.
    """
    result = set()
    for info in _DOMAINS.values():
        if not active_only or info.status == "active":
            result.update(info.suffixes)
    return result


# ==================================
# benchmark dispatch (registry-driven)
# ==================================


@dataclass(frozen=True)
class DomainBenchmarkSpec:
    """Benchmark assembly spec for non-narrative domains.

    The four non-narrative domains share one isomorphic shape in
    evaluate(): profiler stage → evaluator.evaluate(...) → same
    benchmark dict. Only the instance attribute, stage label and
    whether strategy is passed differ. Registering one spec per new
    domain keeps evaluate() free of per-domain if branches.

    narrative is not in the table: its assembly includes DQR,
    strategy echo, PDF reliability context and advisory
    calibration — kept as the explicit branch in client.evaluate().
    """

    evaluator_name: str
    """metadata["benchmark"]["evaluator"] 上报的评估器名。"""

    evaluator_attr: str
    """Provendoc 实例属性名（getattr 延迟解析，避免本模块
    反向依赖 intelligence 层造成循环导入）。"""

    stage: str
    """RuntimeProfiler 的 stage 标签（沿用既有标签，行为不变）。"""

    accepts_strategy: bool
    """评估器签名是 evaluate(document, repr, strategy) 还是两参。"""

    def invoke(
        self, evaluator: Any, document: Any,
        semantic_result: Any, strategy: Any,
    ) -> Any:
        """Call the domain evaluator per the frozen spec shape."""
        if self.accepts_strategy:
            return evaluator.evaluate(
                document, semantic_result, strategy,
            )
        return evaluator.evaluate(document, semantic_result)


DOMAIN_BENCHMARK_SPECS: Dict[str, DomainBenchmarkSpec] = {
    DOMAIN_STRUCTURED_CONFIG: DomainBenchmarkSpec(
        evaluator_name="ConfigQuality",
        evaluator_attr="_config_evaluator",
        stage="config_quality",
        accepts_strategy=True,
    ),
    DOMAIN_CODE: DomainBenchmarkSpec(
        evaluator_name="CodeQuality",
        evaluator_attr="_code_evaluator",
        stage="code_quality",
        accepts_strategy=False,
    ),
    DOMAIN_SPREADSHEET: DomainBenchmarkSpec(
        evaluator_name="TableQuality",
        evaluator_attr="_table_evaluator",
        stage="table_quality",
        accepts_strategy=True,
    ),
    DOMAIN_RECORD_STREAM: DomainBenchmarkSpec(
        evaluator_name="EventQuality",
        evaluator_attr="_event_evaluator",
        stage="event_quality",
        accepts_strategy=True,
    ),
}


def get_domain_benchmark_spec(
    domain_name: str,
) -> Optional[DomainBenchmarkSpec]:
    """Return the registry-driven benchmark spec for a domain, if any."""
    return DOMAIN_BENCHMARK_SPECS.get(domain_name)
