# -*- coding: utf-8 -*-
"""Consolidated security-defaults declaration.

Freezes the repository's safe-by-default posture into a single
machine-readable statement and binds a compact reference to it on every
parsed document, so the defaults in force at processing time are
auditable per document.

Design points:

- ``describe_security_defaults()`` aggregates security defaults from
  across the repository into one machine-readable statement — but it is
  **not a second source of truth**: every numeric field references the
  real constants from the owning modules (font limit from
  ingestion_status, ZIP limits from container_guard, configuration
  ceilings from config_validation, format whitelist from
  parser_registry). Reconciliation tests freeze their consistency
  (same discipline as doc_freshness).
- "Disabled by default" entries are recorded per this library's actual
  shape as **structural disablement** (no code path exists, so no
  configuration could ever turn them on) — a stronger claim than
  "off by default".
- ``compute_security_defaults_hash()`` derives a content-addressed
  fingerprint of the whole statement; ``stamp_security_defaults`` stamps
  a compact ``scheme + profile + hash`` reference on each parsed
  result, so a single document can audit which defaults contract it
  was processed under without duplicating the full statement (avoids
  metadata bloat).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict

from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

SECURITY_DEFAULTS_SCHEME = "security_defaults_v1"
SECURITY_DEFAULTS_PROFILE = "local_library_v1"

# Semantic defaults (non-numeric, cannot drift): structural facts of
# how this library runs.
_SECURITY_POSTURE = {
    # Network access: the parsing path contains no network code at all
    # (external renderers such as MinerU are explicit opt-in standalone
    # integrations, not on the default parsing path).
    "network_access": "structurally_disabled",
    "script_execution": "structurally_disabled",
    "macro_execution": "structurally_disabled",
    "external_entities": "structurally_disabled",
    "external_attachments": "not_extracted",
    "formula_recalculation": "disabled_evidence_only",
    "automatic_retry": "no_infinite_retry",
    "failure_discipline": "honest_failure_evidence_only",
    "temp_files": "no_persistent_intermediate_files_by_default",
    "privileged_execution": "not_applicable_local_library",
}


def describe_security_defaults() -> Dict[str, Any]:
    """Aggregate repository-wide security defaults into one machine-readable statement.

    Returns:
        dict with ``scheme`` / ``profile`` / ``format_whitelist`` /
        ``posture`` / numeric limit snapshots (font_limit /
        zip_package_limits / configuration_ceilings).
    """
    # Numeric fields all reference the real constants — this function
    # is not a second source of truth.
    from provendoc.core.parsers.container_guard import (
        DEFAULT_ZIP_LIMITS,
        GUARD_VERSION,
    )
    from provendoc.core.pipeline.config_validation import (
        CONFIGURATION_CEILINGS,
    )
    from provendoc.core.pipeline.ingestion_status import (
        DEFAULT_FONT_LIMIT,
    )
    from provendoc.core.pipeline.parser_registry import (
        supported_suffixes,
    )

    return {
        "scheme": SECURITY_DEFAULTS_SCHEME,
        "profile": SECURITY_DEFAULTS_PROFILE,
        "format_whitelist": sorted(supported_suffixes()),
        "posture": dict(_SECURITY_POSTURE),
        "font_limit": DEFAULT_FONT_LIMIT,
        "zip_package_limits": {
            "guard_version": GUARD_VERSION,
            **DEFAULT_ZIP_LIMITS.as_dict(),
        },
        "configuration_ceilings": dict(CONFIGURATION_CEILINGS),
    }


def compute_security_defaults_hash() -> str:
    """Content-addressed fingerprint of the defaults statement (sha256[:16], key order normalized).

    Binds the security-defaults contract in force at processing time
    to every parsed result — any field drift in the statement changes
    the hash (reconciliation tests freeze the current value).
    """
    payload = json.dumps(
        describe_security_defaults(), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def stamp_security_defaults(document) -> Dict[str, Any] | None:
    """Idempotently stamp ``metadata["security_defaults"]`` (compact reference; silent on failure).

    Stamps only scheme / profile / defaults_hash — the full statement
    is fetched on demand via ``describe_security_defaults()`` and never
    copied into per-document metadata (avoids the 40+ suffix whitelist
    bloating per-document metadata).

    Args:
        document: Document instance (must carry dict-shaped metadata).

    Returns:
        The compact reference dict; None if the document shape is wrong.
    """
    try:
        metadata = document.metadata
        if not isinstance(metadata, dict):
            return None
        if "security_defaults" in metadata:
            return metadata["security_defaults"]
        try:
            defaults_hash = compute_security_defaults_hash()
        except Exception:
            _log.debug(
                "security-defaults hash computation failed", exc_info=True
            )
            defaults_hash = None
        entry = {
            "scheme": SECURITY_DEFAULTS_SCHEME,
            "profile": SECURITY_DEFAULTS_PROFILE,
            "defaults_hash": defaults_hash,
        }
        metadata["security_defaults"] = entry
        return entry
    except Exception as exc:
        _log.debug("security-defaults stamping failed", exc_info=True)
        record_degradation(document, "security_defaults", exc)
        return None


__all__ = [
    "SECURITY_DEFAULTS_PROFILE",
    "SECURITY_DEFAULTS_SCHEME",
    "compute_security_defaults_hash",
    "describe_security_defaults",
    "stamp_security_defaults",
]
