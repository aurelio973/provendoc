"""Image asset contracts: manifest schema and asset identity.

Images are first-class assets with separate storage: the original bytes are
bit-exact and never modified (``original/``), derived renditions (deskewed,
cropped, thumbnails) live under ``derived/`` with a recorded pipeline name,
parameter fingerprint and version.  The manifest is the single join point
between the narrative text (placeholder blocks carry the asset id) and the
binary store — both sides resolve through the same ``asset_id``.

Everything is evidence: extraction facts (page, bbox, paragraph index,
alt text, captions) are recorded as authored; images that cannot be
extracted (inline PDF images without an xref, broken rel targets) are
reported as not-extracted rather than silently dropped.
"""

from __future__ import annotations

import hashlib
from typing import Any

MANIFEST_VERSION = "image_assets_v1"

# Byte payloads travel with the asset record in memory (key ``_bytes``)
# and are stripped before the manifest is serialized to disk.
PAYLOAD_KEY = "_bytes"


def asset_id_for(data: bytes) -> str:
    """Stable asset identity: content hash prefix of the original bytes.

    Two placements of the same image (shared media part or duplicated
    xref) resolve to one asset — the manifest records the reference
    count instead of duplicating storage.
    """
    return "img_" + hashlib.sha256(data).hexdigest()[:12]


def image_format_for(part_name: str | None, ext: str | None) -> str:
    """Normalized format tag from the source part name or extension."""
    source = (ext or "").strip().lstrip(".").lower()
    if not source and part_name:
        source = part_name.rsplit(".", 1)[-1].lower()
    if source in {"jpeg", "jpe", "jfif"}:
        return "jpg"
    return source or "unknown"


def strip_payloads(records: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]],
    dict[str, bytes],
]:
    """Split in-memory records into serializable facts and byte payloads.

    Only successfully extracted placements carry payloads; the payload map
    is keyed by asset id, so duplicated placements deduplicate naturally.
    """
    facts: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for record in records:
        clean = {k: v for k, v in record.items() if k != PAYLOAD_KEY}
        data = record.get(PAYLOAD_KEY)
        if record.get("extracted") and isinstance(data, (bytes, bytearray)):
            payloads[record["asset_id"]] = bytes(data)
        facts.append(clean)
    return facts, payloads


def image_assets_metadata(
    records: list[dict[str, Any]],
    extractor: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Assemble a parser's ``<format>_image_assets`` metadata entry.

    Shared by the docx/pptx/pdf parsers: facts are stripped of byte
    payloads, counts are derived, and the payload map travels
    separately in metadata for the export layer (stripped before any
    JSON serialization — same in-memory-passing pattern as the RE
    envelope).
    """
    facts, payloads = strip_payloads(records)
    extracted = sum(1 for f in facts if f.get("extracted"))
    entry = {
        "extractor": extractor,
        "counts": {
            "placements": len(facts),
            "unique_assets": len(payloads),
            "extracted": extracted,
            "not_extracted": len(facts) - extracted,
        },
        "assets": facts,
    }
    return entry, payloads


def build_manifest(
    records: list[dict[str, Any]],
    source: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Assemble the on-disk manifest plus the payload map for the exporter.

    ``source`` describes the provenance of the extraction run
    (file_type, extractor version, optional pipeline description).
    """
    facts, payloads = strip_payloads(records)
    extracted = [r for r in facts if r.get("extracted")]
    return {
        "manifest_version": MANIFEST_VERSION,
        "source": source,
        "counts": {
            "placements": len(facts),
            "unique_assets": len(payloads),
            "extracted": len(extracted),
            "not_extracted": len(facts) - len(extracted),
            "deduplicated_placements": len(extracted) - len(payloads),
        },
        "assets": facts,
    }, payloads


__all__ = [
    "MANIFEST_VERSION",
    "PAYLOAD_KEY",
    "asset_id_for",
    "image_format_for",
    "strip_payloads",
    "image_assets_metadata",
    "build_manifest",
]
