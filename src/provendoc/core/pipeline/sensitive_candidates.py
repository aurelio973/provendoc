# -*- coding: utf-8 -*-
"""Sensitive candidates — cross-format central stamping point for sensitive-candidate detection.

The detection itself (``core/diagnostics/sensitive_signals.py``) is used
unchanged — this module only wires it into the existing stamping
sequences of UniversalParser / the SDK client (no standalone service
extracted), same discipline as content_completeness / input_integrity.

Design:

- **Central scan**: scans ``document.text`` (the working text =
  structured view, always present for all formats per the Document
  three-text-field contract); the detection function is the very same
  implementation as on the txt side — 8 pattern classes / Luhn
  suppression / locations capped at 20 / **never store the value**
  (only kind + line positions + count are recorded, the matched
  string is discarded — a leak registry is itself a leak surface);
- **Idempotent skip (no double scan)**: if ``metadata`` already has
  the ``sensitive_candidates`` key (the txt-domain parser already
  stamped at parse time) → no-op return; the existing txt_parser
  wiring is kept as-is with zero behavior change;
- **Non-empty writes only**: no candidates → no key written (most
  clean documents gain no new key; a None detect result passes
  through unchanged);
- **Identify, never mask**: ``redacted`` is always False; masking
  (retain/redact/hash/drop) is a downstream Policy decision
  (parsing_contract: no masking at ingestion, policy is
  downstream);
- **Silent degradation on failure**: returns None without blocking
  the main flow (a missing completeness view must not block
  ingestion).

Location semantics: locations are line numbers in
``document.text`` (the structured view) — same convention as the
txt side; for non-txt formats the line numbers are retrievable but
**not equal to source-file page positions** (native pdf/docx
page/slide/paragraph positions are deliberately not fabricated —
no invented anchors; candidate registration is not a precise
locationing scenario).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from provendoc.core.diagnostics.sensitive_signals import (
    detect_sensitive_candidates,
)
from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

SENSITIVE_CANDIDATES_KEY = "sensitive_candidates"


def stamp_sensitive_candidates(document: Any) -> Optional[Dict[str, Any]]:
    """Scan ``document.text`` and write ``metadata["sensitive_candidates"]``.

    Idempotent skip: key already present (stamped at parse time in the
    txt domain) → no-op, preventing double scans; non-empty writes
    only (no key when there are no candidates); returns None on
    failure without blocking the main flow.
    """
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        if metadata.get(SENSITIVE_CANDIDATES_KEY) is not None:
            # txt_parser already stamped at parse time (existing wiring
            # kept, zero-change red line).
            return metadata[SENSITIVE_CANDIDATES_KEY]
        text = getattr(document, "text", None)
        if not isinstance(text, str) or not text:
            return None
        candidates = detect_sensitive_candidates(text)
        if candidates is None:
            return None  # non-empty writes only: no key without candidates
        metadata[SENSITIVE_CANDIDATES_KEY] = candidates
        return candidates
    except Exception as exc:
        _log.debug("Sensitive candidates stamping failed", exc_info=True)
        record_degradation(document, "sensitive_candidates", exc)
        return None


__all__ = [
    "SENSITIVE_CANDIDATES_KEY",
    "stamp_sensitive_candidates",
]
