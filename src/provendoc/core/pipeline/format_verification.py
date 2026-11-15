# -*- coding: utf-8 -*-
"""Format magic-number gate — extension declaration cross-checked against file magic.

Design (the first validation layer for this local SDK library):

- **Only binary formats with magic numbers are verified** (PDF /
  ZIP family / OLE / stream compression / tar / parquet). Text
  formats (txt/md/csv/json/yaml/code/...) have no magic and
  honestly return ``no_magic_expected`` — no fake validation.
- **mismatch = honest failure**: when the declared extension does
  not match the actual container family (a `.pdf` that is really a
  ZIP, a `.docx` that is really OLE, ...), the entry layer builds a
  parse_failed document with full evidence (observed container
  family, file-header hex digest, candidate interpretations) and
  **never hands the file to the declared format's parser to try** —
  the "do not feed it to a generic parser" rule.
- **Evidence first**: the verdict itself is serializable into
  document.metadata (``format_verification`` key) so downstream can
  audit "was this file verified at intake".
- **Lenient exemptions**: truncated/empty files ->
  ``insufficient_bytes`` (handed to the parser for its own
  honest-failure handling); old tar without the ustar magic ->
  ``undecided`` (no false kills of historical formats); the PDF
  spec allows a preamble before %PDF-, so the first 1024 bytes are
  searched.

Relations to existing parser-layer capabilities (no duplication,
no replacement):

- docx/pptx ``classify_container`` handles the finer split of
  "OOXML suffix but OLE container" (MS-OFFCRYPTO encrypted vs
  legacy binary) — when this gate stops the same kind of file at a
  more outer layer, the evidence notes "classification details:
  docx_security"; callers using the parsers directly still take the
  classify_container path.
- Decompression-bomb defense for ZIP containers belongs to
  ``container_guard`` (central-directory limits before extraction);
  this module only answers "what container family is this".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# File-header length for observation: covers every short magic
# except the tar magic offset (257); the PDF preamble search window
# also uses it.
_HEADER_BYTES = 1024

# Fixed tar magic offset (POSIX ustar).
_TAR_MAGIC_OFFSET = 257

# Trailing-data probing: tail search window size (covers common
# EOCD/%%EOF offsets).
_TAIL_WINDOW = 65536

# Whitespace bytes tolerated after a container end marker
# (EOL/NUL/space).
_TOLERATED_TRAILING_WHITESPACE = b"\x00\x09\x0a\x0d\x20"


# ------------------------------------------------------------
# Container-family magic table: (family, offset, magic). Order is
# the candidate presentation order.
# ------------------------------------------------------------

_MAGIC_SIGNATURES: Tuple[Tuple[str, int, bytes], ...] = (
    ("zip", 0, b"PK\x03\x04"),      # standard local file header
    ("zip", 0, b"PK\x05\x06"),      # empty archive (EOCD only)
    ("zip", 0, b"PK\x07\x08"),      # spanned-archive marker
    ("ole_cfb", 0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),  # OLE2 CFB
    ("gzip", 0, b"\x1f\x8b"),
    ("bzip2", 0, b"BZh"),
    ("xz", 0, b"\xfd7zXZ\x00"),
    ("parquet", 0, b"PAR1"),
    ("tar", _TAR_MAGIC_OFFSET, b"ustar"),
)

# PDF special case: preamble tolerated; any position within the
# first 1024 bytes counts.
_PDF_MAGIC = b"%PDF-"


def _observed_family(head: bytes) -> Optional[str]:
    """Identify the container family from the file header; PDF uses the preamble search, the rest exact offset matching."""
    if _PDF_MAGIC in head:
        return "pdf"
    for family, offset, magic in _MAGIC_SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return family
    return None


# ------------------------------------------------------------
# Suffix -> expected container family (single source of truth;
# only binary formats with magic are registered)
# ------------------------------------------------------------

_ZIP_FAMILY_SUFFIXES = frozenset({
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xlsb", ".pptx", ".epub", ".ods",
    # PowerPoint-family OpenXML variants
    ".pptm", ".ppsx", ".ppsm", ".potx", ".potm",
    # ODP adjacent route: ODF zip container
    ".odp",
    # ODP-family deepening: .otp template zip shares the chain;
    # .fodp flat file / .sti legacy extension carry no zip-magic
    # contract (no_magic_expected channel, routed by content)
    ".otp",
    ".zip", ".jar", ".war", ".ear", ".apk", ".whl", ".egg",
})

# .pps/.pot — PowerPoint-family legacy CFB slideshow/template
# suffixes; the .pot gettext text ambiguity is settled naturally by
# the container magic (non-CFB means mismatch rejection)
_OLE_FAMILY_SUFFIXES = frozenset(
    {".doc", ".dot", ".xls", ".ppt", ".pps", ".pot"}
)

# ODP-family zip suffixes: they declare a zip container, but Flat
# ODF XML flat files (no container magic, an XML document at the
# head) are a legal variant — tolerated and passed to OdpParser
# for content-based routing (content identification first).
# .fodp/.sti carry no zip-magic contract (no_magic_expected
# channel) and are not in this table.
_ODP_ZIP_FAMILY_SUFFIXES = frozenset({".odp", ".otp"})


def _looks_like_xml_document(head: bytes) -> bool:
    """Head strips to a ``<?xml`` or ``<`` start (after BOM/whitespace) -> XML document candidate."""
    stripped = head.lstrip(b"\xef\xbb\xbf\x09\x0a\x0d\x20")
    return stripped.startswith((b"<?xml", b"<"))

_EXPECTED_FAMILY_BY_SUFFIX: Dict[str, str] = {
    ".pdf": "pdf",
    ".gz": "gzip",
    ".bz2": "bzip2",
    ".xz": "xz",
    ".tar": "tar",
    ".parquet": "parquet",
}
for _suffix in _ZIP_FAMILY_SUFFIXES:
    _EXPECTED_FAMILY_BY_SUFFIX[_suffix] = "zip"
for _suffix in _OLE_FAMILY_SUFFIXES:
    _EXPECTED_FAMILY_BY_SUFFIX[_suffix] = "ole_cfb"


def _nearest_declared_suffixes(observed: str) -> List[str]:
    """Suffixes registered in the table for the observed container family (user-facing candidates)."""
    return sorted(
        suffix
        for suffix, family in _EXPECTED_FAMILY_BY_SUFFIX.items()
        if family == observed
    )


def verify_format(file_path: str | Path, suffix: str) -> Dict[str, Any]:
    """Verify that the declared suffix agrees with the file magic.

    Args:
        file_path: file path (reads the header only; never modifies
            the file).
        suffix: normalized lowercase suffix (with dot; the caller
            has already handled dotfile/bare-name normalization —
            same criteria as UniversalParser.parse).

    Returns:
        Evidence dict, ``verdict`` values:

        - ``no_magic_expected`` — text format, no magic to verify
          (pass);
        - ``match`` — declaration agrees with the magic (pass);
        - ``insufficient_bytes`` — file too short to decide (let the
          parser's honest-failure path handle it);
        - ``undecided`` — a legal no-magic variant (pre-POSIX tar)
          (pass);
        - ``mismatch`` — declaration disagrees with the actual
          container family (the caller should reject as an honest
          failure, never handing the file to the declared format's
          parser).
    """
    expected = _EXPECTED_FAMILY_BY_SUFFIX.get(suffix)
    if expected is None:
        return {
            "declared_suffix": suffix,
            "expected_family": None,
            "observed_family": None,
            "verdict": "no_magic_expected",
        }

    try:
        with open(file_path, "rb") as handle:
            head = handle.read(_HEADER_BYTES)
    except OSError as error:
        # Unreadable file (concurrent deletion etc.): let the caller
        # surface the error naturally; no answer on its behalf.
        return {
            "declared_suffix": suffix,
            "expected_family": expected,
            "observed_family": None,
            "verdict": "unreadable",
            "error": str(error),
        }

    observed = _observed_family(head)

    if observed == expected:
        return {
            "declared_suffix": suffix,
            "expected_family": expected,
            "observed_family": observed,
            "verdict": "match",
        }

    # Pre-POSIX (v7) tar carries no ustar magic — honest pass, no
    # false kill.
    if expected == "tar" and observed is None:
        return {
            "declared_suffix": suffix,
            "expected_family": expected,
            "observed_family": None,
            "verdict": "undecided",
            "note": "pre-POSIX tar has no magic; container family unverifiable",
        }

    # Flat ODF XML: an ODP-family zip suffix + an XML document head
    # is a legal flat-file variant — pass to OdpParser for
    # content-based routing (content identification first; the
    # format_mismatch record is written at the parser layer); a
    # genuine wrong container (PDF/OLE head) is still a mismatch
    # rejection.
    if (
        observed is None
        and suffix in _ODP_ZIP_FAMILY_SUFFIXES
        and _looks_like_xml_document(head)
    ):
        return {
            "declared_suffix": suffix,
            "expected_family": expected,
            "observed_family": None,
            "verdict": "undecided",
            "note": (
                "Flat ODF XML (no container magic) under an "
                "ODP-family zip suffix; routed by content in the "
                "ODP parser"
            ),
        }

    if not head:
        return {
            "declared_suffix": suffix,
            "expected_family": expected,
            "observed_family": None,
            "verdict": "insufficient_bytes",
        }

    evidence: Dict[str, Any] = {
        "declared_suffix": suffix,
        "expected_family": expected,
        "observed_family": observed,
        "verdict": "mismatch",
        "head_hex": head[:16].hex(),
    }
    if observed is not None:
        evidence["observed_family_suffixes"] = _nearest_declared_suffixes(
            observed
        )
        if observed == "ole_cfb" and expected == "zip":
            evidence["note"] = (
                "OLE/CFB container under an Open XML suffix: legacy "
                "binary Office document or MS-OFFCRYPTO encrypted "
                "package (classification details: docx_security)"
            )
    else:
        evidence["note"] = (
            "no known container magic in the file header; the declared "
            "suffix is not supported by the observed bytes"
        )
    return evidence


def format_mismatch_error(evidence: Dict[str, Any]) -> str:
    """mismatch evidence -> parse_error message (unified wording, shared by both entry points)."""
    observed = evidence.get("observed_family")
    declared = evidence.get("declared_suffix")
    expected = evidence.get("expected_family")
    if observed is None:
        return (
            f"format verification failed: {declared} declares a "
            f"{expected} container but the file header matches no known "
            "container magic"
        )
    return (
        f"format verification failed: {declared} declares a "
        f"{expected} container but the file is actually {observed} "
        "(extension/content mismatch)"
    )


# ------------------------------------------------------------
# Trailing-data probing (polyglot files and trailing data)
#
# Only formats with a **reliable end anchor** are probed: PDF (the
# last %%EOF; with incremental updates all earlier %%EOF sit before
# it), ZIP (the last EOCD + comment length; ZIP64 records/signature
# blocks sit before the EOCD; an SFX prefix does not affect the
# tail verdict), parquet (the trailing PAR1 magic). tar (block
# padding is legal) and gzip/bzip2/xz (multi-stream concatenation
# is legal) have loose end semantics — honestly skipped, no fake
# probe that cannot tell legal from anomalous.
#
# Behavior rule: trailing data is **warning evidence, not a
# rejection reason** — the probe result is written into the
# format_verification evidence for audit and manual analysis; when
# the trailing data itself carries a known container magic (a true
# polyglot), ``polyglot_detected`` is set and the caller broadcasts
# a warning. Guards are non-fatal.
# ------------------------------------------------------------


def _pdf_trailing_evidence(
    file_size: int, tail: bytes, window: int,
) -> Optional[Dict[str, Any]]:
    """PDF: non-whitespace bytes after the last %%EOF are trailing data."""
    anchor = tail.rfind(b"%%EOF")
    if anchor < 0:
        return {
            "detected": False,
            "note": "no %%EOF anchor in the tail window; truncated PDF "
            "is left to the parser's honest-failure path",
        }
    end = anchor + len(b"%%EOF")
    rest = tail[end:]
    stripped = rest.strip(_TOLERATED_TRAILING_WHITESPACE)
    if not stripped:
        return {"detected": False}
    trailing_offset = file_size - (len(tail) - end)
    return {
        "detected": True,
        "anchor": "%%EOF",
        "offset": trailing_offset,
        "length": len(stripped),
        "head_hex": stripped[:16].hex(),
    }


def _zip_trailing_evidence(
    file_size: int, tail: bytes, window: int,
) -> Optional[Dict[str, Any]]:
    """ZIP: data after the last EOCD (including its comment length) is trailing data."""
    anchor = tail.rfind(b"PK\x05\x06")
    if anchor < 0:
        return {
            "detected": False,
            "note": "no EOCD anchor in the tail window; corrupted "
            "zip is left to the container guard's evidence path",
        }
    # EOCD minimum 22 bytes: sig(4)+disk(2)+cd_disk(2)+entries(2)+
    # entries_total(2)+cd_size(4)+cd_offset(4)+comment_len(2)
    if len(tail) - anchor < 22:
        return {
            "detected": True,
            "anchor": "EOCD",
            "note": "EOCD structure is truncated",
        }
    comment_len = int.from_bytes(tail[anchor + 20 : anchor + 22], "little")
    eocd_end = anchor + 22 + comment_len
    rest = tail[eocd_end:]
    if not rest:
        return {"detected": False}
    trailing_offset = file_size - (len(tail) - eocd_end)
    return {
        "detected": True,
        "anchor": "EOCD",
        "offset": trailing_offset,
        "length": len(rest),
        "head_hex": rest[:16].hex(),
    }


def _parquet_trailing_evidence(
    file_size: int, tail: bytes, window: int,
) -> Optional[Dict[str, Any]]:
    """parquet: the file tail must be the PAR1 magic, otherwise the tail structure is anomalous."""
    if tail.endswith(b"PAR1"):
        return {"detected": False}
    return {
        "detected": True,
        "anchor": "PAR1_tail",
        "note": "file does not end with the PAR1 magic; trailing "
        "region length cannot be located precisely (honest)",
    }


_TRAILING_PROBES = {
    "pdf": _pdf_trailing_evidence,
    "zip": _zip_trailing_evidence,
    "parquet": _parquet_trailing_evidence,
}


def detect_trailing_data(
    file_path: str | Path, suffix: str,
) -> Optional[Dict[str, Any]]:
    """Probe for anomalous trailing data after the legal container end anchor (polyglot files).

    Args:
        file_path: file path (reads the tail window only; never
            modifies the file).
        suffix: normalized lowercase suffix (with dot).

    Returns:
        Evidence dict (see the probes); when ``detected=True`` it
        also carries ``polyglot_detected`` (the trailing data head
        holds a known container magic). Formats without a reliable
        end anchor (tar/gzip family/text formats/unknown suffixes)
        -> None — honestly not probed. Unreadable file -> None (let
        the caller surface the error naturally).
    """
    expected = _EXPECTED_FAMILY_BY_SUFFIX.get(suffix)
    probe = _TRAILING_PROBES.get(expected or "")
    if probe is None:
        return None
    try:
        with open(file_path, "rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            window = min(file_size, _TAIL_WINDOW)
            if window:
                handle.seek(-window, 2)
            tail = handle.read(window)
    except OSError:
        return None
    if not tail:
        return None
    evidence = probe(file_size, tail, window)
    if evidence is None:
        return None
    if evidence.get("detected") and "head_hex" in evidence:
        head = bytes.fromhex(evidence["head_hex"])
        embedded = _observed_family(head)
        evidence["polyglot_detected"] = embedded is not None
        if embedded is not None:
            evidence["embedded_family"] = embedded
    return evidence


__all__ = [
    "verify_format",
    "format_mismatch_error",
    "detect_trailing_data",
]
