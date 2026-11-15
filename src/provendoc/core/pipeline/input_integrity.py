# -*- coding: utf-8 -*-
"""Input integrity — input-level truncation/concatenation fact-code summary view.

Design (same "summarize, do not rebuild" discipline as
content_completeness):

- **Read-only over existing evidence keys** (truncation /
  format_verification.trailing_data / rtf_structure.unbalanced_groups /
  tex_structure.unclosed_group_depth), deriving
  ``metadata["input_integrity"]`` — additive, idempotent
  (overwrite rewrite), fail-silent, **non-empty writes only** (no
  input-level evidence -> no key; most clean documents gain zero
  new keys).
- **Four-code semantic boundary**: only facts about the input file
  itself being incomplete or concatenated are registered; the
  budget truncation enforced by this project's guards
  (record_columns / sheet_rows / sheet_columns /
  sheet_cells_truncated, streaming early-stop) is not an input
  defect and never enters this table — the two semantics are
  registered separately to prevent confusion.
- **Mapping surface** (INPUT_INTEGRITY_EVIDENCE_MAP lives in
  vocab_registry; this module is its execution surface):
  - TRUNCATED_INPUT <- truncation.truncated (the txt code's direct
    meaning) / rtf_structure.unbalanced_groups (unclosed group at
    EOF directly indicates truncation; a surplus of closing braces
    is a mixed "truncated or corrupt" semantics — noted in the
    parser docstring) / tex_structure.unclosed_group_depth > 0;
  - APPENDED_DATA <- trailing_data.detected (the direct meaning of
    the three-anchor trailing probe: pdf %%EOF / zip EOCD /
    parquet PAR1);
  - MULTIPLE_DOCUMENTS_DETECTED <- trailing_data.polyglot_detected
    (the trailing region holds a known container magic — strong
    evidence of a second concatenated container). **Prefix-style
    concatenation has no reliable start anchor and is deliberately
    not probed** (better absent than invented);
  - INCOMPLETE_REVISION **has no mapping** — pdf multiple_revisions
    (multiple revisions present) does not mean the last revision is
    incomplete (multi-revision documents are the norm for signed
    files; adjacency is not semantic directness).

Relations to existing modules (no duplication, no replacement):

- The ingestion_status three-state contract is untouched — this
  module never changes status;
- txt truncation / rtf unbalanced_groups / tex structure keys are
  unchanged — this module only reads them;
- format_verification.trailing_data is unchanged — APPENDED_DATA
  is its document-level fact-code view, not a replacement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from provendoc.core.pipeline.pipeline_stamps import record_degradation

_log = logging.getLogger(__name__)

INPUT_INTEGRITY_VERSION = "input_integrity_v1"


def _derive_codes(
    metadata: Dict[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    """Evidence keys -> input-level fact-code list + evidence view (read-only; empty without evidence)."""
    codes: List[str] = []
    evidence: Dict[str, Any] = {}

    def _mark(code: str) -> None:
        if code not in codes:  # dedup: one code, several evidence sources merged
            codes.append(code)

    # --- TRUNCATED_INPUT ---
    truncation = metadata.get("truncation")
    if isinstance(truncation, dict) and truncation.get("truncated"):
        _mark("TRUNCATED_INPUT")
        ev = truncation.get("evidence")
        evidence["truncation_evidence"] = (
            [str(e) for e in ev if isinstance(e, str)] if isinstance(ev, list) else []
        )

    rtf_structure = metadata.get("rtf_structure")
    if isinstance(rtf_structure, dict) and rtf_structure.get("unbalanced_groups"):
        # An unclosed group at EOF directly indicates truncation; a
        # surplus of closing braces is a mixed "truncated or
        # corrupt" semantics (rtf_parser L1284/L2030) — the
        # evidence key does not separate the two shapes, and the
        # key name is kept at registration for downstream audit
        # (frozen ruling).
        _mark("TRUNCATED_INPUT")
        evidence["rtf_unbalanced_groups"] = True

    tex_structure = metadata.get("tex_structure")
    if isinstance(tex_structure, dict):
        depth = tex_structure.get("unclosed_group_depth")
        if isinstance(depth, int) and depth > 0:
            _mark("TRUNCATED_INPUT")
            evidence["tex_unclosed_group_depth"] = depth

    # --- APPENDED_DATA / MULTIPLE_DOCUMENTS_DETECTED ---
    verification = metadata.get("format_verification")
    if isinstance(verification, dict):
        trailing = verification.get("trailing_data")
        if isinstance(trailing, dict) and trailing.get("detected"):
            _mark("APPENDED_DATA")
            trailing_view: Dict[str, Any] = {
                "anchor": trailing.get("anchor"),
            }
            for field in ("offset", "length"):
                if isinstance(trailing.get(field), int):
                    trailing_view[field] = trailing[field]
            evidence["trailing_data"] = trailing_view
            if trailing.get("polyglot_detected"):
                _mark("MULTIPLE_DOCUMENTS_DETECTED")
                evidence["embedded_family"] = trailing.get("embedded_family")

    # INCOMPLETE_REVISION: no mapping (better absent than invented).
    return codes, evidence


def summarize_input_integrity(
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Parser metadata -> input-level integrity view (read-only derivation).

    metadata is not modified; the return value is serializable
    (writing it into ``metadata["input_integrity"]`` is done by the
    stamp_* layer).
    """
    codes, evidence = _derive_codes(metadata)
    view: Dict[str, Any] = {
        "input_integrity_version": INPUT_INTEGRITY_VERSION,
    }
    if codes:
        view["codes"] = codes
    if evidence:
        view["evidence"] = evidence
    return view


def stamp_input_integrity(document: Any) -> Optional[Dict[str, Any]]:
    """Derive and write ``document.metadata["input_integrity"]``.

    Idempotent (overwrite write; rerun on the same input yields the
    same view); **non-empty writes only** — no key is written
    without input-level evidence (honest absence; most clean
    documents gain zero new keys); failures return None and never
    block the main flow (same discipline as provenance /
    ingestion_status / content_completeness — a missing integrity
    view must not block ingestion).
    """
    try:
        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        view = summarize_input_integrity(metadata)
        if len(view) == 1:  # version field only = no evidence
            return None
        metadata["input_integrity"] = view
        return view
    except Exception as exc:
        _log.debug("Input integrity stamping failed", exc_info=True)
        record_degradation(document, "input_integrity", exc)
        return None


__all__ = [
    "INPUT_INTEGRITY_VERSION",
    "summarize_input_integrity",
    "stamp_input_integrity",
]
