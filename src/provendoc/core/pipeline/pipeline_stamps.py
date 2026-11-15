"""Unified runner for the cross-cutting stamp sequence + degradation fact codes.

Previously the cross-cutting stamp chain (ingestion_status ->
engineering_keys -> content_completeness -> input_integrity ->
sensitive_candidates -> dual_naming -> content_sources ->
[config_validation] -> security_defaults -> [scenario]) was
hand-copied at four positions — the UniversalParser.parse and sdk
client.parse entry points x the normal/mismatch paths — kept in
sync only by comment convention; and each stamp module silently
swallowed errors in its internal ``except Exception``, so failure
and honest absence were indistinguishable on the output surface.
This module fixes both:

- ``run_stamp_sequence``: the single source of truth for the
  sequence, shared by all four positions; the existing differences
  (the client has no configuration policy parameter source ->
  config_validation skipped; the mismatch path has no scenario) are
  expressed explicitly via StampContext flags, not erased.
- ``record_degradation``: the additive fact-code
  ``metadata["pipeline_degraded"]`` — key absent = no degradation;
  key present = a degradation happened and the events are
  enumerable. Both the swallow points inside the stamp modules and
  the runner's defense line register, making failures observable.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from provendoc.base.models.document import Document
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.pipeline.format_verification import (
    format_mismatch_error,
)
from provendoc.core.pipeline.log_hygiene import sanitize_log_value

_log = logging.getLogger(__name__)

#: Metadata key for the degradation fact code. Absent = no
#: degradation; present = degradation events exist.
DEGRADED_METADATA_KEY = "pipeline_degraded"

#: Event-count cap (bomb-proofing: the stamp sequence must not grow
#: metadata without bound on pathological input).
_MAX_DEGRADATION_EVENTS = 20


def record_degradation(document: Any, component: str, exc: BaseException) -> None:
    """Register one pipeline degradation event (additive; not idempotent-safe — one entry per failure).

    Shape: ``metadata["pipeline_degraded"] =
    {"extractor": "pipeline_degradation_v1", "events": [...]}``,
    each event ``{component, error_type, message}``. Past the cap
    ``truncated: True`` is set and nothing more is appended (the
    cap itself is a fact, disclosed honestly). Documents without
    dict metadata are silently dropped (the registration itself is
    never fatal).
    """
    metadata = getattr(document, "metadata", None)
    if not isinstance(metadata, dict):
        return
    ledger = metadata.setdefault(
        DEGRADED_METADATA_KEY,
        {"extractor": "pipeline_degradation_v1", "events": []},
    )
    events = ledger.setdefault("events", [])
    if len(events) >= _MAX_DEGRADATION_EVENTS:
        ledger["truncated"] = True
        return
    events.append(
        {
            "component": component,
            "error_type": type(exc).__name__,
            "message": str(exc)[:200],
        }
    )


@dataclass(frozen=True)
class StampContext:
    """Call context for the stamp sequence (explicit expression of the frozen difference surface).

    file_path: source file path (needed by engineering_keys /
        dual_naming).
    parser_name: the parser class name on the normal path; None on
        the mismatch path (the parser never ran — honest absence).
    configuration_parameters: the pipeline-level effective-config
        snapshot; None = skip config_validation (the sdk client has
        no configuration policy parameter source — an intentional
        difference, not an omission).
    include_scenario: whether to stamp the scenario key (True on
        the normal path, False on the mismatch path — scenario
        needs the parse-produced domain_signals, which mismatch
        documents do not have).
    include_understanding: whether to additionally stamp the
        on-demand task-layer understanding (the
        scenario_understanding key, 2026-11-08 opt-in). Default
        False = a clean zero-difference; only meaningful when
        include_scenario is set (mismatch documents have no parse
        output, nothing to understand).
    """

    file_path: Optional[str] = None
    parser_name: Optional[str] = None
    configuration_parameters: Optional[Dict[str, Any]] = None
    include_scenario: bool = False
    include_understanding: bool = False


def _stamp_step(document: Any, component: str, fn) -> None:
    """Execute one step + defense line two: exceptions the stamper did not swallow itself are caught and registered here.

    Existing stamp modules already swallow internally (each has its
    own ``except Exception`` -> return None); this layer covers
    future stampers and anything beyond the existing modules'
    Exception handling; no stamp failure may drag down the parsing
    main flow.
    """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — 观测盖章绝不拖垮解析
        record_degradation(document, component, exc)
        _log.debug("stamp step %s failed", component, exc_info=True)


def run_stamp_sequence(document: Any, context: StampContext) -> None:
    """Run the cross-cutting stamp sequence in its frozen order (single source of truth).

    Order (measured and frozen; identical for both entry points):
    ingestion_status -> engineering_keys -> content_completeness ->
    input_integrity -> sensitive_candidates -> dual_naming ->
    content_sources -> [config_validation] -> security_defaults ->
    [scenario]. Bracketed items are gated by context flags.
    """
    from provendoc.core.pipeline.config_validation import (
        stamp_config_validation,
    )
    from provendoc.core.pipeline.content_completeness import (
        stamp_content_completeness,
    )
    from provendoc.core.pipeline.content_source import (
        stamp_content_sources,
    )
    from provendoc.core.pipeline.dual_naming import (
        stamp_dual_naming,
    )
    from provendoc.core.pipeline.engineering_keys import (
        stamp_engineering_keys,
    )
    from provendoc.core.pipeline.ingestion_status import (
        stamp_ingestion_status,
    )
    from provendoc.core.pipeline.input_integrity import (
        stamp_input_integrity,
    )
    from provendoc.core.pipeline.security_defaults import (
        stamp_security_defaults,
    )
    from provendoc.core.pipeline.sensitive_candidates import (
        stamp_sensitive_candidates,
    )

    # Credibility contract: summarize scattered evidence ->
    # metadata["ingestion_status"]; the FAILED view is stamped with
    # the same criteria.
    _stamp_step(
        document, "ingestion_status", lambda: stamp_ingestion_status(document)
    )
    # Four engineering keys, domain-wide sync: fill-gap (native
    # txt/code keys are never overwritten).
    _stamp_step(
        document,
        "engineering_keys",
        lambda: stamp_engineering_keys(
            document, context.file_path, context.parser_name
        ),
    )
    # Loss-cause/coverage contract: additive, idempotent, non-empty
    # writes only.
    _stamp_step(
        document,
        "content_completeness",
        lambda: stamp_content_completeness(document),
    )
    # Input integrity contract: input-level truncation/concatenation
    # fact codes.
    _stamp_step(
        document, "input_integrity", lambda: stamp_input_integrity(document)
    )
    # Sensitive-candidate detection: identify, never mask; no values
    # stored; non-empty writes only.
    _stamp_step(
        document,
        "sensitive_candidates",
        lambda: stamp_sensitive_candidates(document),
    )
    # Dual naming: display-name normalization + content-addressed
    # internal name.
    _stamp_step(
        document,
        "dual_naming",
        lambda: stamp_dual_naming(document, context.file_path),
    )
    # Content sources: per-category extract/ignore boundary
    # declaration.
    _stamp_step(
        document, "content_sources", lambda: stamp_content_sources(document)
    )
    # Config validation: stamped only when the caller has a
    # configuration policy parameter source (the universal pipeline
    # has one; the sdk client does not — an intentional
    # difference).
    if context.configuration_parameters is not None:
        _stamp_step(
            document,
            "config_validation",
            lambda: stamp_config_validation(
                document, context.configuration_parameters
            ),
        )
    # Security defaults: frozen safe-default fingerprint (compact
    # reference).
    _stamp_step(
        document,
        "security_defaults",
        lambda: stamp_security_defaults(document),
    )
    # L2 scenario recognition: single source of truth
    # core/scenario/frameworks.stamp_scenario; parsers outside the
    # narrative domain or without domain_signals stamped produce no
    # scenario key (absence is not failure). Mismatch documents
    # have no parse output and are not stamped (context-gated).
    if context.include_scenario:

        def _scenario_step() -> None:
            from provendoc.core.scenario.frameworks import (
                stamp_scenario,
            )

            stamp_scenario(
                document,
                include_understanding=context.include_understanding,
            )

        _stamp_step(document, "scenario", _scenario_step)


def build_format_mismatch_document(file_path, suffix, verification) -> "Document":
    """The honest-failure document for a format magic mismatch (both entry points share this constructor).

    When the declared suffix disagrees with the actual container
    family: the evidence is fully registered (format_verification +
    parse_error), the file is never handed to the declared format's
    parser; the warning is raised here uniformly (so callers do not
    each improvise one). The metadata construction matches the
    previous per-entry-point implementations field by field.
    """
    metadata = _build_metadata(file_path)
    metadata["file_type"] = suffix.lstrip(".")
    metadata["parse_failed"] = True
    metadata["parse_error"] = format_mismatch_error(verification)
    metadata["format_verification"] = verification
    _log.warning(
        "Format verification failed for %s: %s",
        sanitize_log_value(file_path),
        sanitize_log_value(metadata["parse_error"]),
    )
    return Document(text="", metadata=metadata)


#: Missing module name (first segment of the ImportError message) → pip extra.
#: The installable surface follows pyproject's [project.optional-dependencies]
#: and the README Install section; unmapped → generic guidance (a hint, not a promise).
_MISSING_MODULE_EXTRA_HINTS = {
    "pymupdf": "pdf",
    "fitz": "pdf",
    "docx": "docx",
    "olefile": "doc",
    "pyxlsb": "spreadsheet",
    "odfpy": "spreadsheet",
    "pyarrow": "parquet",
}


def _missing_module_name(import_error: BaseException) -> str:
    """Extract the missing module name from the ImportError message ("No module named 'x'")."""
    match = re.search(r"No module named '([^']+)'", str(import_error))
    return match.group(1) if match else ""


def build_backend_missing_document(
    file_path, suffix, import_error
) -> "Document":
    """Honest-failure document for a missing optional parser backend
    (SDK entry points).

    When the suffix is registered but its optional dependency is not
    installed (parsing .pdf/.docx/.xlsb etc. under a core-only install:
    the parser module's top-level import raises ImportError), the SDK
    parse chain never crashes with an exception (README/NOTICE
    honest-degradation contract: auditable fallback, never a crash):
    parse_failed + parse_error (missing module and install guidance)
    are recorded in metadata and the warning is emitted here. Same
    construction contract as build_format_mismatch_document — the
    parser never ran, the text is honestly empty, no speculative
    extraction; the install guidance maps the missing module to a pip
    extra, degrading to generic guidance when unmapped.
    """
    metadata = _build_metadata(file_path)
    module = _missing_module_name(import_error)
    extra = _MISSING_MODULE_EXTRA_HINTS.get(module)
    hint = (
        f'pip install "provendoc[{extra}]" (or provendoc[all])'
        if extra
        else "see the README install section for the matching pip extra"
    )
    metadata["file_type"] = suffix.lstrip(".")
    metadata["parse_failed"] = True
    metadata["parse_error"] = (
        f"optional parser backend for {suffix!r} is not installed "
        f"(missing module: {module or 'unknown'}); "
        f"install it and retry: {hint}"
    )
    metadata["missing_backend"] = {
        "missing_module": module or "unknown",
        "pip_extra": extra,
        "install_hint": hint,
    }
    _log.warning(
        "Optional parser backend missing for %s: %s",
        sanitize_log_value(file_path),
        sanitize_log_value(metadata["parse_error"]),
    )
    return Document(text="", metadata=metadata)
