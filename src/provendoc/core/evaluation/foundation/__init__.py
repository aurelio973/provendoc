"""Shared Evaluation Foundation.

Universal quality primitives shared across ALL ingestion domains.

These are NOT domain-specific metrics. They measure basic ingestion
health signals that every domain needs:

    parse_success          — did parsing complete without error?
    diagnostics             — encoding, control chars, hidden unicode
    confidence             — how reliable is this ingestion?
    warnings               — non-fatal issues encountered
    corruption_signals     — evidence of data corruption
    malformed_recovery     — did the parser recover from bad input?
    extraction_completeness — was all meaningful content extracted?
    metadata_consistency   — is metadata internally consistent?

Existing primitives already scattered across the codebase:
    printable_ratio        (core/text/diagnostics.py)
    suspicious_chars       (core/diagnostics/unicode_diagnostics.py)
    replacement_chars      (core/text/decoding.py)

This module is the future home for consolidating those primitives
into a single shared foundation that all domain quality
evaluators can build upon.
"""
