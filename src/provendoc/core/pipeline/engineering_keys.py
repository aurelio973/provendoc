# -*- coding: utf-8 -*-
"""Engineering keys — parser-level four-key stamp applied domain-wide (cross-cutting stamp).

Baseline and precedent:

- Baseline keys = the narrative(txt) four-key set:
  ``parse_status`` / ``decode_errors`` / ``output_views`` /
  ``parse_config``;
- First sync precedent = the code domain (single-point wiring in
  BaseCodeParser); this batch generalizes the same contract to every
  domain, moving the wiring point from inside the parser to the
  pipeline layer (the ingestion_status precedent).

Design rules (same as ingestion_status):

- **fill-gap only**: existing keys are never overwritten — the
  native four keys of the txt/code parsers (with their richer
  parse_config / output_views) stay as-is; the cross-cutting layer
  only fills gaps. Idempotent (rerun on the same input yields the
  same facts).
- **Summarize, do not rebuild**: ``parse_status`` is a derived view
  over already-registered evidence keys (no new detection logic);
  the md/tex/rst prefixed three-state keys (finer-grained) are
  untouched, the bare key is the unified summary view — the same
  layering as ``parse_status`` vs ``ingestion_status`` (parser-level
  lowercase three states + fine-grained reasons; pipeline-level
  uppercase credibility, each with its own job).
- **Honest absence**: no ``encoding_used`` (binary-container domains;
  record_stream decodes on its own and registers nothing) -> no
  ``decode_errors`` key; no ``Document.source_text``
  (docx/pdf/pptx/xlsx/CFB/image families) -> ``output_views``
  declares only structured/display and states why raw is not
  addressable, never faking it with false/None. Dimensions the
  pipeline layer cannot know (tolerant, in-domain probe parameters)
  are not written into ``parse_config``.
- **additive, fail-silent**: any sub-key failure only logs at debug
  and never blocks ingestion (same as provenance / doc_identity /
  ingestion_status).

``parse_status`` reasons, frozen order (derivation criteria,
domain-wide summary):

- ``failed``  — ``parse_failed`` truthy (overall failure fallback);
- ``partial`` — any evidence present: ``truncation.truncated``,
  ``encoding_fallback``, ``sanitization.replacement_injected>0``,
  ``invalid_line_count>0``, ``oversized_line_count>0``, any
  non-empty ``*_unresolved`` registry, any prefixed three-state key
  (markdown/tex/rst_parse_status) reporting partial;
- ``complete`` — none of the above.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

# Prefixed three-state view keys (md/tex/rst native fine-grained;
# read-only during summary)
_PREFIXED_STATUS_KEYS = (
    "markdown_parse_status",
    "tex_parse_status",
    "rst_parse_status",
)

# Object-level unresolved-registry keys (per-domain prefixes; a
# non-empty records list is partial evidence). Registry record
# shape: ``{format, records}``.
_UNRESOLVED_KEYS = (
    "txt_unresolved",
    "code_unresolved",
    "config_unresolved",
    "jsonl_unresolved",
    "spreadsheet_unresolved",
    "epub_unresolved",
)

# raw-view guarantees (same wording as the txt baseline; applies to
# every parser that produces Document.source_text — the raw view's
# nature is decided by the shared decode/extraction layer, not by
# the domain)
_RAW_GUARANTEES = (
    "no_line_merging",
    "no_indent_stripping",
    "no_zero_width_removal",
    "no_transcoding",
    "no_value_rewriting",
    "no_deduplication",
)


def _derive_parse_status(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Registered evidence keys -> document-level three-state summary view (frozen reasons order)."""
    reasons = []

    truncation = metadata.get("truncation")
    if isinstance(truncation, dict) and truncation.get("truncated"):
        reasons.append("truncation")

    if metadata.get("encoding_fallback"):
        reasons.append("encoding_fallback")

    sanitization = metadata.get("sanitization")
    if isinstance(sanitization, dict) and (
        sanitization.get("replacement_injected") or 0
    ) > 0:
        reasons.append("replacement_injected")

    if metadata.get("invalid_line_count"):
        reasons.append("invalid_lines")

    if metadata.get("oversized_line_count"):
        reasons.append("oversized_lines")

    for key in _UNRESOLVED_KEYS:
        record = metadata.get(key)
        if isinstance(record, dict):
            if record.get("records"):
                reasons.append("unresolved")
                break
        elif record:
            reasons.append("unresolved")
            break

    for prefix in _PREFIXED_STATUS_KEYS:
        view = metadata.get(prefix)
        if isinstance(view, dict) and view.get("status") == "partial":
            reasons.append(f"{prefix}_partial")

    if metadata.get("parse_failed"):
        return {"status": "failed", "reasons": ["parse_failed", *reasons]}
    if reasons:
        return {"status": "partial", "reasons": reasons}
    return {"status": "complete", "reasons": []}


def _build_decode_errors(
    file_path: Optional[str],
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """``encoding_used`` present -> reuse the strict txt re-decode; otherwise no key.

    Binary-container domains (docx/pdf/pptx/xlsx/...) and
    record_stream (decodes on its own, no encoding evidence
    registered) are honestly absent — without encoding facts there
    is no re-decode criterion to speak of.
    """
    encoding_used = metadata.get("encoding_used")
    if not encoding_used or not file_path:
        return None
    from provendoc.core.parsers.narrative.txt_decode_errors import (
        collect_decode_errors,
    )

    return collect_decode_errors(file_path, encoding_used)


def _build_output_views(document: Any) -> Dict[str, Any]:
    """Declare output views per the Document's actual capabilities (raw declared only when addressable)."""
    has_raw = getattr(document, "source_text", None) is not None
    if has_raw:
        return {
            "raw": "source_text",
            "structured": "text",
            "display": "text",
            "raw_guarantees": list(_RAW_GUARANTEES),
            "note": (
                "raw view = faithful decoded byte stream "
                "(pre-transform); truly-raw bytes remain addressable "
                "via metadata.sha256 of the source file; display "
                "view = sanitized readable text (Document.text); "
                "structured view = representation-layer units"
            ),
        }
    return {
        "structured": "text",
        "display": "text",
        "note": (
            "raw view not addressable: this parser emits no "
            "Document.source_text (binary/container-extraction "
            "domain); truly-raw bytes remain addressable via "
            "metadata.sha256 of the source file"
        ),
    }


def _build_parse_config(
    metadata: Dict[str, Any],
    parser_name: Optional[str],
) -> Dict[str, Any]:
    """Pipeline-layer config snapshot: same file + same config = same facts.

    Only pipeline-visible dimensions are recorded; in-domain parser
    parameters (tolerant, probe limits, ...) are unknowable at this
    layer and honestly omitted (the richer native txt/code
    parse_config survives via fill-gap and is not faked by this
    key).
    """
    from provendoc import __version__

    effective: Dict[str, Any] = {
        # The parser name prefers the call-site argument (the parser
        # object's class name; metadata["parser"] is not yet written
        # for some domains at stamping time); the FAILED-mismatch
        # path has no parser at all -> None (honest absence).
        "parser": parser_name or metadata.get("parser"),
        "parser_version": __version__,
        "layer": "pipeline_stamp",
    }
    if metadata.get("encoding_used"):
        effective["encoding_used"] = metadata.get("encoding_used")
    canonical = json.dumps(effective, sort_keys=True, ensure_ascii=True)
    effective["config_hash"] = hashlib.sha256(
        canonical.encode("ascii")
    ).hexdigest()[:16]
    return effective


def stamp_engineering_keys(
    document: Any,
    file_path: Optional[str] = None,
    parser_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fill-gap stamp the four-key set; returns the keys actually written this round (nothing written -> {}).

    ``parser_name``: the normal path passes the parser class name
    (the parser object is in scope at the call site); the
    FAILED-mismatch path has no parser and passes none (honest
    None). Idempotent (gap keys only; existing keys are never
    touched); failures return None and never block the main flow
    (same discipline as ingestion_status).
    """
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        stamped: Dict[str, Any] = {}

        if "parse_status" not in metadata:
            metadata["parse_status"] = _derive_parse_status(metadata)
            stamped["parse_status"] = metadata["parse_status"]

        if "decode_errors" not in metadata:
            decode_errors = _build_decode_errors(file_path, metadata)
            if decode_errors is not None:
                metadata["decode_errors"] = decode_errors
                stamped["decode_errors"] = decode_errors

        if "output_views" not in metadata:
            metadata["output_views"] = _build_output_views(document)
            stamped["output_views"] = metadata["output_views"]

        if "parse_config" not in metadata:
            metadata["parse_config"] = _build_parse_config(
                metadata, parser_name
            )
            stamped["parse_config"] = metadata["parse_config"]

        return stamped
    except Exception as exc:
        _log.debug("Engineering keys stamping failed", exc_info=True)
        record_degradation(document, "engineering_keys", exc)
        return None


__all__ = [
    "stamp_engineering_keys",
]
