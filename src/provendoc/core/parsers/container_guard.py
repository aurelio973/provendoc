# -*- coding: utf-8 -*-
"""ZIP container guard — pre-decompression central-directory limits (zip-bomb gate).

Contract source: document_ingestion_security_register_v1.md
§75 v1 priority item "zip bomb protection", plus §48-49 (Office-ZIP specifics)
and §63 (partition storage limit semantics). Per the §75 freeze clause it is a
built-in pre-check inside the existing parse() entries, not a separate service.

Why "before opening":

- docx/xlsx/pptx/epub/ods are all ZIP containers; python-docx / openpyxl /
  python-pptx allocate and decompress members as soon as they open the archive.
  A classic 46KB zip bomb declares gigabytes uncompressed — counting bytes
  after decompression starts is too late.
- The guard only reads the central directory (``zipfile.ZipFile.infolist()``
  decompresses nothing) and delivers a verdict before the first member is
  extracted: member count / per-member uncompressed bytes / total uncompressed
  bytes / compression ratio / encrypted members.

Division of labor with ArchiveExpander (no duplication):

- ``ArchiveExpander`` (ingestion time) counts real bytes while **streaming
  decompression** — the second line of defense against a lying central
  directory (declared small, actually streams huge);
- this guard (parser time) pre-rejects on declared values **without
  decompressing** — the first line so memory/CPU never get burned. Limit
  semantics are identical; the numbers are independent.

Honesty discipline (same as the whole repo):

- Violations never raise: an honest-failure evidence dict is returned and the
  parser records ``parse_failed`` + ``zip_package_guard`` in metadata;
- The guard itself is never fatal: if the central directory cannot be read
  (file is not a zip at all), it degrades to an ``unreadable`` evidence and
  lets the file through — the real failure surfaces naturally from later
  parsing; the guard never answers on its behalf.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

GUARD_VERSION = "1.0"
"""限额契约版本（语义变更时递增，同 PDFRuntimeLimits 纪律）。"""

# ZIP general-purpose encryption flag bit (bit 0). Encrypted members are never
# decrypted — only reported as evidence (register §68: decryption never happens
# in this library).
_ENCRYPTED_FLAG_MASK = 0x1


@dataclass(frozen=True)
class ZipPackageLimits:
    """Pre-decompression limits for ZIP containers (OOXML/ODF/EPUB).

    All are guards, not filters: exceeding a limit refuses parsing (honest
    failure), but the document and all other evidence stay complete in
    metadata — nothing is silently dropped.
    """

    max_members: int = 4096
    # Per-member uncompressed cap (512MB: accommodates large Office docs with
    # embedded images, blocks GB-scale single-stream bombs).
    max_member_bytes: int = 512 * 1024 * 1024
    # Whole-package uncompressed cap (2GB: a single-document container should
    # never exceed this magnitude).
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    # Per-member compression-ratio cap (normal XML/image containers stay
    # < 20:1; the deflate theoretical limit is ~1032:1, > 500:1 can only be a
    # deliberate padding bomb).
    max_compression_ratio: int = 500

    def as_dict(self) -> Dict[str, Any]:
        """Serialize to a dict (the limits snapshot stored in metadata)."""
        return {
            "version": GUARD_VERSION,
            "max_members": self.max_members,
            "max_member_bytes": self.max_member_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }


# Default limits for parser wiring (module-level singleton, tests may replace
# it wholesale).
DEFAULT_ZIP_LIMITS = ZipPackageLimits()


def check_zip_package(
    file_path: str | Path,
    limits: ZipPackageLimits | None = None,
) -> Dict[str, Any]:
    """Read only the central directory for a pre-decompression verdict; never decompress member data.

    Returns:
        Evidence dict; ``verdict`` is ``ok`` / ``violation`` / ``unreadable``
        (central directory unreadable — let later parsing fail naturally).
        facts is always JSON-serializable (goes into document.metadata).
    """
    limits = limits or DEFAULT_ZIP_LIMITS

    try:
        with zipfile.ZipFile(file_path) as archive:
            infos = archive.infolist()
    except Exception as error:
        return {
            "verdict": "unreadable",
            "error": str(error),
            "limits": limits.as_dict(),
        }

    member_count = len(infos)
    total_uncompressed = sum(int(info.file_size) for info in infos)
    total_compressed = sum(int(info.compress_size) for info in infos)
    encrypted_members = sum(
        1 for info in infos if info.flag_bits & _ENCRYPTED_FLAG_MASK
    )

    max_member = max(
        (int(info.file_size) for info in infos), default=0,
    )
    max_ratio = 0.0
    for info in infos:
        if info.compress_size > 0:
            ratio = int(info.file_size) / int(info.compress_size)
            if ratio > max_ratio:
                max_ratio = ratio

    violations: list[str] = []
    if member_count > limits.max_members:
        violations.append(
            f"member count {member_count} exceeds max_members "
            f"{limits.max_members}"
        )
    if max_member > limits.max_member_bytes:
        largest = max(infos, key=lambda i: int(i.file_size))
        violations.append(
            f"member '{largest.filename}' declares {max_member} bytes "
            f"uncompressed, exceeds max_member_bytes "
            f"{limits.max_member_bytes}"
        )
    if total_uncompressed > limits.max_total_bytes:
        violations.append(
            f"total uncompressed {total_uncompressed} bytes exceeds "
            f"max_total_bytes {limits.max_total_bytes}"
        )
    if max_ratio > limits.max_compression_ratio:
        violations.append(
            f"compression ratio {max_ratio:.1f}:1 exceeds "
            f"max_compression_ratio {limits.max_compression_ratio}:1"
        )

    facts: Dict[str, Any] = {
        "verdict": "violation" if violations else "ok",
        "limits": limits.as_dict(),
        "facts": {
            "members": member_count,
            "total_uncompressed_bytes": total_uncompressed,
            "total_compressed_bytes": total_compressed,
            "max_member_uncompressed_bytes": max_member,
            "max_compression_ratio": round(max_ratio, 2),
            "encrypted_members": encrypted_members,
        },
    }
    if violations:
        facts["violations"] = violations
    if encrypted_members:
        # Encrypted members are not a violation (legitimate password-protected
        # docs exist) but must be reported: decryption never happens, so their
        # content is inaccessible by design.
        facts["notes"] = (
            "encrypted members present; decryption never happens in "
            "this library, their content is inaccessible by design"
        )
    return facts


def check_zip_package_quietly(
    file_path: str | Path,
    limits: ZipPackageLimits | None = None,
) -> Dict[str, Any] | None:
    """Defensive wrapper around the guard: any unexpected exception is swallowed, returning ``None``.

    Parser wiring discipline (same repo-wide honest-failure policy): the guard
    itself is never fatal — when the central directory cannot be read (not a
    zip, an OLE container, permission issues) the file is handed to later
    parsing to fail naturally; the guard neither answers nor blocks.
    """
    try:
        return check_zip_package(file_path, limits)
    except Exception:  # noqa: BLE001 — 守卫永不致命
        return None


def zip_guard_error(evidence: Dict[str, Any]) -> str:
    """Violation evidence → parse_error message (unified wording, shared by parsers)."""
    violations = evidence.get("violations") or ["unknown zip package violation"]
    return "zip package bomb guard: " + "; ".join(violations)


__all__ = [
    "ZipPackageLimits",
    "DEFAULT_ZIP_LIMITS",
    "check_zip_package",
    "check_zip_package_quietly",
    "zip_guard_error",
]
