# ============================================================
# Narrative Domain
# PPTX presentation unified node tree — P0 (evidence mapping)
# ============================================================
r"""PowerPoint-family unified node tree — P0 pure mapping (batch 3, b3).

Basis: ``pptx_unified_nodes_prereg_v1.md`` (D1-D6 all decided/frozen A),
blueprint three ``pptx_family_unification_blueprint_2026-10-02.md`` §2
(21 node types + 11 fields) + §5 (four-state node status).

Positioning discipline (decided/frozen items):

- Zero change to existing chunk output — this module only adds
  ``metadata["presentation_nodes"]`` (additive, same precedent as
  markdown_nodes);
- Pure function consuming existing evidence keys (pptx_slides /
  heading_candidates / pptx_charts / pptx_smartart / pptx_formulas /
  pptx_comments / pptx_unresolved) — zero new IO, zero XML scans;
  evidence keys are read-only;
- P0 = 10 pure-mapping node types: presentation (root) / slide /
  heading / chart / smartart / equation / comment / media /
  embedded_object / unresolved;
- P1 = 11 probing node types (batch 3 P1, prereg
  pptx_node_probing_prereg_v1 all A): consumes the new evidence key
  ``metadata["pptx_slide_objects"]`` (per-page probing in the
  parser's ``_slide_object_facts``) to append paragraph / list /
  list_item / table / table_row / table_cell / image / shape /
  text_box / hyperlink / notes nodes; P0 generation logic unchanged
  (append-only branch), completing the blueprint's 21 types;
- No evidence, no fabricated values: the five fields content / bbox /
  confidence / order / warnings are not carried (same decision as
  markdown P0 D-P4); the 21-type end state is not trimmed (the 11
  types paragraph/list/table/image etc. belong to the new P1 probing,
  under a separate prereg);
- CFB-failed packages emit zero nodes — failed packages never reach
  this function (batch 2 contract).

Node schema (the P0-derivable subset of the blueprint's 11 fields,
§2.3): ``node_id`` = ``ppt-{type}-{seq}`` (global increment within a
type; root ``ppt-n0000``); ``source_ref`` carries slide provenance +
char span (slide/heading, same calibre as pptx_slides);
``resolution_status`` is one of complete / partial / unsupported /
unresolved (records' original value passed through; successful
mapping = complete); ``evidence_type`` ∈ constructed /
native_presentationml / structured_extractor / unresolved_registry.

Caps: non-root nodes capped at ``_NODE_LIMIT``=2000 (same value as
markdown P0); beyond that, truncate in document order and set
``nodes_truncated=true``; when the comment face exceeds its samples
cap, register ``comment_samples_truncated=true`` plus the full
``comment_count`` (D6 honest disclosure).
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["derive_presentation_nodes", "NODE_LIMIT"]

# Non-root node cap (D-P5, same value/precedent as markdown)
NODE_LIMIT = 2000

_SOURCE_FORMAT = "pptx-family-openxml"
_PARSER_ROUTE = "pptx_openxml"

# pptx_unresolved object_type → node_type (P0 mapping table, §2.2)
_RECORD_NODE_TYPES = {
    "video": "media",
    "audio": "media",
    "ole": "embedded_object",
}


def _base_node(
    node_id: str,
    node_type: str,
    evidence_type: str,
    resolution_status: str = "complete",
    **extra: Any,
) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "node_id": node_id,
        "node_type": node_type,
        "source_format": _SOURCE_FORMAT,
        "parser_route": _PARSER_ROUTE,
        "resolution_status": resolution_status,
        "evidence_type": evidence_type,
    }
    node.update(extra)
    return node


def _slide_ref(slide_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Shared provenance for slide/heading nodes (pptx_slides fields passed through)."""
    return {
        "slide_index": slide_entry["slide_index"],
        "slide_number": slide_entry["slide_number"],
        "part": slide_entry["part"],
    }


def derive_presentation_nodes(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive the unified node tree from existing evidence keys (pure function, zero IO).

    Input = PptxParser success-case metadata (failed packages are
    never passed in, §2.5). Returns the node list (root first); an
    empty evidence face (a success case with no slide spans) returns
    an empty list — the caller writes only when non-empty.
    """
    slides = metadata.get("pptx_slides") or []
    if not slides:
        return []

    nodes: List[Dict[str, Any]] = []
    truncated = False

    def _add(node: Dict[str, Any]) -> None:
        nonlocal truncated
        # The root is already in the list: count non-root nodes by
        # subtracting it (the root does not consume the cap).
        if len(nodes) - 1 >= NODE_LIMIT:
            truncated = True
            return
        nodes.append(node)

    # ── root node (presentation): always built, never counted against the cap ──
    root_facts: Dict[str, Any] = {"slide_count": len(slides)}
    package = metadata.get("pptx_package")
    if isinstance(package, dict):
        variant = package.get("format_variant")
        if variant:
            root_facts["format_variant"] = variant
    order_method = metadata.get("slide_order_method")
    if order_method:
        root_facts["slide_order_method"] = order_method
    nodes.append(_base_node(
        "ppt-n0000", "presentation", "constructed",
        slide_span={"start": 0, "end": slides[-1].get("end", 0)},
        **root_facts,
    ))

    # ── slide (1:1 with pptx_slides, char spans passed through) ──
    headings = list(metadata.get("heading_candidates") or [])
    for entry in slides:
        _add(_base_node(
            f"ppt-slide-{entry['slide_index']:04d}", "slide",
            "native_presentationml",
            source_ref=_slide_ref(entry),
            title=entry.get("title"),
            span={"start": entry["start"], "end": entry["end"]},
        ))

    # ── heading (1:1 with heading_candidates, same start/end calibre) ──
    for seq, candidate in enumerate(headings, start=1):
        _add(_base_node(
            f"ppt-heading-{seq:04d}", "heading",
            "native_presentationml",
            source_ref={"span_start": candidate.start,
                        "span_end": candidate.end},
            raw=candidate.raw,
            depth_hint=getattr(candidate, "depth_hint", None),
        ))

    # ── chart / smartart / equation (three channels, per object) ──
    charts = ((metadata.get("pptx_charts") or {}).get("charts")) or []
    for seq, chart in enumerate(charts, start=1):
        _add(_base_node(
            f"ppt-chart-{seq:04d}", "chart",
            "structured_extractor",
            source_ref={
                "slide_index": chart.get("slide_index"),
                "part": chart.get("part"),
            },
            title=chart.get("title"),
        ))

    diagrams = (
        (metadata.get("pptx_smartart") or {}).get("diagrams")
    ) or []
    for seq, diagram in enumerate(diagrams, start=1):
        _add(_base_node(
            f"ppt-smartart-{seq:04d}", "smartart",
            "structured_extractor",
            source_ref={
                "slide_index": diagram.get("slide_index"),
                "part": diagram.get("part"),
            },
        ))

    formulas = (
        (metadata.get("pptx_formulas") or {}).get("formulas")
    ) or []
    for seq, formula in enumerate(formulas, start=1):
        ref: Dict[str, Any] = {"seq": seq}
        slide_index = formula.get("slide_index")
        if slide_index is not None:
            ref["slide_index"] = slide_index
        part = formula.get("part")
        if part:
            ref["part"] = part
        _add(_base_node(
            f"ppt-equation-{seq:04d}", "equation",
            "structured_extractor",
            source_ref=ref,
        ))

    # ── comment (samples truncation disclosure, D6) ──
    comments = metadata.get("pptx_comments") or {}
    if isinstance(comments, dict):
        samples = comments.get("samples") or []
        comment_count = comments.get("comment_count")
        if samples or comment_count:
            # The samples cap is not hard-coded/guessed — the gap
            # between samples length and comment_count is the
            # authority (a gap means disclose).
            if (
                isinstance(comment_count, int)
                and len(samples) < comment_count
            ):
                metadata[_BATCH_COMMENT_TRUNCATED] = True
            for seq, sample in enumerate(samples, start=1):
                _add(_base_node(
                    f"ppt-comment-{seq:04d}", "comment",
                    "unresolved_registry",
                    resolution_status="partial",
                    source_ref={
                        "slide_number": sample.get("slide_number"),
                        "part": sample.get("part"),
                    },
                    reason_code="COMMENT_SAMPLES_TRUNCATED",
                    raw=sample.get("text"),
                    author_name=sample.get("author_name"),
                ))

    # ── media / embedded_object / unresolved (1:1 with records) ──
    unresolved_records = (
        (metadata.get("pptx_unresolved") or {}).get("records")
    ) or []
    media_seq = ole_seq = unres_seq = 0
    for record in unresolved_records:
        object_type = record.get("object_type")
        node_type = _RECORD_NODE_TYPES.get(object_type, "unresolved")
        if node_type == "media":
            media_seq += 1
            node_id = f"ppt-media-{media_seq:04d}"
        elif node_type == "embedded_object":
            ole_seq += 1
            node_id = f"ppt-embedded-{ole_seq:04d}"
        else:
            unres_seq += 1
            node_id = f"ppt-unresolved-{unres_seq:04d}"
        source_ref: Dict[str, Any] = {"source": record.get("source")}
        if record.get("object_id"):
            source_ref["object_id"] = record["object_id"]
        extra: Dict[str, Any] = {
            "source_ref": source_ref,
            "reason_code": record.get("reason_code"),
        }
        if record.get("rel_id"):
            extra["rel_id"] = record["rel_id"]
        if record.get("placeholder_text"):
            extra["placeholder_text"] = record["placeholder_text"]
        resolution = record.get("resolution") or "unresolved"
        _add(_base_node(
            node_id, node_type, "unresolved_registry",
            resolution_status=resolution, **extra,
        ))

    # ── P1 probing nodes (pptx_slide_objects, batch 3 P1 append branch) ──
    _derive_p1_nodes(metadata, _add)

    if truncated:
        metadata["presentation_nodes_truncated"] = True
    return nodes


# ── P1 probing nodes (batch 3 P1, consumes pptx_slide_objects) ──

_P1_NODE_KINDS = (
    "paragraph", "list", "list_item", "table", "table_row",
    "table_cell", "image", "shape", "text_box", "hyperlink",
    "notes",
)

# Generic record keys — not object-level node fields (into source_ref or dropped)
_P1_GENERIC_KEYS = frozenset((
    "kind", "slide_index", "slide_number", "part",
    "object_seq", "notes_part", "placeholder_type",
))


def _derive_p1_nodes(
    metadata: Dict[str, Any],
    add_node,
) -> None:
    """Append P1 nodes (consumes pptx_slide_objects; pure function, zero IO).

    Emission order: list kind-level summary nodes first, then
    per-object nodes in document order (node_id increments within a
    type are independent of emission order). list is a kind-level
    summary node (marker_kind deduped by first document appearance,
    source_ref anchored to the first list_item's slide provenance).
    notes coming through the filename_fallback channel are partial
    (honest disclosure of extraction quality, D6).
    """
    objects = metadata.get("pptx_slide_objects") or []
    if not objects:
        return

    # ── list summaries (kind-level, before per-object nodes) ──
    list_summary: Dict[str, Dict[str, Any]] = {}
    for record in objects:
        if record.get("kind") == "list_item":
            marker_kind = record.get("marker_kind")
            if marker_kind and marker_kind not in list_summary:
                list_summary[marker_kind] = record
    for seq, (marker_kind, first) in enumerate(
        list_summary.items(), start=1,
    ):
        add_node(_base_node(
            f"ppt-list-{seq:04d}", "list",
            "structured_extractor",
            source_ref={
                "slide_index": first.get("slide_index"),
                "slide_number": first.get("slide_number"),
                "part": first.get("part"),
                "object_seq": first.get("object_seq"),
            },
            marker_kind=marker_kind,
        ))

    # ── per-object nodes (every kind except the list summary) ──
    kind_seq = {kind: 0 for kind in _P1_NODE_KINDS}
    for record in objects:
        kind = record.get("kind")
        if kind == "list" or kind not in _P1_NODE_KINDS:
            continue
        kind_seq[kind] += 1
        ref: Dict[str, Any] = {
            "slide_index": record.get("slide_index"),
            "slide_number": record.get("slide_number"),
            "part": record.get("part"),
            "object_seq": record.get("object_seq"),
        }
        if kind == "notes" and record.get("notes_part"):
            ref["notes_part"] = record["notes_part"]
        if kind == "paragraph" and record.get("placeholder_type"):
            # D2: placeholder type of non-title ph containers goes into source_ref
            ref["placeholder_type"] = record["placeholder_type"]
        extra = {
            key: value for key, value in record.items()
            if key not in _P1_GENERIC_KEYS
        }
        resolution = "complete"
        if (
            kind == "notes"
            and record.get("channel") == "filename_fallback"
        ):
            resolution = "partial"
        add_node(_base_node(
            f"ppt-{kind}-{kind_seq[kind]:04d}", kind,
            "structured_extractor",
            resolution_status=resolution,
            source_ref=ref, **extra,
        ))


# metadata write key for the comment truncation flag (module-level constant, for test reconciliation)
_BATCH_COMMENT_TRUNCATED = "presentation_comment_samples_truncated"
