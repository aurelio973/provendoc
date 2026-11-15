"""Evaluation layer — evaluation infrastructure.

Shared quality primitives shared across ALL ingestion domains.

Public API:
    extract_shared_signals()    — extract shared quality signals from a Document
    summarize_shared_signals()  — summarize the shared signals

    PARSE_SUCCESS, DIAGNOSTICS, CONFIDENCE, WARNINGS,
    CORRUPTION_SIGNALS, MALFORMED_RECOVERY,
    EXTRACTION_COMPLETENESS, METADATA_CONSISTENCY
        — the 8 shared quality primitive identifiers
"""

from provendoc.core.evaluation.foundation.shared_primitives import (
    CONFIDENCE,
    CORRUPTION_SIGNALS,
    DIAGNOSTICS,
    EXTRACTION_COMPLETENESS,
    MALFORMED_RECOVERY,
    METADATA_CONSISTENCY,
    PARSE_SUCCESS,
    WARNINGS,
    extract_shared_signals,
    summarize_shared_signals,
)

__all__ = [
    "extract_shared_signals",
    "summarize_shared_signals",
    "PARSE_SUCCESS",
    "DIAGNOSTICS",
    "CONFIDENCE",
    "WARNINGS",
    "CORRUPTION_SIGNALS",
    "MALFORMED_RECOVERY",
    "EXTRACTION_COMPLETENESS",
    "METADATA_CONSISTENCY",
]
