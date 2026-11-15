"""Document / chunk identity contract (doc_id / chunk_id).

Multi-file ingestion and incremental change detection need stable
content identity. This module defines a two-level identity contract
(both non-cryptographic hashes — stability against collisions is
the only goal, not attack resistance):

Two-level change detection
==========================

- Byte-level ``source_sha256``: sha256 of the raw file bytes.
  Identical bytes = identical file (the first test for upload
  deduplication); changed bytes whose cleaned text is unchanged
  (e.g. line-ending/BOM/whitespace differences) count as
  content-equivalent.
- Semantic-level ``doc_id``: first 12 hex chars of the sha256 of
  the normalized cleaned text. Content equivalence = same doc_id.

Contract format
===============

- ``doc_id``   = sha256(normalized_text).hexdigest()[:12]
- ``chunk_id`` = ``{doc_id8}:{index:05d}:{text_hash10}``

  - ``doc_id8``: first 8 chars of doc_id — the first segment
    isolating a document from others in the same library (prefix
    collisions within a library are doubly backstopped by the
    12-char doc_id plus the file-level source_sha256);
  - ``index:05d``: in-document chunk ordinal, 5 decimal digits
    (no overflow below 100k chunks — the 05d width removes the
    3-digit overflow disorder seen with thousand-chunk PDFs);
  - ``text_hash10``: first 10 chars of the chunk text sha256 —
    the content fingerprint.

Change detection semantics
==========================

- Rerun stability: same input → same doc_id, same chunk_id set
  (deterministic; no timestamps or randomness);
- Localized changes: chunks before the change point keep their
  text_hash; chunks after it get a shifted index segment — but
  the text_hash segment still provides a matching anchor for the
  old chunk content (shifted chunks align with the previous
  version via text_hash);
- Multi-file: doc_id/chunk_id are per-document identities;
  different documents are isolated by the doc_id8 prefix.

Landing points
==============

- ``document.metadata["doc_identity"]``: {doc_id, doc_id_scheme,
  chunk_id_scheme, source_sha256 (when available)};
- ``unit.metadata["chunk_id"]``: the in-document identity of every
  representation-layer chunk.

Relation to RepresentationUnit.semantic_primitives.identity:
identity is the runtime key for cross-reference matching between
chunks, fallback format ``{unit_type}:{sequence_index}:{text_hash10}``;
chunk_id is the cross-run persistent identity, attached to metadata —
the two do not replace each other.
"""

import hashlib
from typing import Any, Dict, Iterable, List, Optional

DOC_ID_LENGTH = 12
DOC_ID_PREFIX_LENGTH = 8
CHUNK_TEXT_HASH_LENGTH = 10
INDEX_WIDTH = 5  # 05d — no overflow below 100k chunks

_DOC_ID_SCHEME = "sha256_cleaned_text_12"
_CHUNK_ID_SCHEME = "docid8:index05d:texthash10"


def normalize_for_identity(text: str) -> str:
    """Normalize before identity hashing — removes only representation
    differences that carry no semantics.

    Line endings are unified to ``\\n``; leading/trailing whitespace
    stripped. No NFC/case normalization — that would change the
    granularity of content identity (case differences are semantic
    in the code domain).
    """
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def compute_doc_id(text: str) -> str:
    """Cleaned text → 12-hex-char doc_id (non-cryptographic, content fingerprint)."""
    normalized = normalize_for_identity(text)
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()[:DOC_ID_LENGTH]


def _text_hash10(text: str) -> str:
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()[:CHUNK_TEXT_HASH_LENGTH]


def compute_source_sha256(file_path: str) -> Optional[str]:
    """Raw file bytes → sha256 (byte-level change criterion). Returns None on read failure."""
    try:
        digest = hashlib.sha256()
        with open(file_path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def stamp_document_identity(
    document: Any,
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute and write document.metadata['doc_identity'] (idempotent).

    doc_id is based on document.text (the caller guarantees cleaned
    text); source_sha256 first reuses the metadata['sha256'] already
    written by the parser (DOCX path), otherwise is computed from
    file_path, and defaults to absent when neither exists — the
    byte-level criterion is honestly missing, never faked with a
    placeholder.
    """
    metadata = getattr(document, "metadata", None) or {}
    identity: Dict[str, Any] = {
        "doc_id": compute_doc_id(getattr(document, "text", "") or ""),
        "doc_id_scheme": _DOC_ID_SCHEME,
        "chunk_id_scheme": _CHUNK_ID_SCHEME,
    }

    source_sha256 = metadata.get("sha256")
    if not source_sha256 and file_path:
        source_sha256 = compute_source_sha256(file_path)
    if source_sha256:
        identity["source_sha256"] = source_sha256

    if not getattr(document, "metadata", None):
        try:
            document.metadata = metadata
        except AttributeError:
            return identity
    document.metadata["doc_identity"] = identity
    return identity


def stamp_chunk_ids(
    units: Iterable[Any],
    doc_id: str,
) -> List[str]:
    """Write metadata['chunk_id'] onto each unit by in-document ordinal.

    Idempotent: overwrite-style writes (rerunning the same input
    yields the same result). Returns the chunk_id list (in input
    order). Units without a metadata dict are skipped
    (RepresentationUnit always has one; guards against bare
    external objects).
    """
    chunk_ids: List[str] = []
    prefix = doc_id[:DOC_ID_PREFIX_LENGTH]
    for index, unit in enumerate(units):
        metadata = getattr(unit, "metadata", None)
        if not isinstance(metadata, dict):
            chunk_ids.append("")
            continue
        chunk_id = (
            f"{prefix}:{index:0{INDEX_WIDTH}d}:"
            f"{_text_hash10(unit.text)}"
        )
        metadata["chunk_id"] = chunk_id
        chunk_ids.append(chunk_id)
    return chunk_ids


def stamp_representation_identity(
    representation_result: Any,
    document: Any,
    file_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One-step wiring: doc identity + chunk ids (additive, idempotent).

    Called by universal_parser and the SDK client after representation
    assembly. Returns the doc_identity dict (None on failure — a
    missing identity must not block the main flow, same discipline
    as the provenance pass).
    """
    try:
        identity = stamp_document_identity(document, file_path)

        content = getattr(representation_result, "content", None)
        units = content
        if units is None:
            return identity
        if hasattr(units, "base"):
            units = units.base
        elif hasattr(units, "all_units"):
            units = units.all_units()

        stamp_chunk_ids(units, identity["doc_id"])

        # Result-level summary (consumers read doc_id without
        # iterating units)
        result_metadata = getattr(representation_result, "metadata", None)
        if isinstance(result_metadata, dict):
            result_metadata.setdefault("doc_id", identity["doc_id"])
        return identity
    except Exception:
        return None
