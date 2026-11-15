# -*- coding: utf-8 -*-
"""Safety validation for parsing and chunking configuration.

Applies not only to input files but also to parser/chunking settings:
page ranges, OCR resolution, chunk size, overlap, recursion depth,
retry counts, external links, attachment types. Defense is a ceiling
whitelist over every caller-configurable numeric parameter.

Implementation in this library:

- **Ceiling whitelist**: ``CONFIGURATION_CEILINGS`` freezes the upper
  bounds of every numeric parsing/chunking parameter callers may set.
  Exceeding a ceiling does NOT raise — per the honest-failure rule the
  violation is recorded as evidence with a warning, and the caller
  keeps the verdict (no silent clamping, so behavior never drifts).
- **Structurally disabled**: network access / script execution /
  macro execution / external entities / external attachments /
  formula recalculation have NO code path in this library, hence no
  switch a dangerous configuration could turn on — registered as
  ``STRUCTURALLY_DISABLED`` rather than "off by default".
- **Versioning**: the scheme string is the validation contract
  version; semantic changes must bump it.
- Stamp points: UniversalParser.parse (pipeline-level effective
  config) and SDK chunk() (chunking-level config) — both additive,
  idempotent, fail-silent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

CONFIG_VALIDATION_SCHEME = "config_validation_v1"

# Numeric configuration ceilings (frozen whitelist; units are
# chars/counts, not tokens). Basis: RuntimePackagingPolicy defaults of
# soft 500 / hard 800, orphan 120 — the ceilings sit one order of
# magnitude above (20x) and target hostile configs (stuffing a whole
# book into one chunk, overlap larger than the body causing quadratic
# output growth), not normal use.
CONFIGURATION_CEILINGS: Dict[str, int] = {
    "chunk_size": 16_000,
    "overlap": 8_000,
    "size_hard_limit": 16_000,
    "size_soft_limit": 16_000,
    "orphan_threshold": 4_000,
}

# Parameters where 0 is legal (overlap=0 means no overlap, a valid
# configuration rather than a pathological value).
_MAY_BE_ZERO = frozenset({"overlap"})

# Structurally disabled behaviors: no code path exists for these in
# this library, so there is no configurable switch either.
STRUCTURALLY_DISABLED = (
    "network_access",
    "script_execution",
    "macro_execution",
    "external_entities",
    "external_attachments",
    "formula_recalculation",
    "automatic_retry",
    "external_link_updates",
)


def _validate_parameter(name: str, value: Any) -> Dict[str, Any] | None:
    """Validate one parameter; return a finding dict or None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return {
            "parameter": name,
            "value": value,
            "finding": "not_an_integer",
        }
    if value < 0 or (value == 0 and name not in _MAY_BE_ZERO):
        return {
            "parameter": name,
            "value": value,
            "finding": "non_positive",
        }
    ceiling = CONFIGURATION_CEILINGS.get(name)
    if ceiling is not None and value > ceiling:
        return {
            "parameter": name,
            "value": value,
            "ceiling": ceiling,
            "finding": "exceeds_ceiling",
        }
    return None


def validate_parsing_configuration(**parameters) -> Dict[str, Any]:
    """Validate parsing/chunking parameters against the frozen ceiling whitelist (pure function).

    Args:
        **parameters: Numeric config to validate (chunk_size / overlap /
            size_soft_limit / size_hard_limit / orphan_threshold /
            ...). None and unknown parameter names are skipped
            (unknowns land in ``unchecked_parameters`` — honest
            reporting, never pretending they were checked).

    Returns:
        dict with ``scheme`` / ``verdict`` (ok|violation) /
        ``findings`` / ``ceilings`` / ``structurally_disabled`` /
        ``unchecked_parameters``.
    """
    findings: list[Dict[str, Any]] = []
    unchecked: list[str] = []
    for name, value in parameters.items():
        if name not in CONFIGURATION_CEILINGS:
            unchecked.append(name)
            continue
        finding = _validate_parameter(name, value)
        if finding is not None:
            findings.append(finding)
    return {
        "scheme": CONFIG_VALIDATION_SCHEME,
        "verdict": "violation" if findings else "ok",
        "findings": findings,
        "ceilings": dict(CONFIGURATION_CEILINGS),
        "structurally_disabled": list(STRUCTURALLY_DISABLED),
        "unchecked_parameters": sorted(unchecked),
    }


def configuration_parameters_from_packaging(packaging) -> Dict[str, Any]:
    """Read the effective-config snapshot from a RuntimePackagingPolicy (wiring helper).

    Field-by-field getattr; a missing field yields None (the
    validator honestly skips None); a None packaging yields an empty
    dict.
    """
    if packaging is None:
        return {}
    return {
        "chunk_size": getattr(packaging, "chunk_size", None),
        "overlap": getattr(packaging, "overlap", None),
        "size_soft_limit": getattr(packaging, "size_soft_limit", None),
        "size_hard_limit": getattr(packaging, "size_hard_limit", None),
        "orphan_threshold": getattr(packaging, "orphan_threshold", None),
    }


def stamp_config_validation(
    document, parameters: Dict[str, Any]
) -> Dict[str, Any] | None:
    """Validate and idempotently stamp ``metadata["config_validation"]`` (fail-silent).

    A violation is reported as an auditable warning but never raises
    and never clamps the config — the verdict stays with the caller
    (guards are non-fatal).

    Args:
        document: Document instance (must carry dict-shaped metadata).
        parameters: Effective parsing/chunking config (key/value pairs).

    Returns:
        The validation evidence dict; None when the document shape
        does not fit.
    """
    try:
        evidence = validate_parsing_configuration(**parameters)
        if evidence.get("verdict") == "violation":
            _log.warning(
                "Parsing configuration violation: %s",
                evidence.get("findings"),
            )
        metadata = document.metadata
        if not isinstance(metadata, dict):
            return None
        if "config_validation" in metadata:
            return metadata["config_validation"]
        metadata["config_validation"] = evidence
        return evidence
    except Exception as exc:
        _log.debug("config-validation stamping failed", exc_info=True)
        record_degradation(document, "config_validation", exc)
        return None


__all__ = [
    "CONFIGURATION_CEILINGS",
    "CONFIG_VALIDATION_SCHEME",
    "STRUCTURALLY_DISABLED",
    "stamp_config_validation",
    "validate_parsing_configuration",
]
