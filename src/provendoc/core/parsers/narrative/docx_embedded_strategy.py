"""Embedded-object parent-child strategy layer for DOCX packages.

p2b slice (docx_embedded_strategy_prereg_v1): the spec requires that
embedded objects be registered with a parent/child document model and
that recursive parsing be governed by an explicit policy.  This module
builds that model from three evidence channels only — zip part names,
container magic bytes, and ``o:OLEObject`` ProgID declarations — and
never expands, extracts or executes any embedded part.

v1 policy is register-only (``recursive_parse: false``): embedded
content never enters the narrative text, and ``recursive_parse_status``
is therefore always ``not_attempted``.  Flipping the policy (e.g. once
the DOC CFB parser exists) is a deliberate strategy change, never an
implementation accident.

Discipline: existing keys are never rewritten (``docx_security
.embedded_objects`` and the ``docx_markers`` F8 records stay exactly
as they are); the key is only written when embedding parts exist
(no key, no fabricated records); OLE Package / AcroExch wrappers keep
their container-level ``ole`` verdict — a ProgID is a declaration,
not container evidence.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from docx.oxml.ns import qn

from provendoc.core.parsers.narrative.shared import CFB_MAGIC

_OFFICE_NS = "urn:schemas-microsoft-com:office:office"

_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"

# Container-level format verdicts from magic bytes + part-name suffix.
# The verdict says what the bytes are, never what an application claims
# they contain (a CFB ``oleObject1.bin`` with ProgID ``Package`` is
# ``ole``, not whatever happens to be packaged inside).
_ZIP_SUFFIX_FORMATS = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}
_CFB_SUFFIX_FORMATS = {
    ".doc": "doc",
    ".xls": "xls",
}

_POLICY_NOTE = (
    "registration and container-level format detection only; "
    "embedded parts are never expanded, extracted or executed; "
    "original binary assets stay in the package; macros and "
    "ActiveX controls are never executed"
)


def _read_head(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """First 8 bytes of a part, or None when the member is unreadable."""
    try:
        return archive.read(name)[:8]
    except Exception:
        return None


def _embedded_format(head: bytes | None, part_name: str) -> str:
    """Container-level verdict from magic bytes + part-name suffix."""
    suffix = Path(part_name).suffix.lower()
    if head is None:
        return "unknown"
    if head.startswith(_ZIP_MAGIC):
        return _ZIP_SUFFIX_FORMATS.get(suffix, "zip")
    if head.startswith(CFB_MAGIC):
        return _CFB_SUFFIX_FORMATS.get(suffix, "ole")
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    return "unknown"


def _ole_prog_id_by_part(doc: Any) -> dict[str, str]:
    """Map normalized embeddings part name -> ProgID from o:OLEObject.

    The ``r:id`` attribute of each declaration is resolved through the
    document part relationships; declarations whose target cannot be
    resolved are left to the docx_security evidence channel (never
    fabricated here).
    """
    mapping: dict[str, str] = {}
    try:
        nodes = list(doc.element.body.iter(f"{{{_OFFICE_NS}}}OLEObject"))
    except Exception:
        return mapping
    rels = getattr(doc.part, "rels", {})
    for node in nodes:
        prog_id = node.get("ProgID")
        rid = node.get(qn("r:id"))
        if not prog_id or not rid:
            continue
        try:
            rel = rels[rid]
            part_name = str(rel.target_part.partname)
        except Exception:
            continue
        if part_name.startswith("/"):
            part_name = part_name[1:]
        if "/embeddings/" in part_name:
            mapping[part_name] = prog_id
    return mapping


def extract_embedded_strategy(
    doc, file_path: str | Path, parent_document_id: str | None
) -> dict[str, Any] | None:
    """Build the parent/child registration for embedded package parts.

    Returns None when the package has no ``word/embeddings/`` parts
    (non-empty-write discipline: no key, no records).  Content bytes
    are never read beyond the 8-byte magic head.
    """
    try:
        with zipfile.ZipFile(file_path) as archive:
            part_names = [
                name
                for name in archive.namelist()
                if "/embeddings/" in name
            ]
            if not part_names:
                return None
            prog_id_by_part = _ole_prog_id_by_part(doc)
            records: list[dict[str, Any]] = []
            for index, name in enumerate(part_names, start=1):
                head = _read_head(archive, name)
                records.append(
                    {
                        "embedded_object_id": f"embed-{index}",
                        "source_part": name,
                        "embedded_format": _embedded_format(head, name),
                        "prog_id": prog_id_by_part.get(name),
                        "recursive_parse_status": "not_attempted",
                    }
                )
    except Exception:
        # Silent degradation: an unreadable package simply has no
        # strategy records at this layer (container evidence lives in
        # docx_security.encryption).
        return None

    return {
        "extractor": "docx_embedded_strategy_v1",
        "parent_document_id": parent_document_id,
        "policy": {
            "recursive_parse": False,
            "macro_execution": "prohibited",
            "note": _POLICY_NOTE,
        },
        "records": records,
    }
