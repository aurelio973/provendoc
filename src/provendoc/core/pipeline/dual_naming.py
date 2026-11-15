# -*- coding: utf-8 -*-
"""Dual naming — original-name / internal-name two-track contract.

Risk model: file names do not only reach disk paths — they also land
in metadata, audit logs, cache keys, downstream databases, and
presentation layers. User-controlled original names in those surfaces
are an injection face: path separators (``../`` traversal), control
characters (log-field escape / newline injection), overly long names
(resource exhaustion, log flooding), Unicode confusion
(visually similar characters).

Two-name design:

- ``original_name`` — the raw file name, **display only**: length
  limit, Unicode NFC normalization, control-character filtering,
  path-separator replacement; the processing facts (how many control
  characters removed, whether truncated) are recorded alongside as
  evidence — every "correction" of the display name is auditable.
- ``internal_name`` — system-generated, **not user-controlled**:
  content-addressed first (first 16 hex chars of ``source_sha256`` —
  same bytes, same name, natural dedup), falling back to a random
  ``uuid4`` name when unavailable. Audit logs, object keys, and cache
  naming reference internal_name, not original_name.

Behavior rules:

- additive stamping (``metadata["file_naming"]``), fail-silent — a
  missing naming contract must not block ingestion (same discipline
  as provenance / doc_identity);
- guards are non-fatal: a dangerous name is only **normalized into a
  display-safe name**; parsing is never rejected over the file name
  itself (the gate on file CONTENT is format_verification /
  container_guard).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import PureWindowsPath
from typing import Any, Dict, Optional

from provendoc.core.pipeline.pipeline_stamps import record_degradation

# Original-name length limit (display-safety ceiling; overlong names
# are truncated with the fact recorded).
MAX_ORIGINAL_NAME_LENGTH = 256

# Internal-name length (content-addressed prefix).
_INTERNAL_NAME_HASH_LENGTH = 16

# Path separators and device-reserved characters (conservative
# POSIX/Windows union).
_PATH_SEPARATORS = ("/", "\\")

# Control characters (C0 + C1 + DEL + undefined code points): always
# stripped for display use.
_CONTROL_CHARACTER_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\ufff0-\uffff\ud800-\udfff]"
)


def sanitize_original_name(name: Optional[str]) -> Dict[str, Any]:
    """Raw file name -> display-safe name + processing facts.

    Processing chain (each step records its fact):

    1. Unicode NFC normalization (guards visual confusion / encoding
       drift);
    2. control-character removal (log-field escape, terminal control
       sequences);
    3. path separators replaced by ``_`` (``../`` traversal,
       cross-platform separators);
    4. truncation to ``MAX_ORIGINAL_NAME_LENGTH``.

    Missing / non-string name -> empty display name + absent fact
    (honest absence).
    """
    facts: Dict[str, Any] = {
        "original_length": 0,
        "nfc_normalized": False,
        "control_characters_removed": 0,
        "path_separators_replaced": 0,
        "truncated": False,
    }
    if not isinstance(name, str) or not name:
        return {"display_name": "", **facts}

    facts["original_length"] = len(name)

    normalized = unicodedata.normalize("NFC", name)
    if normalized != name:
        facts["nfc_normalized"] = True

    stripped = _CONTROL_CHARACTER_RE.sub("", normalized)
    facts["control_characters_removed"] = len(normalized) - len(stripped)

    separator_count = sum(
        stripped.count(separator) for separator in _PATH_SEPARATORS
    )
    for separator in _PATH_SEPARATORS:
        stripped = stripped.replace(separator, "_")
    facts["path_separators_replaced"] = separator_count

    # Windows device-reserved names (CON/PRN/AUX/NUL/COM1-9/LPT1-9)
    # pose no display hazard here (this library never writes the
    # user's name to disk) and are kept as-is.

    if len(stripped) > MAX_ORIGINAL_NAME_LENGTH:
        stripped = stripped[:MAX_ORIGINAL_NAME_LENGTH]
        facts["truncated"] = True

    return {"display_name": stripped, **facts}


def derive_internal_name(
    source_sha256: Optional[str] = None,
) -> str:
    """Internal name — content-addressed first, random fallback (never user-controlled).

    - ``source_sha256`` available -> its first 16 hex chars (same
      bytes, same internal name; natural dedup, consistent with the
      result binding);
    - missing -> a ``uuid4`` random name (unpredictable).
    """
    if isinstance(source_sha256, str) and len(source_sha256) >= (
        _INTERNAL_NAME_HASH_LENGTH
    ):
        return source_sha256[:_INTERNAL_NAME_HASH_LENGTH]
    return uuid.uuid4().hex[:_INTERNAL_NAME_HASH_LENGTH]


def _base_name(file_path: Optional[str]) -> Optional[str]:
    """Path -> file name (last segment only; PureWindowsPath covers cross-platform separators)."""
    if not file_path:
        return None
    try:
        return PureWindowsPath(file_path).name or None
    except (TypeError, ValueError):
        return None


def stamp_dual_naming(
    document: Any,
    file_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Stamp the ``metadata["file_naming"]`` dual-name stamp (additive, idempotent).

    The internal name prefers the parser-written
    ``metadata["sha256"]`` (the raw-byte fingerprint, the same root
    anchor source as version_manifest); failures return None and
    never block the main flow.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        source_sha256 = metadata.get("sha256")
        naming: Dict[str, Any] = {
            "scheme": "dual_naming_v1",
            "original_name": sanitize_original_name(
                _base_name(file_path)
            ),
            "internal_name": derive_internal_name(source_sha256),
            # Audit rule reminder: audit/object keys reference
            # internal_name.
            "audit_reference": "internal_name",
        }
        metadata["file_naming"] = naming
        return naming
    except Exception as exc:
        log.debug("Dual naming stamping failed", exc_info=True)
        record_degradation(document, "dual_naming", exc)
        return None


__all__ = [
    "MAX_ORIGINAL_NAME_LENGTH",
    "sanitize_original_name",
    "derive_internal_name",
    "stamp_dual_naming",
]
