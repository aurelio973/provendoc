# ============================================================
# Narrative Domain
# Byte- and line-level facts for .txt files (observation only)
# ============================================================
"""Observation-layer facts for .txt inputs: disguised-container sniffing,
line-ending inventory, empty-file classification, truncation evidence and
a content statistics block.

TXT has no self-describing structure: it is a byte stream whose "format"
lives only in conventions.  This module therefore records *facts about
the bytes and lines* before/around the shared pipeline transforms, so
downstream stages (recovery, representation, reporting) can reason from
evidence instead of re-deriving it.

Fact boundaries (documented, not guessed):

- everything here is registered evidence, never a behaviour change: the
  decoded text, sanitization and structure detection are untouched;
- the disguised-container policy is the **lenient** grade: a ``.txt``
  file whose head matches a container/image/executable magic is recorded
  (``metadata["disguised_container"]`` + a warning) and then parsed as
  text anyway.  The blocking grade lives in
  ``core/pipeline/format_verification.py`` for extension-routed binary
  formats; this module deliberately does not intercept;
- security posture (register-driven): read-only byte observation, no
  delegation to the observed family's parser, no archive expansion, no
  execution, no network access; evidence is capped at the first 16
  bytes of the head and the head read itself is size-bounded;
- line-ending counts are taken from ``source_text`` (the faithful
  decoded view before BOM stripping / newline normalization /
  sanitization), so CRLF/CR/LF provenance survives the normalizer.

Output shape (all keys additive, registered only when applicable)::

    metadata["line_endings"] = {"lf": n, "crlf": n, "cr": n, "mixed": bool}
    metadata["content_classification"] = "empty_file" | "bom_only" |
        "blank_text" | "control_only" | "content_present"
    metadata["text_stats"] = {
        "byte_count": int, "character_count": int, "line_count": int,
        "non_empty_line_count": int, "longest_line_length": int,
        "paragraph_count": int, "decode_error_count": int,
    }
    metadata["disguised_container"] = {   # only when magic observed
        "family": str, "evidence_hex": str,
        "action": "registered_only_parsed_as_text",
    }
    metadata["truncation"] = {            # only when evidence found
        "truncated": True, "evidence": [str, ...],
    }
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.structure.special_modules import CODE_FENCE_RE
from provendoc.core.structure.subtype_signals import (
    SUBTYPE_PROBE_CHAR_CAP,
)
from provendoc.core.text.normalization import (
    normalize_newlines,
    strip_bom,
)

# ---------------------------------------------------------------------------
# Disguised-container sniffing (txt-side, lenient grade)
# ---------------------------------------------------------------------------

# Bytes read for magic sniffing.  Bounded: the sniffer never reads more
# than this, regardless of file size.
_HEAD_SNIFF_BYTES = 4096

# Evidence cap: only the first 16 bytes of the head are hex-registered.
_EVIDENCE_BYTES = 16

# Container / image / executable signatures, offset-0 exact match.
# Text-side companion of format_verification._MAGIC_SIGNATURES: that
# gate covers extension-declared binary formats (blocking grade); this
# table covers *text-claimed* files (lenient grade).  PNG/JPEG/7z/RAR/
# ELF/PE are added because a `.txt` that is actually one of those is
# otherwise invisible to the binary-side gate.
_SIGNATURES: Tuple[Tuple[str, bytes], ...] = (
    ("zip", b"PK\x03\x04"),      # standard local file header
    ("zip", b"PK\x05\x06"),      # empty archive (EOCD only)
    ("zip", b"PK\x07\x08"),      # spanned marker
    ("ole_cfb", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),  # OLE2 compound
    ("gzip", b"\x1f\x8b"),
    ("bzip2", b"BZh"),
    ("xz", b"\xfd7zXZ\x00"),
    ("7z", b"7z\xbc\xaf\x27\x1c"),
    ("rar", b"Rar!\x1a\x07"),
    ("png", b"\x89PNG\r\n\x1a\n"),
    ("jpeg", b"\xff\xd8\xff"),
    ("elf", b"\x7fELF"),
    ("pe_mz", b"MZ"),
    ("parquet", b"PAR1"),
)

# PDF allows a small preamble; searched only within this many bytes to
# keep prose mentioning "%PDF-" from false-positiving.
_PDF_MAGIC = b"%PDF-"
_PDF_PREAMBLE_WINDOW = 64

# "MZ" is only two bytes and legitimately occurs at the start of text
# (e.g. "MZ123 order..."); a real PE has NUL bytes early in the header.
_MZ_NUL_WINDOW = 512


def read_head_bytes(file_path: str | Path, limit: int = _HEAD_SNIFF_BYTES) -> bytes:
    """Read at most ``limit`` bytes from the file head; failures return b""."""
    try:
        with open(file_path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


# Tail window for EOF truncation evidence (a UTF-8 sequence is at most
# 4 bytes; 8 leaves room for a trailing newline before the cut point).
_TAIL_SNIFF_BYTES = 8


def read_tail_bytes(file_path: str | Path, limit: int = _TAIL_SNIFF_BYTES) -> bytes:
    """Read at most ``limit`` bytes from the file tail; failures return b""."""
    try:
        with open(file_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read(limit)
    except OSError:
        return b""


def sniff_disguised_container(head: bytes) -> Optional[Dict[str, Any]]:
    """Return a registration record if ``head`` matches a known magic.

    Lenient grade: the caller keeps parsing the file as text; this record
    is evidence only.  Returns ``None`` when no magic matches.
    """
    if not head:
        return None

    family: Optional[str] = None
    if _PDF_MAGIC in head[:_PDF_PREAMBLE_WINDOW]:
        family = "pdf"
    else:
        for name, magic in _SIGNATURES:
            if head[: len(magic)] == magic:
                if name == "pe_mz" and b"\x00" not in head[:_MZ_NUL_WINDOW]:
                    continue  # "MZ" prose guard: real PE binaries have early NULs
                family = name
                break

    if family is None:
        return None

    return {
        "family": family,
        "evidence_hex": head[:_EVIDENCE_BYTES].hex(),
        "action": "registered_only_parsed_as_text",
        "note": (
            "lenient policy: magic observed on a text-claimed file; "
            "registered with warning, parsing continued as text; "
            "no delegation, extraction or execution performed"
        ),
    }


# ---------------------------------------------------------------------------
# Empty-file classification
# ---------------------------------------------------------------------------


def classify_content(source_text: str, byte_count: int) -> str:
    """Classify degenerate content instead of treating it as opaque text.

    ``source_text`` is the decoded view before BOM stripping, so a
    BOM-only file still shows its U+FEFF here.
    """
    if byte_count == 0:
        return "empty_file"

    without_bom = source_text.lstrip("\ufeff")
    if without_bom == "" and source_text != "":
        return "bom_only"
    if without_bom.strip() == "":
        return "blank_text"

    non_space = [c for c in without_bom if not c.isspace()]
    if non_space and all(
        unicodedata.category(c) in ("Cc", "Cf") for c in non_space
    ):
        return "control_only"
    return "content_present"


# ---------------------------------------------------------------------------
# Line-ending inventory (pre-normalization)
# ---------------------------------------------------------------------------


def count_line_endings(source_text: str) -> Dict[str, Any]:
    """Count CRLF / LF / CR terminators on the faithful decoded view."""
    crlf = source_text.count("\r\n")
    without_crlf = source_text.replace("\r\n", "")
    cr = without_crlf.count("\r")
    lf = without_crlf.count("\n")
    kinds = sum(1 for n in (crlf, cr, lf) if n > 0)
    return {"lf": lf, "crlf": crlf, "cr": cr, "mixed": kinds > 1}


# ---------------------------------------------------------------------------
# Content statistics block
# ---------------------------------------------------------------------------


def detect_unicode_normalization(source_text: str) -> Dict[str, Any] | None:
    """Observe whether NFC/NFKC normalization would change the text.

    txt_engineering_gaps_prereg_v1 G9: facts only — the normalized
    full-text views are deliberately NOT materialized (double memory
    and a conflict with the raw-fidelity prime contract); this fact
    gives downstream policy the evidence to decide.
    """
    if not source_text:
        return None
    nfc_changes = unicodedata.normalize("NFC", source_text) != source_text
    nfkc_changes = (
        unicodedata.normalize("NFKC", source_text) != source_text
    )
    if not (nfc_changes or nfkc_changes):
        return None
    return {
        "nfc_would_change": nfc_changes,
        "nfkc_would_change": nfkc_changes,
        "note": (
            "observation only: normalized full-text views are not "
            "materialized (raw-fidelity prime contract); downstream "
            "policy decides whether to normalize"
        ),
    }


def build_text_stats(source_text: str, byte_count: int) -> Dict[str, int]:
    """Deterministic counters over the faithful decoded view."""
    lines = source_text.splitlines()
    non_empty = [ln for ln in lines if ln.strip() != ""]

    paragraphs = 0
    in_paragraph = False
    for ln in lines:
        if ln.strip() == "":
            in_paragraph = False
        elif not in_paragraph:
            paragraphs += 1
            in_paragraph = True

    return {
        "byte_count": byte_count,
        "character_count": len(source_text),
        "line_count": len(lines),
        "non_empty_line_count": len(non_empty),
        "longest_line_length": max((len(ln) for ln in lines), default=0),
        "paragraph_count": paragraphs,
        # U+FFFD appears in the decoded view only via replacement on
        # decode errors, so it doubles as the decode-error counter.
        "decode_error_count": source_text.count("\ufffd"),
    }


# ---------------------------------------------------------------------------
# Truncation evidence
# ---------------------------------------------------------------------------


def _incomplete_utf8_tail(tail: bytes) -> bool:
    """Whether ``tail`` stops mid UTF-8 multibyte sequence (byte-level fact).

    Independent of the encoding the decoder happened to choose: a GB18030
    decode can consume ``E4 B8`` cleanly while the byte stream is still a
    truncated UTF-8 sequence, so the decoded view alone cannot see it.
    """
    if not tail:
        return False
    i = len(tail) - 1
    continuation = 0
    while i >= 0 and 0x80 <= tail[i] <= 0xBF and continuation < 3:
        continuation += 1
        i -= 1
    if i < 0:
        return False
    lead = tail[i]
    if lead < 0x80:
        return False
    if 0xC0 <= lead < 0xE0:
        expected = 2
    elif 0xE0 <= lead < 0xF0:
        expected = 3
    elif 0xF0 <= lead < 0xF8:
        expected = 4
    else:
        return False
    # the lead byte must sit exactly before the trailing continuation run
    return i + continuation == len(tail) - 1 and (1 + continuation) < expected


def detect_truncation(
    source_text: str, text: str, tail_bytes: bytes = b""
) -> Optional[Dict[str, Any]]:
    """Collect truncation evidence; ``None`` when the file looks complete.

    Evidence kinds:

    - ``INCOMPLETE_TRAILING_SEQUENCE``: the decoded view ends in U+FFFD,
      i.e. replacement happened at the end of the byte stream;
    - ``INCOMPLETE_UTF8_SEQUENCE_AT_EOF``: the raw bytes stop mid UTF-8
      multibyte sequence (caught even when statistical decoding picked
      another encoding that consumed the bytes cleanly);
    - ``UNCLOSED_CODE_FENCE``: an odd number of fence lines in the
      normalized text (a fence opened but never closed).
    """
    evidence: List[str] = []
    if source_text.endswith("\ufffd"):
        evidence.append("INCOMPLETE_TRAILING_SEQUENCE")
    elif _incomplete_utf8_tail(tail_bytes):
        evidence.append("INCOMPLETE_UTF8_SEQUENCE_AT_EOF")

    fence_lines = sum(
        1 for ln in text.splitlines() if CODE_FENCE_RE.match(ln.strip())
    )
    if fence_lines % 2 == 1:
        evidence.append("UNCLOSED_CODE_FENCE")

    if not evidence:
        return None
    return {"truncated": True, "evidence": evidence}


# ---------------------------------------------------------------------------
# Aggregation entry point (called by TxtParser, guarded there)
# ---------------------------------------------------------------------------


def collect_txt_file_facts(
    file_path: str | Path,
    metadata: Dict[str, Any],
    source_text: str,
    text: str,
) -> None:
    """Register all byte/line-level facts into ``metadata`` in place.

    Additive only: never mutates text, never removes existing keys.
    Warnings are appended (not replaced) to ``metadata["warnings"]``.
    """
    byte_count = int(metadata.get("file_size") or 0)

    metadata["line_endings"] = count_line_endings(source_text)
    metadata["content_classification"] = classify_content(
        source_text, byte_count
    )
    metadata["text_stats"] = build_text_stats(source_text, byte_count)

    # Unicode normalization observation (txt_engineering_gaps_prereg_v1 G9):
    # register only the fact of "whether normalization changes the text";
    # no materialized normalized full-text view (double-text memory and it
    # conflicts with the original-text-first master contract, see prereg F2).
    normalization_facts = detect_unicode_normalization(source_text)
    if normalization_facts is not None:
        metadata["unicode_normalization"] = normalization_facts

    disguised = sniff_disguised_container(read_head_bytes(file_path))
    if disguised is not None:
        metadata["disguised_container"] = disguised
        metadata.setdefault("warnings", [])
        if isinstance(metadata["warnings"], list):
            metadata["warnings"].append(
                "disguised_container: text-claimed file starts with "
                f"{disguised['family']} magic "
                f"(0x{disguised['evidence_hex']}); registered, "
                "parsed as text (lenient policy)"
            )

    truncation = detect_truncation(
        source_text, text, read_tail_bytes(file_path)
    )
    if truncation is not None:
        metadata["truncation"] = truncation


# ---------------------------------------------------------------------------
# Engineering-grade facts: output views, positions, config reproducibility,
# output strategy (2026-09-29 batch, observation only)
# ---------------------------------------------------------------------------

# Codecs for which ``source_text`` char offsets map to file byte offsets
# by simple prefix re-encoding (no BOM-shift ambiguity, no stateful
# multibyte resets).  utf-8-sig maps to utf-8 because U+FEFF re-encodes
# to exactly the BOM bytes consumed at decode time.
_BYTE_OFFSET_CODECS = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8",
    "ascii": "ascii",
    "gbk": "gbk",
    "gb18030": "gb18030",
    "big5": "big5",
    "cp1252": "cp1252",
    "latin-1": "latin-1",
    "iso8859-1": "latin-1",
    "iso-8859-1": "latin-1",
}

# Soft review threshold for the output-strategy fact: above this size a
# single in-memory Document deserves human review (not a hard limit —
# parsing proceeds either way; chunked output is representation-layer).
_OUTPUT_SOFT_THRESHOLD_BYTES = 64 * 1024 * 1024


def build_output_views() -> Dict[str, Any]:
    """Declare the two output views and the raw-mode guarantees.

    Raw mode already exists as ``Document.source_text`` (the faithful
    decoded view before BOM stripping / newline normalization /
    sanitization); this fact makes the contract explicit and testable:
    raw never merges lines, strips indentation, removes zero-width
    characters, transcodes, rewrites values (amounts/dates) or
    deduplicates content.  The structured view is ``Document.text``
    plus the structure facts registered on metadata.
    """
    return {
        "raw": "source_text",
        "structured": "text",
        "display": "text",
        "raw_guarantees": [
            "no_line_merging",
            "no_indent_stripping",
            "no_zero_width_removal",
            "no_transcoding",
            "no_value_rewriting",
            "no_deduplication",
        ],
        "note": (
            "raw view = faithful decoded byte stream (pre-transform); "
            "truly-raw bytes remain addressable via metadata.sha256 "
            "of the source file; display view = sanitized readable text "
            "(Document.text); structured view = representation-layer "
            "units (TxtNarrativeRepresentation output)"
        ),
    }


def build_parse_config(
    metadata: Dict[str, Any],
    declared_encoding: str,
    tolerant: bool,
) -> Dict[str, Any]:
    """Config-reproducibility block: same file + same config = same facts.

    Records the effective configuration and a stable hash so diverging
    parse results across runs can be attributed to config drift instead
    of guessed at.
    """
    from provendoc import __version__

    effective: Dict[str, Any] = {
        "parser": "TxtParser",
        "parser_version": __version__,
        "profile": "general",
        "encoding_override": declared_encoding,
        "tolerant": tolerant,
        "subtype_probe_char_cap": SUBTYPE_PROBE_CHAR_CAP,
        "head_sniff_bytes": _HEAD_SNIFF_BYTES,
        "tail_sniff_bytes": _TAIL_SNIFF_BYTES,
    }
    canonical = json.dumps(effective, sort_keys=True, ensure_ascii=True)
    effective["config_hash"] = hashlib.sha256(
        canonical.encode("ascii")
    ).hexdigest()[:16]
    effective["encoding_used"] = metadata.get("encoding_used")
    return effective


def build_output_strategy(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Honest output-strategy declaration for large inputs.

    The parser currently emits exactly one in-memory Document; chunked /
    streaming RepresentationUnit output (chunk_index / continuation /
    byte ranges per chunk) is a representation-layer next-step item and
    is deliberately NOT claimed here.
    """
    size = int(metadata.get("file_size") or 0)
    stats = metadata.get("text_stats") or {}
    return {
        "mode": "single_document",
        "streaming": False,
        "chunked": False,
        "size_bytes": size,
        "line_count": stats.get("line_count"),
        "soft_review_threshold_bytes": _OUTPUT_SOFT_THRESHOLD_BYTES,
        "exceeds_soft_threshold": size > _OUTPUT_SOFT_THRESHOLD_BYTES,
        "note": (
            "one in-memory Document; chunked/streaming unit output with "
            "per-chunk hash/offset/line-range/continuation is a "
            "representation-layer next-step item"
        ),
    }


def enrich_special_module_positions(
    metadata: Dict[str, Any],
    text: str,
    source_text: str,
) -> None:
    """Add line/char/byte positions to ``special_modules`` records in place.

    - ``line_start`` / ``line_end``: 1-based lines on the structured
      view (where the modules were detected);
    - ``char_start`` / ``char_end``: aliases of the existing offsets;
    - ``byte_start`` / ``byte_end``: registered only when char offsets
      are stable against the raw stream (no sanitization deletions /
      injections) AND the codec allows deterministic prefix
      re-encoding; otherwise ``None`` plus ``byte_offset_status`` —
      guessed byte positions would be worse than absent ones.
    """
    modules = metadata.get("special_modules")
    if not isinstance(modules, list) or not modules:
        return

    offsets_stable = normalize_newlines(strip_bom(source_text)) == text
    codec = _BYTE_OFFSET_CODECS.get(
        str(metadata.get("encoding_used") or "").lower()
    )
    byte_ok = offsets_stable and codec is not None
    if offsets_stable and codec is None:
        byte_status = "unavailable:codec_not_byte_deterministic"
    elif not offsets_stable:
        byte_status = "unavailable:offsets_shifted_by_sanitization"
    else:
        byte_status = "ok"

    for record in modules:
        if not isinstance(record, dict):
            continue
        start = record.get("start")
        end = record.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        record["char_start"] = start
        record["char_end"] = end
        record["line_start"] = text.count("\n", 0, start) + 1
        record["line_end"] = text.count("\n", 0, end) + 1
        if byte_ok:
            try:
                record["byte_start"] = len(
                    source_text[:start].encode(codec)  # type: ignore[arg-type]
                )
                record["byte_end"] = len(
                    source_text[:end].encode(codec)  # type: ignore[arg-type]
                )
                record["byte_offset_status"] = "ok"
            except (UnicodeEncodeError, LookupError):
                record["byte_start"] = None
                record["byte_end"] = None
                record["byte_offset_status"] = "unavailable:reencode_failed"
        else:
            record["byte_start"] = None
            record["byte_end"] = None
            record["byte_offset_status"] = byte_status


def collect_txt_engineering_facts(
    metadata: Dict[str, Any],
    source_text: str,
    text: str,
    declared_encoding: str,
    tolerant: bool,
) -> None:
    """Aggregate the engineering-grade facts into ``metadata`` in place."""
    metadata["output_views"] = build_output_views()
    metadata["parse_config"] = build_parse_config(
        metadata, declared_encoding, tolerant
    )
    metadata["output_strategy"] = build_output_strategy(metadata)
    enrich_special_module_positions(metadata, text, source_text)
