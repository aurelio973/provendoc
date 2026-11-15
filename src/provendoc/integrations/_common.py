"""Shared conversion layer for ecosystem adapters.

Single source of truth for mapping ``IngestionResult`` → ecosystem
documents (LangChain / LlamaIndex). Both adapters consume
``result_to_items`` so the two ecosystems can never drift apart
in what metadata they expose.

Mapping policy (evidence-first, never invented):

- ``source`` / ``chunk_index`` — provided by the adapter (it owns
  the file path); never guessed from the result.
- ``unit_type`` / ``identity`` / ``hierarchy_path`` — taken from
  the unit's ``semantic_primitives`` when present.
- ``doc_id`` / ``version_id`` / ``ingestion_domain`` / ``file_type``
  / ``cleaning_level`` — copied from pipeline metadata when present.
- ``quality_overall`` — canonical quality from
  ``metadata["benchmark"]["evaluation"]`` when an evaluation ran
  (mode="evaluate"): ``invariant_composite`` for the narrative
  invariant track, ``overall`` for domain evaluators; absent for chunk mode.

All values are coerced to JSON-safe primitives (str / int / float /
list[str]); missing fields are simply omitted — never ``None`` and
never synthesized defaults.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

Item = Tuple[str, Dict[str, Any]]

# JSON-safe scalar types
_SCALARS = (str, int, float, bool)


def _safe(value: Any) -> Any:
    """Coerce a metadata value to a JSON-safe primitive (or drop it)."""
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, (list, tuple)):
        out = [v for v in (_safe(v) for v in value) if v is not None]
        return out or None
    if value is None:
        return None
    return str(value)


def extract_units(result: Any) -> List[Any]:
    """Normalize ``representations["semantic"].content`` to a unit list.

    Mirrors the SDK's own normalization (client.evaluate step 4):
    MultiGranularityOutput → ``.base``; legacy containers →
    ``.all_units()``; plain lists pass through.
    """
    semantic = result.representations.get("semantic")
    if not semantic:
        return []
    content = semantic.content
    if hasattr(content, "base"):
        return content.base
    if hasattr(content, "all_units"):
        return content.all_units()
    return content


def result_to_items(result: Any, source: str) -> List[Item]:
    """Convert an ``IngestionResult`` to ``(text, metadata)`` items.

    ``source`` is the caller-owned file path (or URI) — the adapters
    always know it, the pipeline result does not always record it.
    """
    meta: Dict[str, Any] = {}
    pipeline = result.metadata or {}
    for key in (
        "file_type",
        "ingestion_domain",
        "ingestion_mode",
        "doc_id",
        "version_id",
        "cleaning_level",
        "corpus_tier",
        "parser",
        "representation",
    ):
        if key in pipeline:
            value = _safe(pipeline[key])
            if value is not None:
                meta[key] = value
    evaluation = (
        (pipeline.get("benchmark") or {}).get("evaluation") or {}
    )
    # canonical quality-score read: narrative (invariant track)
    # emits invariant_composite; domain evaluators emit overall.
    # Same semantics as rse_report.canonical_quality (this module
    # stays zero-heavy-deps, so no import).
    quality_overall = evaluation.get("invariant_composite")
    if quality_overall is None:
        quality_overall = evaluation.get("overall")
    if isinstance(quality_overall, (int, float)):
        meta["quality_overall"] = float(quality_overall)

    items: List[Item] = []
    for index, unit in enumerate(extract_units(result)):
        text = getattr(unit, "text", "") or ""
        unit_meta: Dict[str, Any] = {"source": str(source)}
        unit_meta.update(meta)
        unit_meta["chunk_index"] = index
        unit_type = getattr(unit, "unit_type", None)
        if unit_type is not None:
            unit_meta["unit_type"] = getattr(
                unit_type, "value", unit_type
            )
        primitives = getattr(unit, "semantic_primitives", None)
        if primitives is not None:
            identity = getattr(primitives, "identity", None)
            if identity:
                unit_meta["unit_id"] = str(identity)
            hierarchy = getattr(primitives, "hierarchy_path", None)
            if hierarchy:
                unit_meta["hierarchy_path"] = [
                    str(h) for h in hierarchy
                ]
        items.append((text, unit_meta))
    return items
