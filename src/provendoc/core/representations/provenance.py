"""Representation provenance pass — cross-format, evidence-only (B12).

This module is a *post-build enrichment pass* over a RepresentationResult.
It never rewrites unit text, never drops content and never invents facts.
Everything it records is either derived from evidence already present on
the unit / document, or an honest ``None`` with a note explaining why.

Capabilities (each mapped to the gap register's representation-layer list):

1. ``language_profile`` (per unit) — Unicode script-range ratios.  This is a
   *script* profile, not a language ID: it distinguishes CJK vs Latin vs
   Cyrillic etc. and flags mixing, and says so in its ``method`` note.
2. ``duplicate_evidence`` (per unit) — whitespace/case-normalized exact
   duplicates across base units.  Later occurrences are flagged with the
   sequence index / identity of the first occurrence.  Content is never
   removed; the flag is evidence for downstream dedup decisions.
3. ``source_coordinates`` (per unit) — unified coordinate schema built from
   whatever real coordinates the format already carries:

   - PDF units: page points (pages / bboxes / evidence_ids)
   - DOCX units: block index range into ``docx_blocks`` (multi-block → one
     unit is exactly the register's "source span to unit many-to-one map")
   - text formats: char spans into the cleaned document text, located
     verbatim and in order; units whose text is transformed (markdown-ified
     headings, table projections) get ``char_span: null`` with an honest
     note instead of a fabricated offset.
4. ``sensitivity_signals`` (per unit) — regex-heuristic counts only
   (email / api-key-like / long digit sequences).  Matched content is
   never captured into metadata; the method note says this is a heuristic,
   not semantic detection.
5. ``parse_status`` (result level) — document-level vs object-level failure
   separation summarised from existing keys (``parse_failed``,
   ``pdf_native_page_errors``, ``ocr_failed_pages``, ...).  Partial success
   becomes a first-class status instead of being scattered across keys.
6. ``representation_provenance`` (result level) — deterministic snapshot:
   parser / representation class, provendoc version, per-format library
   versions, policy name, cleaning level.  No wall-clock timestamps and
   no parse durations: parsing the same file twice must produce the same
   snapshot (durations stay on ``document.metadata`` for telemetry).
7. ``confidence_stats`` / ``security_propagation`` (result level) — unit
   confidence summary and document-level security evidence propagation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ==================================
# 1. script profile
# ==================================

# Ordered (name, [(lo, hi), ...]) Unicode script ranges.  Deliberately
# coarse: this is a script profile, not a locale detector.
_SCRIPT_RANGES: Sequence[Tuple[str, Sequence[Tuple[int, int]]]] = (
    ("latin", ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F))),
    ("greek", ((0x0370, 0x03FF),)),
    ("cyrillic", ((0x0400, 0x04FF),)),
    ("hebrew", ((0x0590, 0x05FF),)),
    ("arabic", ((0x0600, 0x06FF),)),
    ("devanagari", ((0x0900, 0x097F),)),
    ("thai", ((0x0E00, 0x0E7F),)),
    ("kana", ((0x3040, 0x30FF),)),
    ("cjk", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    ("hangul", ((0x1100, 0x11FF), (0xAC00, 0xD7AF))),
)


def _script_of(ch: str) -> Optional[str]:
    code = ord(ch)
    for name, ranges in _SCRIPT_RANGES:
        for lo, hi in ranges:
            if lo <= code <= hi:
                return name
    return None


def language_profile(text: str) -> Dict[str, Any]:
    """Script-ratio profile of ``text`` (evidence-only, no language ID)."""
    counts: Dict[str, int] = {}
    total = 0
    for ch in text:
        script = _script_of(ch)
        if script is None:
            continue
        counts[script] = counts.get(script, 0) + 1
        total += 1
    if total == 0:
        return {
            "script_ratios": {},
            "dominant_script": None,
            "mixed_scripts": False,
            "letter_count": 0,
            "method": "unicode_script_ranges_not_language_id",
        }
    ratios = {name: round(count / total, 3) for name, count in counts.items()}
    dominant = max(ratios, key=lambda name: ratios[name])
    # A second script holding >= 15% of letters counts as mixed.
    mixed = any(
        ratio >= 0.15 for name, ratio in ratios.items() if name != dominant
    )
    return {
        "script_ratios": dict(sorted(ratios.items(), key=lambda kv: -kv[1])),
        "dominant_script": dominant,
        "mixed_scripts": mixed,
        "letter_count": total,
        "method": "unicode_script_ranges_not_language_id",
    }


# ==================================
# 2. duplicate evidence
# ==================================

# Normalized text shorter than this is not flagged: short strings
# ("Overview", "1.") repeat legitimately and flagging them would be noise.
_DUPLICATE_MIN_CHARS = 20


def _normalize_for_duplicate(text: str) -> str:
    return " ".join(text.split()).casefold()


def _apply_duplicate_evidence(units: Sequence[Any]) -> int:
    """Flag duplicate units against their first occurrence (two levels).

    Level 1 — unit-level: whole unit texts that are whitespace/case-
    normalized equal (plain-text formats whose units are verbatim
    paragraphs hit this level).

    Level 2 — paragraph-level: identical (normalized) paragraphs shared
    across units.  Formats that wrap body text with heading context
    (``[Context: ...]`` / ``## Heading`` prefixes) never match at unit
    level; their repeated *body* paragraphs are still real duplicates and
    are flagged here.

    The first occurrence is never flagged and no content is ever removed.
    Returns the number of flagged (later) units.
    """
    seen_units: Dict[str, Tuple[int, str]] = {}
    seen_paragraphs: Dict[str, Tuple[int, int]] = {}
    flagged = 0
    for index, unit in enumerate(units):
        identity = getattr(unit.semantic_primitives, "identity", "") or ""

        normalized_unit = _normalize_for_duplicate(unit.text)
        unit_level_hit = False
        if len(normalized_unit) >= _DUPLICATE_MIN_CHARS:
            first = seen_units.get(normalized_unit)
            if first is None:
                seen_units[normalized_unit] = (index, identity)
            else:
                first_index, first_identity = first
                unit.duplicate_flag = True
                unit.metadata["duplicate_evidence"] = {
                    "kind": "normalized_exact",
                    "of_sequence_index": first_index,
                    "of_identity": first_identity or None,
                    "note": "evidence_only_content_preserved",
                }
                unit_level_hit = True
                flagged += 1

        if unit_level_hit:
            continue  # whole unit already flagged; skip paragraph scan

        paragraph_hits: List[Dict[str, Any]] = []
        for paragraph_index, raw in enumerate(unit.text.split("\n")):
            normalized = _normalize_for_duplicate(raw)
            if len(normalized) < _DUPLICATE_MIN_CHARS:
                continue
            first_para = seen_paragraphs.get(normalized)
            if first_para is None:
                seen_paragraphs[normalized] = (index, paragraph_index)
                continue
            first_index, first_paragraph = first_para
            paragraph_hits.append(
                {
                    "kind": "normalized_exact_paragraph",
                    "paragraph_index": paragraph_index,
                    "of_sequence_index": first_index,
                    "of_paragraph_index": first_paragraph,
                }
            )
        if paragraph_hits:
            unit.duplicate_flag = True
            unit.metadata["duplicate_evidence"] = {
                "kind": "shared_paragraph",
                "paragraph_duplicates": paragraph_hits,
                "note": "evidence_only_content_preserved",
            }
            flagged += 1
    return flagged


# ==================================
# 3. unified source coordinates
# ==================================


def _source_coordinates(
    unit: Any,
    document_text: str,
    cursor: int,
) -> Tuple[Dict[str, Any], int]:
    """Unified per-unit coordinates from evidence already on the unit."""
    metadata = unit.metadata or {}

    if any(
        key in metadata for key in ("pdf_pages", "pdf_page", "pdf_bboxes")
    ):
        pages = list(metadata.get("pdf_pages") or [])
        page = metadata.get("pdf_page")
        if not pages and page is not None:
            pages = [page]
        return (
            {
                "coordinate_system": "pdf_page_points",
                "pages": pages,
                "bboxes": list(metadata.get("pdf_bboxes") or []),
                "evidence_ids": list(metadata.get("pdf_evidence_ids") or []),
            },
            cursor,
        )

    block_range = metadata.get("docx_block_range")
    if block_range is not None:
        return (
            {
                "coordinate_system": "docx_block_index",
                "block_range": list(block_range),
                "note": "indexes_into_docx_blocks_after_toc_entry_exclusion",
            },
            cursor,
        )

    # Text formats: locate the unit verbatim, in order, inside the
    # (cleaned) document text.  Units whose text is transformed
    # (markdown-ified headings, projected tables) are honestly null.
    needle = unit.text.strip()
    if needle:
        position = document_text.find(needle, cursor)
        if position >= 0:
            end = position + len(needle)
            return (
                {
                    "coordinate_system": "document_text_char_offsets",
                    "char_span": [position, end],
                    "layer": "cleaned_document_text",
                },
                end,
            )
    return (
        {
            "coordinate_system": "document_text_char_offsets",
            "char_span": None,
            "layer": "cleaned_document_text",
            "note": "unit_text_not_locatable_verbatim_transformed_or_reordered",
        },
        cursor,
    )


# ==================================
# 4. sensitivity signals (counts only)
# ==================================

_SENSITIVITY_PATTERNS: Sequence[Tuple[str, re.Pattern[str]]] = (
    (
        "email",
        # Bounded quantifiers ({1,64}) are load-bearing: an unbounded
        # "[A-Za-z0-9._%+-]+@" backtracks O(n) per start position on long
        # runs of class-matching chars (e.g. 1M 'X'), giving O(n²) scans
        # that hang single-line stress files for ~45 minutes. RFC 5321
        # caps local-part at 64 octets, so {1,64} preserves semantics.
        re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}"),
    ),
    (
        "api_key_like",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}"
            r"|AKIA[0-9A-Z]{16}|xoxb-[A-Za-z0-9-]{10,})\b"
        ),
    ),
    (
        "long_digit_sequence",
        re.compile(r"\d{15,}"),
    ),
    (
        "phone_like",
        re.compile(r"(?<!\d)(?:\+?\d{1,3}[- ])?1[3-9]\d{9}(?!\d)"),
    ),
)


def _apply_sensitivity_signals(unit: Any) -> None:
    signals: Dict[str, int] = {}
    for name, pattern in _SENSITIVITY_PATTERNS:
        count = len(pattern.findall(unit.text))
        if count:
            signals[name] = count
    if not signals:
        return
    # Counts only — matched content is deliberately never captured.
    unit.metadata["sensitivity_signals"] = {
        "signals": signals,
        "method": "regex_heuristic_not_semantic",
        "content_never_captured": True,
    }


# ==================================
# 5. parse status (document vs object level)
# ==================================


def _build_parse_status(document_metadata: Dict[str, Any]) -> Dict[str, Any]:
    object_failures: Dict[str, int] = {}

    native_errors = document_metadata.get("pdf_native_page_errors")
    if isinstance(native_errors, list) and native_errors:
        object_failures["pdf_native_page_errors"] = len(native_errors)

    for key in ("ocr_failed_pages", "ocr_budget_exhausted_pages"):
        pages = document_metadata.get(key)
        if isinstance(pages, list) and pages:
            object_failures[key] = len(pages)

    if document_metadata.get("parse_failed"):
        document_level = "failed"
    elif object_failures:
        document_level = "partial"
    else:
        document_level = "ok"

    status: Dict[str, Any] = {
        "document_level": document_level,
        "partial_success": document_level == "partial",
        "object_level_failures": object_failures,
    }
    parse_error = document_metadata.get("parse_error")
    if parse_error:
        status["parse_error"] = str(parse_error)
    return status


# ==================================
# 6. provenance snapshot (deterministic)
# ==================================


def _package_version(package: str) -> Optional[str]:
    try:
        from importlib import metadata as _importlib_metadata

        return _importlib_metadata.version(package)
    except Exception:  # pragma: no cover - environment dependent
        return None


# Canonical format key per parser-reported ``file_type`` value.  Parsers
# disagree on naming ("text", "markdown", "pdf", ``None`` for docx), so
# aliases normalise them; the format is *inferred from evidence keys* in
# ``document.metadata`` as a fallback when the parser reports nothing.
_FILE_TYPE_ALIASES = {
    "pdf": "pdf",
    "text": "text",
    "txt": "text",
    "markdown": "markdown",
    "md": "markdown",
    "docx": "docx",
    "word": "docx",
    "html": "html",
    "htm": "html",
    "excel": "excel",
    "xlsx": "excel",
    "xls": "excel",
    "log": "log",
    "jsonl": "jsonl",
}

# Fallback: format-indicative evidence keys in document.metadata.
_FORMAT_EVIDENCE_KEYS = {
    "pdf_security": "pdf",
    "docx_package": "docx",
    "docx_security": "docx",
    "spreadsheet": "excel",
}

_FILE_TYPE_LIBRARIES = {
    "pdf": ("pymupdf",),
    "docx": ("python-docx",),
    "html": ("beautifulsoup4",),
    "excel": ("openpyxl", "xlrd"),
}


def _canonical_file_type(document_metadata: Dict[str, Any]) -> Optional[str]:
    """Normalise the parser-reported file_type, with evidence-key fallback."""
    reported = str(document_metadata.get("file_type") or "").strip().lower()
    if reported.startswith("."):
        reported = reported[1:]
    if reported in _FILE_TYPE_ALIASES:
        return _FILE_TYPE_ALIASES[reported]
    for key, format_name in _FORMAT_EVIDENCE_KEYS.items():
        if key in document_metadata:
            return format_name
    return reported or None


def _build_provenance_snapshot(
    document_metadata: Dict[str, Any],
    *,
    parser: Optional[str],
    representation: Optional[str],
    policy_name: Optional[str],
    cleaning_level: Optional[str],
) -> Dict[str, Any]:
    from provendoc import __version__ as provendoc_version

    file_type = _canonical_file_type(document_metadata)
    library_versions: Dict[str, Optional[str]] = {}
    for package in _FILE_TYPE_LIBRARIES.get(str(file_type or ""), ()):
        library_versions[package] = _package_version(package)

    snapshot: Dict[str, Any] = {
        "parser": parser,
        "representation": representation,
        "provendoc_version": provendoc_version,
        "library_versions": library_versions,
        "deterministic_snapshot": True,
    }
    if policy_name is not None:
        snapshot["policy_name"] = policy_name
    if cleaning_level is not None:
        snapshot["cleaning_level"] = cleaning_level
    # Deliberately NO parse duration: it is wall-clock telemetry (already
    # on document.metadata["parse_duration_ms"]) and would break the
    # same-file-twice-identical guarantee of the snapshot.
    return snapshot


# ==================================
# 7. security propagation + confidence stats
# ==================================

_SECURITY_EVIDENCE_KEYS = ("pdf_security", "docx_security")

# Document-level fact pass-down key for the invisible-carrier census:
# the parser layer writes metadata["security_carriers"] when the
# census is non-zero; here it is propagated by reference to every
# representation unit — chunk-layer consumers (retrieval / LLM
# context assembly) can filter or warn on it. Observation, not
# adjudication; same discipline as restriction propagation.
_CARRIER_CENSUS_KEY = "security_carriers"


def _build_carrier_propagation(
    document_metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    census = document_metadata.get(_CARRIER_CENSUS_KEY)
    if not isinstance(census, dict) or not census:
        return None
    return {
        "has_invisible_carriers": True,
        "total_carrier_count": sum(
            int(v) for v in census.values()
        ),
        "census": dict(census),
        "note": (
            "document_level_carrier_census_propagated_as_reference"
            " carriers_never_enforced_here"
        ),
    }


def _has_pdf_restrictions(evidence: Dict[str, Any]) -> bool:
    """PDF restriction evidence: locked, encryption dictionary, or
    restricted permission bits.  The always-present ``permissions``
    mapping is deliberately not truthiness-probed."""
    if evidence.get("locked"):
        return True
    encryption = evidence.get("encryption") or {}
    return bool(encryption.get("encrypted") or encryption.get("restricted"))


def _has_docx_restrictions(evidence: Dict[str, Any]) -> bool:
    """DOCX restriction evidence: encrypted OLE package (MS-OFFCRYPTO) or
    documentProtection/writeProtection declarations."""
    encryption = evidence.get("encryption") or {}
    if encryption.get("encrypted_package") or encryption.get("ole_container"):
        return True
    protection = evidence.get("protection") or {}
    return bool(protection.get("present"))


_RESTRICTION_PROBES = {
    "pdf_security": _has_pdf_restrictions,
    "docx_security": _has_docx_restrictions,
}


def _pdf_security_signals(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Compliance signal bits: JS present, invisible text present,
    redaction not removed, visual masking, partial ByteRange,
    CMap contradiction. Same source and meaning as the
    warnings fact codes; propagated to representation units as
    a document-level reference. Observation, not adjudication.
    """
    signals: Dict[str, Any] = {}
    javascript = evidence.get("javascript") or {}
    if isinstance(javascript, dict) and javascript.get("present"):
        signals["javascript_present"] = True
    invisible = evidence.get("invisible_text") or {}
    if isinstance(invisible, dict) and invisible.get("present"):
        signals["invisible_text"] = True
    redactions = evidence.get("redactions") or {}
    if isinstance(redactions, dict):
        recoverable = sum(
            1
            for mark in redactions.get("marks") or []
            if mark.get("body_text_still_present") or mark.get("text_recoverable")
        )
        if recoverable:
            signals["redaction_text_recoverable"] = recoverable
        obscuration = redactions.get("visual_obscuration") or {}
        if isinstance(obscuration, dict) and obscuration.get("count"):
            signals["visual_obscuration"] = int(obscuration["count"])
    signatures = evidence.get("signatures") or {}
    if isinstance(signatures, dict):
        partial = sum(
            1
            for field in signatures.get("signature_fields") or []
            if field.get("coverage_fraction") is not None
            and float(field["coverage_fraction"]) < 1.0
        )
        if partial:
            signals["partial_byte_range"] = partial
    cmap = evidence.get("cmap_audit") or {}
    if isinstance(cmap, dict) and cmap.get("mismatch_count"):
        signals["cmap_mismatch"] = int(cmap["mismatch_count"])
    return signals


def _build_security_propagation(
    document_metadata: Dict[str, Any],
    unit_count: int,
) -> Optional[Dict[str, Any]]:
    present = [
        key
        for key in _SECURITY_EVIDENCE_KEYS
        if isinstance(document_metadata.get(key), dict)
    ]
    if not present:
        return None

    has_restrictions = any(
        _RESTRICTION_PROBES[key](document_metadata[key])
        for key in present
        if key in _RESTRICTION_PROBES
    )
    propagation: Dict[str, Any] = {
        "evidence_keys": present,
        "has_restrictions": has_restrictions,
        "propagated_to_units": unit_count if has_restrictions else 0,
        "note": (
            "document_level_evidence_propagated_as_reference"
            " restrictions_never_enforced_here"
        ),
    }
    # Compliance signal bits: written only when non-zero — same
    # minimization discipline as the census; a document without
    # signals gets no key.
    if "pdf_security" in present and isinstance(
        document_metadata.get("pdf_security"), dict
    ):
        signals = _pdf_security_signals(document_metadata["pdf_security"])
        if signals:
            propagation["security_signals"] = signals
    return propagation


def _build_confidence_stats(units: Sequence[Any]) -> Optional[Dict[str, Any]]:
    values = [
        float(unit.semantic_primitives.confidence)
        for unit in units
        if unit.semantic_primitives is not None
        and unit.semantic_primitives.confidence is not None
    ]
    if not values:
        return None
    return {
        "units_with_confidence": len(values),
        "min": round(min(values), 3),
        "mean": round(sum(values) / len(values), 3),
    }


# ==================================
# public pass
# ==================================


def representation_units(result: Any) -> Tuple[List[Any], bool]:
    """Return the base units of a RepresentationResult.

    Returns ``(units, is_multigranularity)``; plain-list results are treated
    as base-only.
    """
    content = getattr(result, "content", None)
    if content is None:
        return [], False
    if hasattr(content, "base"):
        return list(content.base), True
    return list(content), False


def apply_representation_provenance(
    result: Any,
    document: Any,
    *,
    parser: Optional[str] = None,
    representation: Optional[str] = None,
    policy_name: Optional[str] = None,
    cleaning_level: Optional[str] = None,
) -> Any:
    """Attach cross-format representation evidence (in place, idempotent).

    Evidence-only: unit text, ordering and existing metadata keys are never
    rewritten; all additions live under new keys.  Safe to call twice — the
    second call is a no-op.
    """
    if result is None or getattr(result, "metadata", None) is None:
        return result
    if result.metadata.get("representation_provenance") is not None:
        return result  # idempotent

    document_metadata = getattr(document, "metadata", {}) or {}
    document_text = getattr(document, "text", "") or ""
    units, _ = representation_units(result)
    if not units:
        units = []

    # per-unit evidence ------------------------------------------------
    cursor = 0
    coordinate_systems: Dict[str, int] = {}
    for unit in units:
        if getattr(unit, "metadata", None) is None:
            continue
        unit.metadata["language_profile"] = language_profile(unit.text)
        coordinates, cursor = _source_coordinates(unit, document_text, cursor)
        unit.metadata["source_coordinates"] = coordinates
        system = coordinates["coordinate_system"]
        coordinate_systems[system] = coordinate_systems.get(system, 0) + 1
        _apply_sensitivity_signals(unit)

    flagged_duplicates = _apply_duplicate_evidence(units)

    # result-level evidence ---------------------------------------------
    result.metadata["parse_status"] = _build_parse_status(document_metadata)
    result.metadata["representation_provenance"] = _build_provenance_snapshot(
        document_metadata,
        parser=parser,
        representation=representation,
        policy_name=policy_name,
        cleaning_level=cleaning_level,
    )

    summary: Dict[str, Any] = {
        "base_units": len(units),
        "flagged_duplicate_units": flagged_duplicates,
        "coordinate_systems": coordinate_systems,
    }
    confidence_stats = _build_confidence_stats(units)
    if confidence_stats is not None:
        summary["confidence_stats"] = confidence_stats
    security = _build_security_propagation(document_metadata, len(units))
    carrier = _build_carrier_propagation(document_metadata)
    if security is not None:
        summary["security_propagation"] = security
    if carrier is not None:
        summary["security_carrier_propagation"] = carrier
    if (
        security is not None and security["has_restrictions"]
    ) or carrier is not None:
        for unit in units:
            if getattr(unit, "metadata", None) is not None:
                doc_sec = unit.metadata.setdefault("document_security", {})
                if security is not None and security["has_restrictions"]:
                    doc_sec["has_restrictions"] = True
                    doc_sec["evidence_keys"] = security["evidence_keys"]
                if carrier is not None:
                    doc_sec["has_invisible_carriers"] = True
                    doc_sec["security_carriers"] = carrier["census"]
    result.metadata["representation_evidence_summary"] = summary
    return result
