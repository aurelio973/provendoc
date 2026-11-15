"""Scenario chunking → semantic representation adapter (ann-1).

Bridges the scenario chains' **already-computed ledger chunks** into
the SDK's ``representations["semantic"]`` contract so the main
``chunk()`` pipeline can consume scenario chunking without any
re-derivation.

Wiring contract:

* the scenario DQR attaches stamp their ledgers at PARSE time
  (``metadata["invoice_scenario"]`` / ``metadata
  ["academic_paper_scenario"]``) and both ledgers already contain
  the full chunk list + ``chunk_invariants`` — this adapter CONSUMES
  those facts and never re-runs a chain (the chunkers' own frozen
  discipline: "consumes facts, never re-derives them");
* dispatch discipline mirrors ``frameworks.attach_scenario_chain``:
  a label with no registered chain key is a no-op (returns ``None``,
  absence is not failure);
* any degraded outcome raises :class:`ScenarioChunkFallback` with a
  machine-readable ``reason`` — the caller records it in
  ``metadata["scenario_chunking"]`` and falls back to the generic
  representation path (a degradation is itself auditable, never raises
  past this module).

Coordinate honesty:

* invoice ledger chunks carry BOTH coordinate spaces — salvaged
  ``start_line``/``end_line`` (the chunker's own space, what the
  chunk text literally covers) and ``original_start_line``/
  ``original_end_line`` twins; the unit's ``source_context`` uses
  the salvaged space and the original twins are copied verbatim
  into unit metadata;
* academic ledger chunks are 1-based over ``document.original_text``
  (the parse snapshot) and go straight into ``source_context``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from provendoc.base.models.representation_result import (
    RepresentationResult,
)
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    SourceContext,
    UnitType,
)
from provendoc.core.scenario.frameworks import get_framework
from provendoc.core.scenario.resolver import is_close_margin_label

_log = logging.getLogger(__name__)

# label → ledger metadata key (single source of truth: the DQR
# attach modules' SCENARIO_METADATA_KEY contracts).
_LEDGER_KEY = {
    "invoice": "invoice_scenario",
    "academic_paper": "academic_paper_scenario",
}

REPR_NAME = "ScenarioNarrativeRepresentation"


class ScenarioChunkFallback(Exception):
    """Scenario chunking degraded to the generic path — never fatal.

    ``engine`` is the scenario label when known; ``reason`` is a
    short machine-readable code string (kept stable for tests).
    """

    def __init__(self, engine: str, reason: str) -> None:
        """Store scenario label + stable reason code on the exception."""
        super().__init__(f"{engine}: {reason}")
        self.engine = engine
        self.reason = reason


@dataclass(frozen=True)
class ScenarioChunkOutput:
    """Successful scenario chunking outcome for one document."""

    result: RepresentationResult
    engine: str
    policy_name: str  # framework.representation_profile (named reference)
    params: dict[str, Any]  # ledger-recorded chunker params (may be {})
    chunk_count: int
    invariant_verdict: str


def _scenario_label(document: Any) -> str:
    meta = getattr(document, "metadata", None) or {}
    scenario = meta.get("scenario")
    if isinstance(scenario, dict):
        return str(scenario.get("label", "") or "")
    return ""


def _ledger_chunks(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    chunks_field = ledger.get("chunks")
    if isinstance(chunks_field, dict):
        raw = chunks_field.get("chunks")
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
    if isinstance(chunks_field, list):  # defensive: future ledger shapes
        return [c for c in chunks_field if isinstance(c, dict)]
    return []


def build_scenario_chunk_result(document: Any) -> ScenarioChunkOutput | None:
    """Adapt the scenario ledger's chunks into a semantic result.

    Returns ``None`` when the document carries no chained scenario
    label (generic path, nothing to record). Raises
    :class:`ScenarioChunkFallback` when the label is chained but the
    ledger is unusable — the caller must then record the fallback
    and take the generic path. Never raises anything else (any
    internal anomaly degrades to a fallback with reason, same
    silent-degradation discipline as ``stamp_scenario``).
    """
    label = _scenario_label(document)
    try:
        return _build(document, label)
    except ScenarioChunkFallback:
        raise
    except Exception:
        _log.debug(
            "scenario chunk adapter internal error", exc_info=True
        )
        raise ScenarioChunkFallback(
            label or "unknown", "adapter_internal_error"
        ) from None


def _build(document: Any, label: str) -> ScenarioChunkOutput | None:
    if label not in _LEDGER_KEY:
        return None

    # Low-confidence governance: close-margin labels are not
    # trusted to drive scenario policy — degrade to the
    # generic path before consuming any scenario chunking.
    # The detection is not retracted (the label and margin
    # remain visible via the RSE visibility block); only the
    # harm is bounded: the chain wrong label → wrong
    # chunking branch → downstream amplification is cut here.
    # Reusing ScenarioChunkFallback doubles as the audit:
    # the caller (sdk/client.py) records scenario_chunking
    # {fallback: true, fallback_reason: "scenario_label_low_confidence"}
    # and takes the generic strategy. margin=None (no
    # opponent / not comparable) does not degrade — degrading
    # is itself a judgment call, and when unsure the existing
    # path is left untouched (conservative principle).
    meta = getattr(document, "metadata", None) or {}
    payload = meta.get("scenario")

    if is_close_margin_label(payload):
        raise ScenarioChunkFallback(
            label, "scenario_label_low_confidence"
        )

    framework = get_framework(label)
    if not framework.scenario_chain:
        # registry/code mismatch — degrade honestly, never guess
        raise ScenarioChunkFallback(label, "chain_not_registered")

    ledger = meta.get(_LEDGER_KEY[label])
    if not isinstance(ledger, dict):
        raise ScenarioChunkFallback(label, "ledger_absent")

    chunk_dicts = _ledger_chunks(ledger)
    if not chunk_dicts:
        # zero chunks is a legal ledger outcome (empty salvaged
        # text) but the SDK contract expects the generic path to
        # own that degradation — record and fall back, never invent
        raise ScenarioChunkFallback(label, "ledger_zero_chunks")

    units: list[RepresentationUnit] = []
    for index, chunk in enumerate(chunk_dicts):
        text = str(chunk.get("text", "") or "")
        if not text:
            # an empty-text ledger chunk is a ledger anomaly — skip
            # it and let ``skipped`` count tell the truth below
            continue
        start_line = chunk.get("start_line")
        end_line = chunk.get("end_line")
        source = SourceContext(
            line_start=start_line if isinstance(start_line, int) else None,
            line_end=end_line if isinstance(end_line, int) else None,
        )
        unit_meta: dict[str, Any] = {
            "scenario_chunk": True,
            "scenario_label": label,
            "start_line": start_line,
            "end_line": end_line,
        }
        for twin_key, twin_name in (
            ("original_start_line", "original_start_line"),
            ("original_end_line", "original_end_line"),
        ):
            if chunk.get(twin_key) is not None:
                unit_meta[twin_name] = chunk[twin_key]
        units.append(
            RepresentationUnit(
                text=text,
                unit_type=UnitType.NARRATIVE,
                semantic_primitives=SemanticPrimitives(
                    semantic_role="body",
                    locality_scope="block",
                ),
                sequence_index=index,
                source_context=source,
                metadata=unit_meta,
            )
        )

    if not units:
        raise ScenarioChunkFallback(label, "ledger_empty_chunk_texts")

    chunk_count = len(units)
    skipped = len(chunk_dicts) - chunk_count
    invariants = ledger.get("chunk_invariants")
    verdict = (
        str(invariants.get("verdict"))
        if isinstance(invariants, dict) and invariants.get("verdict")
        else None
    )
    raw_params = ledger.get("params")
    params: dict = dict(raw_params) if isinstance(raw_params, dict) else {}
    if skipped:
        # surface the anomaly without failing — the ledger is the
        # audit record, this key is the chunk-time supplement
        params = {**params, "skipped_empty_chunks": skipped}

    result = RepresentationResult(
        representation_type="semantic",
        content=units,
        metadata={
            "chunk_count": chunk_count,
            "unit_count": chunk_count,
            "scenario_label": label,
            "scenario_chunking_source": "ledger",
            "invariant_verdict": verdict,
        },
    )
    return ScenarioChunkOutput(
        result=result,
        engine=label,
        policy_name=framework.representation_profile,
        params=params,
        chunk_count=chunk_count,
        invariant_verdict=verdict or "",
    )


__all__ = [
    "REPR_NAME",
    "ScenarioChunkFallback",
    "ScenarioChunkOutput",
    "build_scenario_chunk_result",
]
