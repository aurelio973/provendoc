import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Single source of truth for suffix → parser class: this module and
# the SDK client share the same mapping table (including notes such
# as the .xlsb engine injection and the .doc CFB piece-table
# parsing); adding a format means registering it in one place,
# parser_registry.
from provendoc.core.pipeline.parser_registry import (
    BARE_NAME_CANONICAL_SUFFIXES,
    DOTFILE_CANONICAL_NAMES,
    compound_suffix_of,
    create_parser,
    drop_in_suffix_of,
    is_env_variant_name,
    supported_suffixes,
)

# Single source of truth for the cross-cutting stamp sequence and
# the mismatch honest-failure document construction
from provendoc.core.pipeline.pipeline_stamps import (
    StampContext,
    build_backend_missing_document,
    build_format_mismatch_document,
    run_stamp_sequence,
)

# Reconciliation gates: pure functions, zero dependencies; they only
# report numbers and never change behavior.
from provendoc.core.pipeline.reconciliation import (
    CoverageReport,
    clean_gate,
    parse_gate,
    removals_from_decisions,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型引用
    from provendoc.base.models.document import Document

_log = logging.getLogger(__name__)


def _coverage_report_to_dict(report: CoverageReport) -> dict:
    """CoverageReport → serializable dict (additive fields, snake_case)."""
    return {
        "verdict": report.verdict,
        "total_lines": report.total_lines,
        "covered_lines": report.covered_lines,
        "missing_lines": report.missing_lines,
        "explained_missing": report.explained_missing,
        "added_lines": report.added_lines,
        "missing_samples": [
            {"line": idx, "text": text} for idx, text in report.missing_samples
        ],
        "note": report.note,
    }


def _stamp_reconciliation(
    document: "Document",
    pre_clean_text: str,
    cleaning_result: Any,
) -> dict:
    """Reconciliation registration: parse gate + clean gate (pure observation; silent degradation on failure).

    parse gate: source_text → original_text, zero tolerance —
    parsing must never drop content; in-line corruption / reordering
    is a content-level RSE responsibility and outside this gate
    (declared in the note).
    clean gate: original_text → cleaned text; every deletion must be
    explained by the decisions ledger (removed/collapsed line-number
    conversion in reconciliation.removals_from_decisions). The
    spreadsheet domain is exempt — canvas facts are authoritative
    and do not go through this registration.
    """
    result: dict = {"extractor": "reconciliation_v1"}
    try:
        if (
            document.source_text is not None
            and document.original_text is not None
        ):
            result["parse_gate"] = _coverage_report_to_dict(
                parse_gate(document.source_text, document.original_text)
            )
        else:
            result["parse_gate"] = {
                "skipped": "missing source_text/original_text parse snapshot"
            }
        anchor = (
            document.original_text
            if document.original_text is not None
            else pre_clean_text
        )
        result["clean_gate"] = _coverage_report_to_dict(
            clean_gate(
                anchor,
                cleaning_result.text,
                removals_from_decisions(cleaning_result.decisions),
            )
        )
    except Exception as exc:  # noqa: BLE001 — 观测登记绝不拖垮解析
        result["error"] = sanitize_log_value(str(exc))
    return result

# The optional representations mechanism is retired: the single
# source of truth for suffix → representation class now lives in
# representation_registry — this module and the SDK client /
# StrategyOptimizer share that one mapping table; adding a format's
# representation wiring means registering it in one place there.
# Lazy imports / missing optional dependencies affecting only the
# corresponding suffix remains the registry's responsibility.
from provendoc.base.models.ingestion_result import IngestionResult
from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import RepresentationUnit, UnitType
from provendoc.core.cleaning.cleaning_policy import CleaningLevel
from provendoc.core.cleaning.code_cleaner import CodeCleaner
from provendoc.core.cleaning.semantic_cleaner import SemanticCleaner
from provendoc.core.domain.domain_registry import (
    DOMAIN_CODE,
    classify_domain,
    get_domain_warnings,
    is_quality_applicable,
)
from provendoc.core.pipeline.config_validation import (
    configuration_parameters_from_packaging,
)

# Format magic-number verification gate: when the declared suffix and
# the actual container family disagree, fail honestly at the entry
# layer — never hand the file to the declared format's parser.
from provendoc.core.pipeline.format_verification import (
    detect_trailing_data,
    verify_format,
)

# Log hygiene: structured escaping of document-derived strings before
# they reach logs — defends against terminal injection (ANSI) and
# log forgery (newlines).
from provendoc.core.pipeline.log_hygiene import sanitize_log_value
from provendoc.core.pipeline.representation_registry import (
    code_family_suffixes,
    create_code_family_representation,
    create_representation,
    registered_suffixes,
)
from provendoc.core.strategies.representation_policy import RepresentationPolicy
from provendoc.core.structure.heading_patterns import extract_heading_candidates
from provendoc.core.structure.metadata_enricher import ChunkMetadataEnricher
from provendoc.core.structure.special_modules import detect_special_modules


def _common_prefix_len(a: str, b: str) -> int:
    """Position of the first differing byte of two strings (the shorter length if one is a prefix of the other)."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i

# ==================================
# suffix → parser / representation maps
# ==================================
#
# The domain classification (narrative vs structured vs
# tabular etc.) is defined in domain_registry.py — this
# file only maps suffixes to their concrete parser and
# representation implementations.
#
# To add a new format:
#   1. Add it to the appropriate domain in domain_registry.py
#   2. Add the parser/representation class here
#   3. The pipeline logic reads domain info from the registry
# ==================================


class UniversalParser:
    """Full-pipeline parser — parse → cleaning → representation in one pass.

    Unlike the SDK client.parse (parse-only, raw document output),
    this class continues past parsing into semantic cleaning
    (SemanticCleaner/CodeCleaner) and three-granularity
    representation construction — the full pipeline behind the
    runtime/API. Domain membership, quality-assessment
    applicability and other policy queries all go through
    domain_registry; suffix → parser instances all go through
    parser_registry (single source of truth).
    """

    def __init__(
            self,
            representation_policy=None,
            cleaning_level=CleaningLevel.STANDARD,
            pdf_limits=None,
            pdf_ocr_language="eng",
            pdf_ocr_tessdata=None,
            table_limits=None,
            docx_limits=None,
            pptx_limits=None,
            enable_image_assets=True,
            log_grouping="session",
            log_incident_context=(5, 3),
    ):
        """Initialize UniversalParser.

        Args:
            representation_policy: RepresentationPolicy instance (or a
                duck-typed compatible object, auto-normalized by
                RepresentationPolicy.coerce_to_policy)
            cleaning_level: cleaning level
            pdf_limits: PDFRuntimeLimits instance or None.
                Resource guards (guards, not filters): passed only to
                PdfParser; exceeding a limit never drops the document,
                it only degrades expensive paths and records the
                reason honestly. None = no caps (valid large files
                such as books are processed in full).
            pdf_ocr_language: Tesseract language string ("+" combines
                languages, e.g. "chi_sim+eng"). Passed only to
                PdfParser and recorded verbatim in OCR metadata; the
                matching .traineddata must exist in a local tessdata
                directory, otherwise the OCR backend is unavailable
                and native extraction degrades in.
            pdf_ocr_tessdata: explicit tessdata directory path (e.g. an
                in-project .tessdata containing chi_sim); None = the
                PyMuPDF default resolution (TESSDATA_PREFIX or system
                installation).
            table_limits: TableRuntimeLimits instance or None.
                Resource guards (guards, not filters): passed to all
                tabular suffixes — the openpyxl-engine excel suffixes
                (.xlsx/.xlsm), legacy backends (.xls/.xlsb/.ods),
                delimited text (.csv/.tsv/.psv) and .parquet
                (max_sheets does not apply there — single logical
                table, the csv precedent); each backend adjudicates
                its applicable dimensions itself, and non-applicable
                dimensions never trigger nor register. None = no
                caps.
            docx_limits: DocxRuntimeLimits instance or None.
                Passed only to .docx (body element cap); not passed
                for .doc. None = no caps.
            pptx_limits: PptxRuntimeLimits instance or None.
                Resource guards (guards, not filters): passed only to
                .pptx (slide count / per-slide shape caps); not passed
                for .doc — not passing is honest, no fake
                "rate-limited" fact is produced. None = no caps.
            enable_image_assets: automatic image-asset extraction
                (default on). Passed to DocxParser / PdfParser /
                PptxParser: extraction is a natural no-op for
                imageless documents, so on-by-default equals "work
                only when images are detected" — when enabled the
                parser additionally produces
                metadata["image_asset_payloads"] (bytes, in-memory,
                to be stripped before JSON serialization) and asset
                facts; body-block semantics are unchanged, only
                [Image: ...] placeholder blocks and manifest facts
                are added. False = explicitly off.
            log_grouping: ".log" event grouping strategy ("session" by
                default, optionally "incident" — error/traceback
                anchors ± context windows merged into incident units,
                background events still grouped by session).
            log_incident_context: window parameters (before, after)
                of the incident strategy, in event counts; only
                effective when log_grouping="incident".
        """
        if representation_policy is not None:
            self.policy = (
                representation_policy
                if isinstance(representation_policy, RepresentationPolicy)
                else RepresentationPolicy.coerce_to_policy(representation_policy)
            )
        else:
            self.policy = RepresentationPolicy()

        self.cleaning_level = cleaning_level

        # Semantic Cleaning Layer
        self.cleaner = SemanticCleaner(level=cleaning_level)

        # Code-domain Cleaning Layer — never rewrites code, strings,
        # comments or indentation; only collapses artifact blank lines.
        self.code_cleaner = CodeCleaner()

        # suffix → parser instances: built uniformly through the
        # parser_registry single source of truth, sharing the same
        # mapping table with the SDK client (count not hand-written;
        # PARSER_SPECS is the reference). PDF receives the resource
        # guards (page/size/OCR time budgets) and the OCR language
        # string; the tabular/document-domain resource guards are
        # passed to all tabular suffixes (openpyxl excel, legacy
        # backends, delimited text and parquet), each backend
        # adjudicating its applicable dimensions; docx/pdf receive
        # the image-asset switch (always passed: False must reach the
        # parser to disable auto-triggering rather than being silently
        # dropped); the .xlsb engine="pyxlsb" injection is declared in
        # the registry's default_kwargs. Suffixes whose optional
        # dependencies are missing are skipped and reported as
        # unsupported at parse() time.
        self.parsers = {}
        for suffix in supported_suffixes():
            overrides = {}
            if suffix == ".pdf":
                if pdf_limits is not None:
                    overrides["limits"] = pdf_limits
                if pdf_ocr_language and pdf_ocr_language != "eng":
                    overrides["ocr_language"] = pdf_ocr_language
                if pdf_ocr_tessdata:
                    overrides["ocr_tessdata"] = pdf_ocr_tessdata
                overrides["enable_image_assets"] = enable_image_assets
            elif suffix == ".docx":
                overrides["enable_image_assets"] = enable_image_assets
                if docx_limits is not None:
                    overrides["limits"] = docx_limits
            elif suffix in (
                ".pptx", ".pptm", ".ppsx", ".ppsm",
                ".potx", ".potm", ".ppt", ".pps", ".pot",
            ):
                # PowerPoint-family: kwargs are distributed
                # family-wide (enable_image_assets / limits apply to
                # the variants and the CFB tier alike — the CFB tier
                # fails at parse time anyway, the kwargs have no
                # effect but are harmless).
                overrides["enable_image_assets"] = enable_image_assets
                if pptx_limits is not None:
                    overrides["limits"] = pptx_limits
            elif suffix in (
                ".xlsx", ".xlsm", ".csv", ".tsv", ".psv",
                ".xls", ".xlsb", ".ods", ".parquet",
            ):
                if table_limits is not None:
                    overrides["limits"] = table_limits
            try:
                parser = create_parser(suffix, **overrides)
            except ImportError as e:
                _log.debug("Parser for %s not available: %s", suffix, e)
                continue
            if parser is not None:
                self.parsers[suffix] = parser

        # suffix → representation instances: built uniformly through
        # the representation_registry single source of truth, sharing
        # the same mapping table with the SDK client /
        # StrategyOptimizer. The strategy-passing contract is
        # "capability means passing": every representation with
        # accepts_strategy=True gets self.policy injected (the old
        # drift of bare construction without strategy for the docx
        # family / ini family / image Txt channel is thereby
        # eliminated); the .log run-level grouping parameters pass
        # through via overrides. Suffixes whose optional dependencies
        # are missing are skipped and parse() treats them as
        # passthrough (same semantics as the retired
        # _optional_representations).
        self.representations = {}
        for suffix in registered_suffixes():
            overrides = {}
            if suffix == ".log":
                # incident context window: run-level configurable
                # grouping strategy and window sizes; the default
                # session stays zero-drift.
                overrides = {
                    "grouping_strategy": log_grouping,
                    "incident_context_before": log_incident_context[0],
                    "incident_context_after": log_incident_context[1],
                }
            try:
                representation = create_representation(
                    suffix, strategy=self.policy, **overrides
                )
            except ImportError as e:
                _log.debug(
                    "Representation for %s not available: %s", suffix, e
                )
                continue
            if representation is not None:
                self.representations[suffix] = representation

        # code domain — structural representation. AST / component spans are
        # sliced against parse-stage source, so each unit remains addressable
        # even when the code-preserving cleaner normalizes blank lines.
        # Single source of truth: language_map.CODE_SUFFIXES (fetched lazily
        # via the registry — no hardcoded tuples; reconciled by
        # tests/test_domain_invariants.py); all code suffixes share one
        # instance (a stateless representation, registry-singleton
        # semantics).
        try:
            code_representation = create_code_family_representation()
            for suffix in code_family_suffixes():
                self.representations[suffix] = code_representation
        except ImportError as e:
            _log.debug("CodeRepresentation not available: %s", e)

    def _effective_configuration_parameters(self) -> dict:
        """Snapshot of pipeline-effective configuration parameters (input to configuration validation).

        Reads the five configurable numbers of the
        RuntimePackagingPolicy; any read failure skips the whole
        snapshot (the validator honestly skips absent parameters
        rather than validating), no defaults are invented.
        """
        try:
            return configuration_parameters_from_packaging(
                getattr(self.policy, "packaging", None)
            )
        except Exception:
            return {}

    def parse(self, file_path, *, version_source_uri=None, parent_manifest=None,
              tenant_id=None, project_id=None, include_understanding=False):
        """Parse a file and run the full pipeline, returning an IngestionResult.

        Flow: suffix normalization (hidden files like .env identified
        by filename) → **format magic-number verification (declared
        suffix × actual container family; disagreement = honest
        failure)** → parser run (honest failures recorded in
        metadata and logged as warnings here in one place) →
        domain-based cleaning → representation build. Unsupported
        types raise ValueError.

        Args:
            file_path: path of the file to parse.
            version_source_uri: optional version-source URI (written
                into provenance metadata).
            parent_manifest: optional parent manifest (container /
                project index scenarios).
            tenant_id: optional tenant identifier (result binding:
                together with doc_id/version_id it forms the
                four-tuple preventing downstream confusion; written
                to metadata only when provided, omit for purely
                local use).
            project_id: optional project identifier (same).
            include_understanding: optional task-layer scenario
                understanding stamp switch (metadata key
                ``scenario_understanding``, candidate-field IR).
                Default False = clean zero-difference; the
                understanding layer scans the full text for evidence,
                its cost borne by the explicit requester (task-layer,
                opt-in by design).

        Returns:
            IngestionResult: document + representations + domain
            metadata.
        """

        suffix = Path(file_path).suffix.lower()
        # Filename normalization: dotfiles (.env and the INI-dialect
        # family) and bare filenames (Dockerfile/Makefile); the
        # canonical lists live in parser_registry as the single
        # source of truth. Variant names (Dockerfile.dev etc.) are
        # out of scope. The full-name match runs **before** the
        # suffix-empty check — Cargo.lock's Path.suffix is ".lock"
        # (non-empty), so only a full-filename hit on
        # DOTFILE_CANONICAL_NAMES routes it to the TOML parser
        # (case-normalized). dotenv family variant names
        # (.env.local / .env.production / .env.example etc.)
        # normalize by prefix to canonical ".env"; ".envrc" lacks the
        # dot separation and misses the prefix rule, but it hits
        # DOTFILE_CANONICAL_NAMES above by full name and routes to
        # the dedicated EnvrcParser. Compound-suffix normalization
        # runs even earlier — main.tf.json's Path.suffix is ".json",
        # so the compound hit normalizes it to canonical ".tf.json"
        # before the table lookup (plain .json/.hcl/.tf/.tfvars
        # routing unchanged, pinned by tests). systemd drop-in
        # parent-directory gating runs earliest —
        # `example.service.d/override.conf`'s suffix is ".conf", so
        # the parent directory normalizes it to canonical
        # ".service"/".timer" before the table lookup (plain .conf
        # zero-drift — the gate matches only .conf inside
        # `*.service.d`/`*.timer.d`; non-.conf files in .d dirs and
        # other unit-type .d dirs are not gated).
        drop_in = drop_in_suffix_of(file_path)
        name = Path(file_path).name.lower()
        compound = compound_suffix_of(name)
        if drop_in is not None:
            suffix = drop_in
        elif compound is not None:
            suffix = compound
        elif name in DOTFILE_CANONICAL_NAMES:
            suffix = name
        elif is_env_variant_name(name):
            suffix = ".env"
        elif not suffix:
            suffix = BARE_NAME_CANONICAL_SUFFIXES.get(name, suffix)

        # parser availability: suffix absent from self.parsers means
        # either a truly unknown suffix (registry returns None) or a
        # known suffix whose optional backend is missing (registry
        # raises ImportError). Probe once to tell them apart so a
        # missing optional backend is disclosed honestly as
        # backend_missing (README/NOTICE auditable-degradation
        # contract) instead of a misleading "unsupported" verdict.
        backend_error = None
        if suffix in self.parsers:
            parser = self.parsers[suffix]
        else:
            try:
                parser = create_parser(suffix)
            except ImportError as exc:
                parser = None
                backend_error = exc
            if parser is not None:
                # registry can build it even though __init__ skipped
                # it (override-sensitive edge) — register and proceed
                self.parsers[suffix] = parser
            elif backend_error is None:
                raise ValueError(
                    f"Unsupported file type: {suffix}"
                )

        # determine domain from registry
        domain_name = classify_domain(suffix)
        quality_eligible = is_quality_applicable(suffix)

        # ==================================
        # Format verification gate
        # Cross-check of declared suffix against the actual
        # container family: on mismatch an honest-failure document is
        # constructed (evidence intact — the file is never handed to
        # the declared format's parser); every other verdict (match /
        # no_magic_expected / insufficient_bytes / undecided /
        # unreadable) passes through with the evidence attached for
        # audit.
        # ==================================
        verification = verify_format(file_path, suffix)
        # Single variable for the mismatch verdict: later references
        # (document stamping / provenance / IngestionResult report)
        # all cite the same one.
        is_mismatch = verification.get("verdict") == "mismatch"
        if is_mismatch:
            # honest-failure document construction and the
            # cross-cutting stamp sequence both go through the
            # pipeline_stamps single source of truth; a mismatch
            # document has no parser output — parser_name is
            # honestly absent, no scenario is stamped.
            document = build_format_mismatch_document(
                file_path, suffix, verification
            )
            run_stamp_sequence(
                document,
                StampContext(
                    file_path=file_path,
                    configuration_parameters=(
                        self._effective_configuration_parameters()
                    ),
                ),
            )
        elif backend_error is not None:
            # Backend-missing honest-failure document — same
            # construction discipline as mismatch (single source:
            # pipeline_stamps). Format verification evidence is
            # kept: the bytes may be perfectly valid; the missing
            # optional backend is the only reason nothing was parsed.
            document = build_backend_missing_document(
                file_path, suffix, backend_error
            )
            document.metadata["format_verification"] = verification
            run_stamp_sequence(
                document,
                StampContext(
                    file_path=file_path,
                    configuration_parameters=(
                        self._effective_configuration_parameters()
                    ),
                ),
            )
        else:
            # parser
            # Trailing-data probe (registry §17 Polyglot): warning
            # evidence, not a rejection reason — the guard is
            # non-fatal, it only feeds format_verification evidence
            # for audit; a warning is emitted when the trailing data
            # itself carries a container magic number.
            trailing = detect_trailing_data(file_path, suffix)
            if trailing is not None:
                verification["trailing_data"] = trailing
                if trailing.get("polyglot_detected"):
                    _log.warning(
                        "Polyglot trailing data for %s: %s container "
                        "embedded after offset %s",
                        sanitize_log_value(file_path),
                        trailing.get("embedded_family"),
                        trailing.get("offset"),
                    )
            document = parser.parse(file_path)
            document.metadata["format_verification"] = verification
            # The cross-cutting stamp sequence (confidence /
            # engineering keys / integrity / sensitive candidates /
            # dual naming / content source / configuration validation
            # / security defaults / scenario) goes through the
            # pipeline_stamps single source of truth — the same
            # runner as the SDK client.parse; the difference surface
            # (this pipeline has a configuration-policy parameter
            # source, the client does not) is expressed explicitly
            # by StampContext.
            run_stamp_sequence(
                document,
                StampContext(
                    file_path=file_path,
                    parser_name=type(parser).__name__,
                    configuration_parameters=(
                        self._effective_configuration_parameters()
                    ),
                    include_scenario=True,
                    include_understanding=include_understanding,
                ),
            )
            if document.metadata.get("parse_failed"):
                # honest failure is a document fact, not an exception —
                # but it must be auditable: each parser only records
                # metadata["parse_failed"]/["parse_error"], the warning
                # log is emitted here in exactly one place instead of
                # every parser improvising its own.
                _log.warning(
                    "Parse failed for %s: %s",
                    sanitize_log_value(file_path),
                    sanitize_log_value(
                        document.metadata.get("parse_error", "unknown error")
                    ),
                )
        # 2026-10-30 honest-accounting fix: the parser class name is
        # stamped only on paths where the parser actually ran.
        # Previously this ran unconditionally after the if/else, so a
        # mismatch honest-failure document (parser never ran) also got
        # stamped with the declared format's parser class name —
        # contradicting both the mismatch branch's own comment
        # ("parser_name honestly absent") and the stamping layer's
        # engineering_keys contract ("FAILED-mismatch → None
        # (honestly absent)"). The key shape stays stable: mismatch
        # sets None rather than deleting the key (the document-level
        # consumer raw_document_exporter goes through .get(); no
        # caller indexes the key directly). The backend-missing path
        # keeps the same accounting: the parser never ran, honestly
        # absent.
        if is_mismatch or backend_error is not None:
            document.metadata["parser"] = None
        else:
            # parser was assigned in the success branch above (same
            # function scope)
            document.metadata["parser"] = (
                parser.__class__.__name__
            )

        # tenant/project context (result binding; additive, optional
        # for purely local use)
        if tenant_id is not None or project_id is not None:
            document.metadata["ingestion_context"] = {
                "tenant_id": tenant_id,
                "project_id": project_id,
            }

        # ==================================
        # Semantic Cleaning Layer
        # Parser → Cleaning → Representation
        #
        # Re-extract heading_candidates and special_modules
        # AFTER cleaning, because cleaning changes line
        # counts and char offsets — stale offsets would
        # cause the StructureAnalyzer to point at wrong
        # positions in the cleaned text.
        #
        # Code domain uses CodeCleaner: it must not rewrite
        # code, strings, comments or indentation, and the
        # AST spans in metadata["code"] stay bound to
        # original_text (the parse-stage snapshot). Spreadsheet units are
        # built directly from metadata["spreadsheet"] canvas facts, so their
        # diagnostic text is not passed through narrative cleaning.
        # ==================================
        is_code_domain = domain_name == DOMAIN_CODE
        is_spreadsheet_domain = domain_name == "spreadsheet"
        if is_spreadsheet_domain:
            # spreadsheet: canvas facts are authoritative;
            # cleaning must not present a text transform as a
            # canvas transform.
            document.metadata["cleaning"] = {
                "applied": False,
                "reason": "spreadsheet_canvas_facts_are_authoritative",
            }
            cleaning_changed = False
        else:
            cleaner = self.code_cleaner if is_code_domain else self.cleaner
            cleaning_result = cleaner.clean(document.text)
            original_text = document.text
            cleaning_changed = cleaning_result.text != original_text
            document.text = cleaning_result.text
            document.metadata["cleaning"] = cleaning_result.to_document_metadata()
            # Reconciliation registration (pure observation,
            # additive; silent degradation on failure): the parse
            # gate is zero-tolerance, the clean gate consumes the
            # decisions ledger.
            document.metadata["reconciliation"] = _stamp_reconciliation(
                document, original_text, cleaning_result,
            )
        if not is_code_domain and not is_spreadsheet_domain:
            # When the parser supplies trusted position candidates
            # (formats with known structure: tex/odt/pptx), keep them
            # first: cleaning deletes/collapses blank-line regions,
            # character offsets before the first change point are
            # unchanged. If every candidate span lies entirely
            # inside the unchanged prefix before the change point →
            # offsets remain valid, keep them; otherwise (no
            # candidates, or candidates crossing the change point)
            # rescan on the cleaned text.
            parser_candidates = document.metadata.get("heading_candidates")
            keep_candidates = False
            if parser_candidates:
                if not cleaning_changed:
                    keep_candidates = True
                else:
                    first_diff = _common_prefix_len(
                        original_text, document.text,
                    )
                    keep_candidates = all(
                        c.end <= first_diff for c in parser_candidates
                    )
            if not keep_candidates:
                document.metadata["heading_candidates"] = (
                    extract_heading_candidates(document.text)
                )
            document.metadata["special_modules"] = (
                detect_special_modules(document.text)
            )

        # representation (passthrough if not registered)
        representation = self.representations.get(suffix)
        if representation is not None:
            representation_result = (
                representation.build(document)
            )
        else:
            # passthrough: wrap entire document as a single chunk
            representation_result = RepresentationResult(
                representation_type="passthrough",
                content=(
                    [RepresentationUnit(
                        text=document.text,
                        unit_type=UnitType.NARRATIVE,
                        metadata={"chunk_index": 0, "passthrough": True}
                    )]
                    if document.text
                    else []
                ),
                metadata={
                    "chunk_count": 1 if document.text else 0,
                    "representation": "passthrough",
                    "note": (
                        "no dedicated representation for "
                        f"{suffix}, returning raw text"
                        if document.text
                        else "no dedicated representation for "
                        f"{suffix}; parse produced no text"
                    ),
                }
            )
        chunks = representation_result.content
        # MultiGranularityOutput → base units list for metadata
        if hasattr(chunks, "base"):
            chunks = chunks.base

        # ==================================
        # Metadata Enrichment (narrative only)
        # ==================================
        if quality_eligible and chunks:
            try:
                from provendoc.core.structure.structure_analyzer import StructureAnalyzer
                from provendoc.core.structure.structure_map import StructureMap

                analyzer = StructureAnalyzer()
                tree = analyzer.analyze(document)
                smap = StructureMap(tree)
                enricher = ChunkMetadataEnricher(smap)
                enricher.enrich(chunks, document.text)
            except Exception:
                _log.debug(
                    "Metadata enrichment failed for %s",
                    sanitize_log_value(file_path),
                    exc_info=True,
                )

        # ==================================
        # Representation Provenance (evidence-only)
        # Cross-format unit evidence (language profile / duplicate
        # evidence / unified source coordinates / sensitivity signals)
        # plus result-level parse_status & deterministic provenance
        # snapshot.  Additive keys only; failures degrade silently.
        # ==================================
        try:
            from provendoc.core.representations.provenance import (
                apply_representation_provenance,
            )
            apply_representation_provenance(
                representation_result,
                document,
                # Same honest-absence discipline: a mismatch document
                # has no parser run, so the provenance parser
                # dimension is honestly absent (the helper signature
                # is Optional[str] and tolerates None).
                parser=(
                    None
                    if is_mismatch or backend_error is not None
                    else parser.__class__.__name__
                ),
                representation=(
                    representation.__class__.__name__
                    if representation is not None
                    else "passthrough"
                ),
                policy_name=self.policy.name,
                cleaning_level=self.cleaning_level.value,
            )
        except Exception:
            _log.debug(
                "Representation provenance pass failed for %s",
                sanitize_log_value(file_path),
                exc_info=True,
            )

        # ==================================
        # Document / chunk identity (doc_id / chunk_id)
        # Two-level change criteria (source_sha256 byte-level /
        # doc_id semantic-level) + within-document chunk_id
        # ({doc_id8}:{index:05d}:{text_hash10}).
        # additive keys only; failures degrade silently (same
        # discipline as the provenance pass — a missing identity
        # never blocks the main flow).
        # ==================================
        doc_identity = None
        try:
            from provendoc.core.representations.doc_identity import (
                stamp_representation_identity,
            )
            doc_identity = stamp_representation_identity(
                representation_result, document,
                file_path=file_path,
            )
        except Exception:
            _log.debug(
                "Document identity stamping failed for %s",
                sanitize_log_value(file_path),
                exc_info=True,
            )

        # ==================================
        # Version manifest (source-anchored content-addressed
        # version lineage)
        # Root identity anchors the user-uploaded original bytes
        # (source_sha256 — never a parser-side artifact); section
        # hashes anchor the parse-stage structural evidence
        # (heading_path of docx_blocks/pdf_blocks); the parent
        # lineage is caller-supplied (None for the first version).
        # additive key only; failures degrade silently (same
        # discipline as provenance / doc_identity).
        # ==================================
        version_manifest = None
        try:
            from provendoc.core.representations.version_manifest import (
                compute_configuration_hash,
                stamp_version_manifest,
            )
            # Processing binding: parse/chunk code versions (same
            # package, same version) + the effective chunking
            # configuration fingerprint. Degrades to None when
            # unobtainable — the binding dimension is honestly
            # absent, nothing is invented (see the version_manifest
            # module docstring).
            try:
                from provendoc import __version__ as _provendoc_version
            except Exception:
                _provendoc_version = None
            try:
                _config_hash = compute_configuration_hash(
                    chunk_size=self.policy.get_effective_chunk_size(),
                    overlap=self.policy.get_effective_overlap(),
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
        except Exception:
            _log.debug(
                "Version manifest stamping failed for %s",
                sanitize_log_value(file_path),
                exc_info=True,
            )

        # domain warnings (for partially supported domains)
        domain_warnings = get_domain_warnings(suffix)

        # build ingestion result
        ingestion_result = IngestionResult(
            document=document,
            representations={
                "semantic": representation_result,
            },
            metadata={
                # Same honest-absence discipline as
                # document.metadata["parser"]: None on mismatch
                # rather than the declared format's fake class name.
                "parser": (
                    None
                    if is_mismatch or backend_error is not None
                    else parser.__class__.__name__
                ),
                "representation": (
                    representation.__class__.__name__
                    if representation is not None
                    else "passthrough"
                ),
                "chunk_count": (
                    representation_result.metadata.get("chunk_count")
                    or representation_result.metadata.get("unit_count")
                    or len(chunks)
                ),
                "file_type": suffix,
                "ingestion_domain": domain_name,
                # document semantic identity (12-char doc_id; see the
                # doc_identity module)
                "doc_id": (doc_identity or {}).get("doc_id"),
                # version lineage (content-addressed version_id; see
                # the version_manifest module)
                "version_id": (version_manifest or {}).get("version_id"),
                "ingestion_mode": (
                    "code"
                    if is_code_domain
                    else ("narrative" if quality_eligible else "structured")
                ),
                "quality_applicable": quality_eligible,
                "domain_warnings": domain_warnings,
                # Derived from the cleaning trace's single source of
                # truth, never a hardcoded True — the spreadsheet
                # track (applied=False) and mismatch documents must
                # be reported as they are. Same contract as
                # sdk._common.cleaning_applied_flag (inlined here to
                # avoid a core→sdk reverse dependency).
                "cleaning_applied": bool(
                    (document.metadata.get("cleaning") or {}).get(
                        "cleaning_applied",
                        (document.metadata.get("cleaning") or {}).get(
                            "applied", False
                        ),
                    )
                ),
                "cleaning_level": self.cleaning_level.value,
                # NEW: policy metadata
                "policy_name": self.policy.name,
                "policy_type": "RepresentationPolicy",
                "invariants_applied": {
                    "hierarchy_continuity": self.policy.invariants.hierarchy_continuity,
                    "structural_block_complete": self.policy.invariants.structural_block_complete,
                    "discourse_locality": self.policy.invariants.discourse_locality,
                },
            }
        )

        return ingestion_result
