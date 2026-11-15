"""Version manifest — source-anchored, content-addressed version
lineage.

Identity and version contract for multi-file upload / file
replacement. Relation to the two-level identity in ``doc_identity``:

- ``doc_identity`` answers "which document is this, which chunk is
  this" (identity within one document);
- ``version_manifest`` answers "which source and which revision is
  this, and how much changed since the previous revision" (across
  versions).

Root-anchoring invariant
========================

Every hash is rooted in the **user-uploaded original byte document**
(source):

- ``source_sha256``: sha256 of the original file bytes — computed
  from ``file_path``, or reusing the ``metadata['sha256']`` the
  parser wrote before parsing (both are the original bytes, never
  a post-parser artifact);
- ``doc_id``: fingerprint of the cleaned text (semantic layer,
  tolerant of byte-level noise);
- ``section_hash``: fingerprint of the parse-stage structural
  evidence (heading_path groups of docx_blocks/pdf_blocks) —
  anchored to parser output blocks, **never based on post-chunking
  text** (chunk_size changes would cascade-invalidate the anchor;
  the section layer must stay decoupled);
- ``version_id``: content-addressed revision number (git-style) —
  sha256 over (source_uri, source_sha256, doc_id); same content
  means same id, deduplication for free.

The section layer is optional: documents without structural
evidence (e.g. txt) get an empty ``sections`` list and an honestly
missing ``section_evidence`` — no fabricated placeholder structure.

Version lineage
===============

A git-style DAG, not a monotonically increasing counter:

- ``version_id``: content-addressed (above);
- ``parent_version_id``: points at the previous revision's
  version_id, None for the first revision. Callers (upload/replace
  entry points) pass the old manifest when they hold one; lineage
  persistence is the caller's job — this module is stateless.

Change classification chain (classify_change)
==============================================

Priority from top to bottom:

1. ``no-op``             — same source_sha256 (bytes unchanged,
                           same file);
2. ``content-equivalent``— bytes changed but doc_id is the same
                           (line-ending/BOM/whitespace noise, no
                           semantic change);
3. ``incremental``       — same source_uri, doc_id changed, but the
                           section layers intersect (some section
                           hashes match the old revision → partial
                           edit);
4. ``full``              — no old revision / source_uri changed / no
                           section anchor hit (whole rewrite).

Wiring
======

- ``document.metadata["version_manifest"]``: additive key, silent
  degradation on failure (same discipline as provenance /
  doc_identity — missing version info must not block the main
  flow);
- See ``stamp_version_manifest`` for SDK / universal_parser
  wiring.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from provendoc.core.representations.doc_identity import (
    compute_doc_id,
    compute_source_sha256,
)

_log = logging.getLogger(__name__)

MANIFEST_SCHEME = "source_uri+source_sha256+doc_id_v1"
VERSION_ID_LENGTH = 16
SECTION_HASH_SCHEME = "sha256_parse_blocks_by_headingpath_v1"

# Processing binding: fingerprint of the parser/chunker code versions
# and the chunking configuration. **Not part of the version_id hash
# material** — version_id is a content-addressed identity (same
# content, same id); the binding is a separate "which code + which
# config produced this result" dimension, used for cache keys and
# result-binding validation.
PROCESSING_BINDING_SCHEME = "parser_chunker_config_v1"
CACHE_KEY_SCHEME = "version_id+processing_binding_v1"

# Parse-stage structural evidence keys (format-agnostic; order is priority).
_STRUCTURE_EVIDENCE_KEYS: Tuple[str, ...] = (
    "pdf_blocks",
    "docx_blocks",
)

CHANGE_NO_OP = "no-op"
CHANGE_CONTENT_EQUIVALENT = "content-equivalent"
CHANGE_INCREMENTAL = "incremental"
CHANGE_FULL = "full"


# ============================================================
# Section layer (optional) — anchored to parse-stage structural evidence
# ============================================================

def _structure_evidence(
    metadata: Dict[str, Any],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Take the first available parse-stage structural evidence.

    Returns (evidence_key, blocks); (None, []) when absent —
    honestly missing, never falls back to post-chunking text.
    """
    for key in _STRUCTURE_EVIDENCE_KEYS:
        blocks = metadata.get(key)
        if isinstance(blocks, list) and blocks:
            return key, blocks
    return None, []


def _section_text(blocks: Sequence[Any]) -> str:
    """Concatenate section block text (parse-stage text; blocks
    without a "text" field, e.g. tables, contribute "")."""
    parts = []
    for block in blocks:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def compute_section_hashes(
    metadata: Dict[str, Any],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Section hashes — group parse blocks by heading_path.

    Returns (evidence_key, sections):

    - sections in document order, each item
      ``{path, index, hash, block_count, text_length}``;
    - hash = sha256(normalized section text)[:12];
    - no structural evidence → (None, []).

    Section identity is the (path, index) pair — index disambiguates
    headings that repeat within a document.
    """
    evidence_key, blocks = _structure_evidence(metadata)
    if evidence_key is None:
        return None, []

    grouped: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    order: List[Tuple[str, ...]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        path = tuple(
            str(h) for h in (block.get("heading_path") or [])
        )
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append(block)

    sections: List[Dict[str, Any]] = []
    for index, path in enumerate(order):
        section_blocks = grouped[path]
        text = _section_text(section_blocks)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        sections.append(
            {
                "path": list(path),
                "index": index,
                "hash": digest,
                "block_count": len(section_blocks),
                "text_length": len(text),
            }
        )
    return evidence_key, sections


# ============================================================
# Version identity — content-addressed (git-style)
# ============================================================

def compute_version_id(
    source_uri: Optional[str],
    source_sha256: Optional[str],
    doc_id: str,
) -> str:
    """Content-addressed revision number — same (source, bytes,
    semantics) → same id.

    An honestly missing source_uri / source_sha256 joins the hash as
    "absent" — the missing value is itself part of the identity, not
    an error.
    """
    material = "\n".join(
        (
            MANIFEST_SCHEME,
            source_uri or "absent",
            source_sha256 or "absent",
            doc_id,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[
        :VERSION_ID_LENGTH
    ]


def compute_configuration_hash(**config: Any) -> Optional[str]:
    """Chunking/parsing configuration → normalized fingerprint
    sha256[:16].

    - Keys normalized (sorted), values via ``str()`` — the same
      config yields the same fingerprint regardless of argument
      order;
    - Empty config → None (honestly missing: no fabricated
      fingerprint without the real config, never a stand-in
      "empty-string hash").
    """
    if not config:
        return None
    material = "\n".join(
        f"{key}={config[key]}" for key in sorted(config)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def compute_cache_key(
    manifest: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Result cache key: joint fingerprint of version_id × processing
    binding.

    Binds ``version_id / parser_version / chunker_version /
    configuration_hash`` — any change in content or processing chain
    changes the key, preventing old-version results from overriding
    new ones and cache pollution where content changed but the hash
    did not. Honestly missing dimensions join the hash as "absent"
    (the missing value is part of the identity). Invalid manifest →
    None.
    """
    if not isinstance(manifest, dict) or not manifest.get("version_id"):
        return None
    binding = manifest.get("processing_binding") or {}
    material = "\n".join(
        (
            CACHE_KEY_SCHEME,
            str(manifest["version_id"]),
            str(binding.get("parser_version") or "absent"),
            str(binding.get("chunker_version") or "absent"),
            str(binding.get("configuration_hash") or "absent"),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_version_manifest(
    document: Any,
    file_path: Optional[str] = None,
    *,
    source_uri: Optional[str] = None,
    parent_manifest: Optional[Dict[str, Any]] = None,
    parser_version: Optional[str] = None,
    chunker_version: Optional[str] = None,
    configuration_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the version manifest dict (pure computation, no document
    write).

    - ``source_uri``: source identifier, defaults to the file_path
      string (upload layers may pass custom ids like
      "upload://{batch}/{name}");
    - ``parent_manifest``: previous revision's manifest (held by
      the caller); its version_id becomes the lineage pointer;
    - ``parser_version`` / ``chunker_version`` / ``configuration_hash``:
      processing-binding dimensions — which parser/chunker code and
      which chunking config produced the result. An honestly missing
      (None) value still occupies the key as None, meaning "this
      dimension is unbound" rather than "does not exist"; **does not
      affect version_id** (content-addressed identity stays decoupled
      from the processing chain — see the module docstring);
    - Every hash roots in the original bytes: source_sha256 prefers
      the metadata['sha256'] the parser wrote before parsing (the
      original bytes), else computed from file_path — never a
      post-parser artifact.
    """
    metadata = getattr(document, "metadata", None) or {}

    # Root identity: original bytes (pre-parser metadata['sha256'] or computed)
    source_sha256 = metadata.get("sha256")
    if not source_sha256 and file_path:
        source_sha256 = compute_source_sha256(file_path)

    # Derived fingerprint: cleaned text
    doc_id = compute_doc_id(getattr(document, "text", "") or "")

    # Source identifier
    resolved_uri = source_uri or (
        str(file_path) if file_path else None
    )

    # Section layer (optional, anchored to parse-stage evidence)
    evidence_key, sections = compute_section_hashes(metadata)

    version_id = compute_version_id(resolved_uri, source_sha256, doc_id)
    parent_version_id = None
    if isinstance(parent_manifest, dict):
        parent_version_id = parent_manifest.get("version_id")

    manifest: Dict[str, Any] = {
        "scheme": MANIFEST_SCHEME,
        "source_uri": resolved_uri,
        "doc_id": doc_id,
        "version_id": version_id,
        "parent_version_id": parent_version_id,
        # Processing binding: a dimension decoupled from version_id;
        # the key is always present, None = honestly unbound.
        "processing_binding": {
            "scheme": PROCESSING_BINDING_SCHEME,
            "parser_version": parser_version,
            "chunker_version": chunker_version,
            "configuration_hash": configuration_hash,
        },
        "sections": sections,
        "section_hash_scheme": (
            SECTION_HASH_SCHEME if sections else None
        ),
        "section_evidence": evidence_key,
    }
    if source_sha256:
        manifest["source_sha256"] = source_sha256
    return manifest


# ============================================================
# Change classification chain
# ============================================================

def classify_change(
    new_manifest: Optional[Dict[str, Any]],
    old_manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Classify how a new manifest changed versus the old one.

    Returns ``{kind, ...details}``:

    - kind ∈ {no-op, content-equivalent, incremental, full};
    - incremental carries section-level details (matched/added/
      removed counts and matched section indices — incremental
      ingestion may reprocess only the affected sections);
    - old manifest missing / source_uri differs / either manifest
      invalid → full (honestly conservative: when unsure, go full;
      never misjudge a whole rewrite as a partial edit).
    """
    if not isinstance(new_manifest, dict) or not isinstance(
        old_manifest, dict
    ):
        return {"kind": CHANGE_FULL, "reason": "missing-manifest"}

    new_uri = new_manifest.get("source_uri")
    old_uri = old_manifest.get("source_uri")
    if new_uri != old_uri:
        return {
            "kind": CHANGE_FULL,
            "reason": "source-uri-changed",
        }

    new_sha = new_manifest.get("source_sha256")
    old_sha = old_manifest.get("source_sha256")

    # Either byte-level criterion missing → no conservative call
    # possible, degrade to full
    if not new_sha or not old_sha:
        return {"kind": CHANGE_FULL, "reason": "source-sha256-absent"}

    if new_sha == old_sha:
        return {"kind": CHANGE_NO_OP}

    # Bytes changed → check the semantic layer
    if new_manifest.get("doc_id") == old_manifest.get("doc_id"):
        return {
            "kind": CHANGE_CONTENT_EQUIVALENT,
            "reason": "byte-level-noise",
        }

    # Semantics changed too → check section-layer intersection
    # (incremental anchors)
    new_sections = new_manifest.get("sections") or []
    old_sections = old_manifest.get("sections") or []
    old_hashes = {s.get("hash") for s in old_sections if s.get("hash")}
    new_hashes = {s.get("hash") for s in new_sections if s.get("hash")}

    matched = old_hashes & new_hashes
    added = new_hashes - old_hashes
    removed = old_hashes - new_hashes

    # No section layer available (either side empty) or zero hits
    # → whole rewrite
    if not old_hashes or not new_hashes or not matched:
        return {
            "kind": CHANGE_FULL,
            "reason": (
                "no-section-evidence"
                if (not old_hashes or not new_hashes)
                else "no-section-overlap"
            ),
        }

    return {
        "kind": CHANGE_INCREMENTAL,
        "matched_sections": len(matched),
        "added_sections": len(added),
        "removed_sections": len(removed),
        "matched_section_indices": [
            s.get("index")
            for s in new_sections
            if s.get("hash") in matched
        ],
    }


# ============================================================
# Wiring — additive metadata, silent degradation on failure
# ============================================================

def stamp_version_manifest(
    document: Any,
    file_path: Optional[str] = None,
    *,
    source_uri: Optional[str] = None,
    parent_manifest: Optional[Dict[str, Any]] = None,
    parser_version: Optional[str] = None,
    chunker_version: Optional[str] = None,
    configuration_hash: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute and write ``document.metadata['version_manifest']``.

    Idempotent (overwrite-style; rerunning on the same input yields
    the same manifest); returns None on failure without blocking the
    main flow (same discipline as provenance / doc_identity).
    Returns the manifest dict for the caller (upload/replace entry
    point) to hold and persist as the next revision's
    parent_manifest.
    """
    try:
        manifest = build_version_manifest(
            document,
            file_path,
            source_uri=source_uri,
            parent_manifest=parent_manifest,
            parser_version=parser_version,
            chunker_version=chunker_version,
            configuration_hash=configuration_hash,
        )
        metadata = getattr(document, "metadata", None)
        if metadata is None:
            try:
                document.metadata = {}
                metadata = document.metadata
            except AttributeError:
                return manifest
        if isinstance(metadata, dict):
            metadata["version_manifest"] = manifest
        return manifest
    except Exception:
        _log.debug(
            "Version manifest stamping failed",
            exc_info=True,
        )
        return None
