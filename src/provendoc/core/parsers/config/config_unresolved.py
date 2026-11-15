# -*- coding: utf-8 -*-
"""Config-domain object-level unresolved-registry derivation.

Derived-view discipline:

- No new detection logic — every record derives 1:1 from
  existing fact sources: F1 the contract's
  ``entries[*].references[*].resolution_status`` three states
  (unresolved / cyclic / external), F2 shadowed duplicate
  occurrences in last_wins families (scalar elements of
  ``override_chain[:-1]``), F3 yaml tolerant-mode failed
  document chunks (``_error`` dicts inside the ``documents``
  list);
- Existing fact-source keys are kept verbatim, never rewritten
  or migrated (``config_entries`` and ``structured_data`` stay
  byte-identical; the single source of truth stays in the
  original keys — this module produces the object-level detail
  view under the marker_contract v1 contract);
- Records are built via
  ``core.foundation.unresolved_registry.build_unresolved_record``
  (contract validation: one bad record never spoils the batch);
  stamping is done by the caller via
  ``stamp_unresolved_registry`` (non-empty writes only, honest
  absence);
- ``placeholder_text`` is always ``None`` (the source text stays
  fully in ``Document.text``, no placeholder lines — the PDF
  precedent);
- Failures degrade silently: any unexpected shape (entry not a
  dict / path_str not a string / unknown status / unexpected
  chain shape) is only defensively skipped with a debug log,
  never blocking the main parse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
)
from provendoc.core.parsers.config.contracts import (
    DUP_LAST_WINS,
    REFERENCE_RESOLUTION_STATUSES,
)

_log = logging.getLogger(__name__)

# error_summary truncation matches jsonl / txt / epub (120 chars)
_ERROR_SUMMARY_MAX = 120

# resolution_status → (resolution, reason_code) mapping. Of the
# five states, resolved / dynamic / not_applicable produce no
# record; unknown statuses (values outside the closed frozenset)
# are defensively skipped with a debug log, not run through
# MALFORMED — a new status must first extend the closed set
# (add-only) before entering the map.
_REF_STATUS_MAP = {
    "unresolved": ("unresolved", "REFERENCE_UNRESOLVED"),
    "cyclic": ("unresolved", "REFERENCE_CYCLIC"),
    "external": ("unsupported", "REFERENCE_EXTERNAL"),
}

# Shadowed occurrences whose chain value is a container summary
# are not registered: the summary shape is the closed product of
# yaml _node_value_repr / json _chain_value, the child content
# is unrecoverable, so the partial-tier semantics do not apply.
_CONTAINER_SUMMARY_KINDS = ("dict", "list")


def _is_container_summary(value: Any) -> bool:
    """Whether a chain value is a container summary
    (``{"kind", "child_count"}`` shape)."""
    if not isinstance(value, dict):
        return False
    if set(value.keys()) != {"kind", "child_count"}:
        return False
    return value.get("kind") in _CONTAINER_SUMMARY_KINDS


def _doc_index_of(entry: Dict[str, Any]) -> int:
    """Entry doc_index (unexpected shapes fall back to 0, the
    contract make_entry default)."""
    idx = entry.get("doc_index", 0)
    if isinstance(idx, bool) or not isinstance(idx, int):
        return 0
    return idx


def _int_or_none(value: Any) -> Optional[int]:
    """Defensive narrowing for line numbers (a bool is not a line
    number)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _derive_references(contract: Any) -> List[Dict[str, Any]]:
    """F1: reference three-states → REFERENCE_* records (entry
    order, original order within an entry)."""
    records: List[Dict[str, Any]] = []
    if not isinstance(contract, dict):
        return records
    entries = contract.get("entries")
    if not isinstance(entries, list):
        return records
    fmt = contract.get("format")
    fmt = fmt if isinstance(fmt, str) else None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path_str")
        if not isinstance(path, str) or not path:
            continue
        doc_index = _doc_index_of(entry)
        line = _int_or_none(entry.get("line_start"))
        refs = entry.get("references")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            syntax = ref.get("syntax")
            if not isinstance(syntax, str) or not syntax:
                continue
            status = ref.get("resolution_status")
            # isinstance narrowing guarantees the lookup key is a
            # str (bad shapes skipped first)
            mapped = (
                _REF_STATUS_MAP.get(status)
                if isinstance(status, str) else None
            )
            if mapped is None:
                if (
                    isinstance(status, str)
                    and status not in REFERENCE_RESOLUTION_STATUSES
                ):
                    _log.debug(
                        "config unresolved: unknown reference "
                        "status %r skipped", status,
                    )
                continue
            resolution, reason_code = mapped
            target = ref.get("target")
            record = build_unresolved_record(
                object_type="reference",
                object_id=f"doc-{doc_index}:{path}::{syntax}",
                resolution=resolution,
                reason_code=reason_code,
                source=path,
                placeholder_text=None,
                target=target if isinstance(target, str) else None,
                doc_index=doc_index,
                format=fmt,
                line=line,
            )
            if record is not None:
                records.append(record)
    return records


def _derive_shadowed_duplicates(contract: Any) -> List[Dict[str, Any]]:
    """F2: shadowed scalar occurrences in last_wins families →
    DUPLICATE_KEY_SHADOWED records."""
    records: List[Dict[str, Any]] = []
    if not isinstance(contract, dict):
        return records
    if contract.get("duplicate_key_semantics") != DUP_LAST_WINS:
        # ini (override_chain, no shadowing) and toml/xml
        # (rejected, document-level illegal) do not derive.
        return records
    entries = contract.get("entries")
    if not isinstance(entries, list):
        return records
    fmt = contract.get("format")
    fmt = fmt if isinstance(fmt, str) else None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not entry.get("duplicate"):
            continue
        path = entry.get("path_str")
        if not isinstance(path, str) or not path:
            continue
        chain = entry.get("override_chain")
        if not isinstance(chain, list) or len(chain) < 2:
            # chain missing / unexpected shape — the shadowed
            # occurrences cannot be located, honest absence
            continue
        doc_index = _doc_index_of(entry)
        for k, occ in enumerate(chain[:-1]):
            if not isinstance(occ, dict):
                continue
            value = occ.get("value")
            if _is_container_summary(value):
                # container-summary occurrences are not registered
                continue
            occ_line = _int_or_none(occ.get("line"))
            occ_locator = (
                f"line-{occ_line}" if occ_line is not None else f"occ-{k}"
            )
            record = build_unresolved_record(
                object_type="entry",
                object_id=f"doc-{doc_index}:{path}::{occ_locator}",
                resolution="partial",
                reason_code="DUPLICATE_KEY_SHADOWED",
                source=path,
                placeholder_text=None,
                value=value,
                line=occ_line,
                doc_index=doc_index,
                format=fmt,
            )
            if record is not None:
                records.append(record)
    return records


def _derive_yaml_chunks(
    contract: Any, yaml_documents: Any,
) -> List[Dict[str, Any]]:
    """F3: yaml tolerant/strict failed chunks → YAML_DOC_CHUNK_FAILED
    records."""
    records: List[Dict[str, Any]] = []
    if not isinstance(contract, dict):
        return records
    if contract.get("format") != "yaml":
        # F3 is yaml-only — for the other formats a whole-document
        # failure is a document-level fact, reported via the
        # parse_success/parse_error channels.
        return records
    if not isinstance(yaml_documents, list):
        return records
    for i, chunk in enumerate(yaml_documents):
        if not isinstance(chunk, dict):
            continue
        error = chunk.get("_error")
        if not isinstance(error, str) or not error:
            continue
        partial = chunk.get("_partial_data")
        partial_count = len(partial) if isinstance(partial, dict) else 0
        record = build_unresolved_record(
            object_type="document",
            object_id=f"doc-{i}",
            resolution="partial" if partial_count else "unresolved",
            reason_code="YAML_DOC_CHUNK_FAILED",
            source="document",
            placeholder_text=None,
            format="yaml",
            error_summary=error[:_ERROR_SUMMARY_MAX],
            partial_key_count=partial_count,
        )
        if record is not None:
            records.append(record)
    return records


def derive_config_unresolved(
    contract: Any,
    yaml_documents: Any = None,
) -> List[Dict[str, Any]]:
    """Derive unresolved-registry records from existing fact
    sources.

    Args:
        contract: the ``metadata["config_entries"]`` contract dict
            (config_entry_v1.1). F1 (reference three-states) and
            F2 (shadowed scalar occurrences in last_wins
            families) derive from it; unexpected shapes are
            defensively skipped.
        yaml_documents: passed by the yaml parser only — the
            document-chunk list produced by ``_load_yaml``
            (successful chunks mixed with failed
            ``{"_error", "_partial_data"}`` chunks; on strict
            failure it is ``[{"_error": str}]``). The other
            formats never pass it (F3 is frozen as yaml-only).

    Returns:
        Contract-compliant record list, order frozen as
        F1 → F2 → F3, original fact-source order preserved within
        each group (may be empty — the caller writes only
        non-empty via ``stamp_unresolved_registry``).
    """
    return (
        _derive_references(contract)
        + _derive_shadowed_duplicates(contract)
        + _derive_yaml_chunks(contract, yaml_documents)
    )
