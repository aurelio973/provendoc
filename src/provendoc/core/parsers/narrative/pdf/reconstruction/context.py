"""Read-only document context annotations for PDF evidence.

This is intentionally not a filter and never splits a document.  It records
embedded metadata and visible version / date / access markers for later policy
selection, where the consumer can decide how to treat overlapping versions.
"""

from __future__ import annotations

import re
from typing import Any

_MARKERS = {
    "version": re.compile(r"\b(?:version|ver\.?|版本)\s*[:vV]?[\d][\w.\-]*", re.IGNORECASE),
    "effective_date": re.compile(r"\b(?:effective|issued|发布日期|生效日期|实施日期)\b.{0,32}", re.IGNORECASE),
    "date": re.compile(r"\b(?:19|20)\d{2}[\-/年.]\d{1,2}(?:[\-/月.]\d{1,2}日?)?"),
    "access": re.compile(r"\b(?:confidential|internal use|restricted|public|机密|保密|内部|公开)\b", re.IGNORECASE),
    "status": re.compile(r"\b(?:draft|obsolete|superseded|approved|final|草案|废止|作废|已批准|正式)\b", re.IGNORECASE),
}


class DocumentContextAnnotator:
    """Collect factual metadata and marker locations; never enforce policy."""

    def annotate(
        self,
        *,
        document_metadata: dict[str, Any],
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Collect document metadata and visible markers (annotate only, never filter)."""
        embedded = dict(document_metadata.get("pdf_document_metadata") or {})
        markers: list[dict[str, Any]] = []
        for block in blocks:
            text = str(block.get("text", ""))
            for kind, pattern in _MARKERS.items():
                for match in pattern.finditer(text):
                    markers.append(
                        {
                            "kind": kind,
                            "value": match.group(0).strip(),
                            "page": block.get("page"),
                            "bbox": block.get("bbox"),
                            "evidence_id": block.get("evidence_id"),
                        }
                    )
        return {
            "embedded_metadata": embedded,
            "markers": markers,
            "marker_count": len(markers),
            "policy_action": "annotation_only",
            "filtering_applied": False,
            "note": (
                "Version, date, access and status markers are recorded as evidence; "
                "the ingestion runtime does not assert currentness or split policy documents."
            ),
            "governance_boundary": {
                "visible_markers_are_authoritative": False,
                "external_acl_required": True,
                "external_version_registry_required": True,
                "external_effectivity_policy_required": True,
            },
        }
