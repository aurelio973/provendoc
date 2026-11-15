"""Docx runtime resource guards (guards, not filters).

prereg: oversize_structure_limits_prereg_v1.md (frozen).

Isomorphic to ``PDFRuntimeLimits`` (narrative/pdf/contracts.py):
- every dimension defaults to ``None`` (no cap; behaviour identical
  to the historical version);
- triggering never drops a document — expensive paths are
  truncated and the fact recorded into
  ``metadata["resource_guard"]``;
- scope is ``.docx`` only (DocxParser); ``.doc/.pptx`` are not
  wired in this batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional


@dataclass(frozen=True)
class DocxRuntimeLimits:
    """Docx-domain runtime resource guards (guards, not filters; four-dimension contract per the module docstring)."""

    DOCX_LIMITS_VERSION: ClassVar[str] = "docx_limits_v1"

    max_body_elements: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialize to a versioned guard fact (written to resource_guard.limits)."""
        return {
            "version": self.DOCX_LIMITS_VERSION,
            "max_body_elements": self.max_body_elements,
        }
