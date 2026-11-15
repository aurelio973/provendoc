"""Pptx runtime resource guards (guards, not filters).

prereg: pptx_limits_prereg_v1.md (batch 7, frozen),
pptx_evidence_limits_prereg_v1.md (batch 9, frozen).

Isomorphic to ``DocxRuntimeLimits`` (narrative/docx_limits.py):
- every dimension defaults to ``None`` (no cap; behaviour identical
  to the historical version);
- triggering never drops a document — expensive paths are skipped
  and the fact recorded into ``metadata["resource_guard"]``
  (with a structured ``facts`` sub-key, same face as the
  oversize/spreadsheet batches);
- scope is ``.pptx`` only (PptxParser); ``.doc`` is not wired;
- object-level records go through the existing
  ``metadata["pptx_unresolved"]`` channel (no new domain key).

Version ``pptx_limits_v2`` (batch 9): the field set grows from two
dimensions to three; ``max_evidence_parts`` is a package-level shared
budget for the evidence channel (chart part reads / smartart data
part reads / oMath conversion — three extraction actions share the
count, prereg pptx_evidence_limits_prereg_v1 §2); the same
``max_slides`` is reused across the evidence channel in declaration
order. No consumer branches on the version string — pure honest
versioning (a field-set extension is a contract extension).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional


@dataclass(frozen=True)
class PptxRuntimeLimits:
    """Pptx-domain runtime resource guards (guards, not filters; three-dimension contract per the module docstring)."""

    PPTX_LIMITS_VERSION: ClassVar[str] = "pptx_limits_v2"

    max_slides: Optional[int] = None
    max_shapes_per_slide: Optional[int] = None
    max_evidence_parts: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialize to a versioned guard fact (written to resource_guard.limits)."""
        return {
            "version": self.PPTX_LIMITS_VERSION,
            "max_slides": self.max_slides,
            "max_shapes_per_slide": self.max_shapes_per_slide,
            "max_evidence_parts": self.max_evidence_parts,
        }
