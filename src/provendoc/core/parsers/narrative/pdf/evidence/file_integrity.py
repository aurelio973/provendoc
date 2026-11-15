"""File-level integrity evidence (B9, gap register §8).

Records the byte- and object-level facts of a PDF container: header magic
and version, %%EOF/startxref/trailer marker counts (incremental-update
revisions), the health of the last xref target, object-stream and xref-stream
usage, blank pages, and declared standards claims (PDF/A, PDF/X, PDF/UA).

Everything is evidence: a damaged structure is reported (including the fact
that MuPDF repaired it on open), never repaired by this parser, and standards
claims are declared-not-verified — the marker is recorded, conformance is
never asserted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFExtractionResult,
    PDFPage,
)

# Streaming scan window: markers and the startxref value pattern are short
# byte strings (the regex match can span ~20+ bytes), so a 64-byte carry
# between chunks is enough to never split one across a chunk boundary.
_CHUNK_SIZE = 1 << 20
_CARRY_SIZE = 64

# The PDF specification requires the %PDF- magic to sit within the first
# 1024 bytes of the file; MuPDF tolerates (and reports) leading garbage.
_MAGIC = b"%PDF-"
_HEADER_SCAN_WINDOW = 1024
_LINEARIZED_SCAN_WINDOW = 4096

_MARKERS = {
    "eof": b"%%EOF",
    "startxref": b"startxref",
    "trailer": b"trailer",
    "object_stream": b"/ObjStm",
    "xref_stream": b"/XRef",
    "pdfa_output_intent": b"GTS_PDFA",
    "pdfx_output_intent": b"GTS_PDFX",
}

_MAX_BLANK_PAGE_SAMPLES = 50
_MAX_PAGES = 500
_MAX_REPAIR_WARNINGS = 5

_STARTXREF_VALUE_RE = re.compile(rb"startxref\s+(\d+)")
_OBJSTM_HEADER_RE = re.compile(rb"^\d+\s+\d+\s+obj")

_PDF_VERSION_RE = re.compile(rb"%PDF-(\d\.\d)")

_XMP_PDFAID_PART_RE = re.compile(r'pdfaid:part\s*=\s*[\"\'](\d+)')
_XMP_PDFAID_CONF_RE = re.compile(
    r'pdfaid:conformance\s*=\s*[\"\']([A-Za-z]+)'
)


# Raw XMP registration cap: the payload is verbatim up to this size, then
# explicitly truncated (evidence stays bounded but the cut is disclosed).
_XMP_MAX_RAW_CHARS = 65536


def _count_marker(data: bytes, needle: bytes, start: int = 0) -> int:
    return data.count(needle, start)


def _scan_file(path: Path) -> dict[str, Any]:
    """Stream the file once, counting structural markers.

    Counts only matches that touch the freshly read chunk (a marker fully
    inside the carry region was already counted in the previous iteration),
    so straddled markers are counted exactly once.  Compressed content
    streams rarely contain these plaintext markers, so the counts are honest
    raw-pattern evidence — the result note states that.
    """
    counts = {name: 0 for name in _MARKERS}
    last_eof_end: int | None = None
    last_startxref_offset: int | None = None
    file_size = 0
    with open(path, "rb") as handle:
        carry = b""
        offset = 0
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            data = carry + chunk
            base = offset - len(carry)
            for name, needle in _MARKERS.items():
                start = max(0, len(carry) - len(needle) + 1)
                counts[name] += _count_marker(data, needle, start)
            eof_pos = data.rfind(_MARKERS["eof"])
            if eof_pos != -1:
                last_eof_end = base + eof_pos + len(_MARKERS["eof"])
            for match in _STARTXREF_VALUE_RE.finditer(data):
                last_startxref_offset = int(match.group(1))
            carry = data[-_CARRY_SIZE:]
            offset += len(chunk)
            file_size += len(chunk)
    return {
        "counts": counts,
        "file_size_bytes": file_size,
        "last_eof_end": last_eof_end,
        "last_startxref_offset": last_startxref_offset,
    }


def _xref_target_kind(path: Path, offset: int | None) -> str | None:
    """Classify what the last startxref offset actually points at."""
    if offset is None:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            head = handle.read(32)
    except OSError:
        return None
    if head.startswith(b"xref"):
        return "classic"
    if _OBJSTM_HEADER_RE.match(head):
        # An indirect object at the startxref target is an xref stream.
        return "stream"
    return "unknown"


def _repair_warnings() -> list[str]:
    """Collect MuPDF warnings about xref/repair issues, then reset them."""
    try:
        text = fitz.TOOLS.mupdf_warnings(reset=True) or ""
    except Exception:
        return []
    lines = [
        line.strip()
        for line in str(text).splitlines()
        if line.strip()
        and any(key in line.lower() for key in ("xref", "startxref", "repair"))
    ]
    return lines[:_MAX_REPAIR_WARNINGS]


class FileIntegrityExtractor:
    """Extract file-level PDF container facts without modifying anything.

    ``merge_into_pages`` is a passthrough: integrity evidence lives under
    the namespaced metadata key ``pdf_file_integrity`` and never mutates
    page blocks.
    """

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """Extract file-level PDF container integrity evidence."""
        path = Path(file_path)
        raw_scan = _scan_file(path)
        counts = raw_scan["counts"]
        file_size = raw_scan["file_size_bytes"]
        header = self._header_facts(path)

        document_facts = self._document_facts(path)
        return PDFExtractionResult(
            pages=[],
            extraction_method="file_integrity_evidence",
            metadata={
                "pdf_file_integrity": {
                    "file_size_bytes": file_size,
                    "header": header,
                    "revisions": {
                        "eof_marker_count": counts["eof"],
                        "startxref_marker_count": counts["startxref"],
                        "trailer_keyword_count": counts["trailer"],
                        # A well-formed single-revision PDF ends with exactly
                        # one %%EOF; each appended incremental update adds
                        # another.  The estimate is byte-pattern evidence.
                        "incremental_update_estimate": max(
                            0, counts["eof"] - 1
                        ),
                        "multiple_revisions": counts["startxref"] > 1,
                        "bytes_after_last_eof": (
                            file_size - raw_scan["last_eof_end"]
                            if raw_scan["last_eof_end"] is not None
                            and file_size >= raw_scan["last_eof_end"]
                            else None
                        ),
                    },
                    "xref": {
                        "last_startxref_offset": raw_scan[
                            "last_startxref_offset"
                        ],
                        "last_xref_kind": _xref_target_kind(
                            path, raw_scan["last_startxref_offset"]
                        ),
                        "mupdf_repaired": document_facts.get("mupdf_repaired"),
                        "repair_warnings": document_facts.get(
                            "repair_warnings", []
                        ),
                    },
                    # Spec §3 (P2): heavy-recovery registration — when MuPDF
                    # rebuilds xref/objects on open, method + original_preserved
                    # are recorded per the spec's required shape.
                    "recovery": document_facts.get(
                        "recovery",
                        {
                            "performed_by": None,
                            "method": None,
                            "original_preserved": True,
                            "performed_by_self": False,
                            "note": (
                                "document never opened: no recovery was "
                                "performed by MuPDF or by this parser"
                            ),
                        },
                    ),
                    "objects": {
                        "xref_object_count": document_facts.get(
                            "xref_object_count"
                        ),
                        "object_stream_marker_count": counts["object_stream"],
                        "xref_stream_marker_count": counts["xref_stream"],
                        "uses_object_streams": counts["object_stream"] > 0,
                        "uses_xref_streams": counts["xref_stream"] > 0,
                    },
                    "pages": document_facts.get("pages", {}),
                    "standards_claims": document_facts.get(
                        "standards_claims", {}
                    ),
                    # Docinfo (Title/Author/…/TrailerID) verbatim, spec §18.
                    "document_info": document_facts.get("document_info"),
                    "note": (
                        "file-level facts recorded as evidence; marker counts "
                        "are raw byte patterns, broken structures are "
                        "reported (including MuPDF repairs) but never "
                        "repaired by this parser, and standards claims are "
                        "declared-not-verified"
                    ),
                }
            },
        )

    def _header_facts(self, path: Path) -> dict[str, Any]:
        try:
            with open(path, "rb") as handle:
                head = handle.read(
                    max(_HEADER_SCAN_WINDOW, _LINEARIZED_SCAN_WINDOW) + 8
                )
        except OSError:
            head = b""
        match = _PDF_VERSION_RE.search(head[: _HEADER_SCAN_WINDOW + 8])
        offset = head.find(_MAGIC)
        return {
            "magic_ok": 0 <= offset <= _HEADER_SCAN_WINDOW,
            "header_offset": offset if offset >= 0 else None,
            "pdf_version": match.group(1).decode("ascii") if match else None,
            "linearized_marker_present": (
                b"/Linearized" in head[:_LINEARIZED_SCAN_WINDOW]
            ),
        }

    def _document_facts(self, path: Path) -> dict[str, Any]:
        """Document-level facts; every failure degrades to absent keys."""
        facts: dict[str, Any] = {
            "mupdf_repaired": None,
            "repair_warnings": [],
            "xref_object_count": None,
            "pages": {
                "page_count": None,
                "blank_page_count": None,
                "blank_page_numbers": None,
            },
            "standards_claims": self._default_standards_claims(),
            # Docinfo metadata (Title/Author/…) is evidence about the source
            # document: recorded verbatim, never normalized or sanitized.
            "document_info": None,
        }
        try:
            _repair_warnings()  # snapshot/clear warnings before opening
            document = fitz.open(path)
        except Exception:
            return facts
        try:
            facts["mupdf_repaired"] = bool(getattr(document, "is_repaired", False))
            facts["repair_warnings"] = _repair_warnings()
            # Spec §3 (P2): heavy recovery such as xref rebuilds / object
            # scans must be registered explicitly. This parser never
            # rebuilds anything itself; MuPDF's repair-on-open fact is
            # registered in the spec's shape: method + original_preserved
            # (file bytes are never rewritten).
            facts["recovery"] = {
                "performed_by": (
                    "mupdf_on_open" if facts["mupdf_repaired"] else None
                ),
                "method": (
                    "mupdf_rebuilt_xref_or_object_scan"
                    if facts["mupdf_repaired"]
                    else None
                ),
                "original_preserved": True,
                "performed_by_self": False,
                "note": (
                    "this parser never rebuilds xref tables or scans objects "
                    "itself; when MuPDF repairs the document on open, that "
                    "fact (and its warnings) is registered here and the "
                    "original file bytes are never rewritten"
                ),
            }
            try:
                facts["xref_object_count"] = int(document.xref_length())
            # PyMuPDF may raise arbitrary exceptions on a corrupted xref;
            # an absent count is absent evidence — not fatal.
            except Exception:
                pass
            facts["pages"] = self._page_facts(document)
            facts["standards_claims"] = self._standards_claims(document)
            facts["document_info"] = self._document_info_facts(document)
        finally:
            document.close()
        return facts

    @staticmethod
    def _document_info_facts(document: fitz.Document) -> dict[str, Any]:
        """Docinfo metadata + trailer /ID, verbatim (spec §18).

        Missing keys stay absent (or empty as MuPDF reports them) — the
        parser never invents values and never sanitizes privacy-bearing
        fields like Author; disclosure is a downstream policy decision.
        """
        info: dict[str, Any] = {}
        try:
            raw = dict(document.metadata or {})
        except Exception:
            raw = {}
        # Spec names → MuPDF metadata keys; all verbatim, None if absent.
        for spec_key, mupdf_key in (
            ("format", "format"),
            ("Title", "title"),
            ("Author", "author"),
            ("Subject", "subject"),
            ("Keywords", "keywords"),
            ("Creator", "creator"),
            ("Producer", "producer"),
            ("CreationDate", "creationDate"),
            ("ModDate", "modDate"),
            ("Trapped", "trapped"),
            ("Encryption", "encryption"),
        ):
            info[spec_key] = raw.get(mupdf_key)
        try:
            kind, value = document.xref_get_key(-1, "ID")
            info["TrailerID"] = value if kind != "null" else None
        except Exception:
            info["TrailerID"] = None
        # Spec §18 lists XMP metadata explicitly: register the raw XMP
        # verbatim (capped, with an explicit truncation flag) — not just
        # the standards claims distilled from it.
        try:
            xmp = str(document.get_xml_metadata() or "")
        except Exception:
            xmp = ""
        info["XMP"] = {
            "present": bool(xmp.strip()),
            "length": len(xmp),
            "truncated": len(xmp) > _XMP_MAX_RAW_CHARS,
            "raw": xmp[:_XMP_MAX_RAW_CHARS],
        }
        return info

    @staticmethod
    def _page_facts(document: fitz.Document) -> dict[str, Any]:
        """Count blank pages (no text, no images) as evidence.

        A blank page is a fact about the source (scanned backsides, spacer
        pages); it is recorded, never removed from the page sequence.
        """
        page_count = int(document.page_count)
        blank: list[int] = []
        for index in range(min(page_count, _MAX_PAGES)):
            try:
                page = document[index]
                if page.get_text().strip():
                    continue
                if page.get_images(full=True):
                    continue
                blank.append(index + 1)
            except Exception:
                continue
        return {
            "page_count": page_count,
            "blank_page_count": len(blank),
            "blank_page_numbers": blank[:_MAX_BLANK_PAGE_SAMPLES],
        }

    @staticmethod
    def _default_standards_claims() -> dict[str, Any]:
        """Claim template shared by success and open-failure paths."""
        return {
            "pdfa_claimed": False,
            "pdfa_part": None,
            "pdfa_conformance": None,
            "pdfx_claimed": False,
            "pdfua_claimed": False,
        }

    @staticmethod
    def _standards_claims(document: fitz.Document) -> dict[str, Any]:
        """Record declared (never verified) PDF/A, PDF/X, PDF/UA claims."""
        claims: dict[str, Any] = {
            "pdfa_claimed": False,
            "pdfa_part": None,
            "pdfa_conformance": None,
            "pdfx_claimed": False,
            "pdfua_claimed": False,
        }
        try:
            xmp = str(document.get_xml_metadata() or "")
        except Exception:
            xmp = ""
        if "pdfaid:part" in xmp:
            claims["pdfa_claimed"] = True
            part = _XMP_PDFAID_PART_RE.search(xmp)
            conformance = _XMP_PDFAID_CONF_RE.search(xmp)
            claims["pdfa_part"] = (
                int(part.group(1)) if part else None
            )
            claims["pdfa_conformance"] = (
                conformance.group(1) if conformance else None
            )
        if "pdfuaid" in xmp or "PDF/UA" in xmp:
            claims["pdfua_claimed"] = True
        # PDF/X identification lives either in the XMP (pdfxid:GTS_PDFX)
        # or in a GTS_PDFX OutputIntent on the catalog — both are
        # declarations, never conformance verdicts.
        if "GTS_PDFX" in xmp:
            claims["pdfx_claimed"] = True
        try:
            kind, value = document.xref_get_key(
                document.pdf_catalog(), "OutputIntents"
            )
            if kind not in ("null", "unknown") and value and "GTS_PDFX" in str(value):
                claims["pdfx_claimed"] = True
        # The OutputIntents probe may raise arbitrary exceptions on a
        # corrupted catalog; no claim means no record.
        except Exception:
            pass
        return claims

    @staticmethod
    def merge_into_pages(
        text_pages: list[PDFPage],
        integrity_pages: list[PDFPage],
    ) -> list[PDFPage]:
        """Passthrough: integrity evidence lives in metadata, never in pages."""
        return text_pages


__all__ = ["FileIntegrityExtractor"]
