"""Invoice scenario → DQR payload projection (integration wiring).

Turns the full-chain ``InvoiceScenarioResult`` into a JSON-safe dict
and stamps it on ``document.metadata["invoice_scenario"]`` so
``DocumentQualityReport`` can surface the scenario ledger in its
result (the same metadata-stamping pattern as ``diagnostics`` /
``semantic_signals``).

Contract discipline:

* verbatim projection, nothing summarized away — every salvage edit,
  witness, verdict, check and chunk line range is present;
* every downstream line number (salvaged coordinate space) carries
  its ``original_line`` twin via the pipeline ``line_map`` — the DQR
  consumer never has to re-derive coordinates;
* Decimals serialize as exact strings (no float ever);
* DQR surfaces the payload VERBATIM and never folds it into the four
  dimension scores (cold-path discipline: scenario evidence is
  reported facts, not a score input).

Honest boundaries:

* ``salvage.text`` and per-witness raw line text are NOT duplicated
  in the payload — the salvaged text is fully recoverable from the
  chunk texts (coverage is verified by ``chunk_invariants``), and raw
  lines are locatable via (``line``, ``original_line``);
* WHO stamps the metadata is wired upstream: the L2 scenario
  resolver (``core/scenario/resolver.py``) resolves domain_signals
  into ``metadata["scenario"]``, and ``universal_parser`` calls
  ``attach_invoice_scenario`` when the resolved label is
  ``invoice``. This module stays the pure projection/stamping
  primitive that trigger invokes;
* ``attach_invoice_scenario`` mutates ``document.metadata`` in place,
  mirroring how the runtime stamps its own metadata keys; the
  document text fields are never touched.
"""

from __future__ import annotations

from typing import Any

from provendoc.base.models.document import Document
from provendoc.core.scenario.invoice_chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_LINES,
)
from provendoc.core.scenario.invoice_scenario_pipeline import (
    InvoiceScenarioResult,
    run_invoice_scenario,
)

# Metadata key the scenario ledger is stamped under; DQR reads this
# exact key (single source of truth for the integration contract).
SCENARIO_METADATA_KEY = "invoice_scenario"


def build_invoice_scenario_payload(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> dict[str, Any]:
    """Run the full invoice scenario chain and project it to a dict.

    Pure function (no Document, no mutation); the result is JSON-safe
    (``json.dumps`` round-trips it exactly). Every line-numbered
    entry carries both the salvaged-space line and the
    original-space twin.
    """
    result = run_invoice_scenario(
        text, max_chars=max_chars, overlap_lines=overlap_lines
    )
    to_orig = result.to_original_line

    anchors = [
        {
            "field": hit.field,
            "value": hit.value,
            "line": hit.line,
            "original_line": to_orig(hit.line),
            "pattern": hit.pattern,
            "family": hit.family,
        }
        for hit in result.anchors.hits
    ]
    decisions = [
        {
            "field": d.field,
            "family": d.family,
            "verdict": d.verdict,
            "selected_value": d.selected.value if d.selected else None,
            "selected_line": d.selected.line if d.selected else None,
            "selected_original_line": (
                to_orig(d.selected.line) if d.selected else None
            ),
            "selected_key": d.selected_key,
            "witness_count": len(d.witnesses),
            "reason": d.reason,
        }
        for d in result.arbitration.decisions
    ]
    amount_checks = [
        {
            "upper_value": c.upper_value,
            "upper_line": c.upper_line,
            "upper_original_line": (
                to_orig(c.upper_line) if c.upper_line is not None else None
            ),
            "lower_value": c.lower_value,
            "lower_line": c.lower_line,
            "lower_original_line": (
                to_orig(c.lower_line) if c.lower_line is not None else None
            ),
            "upper_decimal": (
                str(c.upper_decimal) if c.upper_decimal is not None else None
            ),
            "lower_decimal": (
                str(c.lower_decimal) if c.lower_decimal is not None else None
            ),
            "verdict": c.verdict,
            "reason": c.reason,
        }
        for c in result.amount_checks.checks
    ]
    line_items = [
        {
            "line": item.line,
            "original_line": to_orig(item.line),
            "amount": item.amount,
            "qty": item.qty,
            "unit_price": item.unit_price,
        }
        for item in result.line_items.items
    ]
    arithmetic_checks = [
        {
            "scope": c.scope,
            "line": c.line,
            "original_line": (
                to_orig(c.line) if c.line is not None else None
            ),
            "expected": str(c.expected) if c.expected is not None else None,
            "actual": str(c.actual) if c.actual is not None else None,
            "verdict": c.verdict,
            "reason": c.reason,
        }
        for c in result.arithmetic_checks.checks
    ]
    chunks = [
        {
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "original_start_line": to_orig(chunk.start_line),
            "original_end_line": to_orig(chunk.end_line),
            "text": chunk.text,
        }
        for chunk in result.chunks.chunks
    ]

    return {
        "params": {
            "max_chars": max_chars,
            "overlap_lines": overlap_lines,
        },
        "input_line_count": len(text.split("\n")),
        "salvage": {
            "substitutions": [
                {"line": line, "after": after}
                for line, after in result.salvage.substitutions
            ],
            "removal_spans": [
                {"start": start, "end": end}
                for start, end in result.salvage.removal_spans
            ],
            "stage_stats": result.salvage.stage_stats,
        },
        "line_map": list(result.line_map),
        "anchors": anchors,
        "arbitration": {
            "decisions": decisions,
            "stats": result.arbitration.stats,
        },
        "amount_checks": {
            "checks": amount_checks,
            "stats": result.amount_checks.stats,
        },
        "line_items": {
            "items": line_items,
            "stats": result.line_items.stats,
        },
        "arithmetic_checks": {
            "checks": arithmetic_checks,
            "stats": result.arithmetic_checks.stats,
        },
        "chunks": {
            "chunks": chunks,
            "stats": result.chunks.stats,
        },
        "chunk_invariants": _invariants_payload(result),
    }


def _invariants_payload(result: InvoiceScenarioResult) -> dict[str, Any]:
    inv = result.chunk_invariants
    return {
        "verdict": inv.verdict,
        "missing_lines": list(inv.missing_lines),
        "integrity_violations": list(inv.integrity_violations),
        "duplicate_lines": list(inv.duplicate_lines),
        "stats": inv.stats,
    }


def attach_invoice_scenario(
    document: Document,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> dict[str, Any]:
    """Run the scenario chain over the document and stamp the ledger.

    Eats ``document.original_text`` (the parse-output snapshot DQR
    also evaluates — the scenario chain's own A-stage salvage is its
    scenario-scoped L1, so the input is the pre-cleaning parse
    output, never the semantically cleaned ``document.text``).
    Stamps the payload under ``metadata["invoice_scenario"]`` and
    returns it. Document text fields are untouched.
    """
    text = (
        document.original_text
        if document.original_text is not None
        else document.text
    )
    payload = build_invoice_scenario_payload(
        text, max_chars=max_chars, overlap_lines=overlap_lines
    )
    document.metadata[SCENARIO_METADATA_KEY] = payload
    return payload


__all__ = [
    "SCENARIO_METADATA_KEY",
    "attach_invoice_scenario",
    "build_invoice_scenario_payload",
]
