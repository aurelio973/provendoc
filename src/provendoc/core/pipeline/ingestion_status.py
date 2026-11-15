# -*- coding: utf-8 -*-
"""Ingestion status — processing-state and credibility contract.

Design (first-version contract for this local SDK library):

- **Summarize, do not rebuild**: this module requires no new parser
  fields; it summarizes the scattered evidence already written into
  ``document.metadata`` (``parse_failed`` / ``warnings`` /
  ``pdf_routing_decision`` / ``ocr_pages`` /
  ``pdf_native_page_errors`` / ``pdf_text_characters`` /
  ``zip_package_guard`` ...) into one unified credibility view under
  the key ``metadata["ingestion_status"]`` — additive, idempotent,
  fail-silent (same discipline as provenance / doc_identity).
- **Honest absence**: contract fields (truncated / pages_processed /
  pages_total / attachments_skipped / ocr_used) are written only
  with evidence; dimensions without evidence get no key, never
  faked with ``false``/``0`` as "complete". The core point: parsed
  text is not necessarily complete, and missing evidence is NOT
  completeness.
- **status grading**:
  - ``FAILED`` — ``parse_failed`` (an honest-failure document);
  - ``PARTIAL`` — explicit integrity-degradation evidence present
    (page-level parse errors / OCR empty output / font limit
    exceeded / attachments skipped);
  - ``COMPLETE`` — everything else (including documents with only
    fact-reporting warnings — observation posts like the ZWJ count
    are not integrity degradation and must not dilute the PARTIAL
    signal; their presence is reflected separately in
    ``parser_warnings``).
- **failure_status enum**: on FAILED a stable error code is
  inferred from existing evidence (``UNSUPPORTED_FORMAT`` /
  ``DECRYPTION_REQUIRED`` / ``RESOURCE_LIMIT`` / ``MALFORMED_FILE``
  ...); when nothing fits, it honestly falls to ``MALFORMED_FILE``
  with the original ``parse_error`` summary attached for internal
  audit (stable code out, detailed evidence in).
- **Font limit**: ``pdf_text_characters.font_count`` over
  ``DEFAULT_FONT_LIMIT`` -> a ``font_limit_exceeded`` warning +
  PARTIAL. Warning evidence, not rejection (guards are non-fatal —
  the fonts are already enumerated; the limit exists to surface
  mapping anomalies like font explosions).

Relations to existing modules (no duplication, no replacement):

- The ``parse_failed`` / ``parse_error`` discipline is unchanged —
  this module only adds a summary view on top and never alters any
  parser's honest-failure behavior;
- The PDF resource guard (classifier) and the ZIP guard
  (container_guard) remain the first gate of their formats — this
  module only consumes their evidence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from provendoc.core.pipeline.pipeline_stamps import record_degradation

# ------------------------------------------------------------
# Processing status
# ------------------------------------------------------------

STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"

# ------------------------------------------------------------
# Failure-status enum (stable error codes)
# ------------------------------------------------------------

FAILURE_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
FAILURE_MALFORMED_FILE = "MALFORMED_FILE"
FAILURE_RESOURCE_LIMIT = "RESOURCE_LIMIT"
FAILURE_PARSER_TIMEOUT = "PARSER_TIMEOUT"
FAILURE_DECRYPTION_REQUIRED = "DECRYPTION_REQUIRED"
FAILURE_EXTERNAL_RESOURCE_BLOCKED = "EXTERNAL_RESOURCE_BLOCKED"
FAILURE_SANDBOX_ERROR = "SANDBOX_ERROR"

# Enum members without a consumer yet in this local-library shape
# (PARSER_TIMEOUT / EXTERNAL_RESOURCE_BLOCKED / SANDBOX_ERROR get
# wired once servitized) stay in the namespace — freeze the
# contract first, let the implementation catch up as needed.
ALL_FAILURE_STATUSES = (
    FAILURE_UNSUPPORTED_FORMAT,
    FAILURE_MALFORMED_FILE,
    FAILURE_RESOURCE_LIMIT,
    FAILURE_PARSER_TIMEOUT,
    FAILURE_DECRYPTION_REQUIRED,
    FAILURE_EXTERNAL_RESOURCE_BLOCKED,
    FAILURE_SANDBOX_ERROR,
)

# ------------------------------------------------------------
# Font limit (evidence-level, non-fatal)
# ------------------------------------------------------------

DEFAULT_FONT_LIMIT = 200

# Stable fragments in parse_error messages that decide
# RESOURCE_LIMIT (the unified message prefixes of the zip guard and
# the PDF resource guard).
_RESOURCE_LIMIT_MARKERS = (
    "zip package bomb guard",
    "resource guard engaged",
)


def _failure_status_from_metadata(
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """parse_failed evidence -> stable error code + internal audit lead."""
    verification = metadata.get("format_verification") or {}
    if verification.get("verdict") == "mismatch":
        observed = verification.get("observed_family")
        expected = verification.get("expected_family")
        if observed == "ole_cfb" and expected == "zip":
            # OLE container under an OOXML suffix: an MS-OFFCRYPTO
            # encrypted package or a legacy binary Office document
            # (classification details: docx_security). Honest call:
            # never assert encryption outright — return
            # UNSUPPORTED_FORMAT with the possible encrypted
            # interpretation noted, avoiding mislabeling an old .doc
            # saved as .docx.
            return {
                "failure_status": FAILURE_UNSUPPORTED_FORMAT,
                "failure_detail": (
                    "OLE/CFB container under an Open XML suffix "
                    "(legacy binary Office or MS-OFFCRYPTO "
                    "encrypted; classification: docx_security)"
                ),
            }
        return {
            "failure_status": FAILURE_UNSUPPORTED_FORMAT,
            "failure_detail": format_mismatch_summary(verification),
        }

    zip_guard = metadata.get("zip_package_guard") or {}
    if zip_guard.get("verdict") == "violation":
        return {
            "failure_status": FAILURE_RESOURCE_LIMIT,
            "failure_detail": "zip package limits violated",
        }

    # The pdf routing decision's guard_reasons separates encryption
    # from limits (encryption always outranks page/size limits — a
    # classifier iron rule).
    routing = metadata.get("pdf_routing_decision") or {}
    signals = routing.get("signals") or {}
    if signals.get("encrypted"):
        return {
            "failure_status": FAILURE_DECRYPTION_REQUIRED,
            "failure_detail": (
                "document is password protected; decryption is "
                "never attempted in this pipeline"
            ),
        }
    if signals.get("guard_reasons"):
        return {
            "failure_status": FAILURE_RESOURCE_LIMIT,
            "failure_detail": "pdf runtime limits violated",
        }

    parse_error = str(metadata.get("parse_error") or "")
    if any(marker in parse_error for marker in _RESOURCE_LIMIT_MARKERS):
        return {
            "failure_status": FAILURE_RESOURCE_LIMIT,
            "failure_detail": "resource guard engaged",
        }

    return {
        "failure_status": FAILURE_MALFORMED_FILE,
        "failure_detail": parse_error[:200] or "unknown parse failure",
    }


def format_mismatch_summary(verification: Dict[str, Any]) -> str:
    """mismatch evidence -> short summary (internal audit; the stable code is what goes out)."""
    return (
        f"declared {verification.get('declared_suffix')} but observed "
        f"{verification.get('observed_family')}"
    )


def _partial_evidence(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Integrity-degradation evidence -> contract fields (the PARTIAL criteria)."""
    warnings: List[str] = []
    result: Dict[str, Any] = {}

    # page-level parse errors (PDF native extraction)
    page_errors = metadata.get("pdf_native_page_errors") or {}
    if isinstance(page_errors, dict) and page_errors:
        warnings.append("page_errors_present")
        result["pages_with_errors"] = len(page_errors)

    # OCR empty-output pages (OCR was invoked but produced nothing —
    # a confidence-degradation indicator)
    ocr_empty = metadata.get("ocr_empty_output_pages") or []
    if ocr_empty:
        warnings.append("ocr_partial_failure")
        result["ocr_empty_pages"] = len(ocr_empty)

    # Font limit: a font explosion is a mapping-anomaly indicator
    characters = metadata.get("pdf_text_characters") or {}
    font_count = characters.get("font_count")
    if isinstance(font_count, int) and font_count > DEFAULT_FONT_LIMIT:
        warnings.append("font_limit_exceeded")
        result["font_count"] = font_count
        result["font_limit"] = DEFAULT_FONT_LIMIT

    # Resource guard engaged: the oversized-structure guard of
    # excel/csv/docx actually skipped/truncated some content ->
    # integrity degrades to PARTIAL (guards, not filters — the
    # document itself remains usable).
    resource_guard = metadata.get("resource_guard") or {}
    guard_reasons = resource_guard.get("reasons") if isinstance(resource_guard, dict) else None
    if guard_reasons:
        warnings.append("resource_limit_guard")
        result["resource_guard_reasons"] = len(guard_reasons)

    if warnings:
        result["partial_warnings"] = warnings
    return result


def summarize_ingestion_status(
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Parser metadata -> credibility contract view.

    Reads existing evidence keys only; metadata is not modified;
    the return value is serializable (the additive write into
    ``metadata["ingestion_status"]`` is done by ``stamp_*``).
    """
    if metadata.get("parse_failed"):
        failure = _failure_status_from_metadata(metadata)
        return {
            "status": STATUS_FAILED,
            "failure_status": failure["failure_status"],
            "failure_detail": failure["failure_detail"],
        }

    status = STATUS_COMPLETE
    contract: Dict[str, Any] = {}
    partial = _partial_evidence(metadata)
    if partial:
        status = STATUS_PARTIAL
        contract.update(partial)

    parser_warnings: List[str] = []
    if partial:
        parser_warnings.extend(partial.get("partial_warnings") or [])

    # Fact-reporting presence (observation posts — does not degrade
    # status; reflected separately)
    if metadata.get("warnings"):
        parser_warnings.append("warnings_present")

    # PDF page dimension (written only with evidence; other formats
    # have no page concept -> no key)
    page_count = metadata.get("page_count")
    if isinstance(page_count, int):
        contract["pages_total"] = page_count
        native_pages = metadata.get("native_text_pages") or []
        ocr_pages = metadata.get("ocr_pages") or []
        if isinstance(native_pages, list) or isinstance(ocr_pages, list):
            contract["pages_processed"] = (
                len(native_pages) + len(ocr_pages)
            )

    if metadata.get("ocr_pages"):
        contract["ocr_used"] = True

    if parser_warnings:
        contract["parser_warnings"] = parser_warnings

    contract["status"] = status
    return contract


def stamp_ingestion_status(document: Any) -> Optional[Dict[str, Any]]:
    """Summarize and write ``document.metadata["ingestion_status"]``.

    Idempotent (overwrite write; rerun on the same input yields the
    same view); failures return None and never block the main flow
    (same discipline as provenance / doc_identity /
    version_manifest — a missing credibility view must not block
    ingestion).
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        contract = summarize_ingestion_status(metadata)
        metadata["ingestion_status"] = contract
        return contract
    except Exception as exc:
        log.debug("Ingestion status stamping failed", exc_info=True)
        record_degradation(document, "ingestion_status", exc)
        return None


__all__ = [
    "STATUS_COMPLETE",
    "STATUS_PARTIAL",
    "STATUS_FAILED",
    "ALL_FAILURE_STATUSES",
    "DEFAULT_FONT_LIMIT",
    "summarize_ingestion_status",
    "stamp_ingestion_status",
    "format_mismatch_summary",
]
