"""Object-level unresolved registry shared infrastructure
(marker_contract.md §1).

Provides uniform registry construction and writing primitives for
all parsers across the five domains (narrative / structured_config /
spreadsheet / code / record_stream):

    - ``build_unresolved_record()``  build one record and validate
      required fields
    - ``stamp_unresolved_registry()`` idempotently write the
      metadata key (only when non-empty)
    - ``collect_unresolved_registry()`` gather all registry keys
      from metadata

Design stance:

    - This module provides only the mechanical "how to register"
      guarantees; the "what to register" business judgment of each
      domain (fact-source identification and derivation rules)
      belongs to that domain's parser.
    - The existing handwritten narrative implementations (pptx /
      docx / pdf) are NOT migrated to this module — migration is a
      separate explicit decision.
    - Failures degrade silently: the registry is a side-channel for
      facts — any invalid input only logs at debug level and skips
      that entry, never blocking the main parse (same discipline as
      provenance / ingestion_status).
    - Key naming and versioning follow marker_contract.md §1.1:
      ``metadata["<format>_unresolved"] = {"extractor":
      "<format>_unresolved_v1", "records": [...]}``, written only
      when non-empty (honest-absence discipline).
"""

import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

# marker_contract.md §1.2 — resolution value domain (v1 has three
# values; "complete" is not registered)
RESOLUTIONS = frozenset({"unsupported", "partial", "unresolved"})

# Required fields (§1.2 table); placeholder_text and format-specific
# evidence fields are optional
REQUIRED_FIELDS = (
    "object_type",
    "object_id",
    "resolution",
    "reason_code",
    "source",
)


def build_unresolved_record(
        object_type: str,
        object_id: str,
        resolution: str,
        reason_code: str,
        source: str,
        placeholder_text: Optional[str] = None,
        **evidence: Any,
) -> Optional[Dict[str, Any]]:
    """Build one object-level unresolved record with contract
    validation.

    Args:
        object_type: object type (chart / smartart / image /
            page_text / table / symbol / line / sheet / entry …;
            each domain freezes its value domain at adoption — no
            whitelist at this layer).
        object_id: stable locator (domain-specific schemes per
            marker_contract.md §1.3; this layer only requires a
            non-empty string).
        resolution: unsupported / partial / unresolved (validated
            against the value domain; an illegal value returns None
            with a debug log).
        reason_code: stable English code (each domain freezes its
            list at adoption).
        source: in-package locator such as part path / page number /
            line number.
        placeholder_text: raw text of the object's placeholder line
            in the body, or None when there is no placeholder line.
        **evidence: format-specific evidence fields (error /
            fallback_reason / not_extracted_reason …), attached
            as-is without validation.

    Returns:
        A contract-compliant record dict, or None on invalid input
        (a required field that is not a non-empty string, or an
        illegal resolution) — callers just check for None before
        appending; one bad record never spoils the batch (mechanical
        guarantee of the honest-absence discipline).
    """
    required = {
        "object_type": object_type,
        "object_id": object_id,
        "resolution": resolution,
        "reason_code": reason_code,
        "source": source,
    }
    for name, value in required.items():
        if not isinstance(value, str) or not value:
            _log.debug(
                "unresolved record rejected: field %r is %r",
                name, value,
            )
            return None
    if resolution not in RESOLUTIONS:
        _log.debug(
            "unresolved record rejected: resolution %r "
            "not in %s", resolution, sorted(RESOLUTIONS),
        )
        return None
    record: Dict[str, Any] = dict(required)
    record["placeholder_text"] = placeholder_text
    record.update(evidence)
    return record


def stamp_unresolved_registry(
        metadata: Dict[str, Any],
        key: str,
        records: Optional[List[Any]],
        extractor_version: str = "v1",
) -> bool:
    """Write the record list to ``metadata[key]`` (only when
    non-empty; idempotent).

    Key convention (marker_contract.md §1.1)::

        metadata["<format>_unresolved"] = {
            "extractor": "<format>_unresolved_v1",
            "records": [...],
        }

    Args:
        metadata: target Document.metadata (written in place).
        key: registry key name (e.g. ``code_unresolved`` /
            ``yaml_unresolved``; shares its root with the extractor
            name).
        records: record list; None / empty / all-invalid writes no
            key (honest absence: documents without unresolved
            objects get no field).
        extractor_version: extractor version suffix, default
            "v1".

    Returns:
        Whether a write actually happened. Single-writer
        assumption: each format's parser calls this once per parse;
        repeated calls overwrite with the latest records (safe for
        idempotent replay).
    """
    if not records:
        return False
    # Defensive: drop non-dict entries (one bad record never
    # spoils the batch), silently degrade
    valid = [r for r in records if isinstance(r, dict)]
    if not valid:
        _log.debug(
            "unresolved registry %r: no dict records, skip", key,
        )
        return False
    if not isinstance(key, str) or not key.endswith("_unresolved"):
        _log.debug(
            "unresolved registry key %r violates '<format>_unresolved' "
            "naming (marker_contract.md s1.1), skip", key,
        )
        return False
    metadata[key] = {
        "extractor": f"{key}_{extractor_version}",
        "records": valid,
    }
    return True


def collect_unresolved_registry(
        metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Gather all ``*_unresolved`` registry keys from metadata
    (read-only projection).

    Used by exporters / report writers / downstream consumers: same
    suffix rule as RepresentationExporter's
    ``unresolved_registry`` field — any new domain registry key is
    picked up automatically, no change needed here.

    Returns:
        A ``{key: registry_block}`` sub-dict; empty dict when there
        is nothing registered (callers decide field emission under
        the write-only-if-non-empty discipline).
    """
    if not metadata:
        return {}
    return {
        k: v for k, v in metadata.items()
        if isinstance(k, str) and k.endswith("_unresolved")
    }
