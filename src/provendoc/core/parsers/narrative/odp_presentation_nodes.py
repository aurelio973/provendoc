# ============================================================
# Narrative Domain
# ODP presentation unified node tree — batch 5
# ============================================================
r"""ODP unified node tree — feeds into presentation_nodes (batch 5, b5).

Basis: ``odp_nodes_prereg_v1.md`` (D1–D6 all decided A), blueprint three
``pptx_family_unification_blueprint_2026-10-02.md`` §2 (21 final node_type
classes; 7 mappable ODP subset classes).

Positioning discipline (decided items):

- Zero change to existing chunk output — this module only produces
  ``presentation_nodes`` (an additive key, merged with the pptx key of
  the same name); the pptx side ``pptx_presentation_nodes.py`` is untouched
  (independent module mirror, D4);
- Pure function consuming ODP evidence keys (odp_slides / heading_candidates /
  odp_slide_objects) — zero IO, evidence keys read-only;
- 7 node classes (S5 no fabrication without evidence): presentation (root) /
  slide / heading / image / hyperlink / table / notes. Blueprint members with
  no ODF evidence channel (chart / smartart / equation / comment / media /
  embedded_object / unresolved, etc.) are not registered (ODF has no OOXML
  rels topology, no comments part);
- evidence_type does not reuse the ``native_presentationml`` literal (ODF is
  not PresentationML — that literal would lie): root constructed / the rest
  structured_extractor (D5); for P-C recovery profiles (metadata carries
  odp_recovery) non-root nodes carry ``recovered_odf_xml`` instead
  (blueprint §25 evidence types — recovered text is distinguishable from
  native text, S8 red line);
- ``source_format="odp"``, ``parser_route="odf_content_xml"``, node_id
  prefix ``odp-`` (distinct from the pptx family's ``ppt-`` prefix);
- CFB/failed profiles produce zero nodes — failed profiles never enter this
  function (batch-2 contract, same style).

Node schema (obtainable subset of the blueprint's 11 fields): ``node_id`` =
``odp-{type}-{seq}`` (globally increasing within a type; root ``odp-n0000``);
``source_ref`` carries slide provenance (slide_index/page_name, same
convention as odp_slides); slide/heading carry char spans (same convention
as odp_slides / heading_candidates).

Cap: non-root node ``NODE_LIMIT``=2000 (markdown/pptx P0/P1 same-value
precedent); excess is truncated in document order with the ``nodes_truncated``
flag set (same key as pptx ``presentation_nodes_truncated``).
"""

from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["derive_odp_nodes", "NODE_LIMIT"]

# Non-root node cap (markdown/pptx same-value precedent, prereg S6)
NODE_LIMIT = 2000

_SOURCE_FORMAT = "odp"
_PARSER_ROUTE = "odf_content_xml"

# Generic keys of an odp_slide_objects record — not node-level object fields
# (they go into source_ref), same philosophy as pptx _P1_GENERIC_KEYS.
_GENERIC_KEYS = frozenset((
    "kind", "slide_index", "page_name", "object_seq",
))


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


def derive_odp_nodes(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive the unified node tree from ODP evidence keys (pure function, zero IO).

    Input = OdpParser success-profile metadata (failed profiles never call
    this, batch-2 contract). Returns the node list (root first); an empty
    evidence surface (a success profile without odp_slides) returns an empty
    list — callers write only when non-empty.
    """
    slides = metadata.get("odp_slides") or []
    if not slides:
        return []

    nodes: List[Dict[str, Any]] = []
    truncated = False
    # P-C (pc prereg v1 D2/D8): non-root node evidence type for recovery
    # profiles — recovered_odf_xml (blueprint §25), distinguishable from native.
    node_evidence = (
        "recovered_odf_xml" if metadata.get("odp_recovery") else "structured_extractor"
    )

    def _add(node: Dict[str, Any]) -> None:
        nonlocal truncated
        # Root is already first in the list: cap counts non-root nodes after
        # subtracting it (the root takes no quota)
        if len(nodes) - 1 >= NODE_LIMIT:
            truncated = True
            return
        nodes.append(node)

    # ── Root node (presentation): always built, takes no cap quota ──
    root_facts: Dict[str, Any] = {"slide_count": len(slides)}
    profile = metadata.get("odp_profile")
    if isinstance(profile, dict):
        mimetype = profile.get("mimetype")
        if mimetype:
            root_facts["mimetype"] = mimetype
    nodes.append(_base_node(
        "odp-n0000", "presentation", "constructed",
        slide_span={"start": 0, "end": slides[-1].get("end", 0)},
        **root_facts,
    ))

    # ── slide (1:1 with odp_slides, char spans passed straight through) ──
    for entry in slides:
        _add(_base_node(
            f"odp-slide-{entry['slide_index']:04d}", "slide",
            node_evidence,
            source_ref={
                "slide_index": entry["slide_index"],
                "page_name": entry.get("page_name"),
            },
            title=entry.get("title"),
            span={"start": entry["start"], "end": entry["end"]},
        ))

    # ── heading (1:1 with heading_candidates, same-convention spans) ──
    for seq, candidate in enumerate(
        metadata.get("heading_candidates") or [], start=1,
    ):
        _add(_base_node(
            f"odp-heading-{seq:04d}", "heading",
            node_evidence,
            source_ref={
                "span_start": candidate.start,
                "span_end": candidate.end,
            },
            raw=candidate.raw,
            depth_hint=getattr(candidate, "depth_hint", None),
        ))

    # ── Object nodes (per odp_slide_objects record, document order) ──
    objects = metadata.get("odp_slide_objects") or []
    kind_seq: Dict[str, int] = {}
    for record in objects:
        kind = record.get("kind")
        kind_seq[kind] = kind_seq.get(kind, 0) + 1
        extra = {
            key: value for key, value in record.items()
            if key not in _GENERIC_KEYS
        }
        _add(_base_node(
            f"odp-{kind}-{kind_seq[kind]:04d}", kind,
            node_evidence,
            source_ref={
                "slide_index": record.get("slide_index"),
                "page_name": record.get("page_name"),
                "object_seq": record.get("object_seq"),
            },
            **extra,
        ))

    if truncated:
        metadata["presentation_nodes_truncated"] = True
    return nodes
