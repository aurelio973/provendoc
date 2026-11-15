"""Content-role annotation over reconstructed PDF blocks.

Spec engineering principle: body vs artifact is classified via
``content_role``, never by deletion. The role is a derived annotation, not
an evidence rewrite: block type, text, geometry and evidence references all
stay intact; the only action is adding a ``content_role`` field.

Roles are derived from existing facts, with no new guessing:

* ``heading`` / ``table`` / ``figure`` / ``formula`` / ``footnote`` come
  directly from the reconstruction layer's block types;
* ``caption`` comes from the caption↔block evidence associations already
  established by the table/figure reconstructors;
* ``repetitive_edge_artifact`` is registered on the repetitive suppressor's
  ledger (suppressed blocks never enter the reconstruction stream; their
  evidence stays in pdf_evidence, with ids listed in the suppressor's
  info) — this module does not re-annotate them, only mentions them in
  the statistics;
* ``watermark`` (P2, spec §2/§5) comes from the watermark channel's
  block-level association: only text blocks verbatim-matching a watermark
  candidate (within the candidate's registered pages) are annotated;
  other types (tables/images etc.) keep their role even if they contain
  the candidate text — the watermark channel only covers text rendering
  surfaces;
* everything else is ``body``.

Margin-note clusters (spec §2 sidebars/margin notes) get block-level
association registration (geometry intersection) but no role change —
sidebar text is content; deleting or downgrading it is a downstream policy
decision. This layer only registers the fact of "which blocks belong to a
margin cluster".
"""

from __future__ import annotations

from collections import Counter
from typing import Any

_TYPE_ROLES = {
    "heading": "heading",
    "table": "table",
    "figure": "figure",
    "formula": "formula",
    "footnote": "footnote",
}

# A block is associated with a margin-note cluster when most of its area
# (>= 80%) sits inside the cluster bbox — reconstruction merges blocks, so
# a strict containment test would miss honest members of the edge cluster.
_CLUSTER_CONTAINMENT_RATIO = 0.8


class ContentRoleAnnotator:
    """Assign a content_role to every reconstructed block; delete nothing."""

    def assign(
        self,
        blocks: list[dict[str, Any]],
        *,
        table_info: dict[str, Any] | None = None,
        figure_info: dict[str, Any] | None = None,
        watermark_info: dict[str, Any] | None = None,
        geometry_info: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Annotate every reconstructed block with content_role (other fields untouched)."""
        caption_ids = self._caption_evidence_ids(blocks, table_info)

        # P2 (spec §2/§5): watermark candidate → block-level association.
        # Only verbatim text matches on text blocks promote the role; the
        # association (candidate ↔ block evidence ids) is registered either
        # way so consumers can locate every watermark glyph carrier.
        watermark_matches = self._watermark_block_matches(
            blocks, watermark_info
        )
        watermark_block_ids = {
            evidence_id
            for match in watermark_matches
            for evidence_id in match["block_evidence_ids"]
        }

        # P2 (spec §2): margin-note cluster → block-level association.
        # Role untouched: sidebar text is content; only the cluster
        # membership is registered.
        margin_note_matches = self._margin_note_block_matches(
            blocks, geometry_info
        )

        role_counts: Counter[str] = Counter()
        for block in blocks:
            role = self._role(block, caption_ids, watermark_block_ids)
            block["content_role"] = role
            role_counts[role] += 1

        info: dict[str, Any] = {
            "content_role_counts": dict(role_counts),
            "classification_only": True,
            "note": (
                "content_role is a derived annotation over existing block "
                "facts; no block is deleted or retyped, and suppressed "
                "repetitive artifacts keep their evidence in pdf_evidence "
                "with role 'repetitive_edge_artifact' (registered by the "
                "repetitive suppressor, not present in this stream)"
            ),
        }
        if watermark_matches:
            info["watermark_block_associations"] = watermark_matches
        if margin_note_matches:
            info["margin_note_block_associations"] = margin_note_matches
        return blocks, info

    @staticmethod
    def _caption_evidence_ids(
        blocks: list[dict[str, Any]],
        table_info: dict[str, Any] | None,
    ) -> set[str]:
        """Collect evidence IDs of blocks already bound as captions."""
        caption_ids: set[str] = set()
        for association in (table_info or {}).get("caption_associations") or []:
            evidence_id = association.get("evidence_id")
            if evidence_id:
                caption_ids.add(str(evidence_id))
        for block in blocks:
            if block.get("type") != "figure":
                continue
            payload = block.get("raw_payload") or {}
            for evidence_id in payload.get("caption_evidence_ids") or []:
                caption_ids.add(str(evidence_id))
        return caption_ids

    @classmethod
    def _role(
        cls,
        block: dict[str, Any],
        caption_ids: set[str],
        watermark_block_ids: set[str],
    ) -> str:
        block_ids = _block_evidence_ids(block)
        if caption_ids & block_ids:
            return "caption"
        # Watermark association overrides only text blocks; table/figure/
        # heading blocks keep their structural role even if their text
        # repeats like a watermark candidate.
        if (
            block_ids & watermark_block_ids
            and str(block.get("type")) == "text"
        ):
            return "watermark"
        return _TYPE_ROLES.get(str(block.get("type")), "body")

    @staticmethod
    def _watermark_block_matches(
        blocks: list[dict[str, Any]],
        watermark_info: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Associate watermark candidates with the blocks carrying them.

        A block matches when its stripped text equals the candidate text
        (verbatim — the watermark channel indexes short repeated strings,
        so equality is the honest join) and the block lives on one of the
        candidate's registered pages.
        """
        candidates = (watermark_info or {}).get("candidates") or []
        if not candidates:
            return []
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate.get("text") or "").strip()
            if not text:
                continue
            pages = {int(p) for p in candidate.get("page_numbers") or []}
            block_evidence_ids: list[str] = []
            block_pages: list[int] = []
            for block in blocks:
                if int(block.get("page", 0) or 0) not in pages:
                    continue
                if str(block.get("text", "")).strip() != text:
                    continue
                evidence_id = str(block.get("evidence_id") or "")
                if not evidence_id:
                    continue
                block_evidence_ids.append(evidence_id)
                block_pages.append(int(block.get("page", 0) or 0))
            if not block_evidence_ids:
                continue
            matches.append(
                {
                    "candidate_text": text,
                    "block_evidence_ids": block_evidence_ids,
                    "block_pages": sorted(set(block_pages)),
                    "registered_pages": sorted(pages),
                }
            )
        return matches

    @staticmethod
    def _margin_note_block_matches(
        blocks: list[dict[str, Any]],
        geometry_info: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Associate margin-note clusters with reconstructed blocks.

        A block joins a cluster when >= 80% of its area falls inside the
        cluster bbox — reconstruction merges evidence blocks, so exact
        equality with the evidence-layer cluster would lose members.
        """
        pages = (geometry_info or {}).get("pages") or []
        if not pages:
            return []
        matches: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_number = page.get("page_number")
            clusters = page.get("margin_note_clusters") or []
            if not isinstance(page_number, int) or not clusters:
                continue
            for cluster in clusters:
                cluster_bbox = cluster.get("bbox")
                if not cluster_bbox:
                    continue
                block_evidence_ids: list[str] = []
                for block in blocks:
                    if int(block.get("page", 0) or 0) != page_number:
                        continue
                    bbox = block.get("bbox")
                    if not bbox:
                        continue
                    ratio = _bbox_containment_ratio(
                        [float(v) for v in bbox],
                        [float(v) for v in cluster_bbox],
                    )
                    if ratio >= _CLUSTER_CONTAINMENT_RATIO:
                        evidence_id = str(block.get("evidence_id") or "")
                        if evidence_id:
                            block_evidence_ids.append(evidence_id)
                if not block_evidence_ids:
                    continue
                matches.append(
                    {
                        "page": page_number,
                        "side": cluster.get("side"),
                        "cluster_bbox": list(cluster_bbox),
                        "block_evidence_ids": block_evidence_ids,
                    }
                )
        return matches


def _bbox_containment_ratio(
    inner: list[float],
    outer: list[float],
) -> float:
    """Share of the inner bbox area covered by the outer bbox."""
    overlap_w = min(inner[2], outer[2]) - max(inner[0], outer[0])
    overlap_h = min(inner[3], outer[3]) - max(inner[1], outer[1])
    if overlap_w <= 0 or overlap_h <= 0:
        return 0.0
    inner_area = max(0.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    if inner_area <= 0:
        return 0.0
    return (overlap_w * overlap_h) / inner_area


def _block_evidence_ids(block: dict[str, Any]) -> set[str]:
    """All evidence IDs a block carries (own + merged sources)."""
    ids = {str(value) for value in block.get("evidence_ids") or [] if value}
    if block.get("evidence_id"):
        ids.add(str(block["evidence_id"]))
    for value in block.get("source_evidence_ids") or []:
        ids.add(str(value))
    return ids


__all__ = ["ContentRoleAnnotator"]
