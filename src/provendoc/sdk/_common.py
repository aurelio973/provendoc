"""Shared SDK helpers: domain checks, suffix normalization, chunk counts.

Split from sdk/client.py; shared by client and its delegate modules
(keeps client <-> delegate imports acyclic).
"""

from pathlib import Path

from provendoc.base.models.representation_result import (
    RepresentationResult,
)
from provendoc.core.domain.domain_registry import (
    DOMAIN_CODE,
    classify_domain,
    is_quality_applicable,
)
from provendoc.core.pipeline.parser_registry import (
    BARE_NAME_CANONICAL_SUFFIXES,
    DOTFILE_CANONICAL_NAMES,
    compound_suffix_of,
    drop_in_suffix_of,
    is_env_variant_name,
)

# ==================================
# ingestion domain classification
# ==================================
#
# Domain classification is defined in
# core/domain/domain_registry.py — the single source
# of truth.  This module reads from the registry
# instead of maintaining its own suffix list.
# ==================================


def _is_narrative(suffix):
    """Return True if this suffix belongs to the
    narrative domain (quality-evaluation-eligible)."""
    return is_quality_applicable(suffix)


def _is_code(suffix):
    """Return True when a suffix uses the code-domain pipeline."""
    return classify_domain(suffix) == DOMAIN_CODE


def _ingestion_mode(suffix):
    """Return the public pipeline mode for a classified suffix."""
    if _is_code(suffix):
        return "code"
    return "narrative" if _is_narrative(suffix) else "structured"


def _suffix_of(file_path):
    """Lowercased suffix; extension-less files normalize by file name:
    dotfiles (``.env`` and the INI-dialect family ``.gitconfig`` etc.)
    map to their own name, bare code filenames (``Dockerfile``,
    ``Makefile``) map via ``BARE_NAME_CANONICAL_SUFFIXES`` — because
    ``Path.suffix`` is empty for all of them. Same normalization as
    ``core/pipeline/universal_parser.py``; the recognized names are
    single-sourced in ``parser_registry``.

    Full-name canonical hits precede the empty-suffix check —
    ``Cargo.lock`` has suffix ``.lock`` (not empty) and routes to
    the TOML parser via its full lowercased name. Dotenv-family
    variant names (``.env.local`` / ``.env.production`` /
    ``.env.example``) normalize by the ``.env.`` name prefix to
    canonical ``.env``; ``.envrc`` (no dot separator) does not
    match the prefix but is canonical in its own right
    (``DOTFILE_CANONICAL_NAMES`` full-name hit -> EnvrcParser).
    """
    path = Path(file_path)
    suffix = path.suffix.lower()
    name = path.name.lower()
    # systemd drop-in parent-dir gating comes first: for
    # ``example.service.d/override.conf`` Path.suffix is ".conf";
    # it must normalize by parent dir to canonical
    # ".service"/".timer" before the table lookup. Plain .conf is
    # unaffected (the gate only fires inside ``*.service.d``/
    # ``*.timer.d``). Same normalization chain/order as
    # universal_parser.parse; drop_in_suffix_of is the shared
    # single source of truth.
    drop_in = drop_in_suffix_of(file_path)
    # compound-suffix normalization precedes dotfile/env-variant/
    # single-suffix returns: ``main.tf.json`` has Path.suffix
    # ".json"; the compound lookup first maps it to canonical
    # ".tf.json" (plain .json/.hcl/.tf/.tfvars routing unaffected).
    compound = compound_suffix_of(name)
    if drop_in is not None:
        return drop_in
    if compound is not None:
        return compound
    if name in DOTFILE_CANONICAL_NAMES:
        return name
    if is_env_variant_name(name):
        return ".env"
    if not suffix:
        return BARE_NAME_CANONICAL_SUFFIXES.get(name, suffix)
    return suffix


def _chunk_count(result: RepresentationResult) -> int:
    """Read modern ``chunk_count`` or legacy ``unit_count`` metadata.

    Narrative representations (txt/markdown/pdf/docx and the
    tex/pptx/epub family) report ``base_units`` instead;
    config/spreadsheet report ``chunk_count``/``unit_count``.
    """
    return result.metadata.get(
        "chunk_count",
        result.metadata.get(
            "unit_count",
            result.metadata.get("base_units", 0),
        ),
    )


def cleaning_applied_flag(document) -> bool:
    """Read the honest cleaning flag from the document's cleaning trace.

    ``document.metadata["cleaning"]`` is the single source of truth
    written by ``_clean_document_for_domain`` / the pipeline cleaning
    layer.  Two trace shapes exist:

    * narrative/code: ``{"cleaning_applied": True, ...}`` (from
      ``CleaningResult.to_document_metadata``)
    * spreadsheet: ``{"applied": False, "reason": ...}`` (canvas
      facts are authoritative — no text transform may be presented
      as a canvas transform)

    The top-level ``cleaning_applied`` flag is derived from the
    trace; when the trace is missing (no cleaning happened) it
    honestly returns False.
    """
    cleaning = (getattr(document, "metadata", None) or {}).get(
        "cleaning"
    ) or {}
    return bool(
        cleaning.get("cleaning_applied", cleaning.get("applied", False))
    )
