"""IngestionResult -> JSON dict -- API presentation-layer serialization.

Reuses core/foundation/serialization.safe_serialize (single source of
truth: handles dataclass / Enum / bytes / cycles); no second encoder.
This module only:

    1. splits IngestionResult into the API response envelope (file /
       document / representations / metadata)
    2. omits the large text fields when include_text=False (the
       structured facts stay)
    3. normalizes MultiGranularityOutput into a {"base": [...],
       "coarse": [...], "fine": [...]} container shape
"""

from provendoc.base.models.ingestion_result import IngestionResult
from provendoc.base.models.representation_result import (
    RepresentationResult,
)
from provendoc.core.foundation.serialization import safe_serialize
from provendoc.core.representations.narrative.granularity import (
    MultiGranularityOutput,
)


def _strip_unit_text(node):
    """Recursively drop a unit's large text fields (facts stay)."""
    if isinstance(node, dict):
        return {
            k: _strip_unit_text(v)
            for k, v in node.items()
            if k not in ("text", "content")
        }
    if isinstance(node, list):
        return [_strip_unit_text(item) for item in node]
    return node


def _serialize_representation(
    result: RepresentationResult,
    include_text: bool,
) -> dict:
    """RepresentationResult -> dict (content normalized to units)."""
    content = result.content
    if isinstance(content, MultiGranularityOutput):
        # MultiGranularityOutput -- three-tier granularity container
        units = {
            "granularity": {
                "base": safe_serialize(content.base),
                "coarse": safe_serialize(content.coarse),
                "fine": safe_serialize(content.fine),
            },
        }
    else:
        units = {"units": safe_serialize(content)}

    if not include_text:
        units = _strip_unit_text(units)

    return {
        "representation_type": result.representation_type,
        "metadata": safe_serialize(result.metadata),
        **units,
    }


def _serialize_document(document, include_text: bool) -> dict:
    """Document -> dict (the three text fields gated by include_text)."""
    payload = {
        "metadata": safe_serialize(document.metadata),
    }
    if include_text:
        payload["text"] = document.text
        payload["original_text"] = document.original_text
        payload["source_text"] = document.source_text
    else:
        payload["text_length"] = len(document.text) if document.text else 0
    return payload


def result_to_dict(
    result: IngestionResult,
    file_name: str,
    file_size: int,
    include_text: bool = True,
) -> dict:
    """IngestionResult -> API response envelope.

    The envelope mirrors the SDK's return one-to-one (document /
    representations / metadata) plus a file block. A representation's
    content is normalized to units (or the granularity container);
    no raw Python object shapes leak out.
    """
    representations = {}
    for name, repr_result in result.representations.items():
        representations[name] = _serialize_representation(
            repr_result, include_text,
        )

    return {
        "file": {
            "name": file_name,
            "size_bytes": file_size,
        },
        "document": _serialize_document(result.document, include_text),
        "representations": representations,
        "metadata": safe_serialize(result.metadata),
    }
