"""Provendoc SDK — four interfaces, each built on parse.

Design:
    Every interface starts from parse(). The user never needs
    to worry about encoding, BOM, hidden unicode, or any parsing
    concern — parse() handles all of that.

    Document carries a provenance contract (see
    base/models/document.py):

        document.original_text — parse output, immutable.
            Never touched by cleaning or repair. DQR evaluates
            this field.
        document.text — working text. Updated by semantic
            cleaning. Representations (chunks) are built from
            this field.

    So no matter which interface you call, the original
    text is always available for comparison, and what the
    chunks were actually built from is always explicit.

    1. parse(file_path) → IngestionResult (document only)
       Pure parsing. No representation, no evaluation.
       document.text == document.original_text.

    2. chunk(file_path) → IngestionResult (document + semantic units)
       Parse → default-params semantic representation.
       For users who just want clean semantic units to feed AI.

    3. evaluate(file_path, **params) → IngestionResult (document +
       semantic units + benchmark)
       Parse → user-tuned semantic representation + quality benchmark.
       For RAG engineers adjusting parameters with feedback.
       Params are semantic-first: size_soft_limit, boundary_primary,
       context_mode, discourse_unit_target, paragraph_target ...
       Legacy chunk_size/overlap remain accepted (routed via
       RepresentationPolicy.from_legacy) but are packaging
       constraints, not semantic truth.

    4. auto_pipeline(file_path) → IngestionResult (document +
       semantic units + benchmark + optimization + optimized outputs)
       Parse → full quality evaluation + auto-optimization
       + optimized semantic units for comparison.

    All four interfaces return IngestionResult objects only.
    No files are created. Export is a separate concern — use
    the standalone functions in sdk/export.py when needed.

Ingestion domains (defined in core/domain/domain_registry.py):
    narrative          txt/md       — NarrativeQuality + optimization
    structured_config  yaml/toml/json/ini/xml — semantic flattening
                                      + ConfigQuality benchmark
    spreadsheet        csv/tsv/psv/xlsx/xls — canvas-aware representation
                                      + TableQuality benchmark
    code               html/py/js/ts/vue/go/... — tolerant AST extraction;
                                      structural AST/component chunks
                                      + CodeQuality benchmark
    record_stream      log/jsonl    — event grouping + EventQuality

    Each domain has its own representation philosophy,
    evaluation philosophy, and optimization philosophy.
    All five domains have a quality index (one quality score per domain);
    chunk-size optimization is narrative-only — the other four
    domains are structure/correlation-driven and
    size-parameter-independent by design.

Output structure:
    All four return IngestionResult. The difference is what's
    populated:

    parse:         document only
    chunk:         document + representations["semantic"]
    evaluate:      document + representations["semantic"]
                                  + metadata["benchmark"] (every domain:
                                  NarrativeQuality / ConfigQuality / TableQuality /
                                  CodeQuality / EventQuality respectively)
    auto_pipeline: document + representations["semantic"]
                                  + representations["optimized_semantic"] (narrative)
                                  + metadata["benchmark"] (narrative only)
                                  + metadata["optimization"] (narrative only)
                                  + metadata["optimized_benchmark"] (narrative only)

    auto_pipeline on non-narrative files stops after the
    representation (semantic flattening / canvas / AST / event
    grouping): no benchmark, no optimization — evaluate() is the
    interface that exposes the domain quality evaluation.

    markdown/json export is NOT part of any interface. Use the
    standalone export functions in sdk/export.py when you need
    to persist results to disk.
"""

import logging
from pathlib import Path

from provendoc.base.models.ingestion_result import IngestionResult
from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import RepresentationUnit, UnitType
from provendoc.core.cleaning.cleaning_policy import CleaningLevel
from provendoc.core.domain.domain_registry import (
    DOMAIN_CODE,
    classify_domain,
    get_domain_warnings,
)
from provendoc.core.pipeline.config_validation import (
    configuration_parameters_from_packaging,
    stamp_config_validation,
)

# Format magic-byte verification gate (shared with UniversalParser.parse).
from provendoc.core.pipeline.format_verification import (
    detect_trailing_data,
    verify_format,
)
from provendoc.core.pipeline.log_hygiene import sanitize_log_value

# Single source for suffix → parser (shared with UniversalParser).
from provendoc.core.pipeline.parser_registry import (
    create_parser,
    get_parser_class,
)

# Cross-cutting stamp sequence and mismatch document construction
# (shared with UniversalParser.parse).
from provendoc.core.pipeline.pipeline_stamps import (
    StampContext,
    build_backend_missing_document,
    build_format_mismatch_document,
    run_stamp_sequence,
)

# Single source for suffix → representation (shared with UniversalParser).
from provendoc.core.pipeline.representation_registry import (
    create_representation,
)
from provendoc.core.representations.narrative.markdown_narrative_representation import (
    MarkdownNarrativeRepresentation,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
)
from provendoc.core.strategies.parameter_space import ParameterSpace
from provendoc.core.strategies.representation_policy import (
    RepresentationPolicy,
)
from provendoc.intelligence.diagnostics.document_quality import (
    DocumentQualityReport,
)
from provendoc.intelligence.rse.code_quality import CodeQuality
from provendoc.intelligence.rse.config_quality import ConfigQuality
from provendoc.intelligence.rse.event_quality import EventQuality
from provendoc.intelligence.rse.narrative_quality import NarrativeQuality
from provendoc.intelligence.rse.table_quality import TableQuality
from provendoc.sdk import _auto_pipeline, _evaluate_support
from provendoc.sdk._common import (
    _chunk_count,
    _ingestion_mode,
    _is_code,
    _is_narrative,
    _suffix_of,
    cleaning_applied_flag,
)

_log = logging.getLogger(__name__)


def _passthrough_representation_result(suffix, document):
    """Whole-document single-chunk passthrough representation
    (missing-backend degradation only).

    Same contract as UniversalParser's passthrough branch: when no
    representation backend is available, the whole-document text is
    wrapped as a single NARRATIVE unit, the degradation is disclosed
    in metadata, and nothing ever crashes on a missing optional
    dependency (README/NOTICE honest-degradation contract). An empty
    parse_failed document (parse already failed honestly under the
    missing backend) has no text to wrap — zero chunks is the honest
    output; no empty-text unit is fabricated (the model layer forbids
    it too).
    """
    text = document.text
    content = (
        [
            RepresentationUnit(
                text=text,
                unit_type=UnitType.NARRATIVE,
                metadata={"chunk_index": 0, "passthrough": True},
            )
        ]
        if text
        else []
    )
    return RepresentationResult(
        representation_type="passthrough",
        content=content,
        metadata={
            "chunk_count": len(content),
            "representation": "passthrough",
            "note": (
                "no representation backend available for "
                f"{suffix}, returning raw text"
                if text
                else "no representation backend available for "
                f"{suffix}; parse produced no text"
            ),
        },
    )


class Provendoc:
    """Provendoc SDK — four interfaces, each built on parse.

    Usage:
        oi = Provendoc()

        # just get the parsed document
        result = oi.parse("file.txt")

        # get clean chunks with default params
        result = oi.chunk("file.txt")

        # evaluate with semantic params (preferred — express intent)
        result = oi.evaluate(
            "file.txt",
            size_soft_limit=384,
            boundary_primary="heading_boundary",
            context_mode="selective",
        )

        # legacy size params still work (packaging constraints)
        result = oi.evaluate("file.txt", chunk_size=200, overlap=50)

        # full evaluation + auto-optimization
        result = oi.auto_pipeline("file.txt")

    For non-narrative domains, evaluate() returns the representation
    result plus that domain's own quality index in
    metadata["benchmark"] (ConfigQuality / TableQuality / CodeQuality /
    EventQuality); auto_pipeline stops after the representation.
    Chunk-size optimization is narrative-only — the other domains
    are size-parameter-independent by design.

    Document carries two text fields:
        original_text — parse output, immutable, DQR evaluates it
        text — working text after cleaning, chunks build on it
    """

    # ==================================
    # parser / representation mapping
    # ==================================
    # Both mappings live in parser_registry / representation_registry
    # (shared with UniversalParser).

    # ==================================
    # initialization
    # ==================================

    def __init__(self, cleaning_level=CleaningLevel.STANDARD):
        """Initialize per-domain evaluators and cleaning level.

        cleaning_level controls cleaning strength (default STANDARD).
        """
        self._evaluator = NarrativeQuality()
        self._config_evaluator = ConfigQuality()
        self._code_evaluator = CodeQuality()
        self._table_evaluator = TableQuality()
        self._event_evaluator = EventQuality()
        self._doc_quality = DocumentQualityReport()
        self._cleaning_level = cleaning_level

    # ==================================
    # 1. parse — pure parsing
    # ==================================

    def parse(self, file_path, *, include_understanding=False):
        """Parse file → raw document.

        Handles encoding detection, BOM stripping, newline
        normalization, diagnostics, and semantic signals.
        No chunking, no evaluation, no cleaning.

        document.original_text is snapshotted here and never
        changes afterwards.

        Args:
            file_path: path to file
            include_understanding: opt-in task-layer scenario
                understanding stamp (metadata key
                ``scenario_understanding``, candidate-field IR;
                2026-11-08 wiring). Default False = zero diff;
                the understanding layer scans the full text for
                evidence, so the cost is paid only on explicit
                request (2026-11-04 direction: task layer enables
                scenario structure on demand).

        Returns:
            IngestionResult with document populated,
            representations empty. document.text equals
            document.original_text.
        """
        suffix = _suffix_of(file_path)
        # Format magic-number verification runs before parser
        # construction (same contract as UniversalParser.parse): it
        # applies only to binary formats with magic numbers; text
        # formats with no_magic_expected pass straight through.
        # Mismatch takes priority over the missing-backend disclosure —
        # a fake .pdf gets the more precise container evidence instead
        # of a generic backend-missing. Parser construction kwargs
        # (e.g. engine="pyxlsb" for .xlsb) are injected uniformly by
        # parser_registry's default_kwargs; nothing is special-cased
        # here. Unknown suffixes fall through to _get_parser's
        # TxtParser fallback (same as the old behavior).
        verification = verify_format(file_path, suffix)
        parser_name = None
        if verification.get("verdict") == "mismatch":
            # Mismatch document construction and stamping go through
            # pipeline_stamps; config_validation is stamped in chunk()
            # (client has no configuration policy source).
            document = build_format_mismatch_document(
                file_path, suffix, verification
            )
            run_stamp_sequence(document, StampContext(file_path=file_path))
        else:
            # Trailing-data probe (polyglot): evidence, not rejection —
            # merged into format_verification; warn if it carries a
            # container magic.
            trailing = detect_trailing_data(file_path, suffix)
            if trailing is not None:
                verification["trailing_data"] = trailing
                if trailing.get("polyglot_detected"):
                    _log.warning(
                        "Polyglot trailing data for %s: %s container embedded after offset %s",
                        sanitize_log_value(file_path),
                        trailing.get("embedded_family"),
                        trailing.get("offset"),
                    )
            # When a registered suffix's optional parser backend is
            # missing, create_parser raises ImportError — produce a
            # honest-failure document instead of propagating the
            # exception (README/NOTICE honest-degradation contract:
            # auditable degradation, never a crash; parse_failed
            # documents are equally auditable and warnable).
            try:
                parser = create_parser(suffix)
            except ImportError as backend_error:
                document = build_backend_missing_document(
                    file_path, suffix, backend_error
                )
                run_stamp_sequence(
                    document, StampContext(file_path=file_path)
                )
            else:
                if parser is None:
                    parser = self._get_parser(suffix)()
                parser_name = type(parser).__name__
                document = parser.parse(file_path)
                document.metadata["format_verification"] = verification
                # The cross-cutting stamp sequence (confidence /
                # engineering keys / integrity / sensitive candidates /
                # dual names / content source / safe defaults /
                # scenario) goes through the pipeline_stamps single
                # source of truth (pipeline_consolidation_prereg_v1 D2)
                # — the same runner as UniversalParser.parse; the
                # client has no configuration-policy parameter source,
                # so config_validation is stamped separately by the
                # chunk layer from the effective chunking config (in
                # this file's chunk()).
                run_stamp_sequence(
                    document,
                    StampContext(
                        file_path=file_path,
                        parser_name=parser_name,
                        include_scenario=True,
                        include_understanding=include_understanding,
                    ),
                )
                if document.metadata.get("parse_failed"):
                    # Honest failure is a document fact, not an
                    # exception — but it must be auditable: same
                    # contract as UniversalParser.parse, the warning
                    # is emitted here in one place (all four SDK
                    # interfaces build on parse; one site covers
                    # every entry point).
                    _log.warning(
                        "Parse failed for %s: %s",
                        sanitize_log_value(file_path),
                        sanitize_log_value(document.metadata.get("parse_error", "unknown error")),
                    )

        return IngestionResult(
            document=document,
            representations={},
            metadata={
                # 2026-10-30 honest-accounting fix: a mismatch document
                # has no parser run, so the declared format's parser
                # class name is not reported — same contract as the
                # stamping layer's existing engineering_keys rule
                # ("FAILED-mismatch path has no parser to speak of →
                # None (honestly absent)"); previously the mismatch
                # branch's stamp sequence deliberately omitted
                # parser_name while the outer report still carried the
                # class name — two contradictory accountings for one
                # document. The normal path reports the real class
                # name. The backend-missing path is the same: the
                # parser never ran, the class name is honestly absent.
                "parser": parser_name,
                "file_type": suffix,
                "ingestion_domain": classify_domain(suffix),
                "ingestion_mode": _ingestion_mode(suffix),
                "domain_warnings": get_domain_warnings(suffix),
            },
        )

    # ==================================
    # 1b. merge_configs — multi-file config merging
    # ==================================

    def merge_configs(self, file_paths):
        """Merge multiple structured-config files into one view.

        Routes every suffix through parser_registry and calls the
        parser's ``parse_many_merged`` channel. Merge semantics are
        disclosed per family / tool profile:

        - systemd units: canonical unit assembled as tier + drop-ins
          (not layered shadowing); mixed .timer/.service pairs are a
          supported use case;
        - layered families (yaml/json/xml/plist/properties/env/toml):
          later files shadow earlier ones (kustomize/helm semantics);
        - conf family: semantics follow the detected tool profile
          (flat_override / first_obtained_wins /
          multi_value_append_candidate / include inline expansion);
          include targets resolve only within the provided file set
          (restricted IO), load order preserved in
          include_graph.resolved_file_order; merged view is a static
          candidate (validated=False, context_dependent=True);
        - cnf family: per detected profile (mysql/mariadb
          later_include_last_wins, openssl include expansion, generic
          fallback); includedir matched by prefix only, never read;
          mixed mysql-family and openssl profiles are rejected.

        Validation: at least two files; every suffix must route to the
        same config-family parser class (cross-format merges, and ≥2
        distinct identified tool profiles in one conf/cnf merge, are
        rejected).

        Args:
            file_paths: ordered list of paths (layering order; conf
                include targets resolve only within this list — no
                globbing, no path search).

        Returns:
            The merged contract dict (not an IngestionResult): header
            with merged/sources/entries/containers/loss, per-entry
            source_file attribution; conf family also carries
            include_graph / include_source_order.

        Raises:
            ValueError: fewer than 2 files, non-config suffix, or
                mixed families/profiles.
        """
        paths = list(file_paths)
        if len(paths) < 2:
            raise ValueError(
                "merge_configs requires at least 2 files "
                f"(got {len(paths)})"
            )

        parser_classes = []
        for fp in paths:
            suffix = _suffix_of(fp)
            parser = create_parser(suffix)
            if parser is None:
                parser = self._get_parser(suffix)()
            if not hasattr(parser, "parse_many_merged"):
                raise ValueError(
                    f"{suffix!r} does not route to a structured-config "
                    "parser (no parse_many_merged channel)"
                )
            parser_classes.append(type(parser))

        first = parser_classes[0]
        for cls, fp in zip(parser_classes, paths):
            if cls is not first:
                raise ValueError(
                    "mixed config families cannot be merged: "
                    f"{first.__name__} vs {cls.__name__} "
                    f"({_suffix_of(fp)!r})"
                )

        return first().parse_many_merged(paths)

    # ==================================
    # 2. chunk — parse + default chunking
    # ==================================

    def chunk(self, file_path, project_root=None, *, version_source_uri=None, parent_manifest=None):
        """Parse → chunk with default parameters.

        For users who just want clean chunks to feed AI.
        No parameter tuning, no benchmark — just clean data.

        Args:
            file_path: path to file
            project_root: optional project root (code domain only).
                When supplied and the target is a code file, a
                read-only cross-file project index is built over
                project_root and proven import/symbol evidence is
                attached to overlapping chunks (``project_references``
                / ``project_symbol_references`` in chunk metadata).
                Never changes chunk text, count or order; index
                failures are recorded in metadata instead of raising.

        Returns:
            IngestionResult with document +
            representations["semantic"].
        """
        # step 1: parse
        parsed = self.parse(file_path)
        document = parsed.document
        suffix = _suffix_of(file_path)

        # step 2: domain-specific cleaning. Code must retain every token,
        # string, comment and indentation character; source spans remain
        # anchored to document.original_text.
        self._clean_document_for_domain(document, suffix)

        # step 3: representation — scenario chains dispatch first.
        # A chained label with usable ledger chunks becomes
        # representations["semantic"] directly (ledger consumption,
        # never re-derived); everything else (no label / no ledger /
        # degradation) takes the generic path, recorded in
        # metadata["scenario_chunking"] (degradation is audited,
        # never fatal).
        from provendoc.core.representations.narrative.scenario_narrative_representation import (
            REPR_NAME as _scenario_repr_name,
        )
        from provendoc.core.representations.narrative.scenario_narrative_representation import (
            ScenarioChunkFallback,
            build_scenario_chunk_result,
        )

        strategy = RepresentationPolicy()
        scenario_output = None
        scenario_ledger = None  # → metadata["scenario_chunking"]
        try:
            scenario_output = build_scenario_chunk_result(document)
        except ScenarioChunkFallback as _fallback:
            scenario_ledger = {
                "engine": _fallback.engine,
                "fallback": True,
                "fallback_reason": _fallback.reason,
            }
        except Exception:
            # Any unexpected error degrades honestly — never raises.
            _log.debug("scenario chunk dispatch failed", exc_info=True)
            _label = ""
            _meta = getattr(document, "metadata", None) or {}
            if isinstance(_meta.get("scenario"), dict):
                _label = str(_meta["scenario"].get("label", "") or "")
            scenario_ledger = {
                "engine": _label or "unknown",
                "fallback": True,
                "fallback_reason": "dispatch_unexpected_error",
            }
        if scenario_output is not None:
            semantic_repr = None  # scenario path: no generic repr object
            semantic_result = scenario_output.result
            scenario_ledger = {
                "engine": scenario_output.engine,
                "fallback": False,
                "policy_name": scenario_output.policy_name,
                "params": scenario_output.params,
                "chunk_count": scenario_output.chunk_count,
                "invariant_verdict": scenario_output.invariant_verdict or None,
                "source": "ledger",
            }
        else:
            # A registered representation's optional dependency is
            # missing (under a core-only install the .pdf representation
            # chain's top-level import raises ImportError) — degrade
            # to passthrough, same contract as UniversalParser.parse's
            # representation branch: whole-document single-chunk
            # wrapping, never a crash, the degradation recorded in
            # metadata for audit.
            try:
                semantic_repr = self._get_representation(suffix, strategy)
            except ImportError as repr_backend_error:
                semantic_repr = None
                semantic_result = _passthrough_representation_result(
                    suffix, document
                )
                _log.warning(
                    "Representation backend missing for %s: %s; "
                    "passthrough chunking engaged",
                    sanitize_log_value(str(file_path)),
                    sanitize_log_value(str(repr_backend_error)),
                )
            else:
                semantic_result = semantic_repr.build(document)
                if scenario_ledger is not None:
                    # fallback → audit what actually ran
                    scenario_ledger["policy_name"] = strategy.name
                    scenario_ledger["chunk_count"] = _chunk_count(semantic_result)
        # B12 evidence-only provenance pass (additive keys, idempotent)
        from provendoc.core.representations.provenance import (
            apply_representation_provenance,
        )

        apply_representation_provenance(
            semantic_result,
            document,
            parser=parsed.metadata.get("parser"),
            representation=(
                semantic_repr.__class__.__name__
                if semantic_repr is not None
                else (
                    _scenario_repr_name
                    if scenario_output is not None
                    else "passthrough"
                )
            ),
            policy_name=(
                strategy.name
                if semantic_repr is not None
                else (
                    scenario_output.policy_name
                    if scenario_output is not None
                    else None
                )
            ),
            cleaning_level=self._cleaning_level.value,
        )
        # doc/chunk identity (additive; failures degrade silently)
        from provendoc.core.representations.doc_identity import (
            stamp_representation_identity,
        )

        stamp_representation_identity(
            semantic_result,
            document,
            file_path=file_path,
        )
        # version manifest (additive; failures degrade silently)
        from provendoc.core.representations.version_manifest import (
            compute_configuration_hash,
            stamp_version_manifest,
        )

        # Processing binding: parser/chunker versions + effective
        # chunking config hash; None when unavailable (honest absence).
        try:
            from provendoc import __version__ as _provendoc_version
        except Exception:
            _provendoc_version = None
        if scenario_output is None:
            try:
                _config_hash = compute_configuration_hash(
                    chunk_size=strategy.get_effective_chunk_size(),
                    overlap=strategy.get_effective_overlap(),
                )
            except Exception:
                _config_hash = None
        else:
            # Scenario-path effective params are already recorded
            # verbatim in the scenario_chunking ledger.
            _config_hash = None
        version_manifest = stamp_version_manifest(
            document,
            file_path,
            source_uri=version_source_uri,
            parent_manifest=parent_manifest,
            parser_version=_provendoc_version,
            chunker_version=_provendoc_version,
            configuration_hash=_config_hash,
        )
        # Config validation contract: effective chunking params vs
        # frozen caps — violations warn, never fail. Skipped on the
        # scenario path (its params live in the ledger).
        if scenario_output is None:
            try:
                stamp_config_validation(
                    document,
                    configuration_parameters_from_packaging(getattr(strategy, "packaging", None)),
                )
            except Exception:
                _log.debug("config-validation stamping failed", exc_info=True)

        # step 3.5 — optional read-only project evidence (code domain);
        # annotate_chunks is idempotent and never blocks chunk output.
        metadata_extra = {}
        metadata_extra["version_id"] = (version_manifest or {}).get("version_id")
        if project_root is not None and classify_domain(suffix) == DOMAIN_CODE:
            error = self._annotate_project_evidence(project_root, file_path, semantic_result)
            if error is None:
                metadata_extra["project_index_applied"] = True
            else:
                metadata_extra["project_index_error"] = error
        elif project_root is not None and isinstance(
            semantic_repr, MarkdownNarrativeRepresentation
        ):
            # Repository observation: resolve local link/image paths
            # (stat only — no content read).
            error = self._annotate_markdown_links(project_root, file_path, document)
            if error is None:
                metadata_extra["repository_context_applied"] = True
            else:
                metadata_extra["markdown_repository_error"] = error

        return IngestionResult(
            document=document,
            representations={"semantic": semantic_result},
            metadata={
                "parser": parsed.metadata["parser"],
                "file_type": suffix,
                "representation": (
                    semantic_repr.__class__.__name__
                    if semantic_repr is not None
                    else _scenario_repr_name
                ),
                "chunk_count": _chunk_count(semantic_result),
                "ingestion_mode": _ingestion_mode(suffix),
                "ingestion_domain": classify_domain(suffix),
                # Derived from the cleaning trace (document.metadata
                # ["cleaning"]) — not hardcoded.
                "cleaning_applied": cleaning_applied_flag(document),
                "cleaning_level": self._cleaning_level.value,
                # Document semantic identity (see doc_identity).
                "doc_id": ((document.metadata or {}).get("doc_identity") or {}).get("doc_id"),
                # Scenario chunking ledger, present only when a
                # scenario label matched (hit or degradation).
                **(
                    {"scenario_chunking": scenario_ledger}
                    if scenario_ledger is not None
                    else {}
                ),
                **metadata_extra,
            },
        )

    # ==================================
    # 2b. convenience one-liners (ecosystem-friendly)
    # ==================================

    def chunk_texts(self, file_path, project_root=None):
        """chunk() → list of chunk texts — the one-liner for feeding AI.

        Equivalent to ``[u.text for u in self.chunk(file_path).chunks]``
        (with the same normalization the ecosystem adapters use, so
        ``chunk_texts`` and the LangChain/LlamaIndex adapters always
        agree on unit selection).

        Args:
            file_path: path to file
            project_root: optional project root (code domain only,
                same semantics as ``chunk()``).

        Returns:
            list[str] — chunk texts in document order.
        """
        from provendoc.integrations._common import result_to_items

        result = self.chunk(file_path, project_root=project_root)
        return [text for text, _meta in result_to_items(result, source=str(file_path))]

    def chunk_dicts(self, file_path, project_root=None):
        """chunk() → list of ``{"text", "metadata"}`` dicts.

        The observability-friendly form: every chunk carries its
        provenance metadata (unit_type, hierarchy_path, doc_id,
        cleaning_level, chunk_index, source — same field set as the
        LangChain/LlamaIndex adapters, single conversion source).

        Args:
            file_path: path to file
            project_root: optional project root (code domain only,
                same semantics as ``chunk()``).

        Returns:
            list[dict] — ``{"text": str, "metadata": dict}`` in
            document order.
        """
        from provendoc.integrations._common import result_to_items

        result = self.chunk(file_path, project_root=project_root)
        return [
            {"text": text, "metadata": metadata}
            for text, metadata in result_to_items(result, source=str(file_path))
        ]

    def index_project(self, root):
        """Build a read-only cross-file project index over ``root``.

        Code-domain deepening (P2 file edges + P5 proven symbol
        edges + unresolved-with-reason records). Pure observation:
        no source is modified, nothing feeds ingestion decisions.
        Returns a JSON-compatible metadata dict (lists capped by
        the module's export limits).
        """
        from provendoc.core.parsers.code.project_indexer import (
            ProjectIndexer,
        )

        return ProjectIndexer().index(Path(root)).to_metadata()

    def _annotate_project_evidence(self, project_root, file_path, semantic_result):
        """Attach cross-file evidence to code chunks; return error or None."""
        try:
            from provendoc.core.parsers.code.project_indexer import (
                ProjectIndexer,
            )

            index = ProjectIndexer().index(Path(project_root))
            ProjectIndexer.annotate_chunks(index, str(file_path), semantic_result.content)
            return None
        except Exception as exc:  # 观测增强不成为新失败点
            return f"{type(exc).__name__}: {exc}"

    def _annotate_markdown_links(self, project_root, file_path, document):
        """Resolve local link/image paths in the markdown_nodes tree.

        Strip ``#fragment`` → resolve against the md parent dir →
        confine to project_root → stat only (no content read, no
        download, no execution). Pure anchors (empty after strip) are
        skipped. Returns an error string on failure, never raises.
        """
        try:
            tree = (document.metadata or {}).get("markdown_nodes")
            if not isinstance(tree, dict) or not tree.get("nodes"):
                return None
            root = Path(project_root).resolve()
            base = Path(file_path).resolve().parent
            for nd in tree["nodes"]:
                if nd.get("node_type") not in ("link", "image"):
                    continue
                if nd.get("resolution_status") != "unresolved":
                    continue
                tf = nd.get("type_facts") or {}
                raw = (tf.get("target_raw") or "").strip()
                path_part = raw.split("#", 1)[0].strip()
                if not path_part:
                    continue
                target = (base / path_part).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    nd["resolution_status"] = "partial"
                    nd["reason_code"] = "OUTSIDE_PROJECT_ROOT"
                    continue
                tf["target_resolved"] = target.relative_to(root).as_posix()
                if target.exists():
                    nd["resolution_status"] = "complete"
                    nd["reason_code"] = None
                    tf["asset_exists"] = True
                else:
                    nd["resolution_status"] = "partial"
                    nd["reason_code"] = "MISSING_ASSET"
                    tf["asset_exists"] = False
            tree["repository_context_applied"] = True
            return None
        except Exception as exc:  # 观测增强不成为新失败点
            return f"{type(exc).__name__}: {exc}"

    # ==================================
    # 3. evaluate — parse + user params + benchmark
    # ==================================

    # recommendation modes accepted by evaluate()
    _RECOMMEND_MODES = _evaluate_support.RECOMMEND_MODES
    # compare mode candidate cap (each candidate re-runs the full
    # parse → chunk → evaluation pipeline, so the fan-out is bounded)
    _COMPARE_MAX_CANDIDATES = _evaluate_support.COMPARE_MAX_CANDIDATES

    def evaluate(
        self,
        file_path,
        stability_sweep=False,
        profile=False,
        *,
        version_source_uri=None,
        parent_manifest=None,
        recommend="rules",
        compare_strategies=None,
        search_mode="controlled",
        **params,
    ):
        """Parse → chunk with user params → benchmark → recommendations.

        For RAG engineers and data engineers who want to
        adjust chunking parameters and get quality feedback.

        Recommendations (narrative domain; ``recommend`` keyword):

            "rules"   (default) — deterministic rule engine over the
                      versioned TOML rule table. Reads the current
                      strategy params + quality profile + confirmed
                      retrieval failure modes + document profile and
                      emits parameter suggestions with side effects,
                      confidence and provenance. Cheap and auditable.
            "compare" — rules plus verification: each top suggestion
                      (and each dict in ``compare_strategies``) is
                      applied over the current params and re-evaluated,
                      reporting quality-score deltas. Costs one full pipeline run
                      per candidate (capped at 5).
            "search"  — heavyweight StrategyOptimizer exploration
                      (``search_mode``: controlled/grid/hierarchical).
                      Explicit only — never the default. Result lands
                      in metadata["optimization"].
            "none"    — skip recommendations (also skips the retrieval
                      failure detection pass they use as input).

        Recommendations are diagnostic hypotheses, not guaranteed
        improvements: every item lists side effects and requires
        re-evaluation. Non-narrative domains return an honest
        ``not_applicable`` payload — their representations are
        size-parameter-independent by design.

        For structured config files (yaml/toml/json/ini) the
        benchmark is ConfigQuality — a config-domain quality index
        measuring semantic path integrity. It is NOT NarrativeQuality
        and does not participate in chunk-size optimization
        (config files use semantic flattening, so size params
        have no effect).

        For other structured domains the benchmark is that domain's
        own quality index — TableQuality for spreadsheet, CodeQuality for
        code, EventQuality for record_stream — never NarrativeQuality and
        never the chunk-size optimizer.

        Parameters are validated and merged with safe defaults
        via ParameterSpace. Only specify what you want to change.
        Semantic-first (preferred):

            oi.evaluate(
                "file.txt",
                size_soft_limit=384,
                boundary_primary="heading_boundary",
            )

        Legacy size params (still accepted, treated as packaging
        constraints and routed via from_legacy):

            oi.evaluate("file.txt", chunk_size=200, overlap=50)

        Mixing chunk_size with size_soft_limit/size_hard_limit
        raises ValueError (one parameter style per call).

        Args:
            file_path: path to file
            stability_sweep: if True, run SemanticStabilityObserver
                sweep across soft_limit range and include stability
                analysis in metadata["stability"] (narrative only).
            **params: semantic-first params — size_soft_limit,
                size_hard_limit, boundary_primary, context_mode,
                overlap_size, discourse_unit_target, paragraph_target,
                merge_short_sibling_sections, short_section_ratio,
                hierarchy_continuity, structural_block_complete,
                discourse_locality, name; plus legacy packaging
                params — chunk_size, overlap, orphan_threshold,
                paragraph_merge, preserve_headings (compat-mapped),
                and no-op compat mirrors collapse_blank_lines /
                long_text_strategy (DEPRECATED, kept for API
                stability; see RepresentationPolicy field docs).
                Any subset — unspecified params use defaults.

        Returns:
            IngestionResult with document +
            representations["semantic"] +
            metadata["benchmark"] (narrative only; canonical quality
            evaluation in the "evaluation" key) +
            metadata["recommendations"] (unless recommend="none") +
            metadata["corpus_tier"] (always) +
            metadata["stability"] (if stability_sweep, narrative only).

        Raises:
            ValueError: if params are invalid, or ``recommend`` is not
                one of rules/compare/search/none.
        """
        if recommend not in self._RECOMMEND_MODES:
            raise ValueError(f"recommend must be one of {self._RECOMMEND_MODES}, got {recommend!r}")

        # RuntimeProfiler
        from provendoc.core.diagnostics.runtime_profiler import RuntimeProfiler

        _profiler = RuntimeProfiler(enabled=profile)
        _profiler.set_label(str(file_path))

        # step 1: parse
        with _profiler.stage("parse"):
            parsed = self.parse(file_path)
        document = parsed.document
        suffix = _suffix_of(file_path)

        # step 2: domain-specific cleaning. Code parsing metadata retains
        # parse-stage spans, so this path intentionally avoids SemanticCleaner.
        with _profiler.stage("cleaning"):
            self._clean_document_for_domain(document, suffix)

        # step 3: build strategy from user params
        strategy = ParameterSpace.build(**params)

        # step 4: representation (always built —
        # semantic flattening for structured,
        # parse-state passthrough for code,
        # context segmentation for narrative);
        # stage implementation lives in sdk/_evaluate_support.
        (
            semantic_repr,
            semantic_result,
            doc_identity,
            version_manifest,
        ) = _evaluate_support.build_semantic_representation(
            self,
            parsed,
            file_path,
            suffix,
            strategy,
            version_source_uri=version_source_uri,
            parent_manifest=parent_manifest,
            profiler=_profiler,
        )
        chunks = _evaluate_support.chunks_from_result(semantic_result)

        # step 5: benchmark
        # narrative → NarrativeQuality, config → ConfigQuality,
        # code → CodeQuality (structural, never feeds chunk-size
        # optimisation: AST/component chunking ignores size params)
        metadata = {
            "parser": parsed.metadata["parser"],
            "file_type": suffix,
            "representation": semantic_repr.__class__.__name__,
            "chunk_count": _chunk_count(semantic_result),
            # Derived from the cleaning trace (same source as chunk()).
            "cleaning_applied": cleaning_applied_flag(document),
            "cleaning_level": self._cleaning_level.value,
            # Document semantic identity (see doc_identity).
            "doc_id": (doc_identity or {}).get("doc_id"),
            # Version lineage (see version_manifest).
            "version_id": (version_manifest or {}).get("version_id"),
        }

        dqr = None
        if _is_narrative(suffix):
            dqr, metadata["benchmark"] = _evaluate_support.narrative_benchmark(
                self,
                file_path,
                suffix,
                document,
                semantic_result,
                strategy,
                chunks,
                _profiler,
            )
        else:
            _evaluate_support.domain_benchmark(
                self,
                suffix,
                file_path,
                document,
                semantic_result,
                strategy,
                metadata,
                _profiler,
            )

        # RSE three-layer envelope — additive benchmark keys (rse /
        # compatibility), same shape as the run-level evaluation.json.
        # Assembly only; None on failure (no new SPOF).
        _evaluate_support.attach_re_envelope(
            self,
            suffix,
            metadata,
            semantic_result,
            strategy,
            document,
        )

        # Recommendations — deterministic rule engine by default;
        # compare/search are explicit escalation modes. Additive and
        # never a failure point: internal errors degrade to an honest
        # unavailable payload (same discipline as the RE envelope).
        if recommend != "none":
            with _profiler.stage("recommendations"):
                _rec_payload, _opt_meta = self._recommendations(
                    recommend,
                    file_path=file_path,
                    suffix=suffix,
                    document=document,
                    chunks=chunks,
                    strategy=strategy,
                    evaluation=(metadata.get("benchmark") or {}).get("evaluation"),
                    dqr=dqr,
                    compare_strategies=compare_strategies,
                    search_mode=search_mode,
                )
                metadata["recommendations"] = _rec_payload
                if _opt_meta is not None:
                    metadata["optimization"] = _opt_meta

        # StructureAnalyzer is a narrative/config text analyzer. Code already
        # carries tree-sitter symbols, comments and diagnostics in metadata["code"].
        _evaluate_support.enrich_structure(
            suffix,
            document,
            chunks,
            metadata,
            _profiler,
        )

        # corpus tier classification
        _file_size = _evaluate_support.attach_corpus_tier(
            suffix,
            file_path,
            chunks,
            metadata,
            _profiler,
        )

        # RuntimeProfiler: complexity metrics + output
        _profiler.record("file_size_bytes", _file_size)
        _profiler.record("char_count", len(document.text) if document.text else 0)
        _profiler.record("line_count", len(document.text.splitlines()) if document.text else 0)
        _profiler.record("base_units", len(chunks))
        if profile:
            metadata["profile"] = _profiler.to_dict()
            print(_profiler.format_text())

        # optional stability sweep
        if stability_sweep and _is_narrative(suffix) and dqr is not None:
            metadata["stability"] = _evaluate_support.stability_sweep(
                self,
                document,
                strategy,
                dqr,
            )

        return IngestionResult(
            document=document,
            representations={"semantic": semantic_result},
            metadata=metadata,
        )

    # ==================================
    # 4. auto_pipeline — full quality + optimization
    # ==================================

    def auto_pipeline(
        self,
        file_path,
        stability_sweep=False,
        profile=False,
        *,
        version_source_uri=None,
        parent_manifest=None,
    ):
        """Parse → semantic representation → benchmark → optimize → optimized outputs.

        Legacy full-pipeline interface. Runs the full pipeline:
        default-params semantic representation, quality evaluation, automatic
        parameter optimization, and generates optimized semantic units
        for comparison.

        Positioning: the optimizer is an explicit heavyweight tool, not
        the default advice path — evaluate() now defaults to the
        deterministic rule engine (recommend="rules"), and
        evaluate(recommend="search") offers the same optimizer
        exploration without the optimized-representation rebuild. Use
        auto_pipeline when you want optimized semantic units side by
        side with the defaults.

        For structured files (yaml/toml), only parse +
        representation (semantic flattening) is performed.
        Evaluation, benchmark, and optimization are skipped — these
        files do not benefit from representation strategy tuning.

        Outputs (narrative):
        - semantic: semantic units with default strategy
        - benchmark: DQR + quality evaluation for default strategy
        - optimization: full search result (trials, phases, etc.)
        - optimized_semantic: semantic units with best strategy
        - optimized_benchmark: DQR + quality evaluation for best strategy

        Outputs (structured):
        - semantic: semantic flattening result
        - ingestion_mode: "structured"

        Args:
            file_path: path to file
            stability_sweep: if True, run SemanticStabilityObserver
                sweep and include stability analysis in
                metadata["stability"] (narrative only).

        Returns:
            IngestionResult with corpus_tier in metadata
            (always) and stability if requested.
        """
        return _auto_pipeline.run(
            self,
            file_path,
            stability_sweep,
            profile,
            version_source_uri=version_source_uri,
            parent_manifest=parent_manifest,
        )

    # ==================================
    # helpers
    # ==================================

    def _clean_document_for_domain(self, document, suffix):
        """Clean a document without violating its domain's provenance contract."""
        domain = classify_domain(suffix)
        if domain == "spreadsheet":
            # Spreadsheet representation reads the canonical cell/canvas facts,
            # not document.text.  Do not normalize its diagnostic text and risk
            # presenting a text-cleaning transform as a canvas transform.
            document.metadata["cleaning"] = {
                "applied": False,
                "reason": "spreadsheet_canvas_facts_are_authoritative",
            }
            return None
        if _is_code(suffix):
            from provendoc.core.cleaning.code_cleaner import CodeCleaner

            cleaner = CodeCleaner()
        else:
            from provendoc.core.cleaning.semantic_cleaner import SemanticCleaner

            cleaner = SemanticCleaner(level=self._cleaning_level)

        cleaning_result = cleaner.clean(document.text)
        if cleaning_result.text != document.text:
            self._refresh_structure_probes(document, cleaning_result.text)
        document.text = cleaning_result.text
        document.metadata["cleaning"] = cleaning_result.to_document_metadata()
        return cleaning_result

    @staticmethod
    def _refresh_structure_probes(document, text):
        """Recompute offset-anchored structure probes after cleaning.

        heading_candidates / special_modules offsets anchor to the
        text coordinate system at extraction time; cleaning rewrites
        the text and shifts every offset, so stale spans would slice
        content at wrong boundaries. Only recomputes candidates from
        the shared extractor (heading_patterns.HeadingCandidate);
        custom candidate structures (tex/pptx) are left untouched.
        """
        metadata = getattr(document, "metadata", None) or {}
        from provendoc.core.structure.heading_patterns import (
            HeadingCandidate,
            extract_heading_candidates,
        )

        candidates = metadata.get("heading_candidates")
        if candidates and all(isinstance(c, HeadingCandidate) for c in candidates):
            metadata["heading_candidates"] = extract_heading_candidates(text)
            if metadata.get("special_modules"):
                from provendoc.core.structure.special_modules import (
                    detect_special_modules,
                )

                metadata["special_modules"] = detect_special_modules(text)

    def _get_representation(self, suffix, strategy):
        """Build the representation matching a parsed file's domain.

        The suffix → representation mapping lives in
        core.pipeline.representation_registry (single source); the
        registry decides whether strategy is passed. Unregistered
        suffixes fall back to TxtNarrativeRepresentation; a missing
        optional dependency raises ImportError for the caller to
        handle.
        """
        representation = create_representation(suffix, strategy=strategy)
        if representation is None:
            return TxtNarrativeRepresentation(strategy=strategy)
        return representation

    def _get_parser(self, suffix):
        """Return the parser class matching the file suffix.

        The suffix → parser mapping lives in
        core.pipeline.parser_registry (shared with UniversalParser,
        including special routes like the .xlsb engine and .doc CFB
        parsing). Unknown suffixes fall back to TxtParser; a missing
        optional dependency raises ImportError for the caller to
        handle.
        """
        parser_cls = get_parser_class(suffix)
        if parser_cls is None:
            from provendoc.core.parsers.narrative.txt_parser import TxtParser

            return TxtParser
        return parser_cls

    # ==================================
    # private compat delegates
    # ==================================
    # Thin delegates kept for code calling these private methods
    # directly; implementations live in sdk/_evaluate_support.py
    # and sdk/_auto_pipeline.py.
    # ==================================

    def _re_envelope(self, suffix, evaluation, semantic_result, strategy=None, document=None):
        """RE envelope assembly (delegates to _evaluate_support)."""
        return _evaluate_support.re_envelope(
            self,
            suffix,
            evaluation,
            semantic_result,
            strategy=strategy,
            document=document,
        )

    def _recommendations(
        self,
        mode,
        *,
        file_path,
        suffix,
        document,
        chunks,
        strategy,
        evaluation,
        dqr,
        compare_strategies,
        search_mode,
    ):
        """Recommendation payload (delegates to _evaluate_support)."""
        return _evaluate_support.recommendations(
            self,
            mode,
            file_path=file_path,
            suffix=suffix,
            document=document,
            chunks=chunks,
            strategy=strategy,
            evaluation=evaluation,
            dqr=dqr,
            compare_strategies=compare_strategies,
            search_mode=search_mode,
        )

    def _rules_recommendations(self, *, file_path, document, chunks, strategy, evaluation, dqr):
        """Rule-engine recommendations (delegates to _evaluate_support)."""
        return _evaluate_support.rules_recommendations(
            self,
            file_path=file_path,
            document=document,
            chunks=chunks,
            strategy=strategy,
            evaluation=evaluation,
            dqr=dqr,
        )

    @staticmethod
    def _failure_summary(chunks, document, file_path):
        """Retrieval failure summary (delegates to _evaluate_support)."""
        return _evaluate_support.failure_summary(chunks, document, file_path)

    def _compare_candidates(
        self, *, file_path, strategy, base_evaluation, rules_payload, compare_strategies
    ):
        """Candidate strategy comparison (delegates to _evaluate_support)."""
        return _evaluate_support.compare_candidates(
            self,
            file_path=file_path,
            strategy=strategy,
            base_evaluation=base_evaluation,
            rules_payload=rules_payload,
            compare_strategies=compare_strategies,
        )

    def _search_recommendations(self, file_path, suffix, document, evaluation, search_mode):
        """StrategyOptimizer exploration (delegates to _evaluate_support)."""
        return _evaluate_support.search_recommendations(
            self,
            file_path,
            suffix,
            document,
            evaluation,
            search_mode,
        )

    @staticmethod
    def _pdf_quality_context(document, suffix):
        """PDF input reliability context (delegates to _evaluate_support)."""
        return _evaluate_support.pdf_quality_context(document, suffix)
