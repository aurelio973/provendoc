"""Cross-channel arbitration for figure descriptors (registry #125 fix).

``FigureReconstructor`` builds each figure descriptor as
``[Figure on page N] ... Native chart text: item; item; ...`` from nearby
native text blocks.  Those blocks ALSO stay in the body stream, so the
document renders the same content twice — once as body text, once inside
the figure descriptor (registry #125 sub-defect ②, proven by synthetic reproduction).

This module arbitrates at the parser layer, generalizing the scenario
level ``figure_channel_dedup`` (prereg invoice_scenario_prereg_v1 §1.A.3)
to every narrative PDF: a payload item whose content demonstrably exists
in the body stream is dropped from the descriptor; an item with no body
witness is KEPT (content-first: the descriptor channel may be the only
witness of content the body lost).  Every decision is ledger-recorded.

Matching semantics (conservative by design, identical to the scenario
version):

* whitespace-normalized substring check against the whitespace-collapsed
  body corpus — tolerant of the payload joining several body lines into
  one item;
* the body corpus excludes every figure block (descriptors must not
  witness their own payload);
* no fuzzy matching, no OCR-variant tolerance — an item that differs
  from its body copy by anything whitespace cannot fix is kept and
  flagged, never guessed away.

Idempotent: kept items stay kept on a second pass; dropped items are
gone from the descriptor so nothing new matches.
"""

from __future__ import annotations

import re
from typing import Any

_DESCRIPTOR_RE = re.compile(
    r"^(?P<prefix>\[Figure on page \d+\].*?) Native chart text: (?P<payload>.*)$",
    re.DOTALL,
)
_ITEM_SEP = "; "

ACTION_DROPPED = "dropped_duplicate"
ACTION_KEPT = "kept_no_counterpart"


def _norm(text: str) -> str:
    """Whitespace collapse — the only tolerance the matcher allows."""
    return " ".join(text.split())


def arbitrate_native_chart_text(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop native-chart-text payload items duplicated in the body stream.

    ``blocks`` is the reconstructed block list (dicts with ``type`` /
    ``text``).  Returns the possibly rewritten list (same objects except
    rewritten figure descriptors) and an info payload with the decision
    ledger.  Pure function; no I/O; never raises on user data shapes.
    """
    body_corpus = _norm(
        " ".join(
            str(block.get("text") or "")
            for block in blocks
            if block.get("type") != "figure"
        )
    )
    if not body_corpus:
        return blocks, _empty_info()

    ledger: list[dict[str, Any]] = []
    stats = {
        "descriptors_seen": 0,
        "items_dropped": 0,
        "items_kept": 0,
    }
    output: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "figure":
            output.append(block)
            continue
        text = str(block.get("text") or "")
        # DOTALL: a descriptor's payload may carry embedded newlines when
        # the nearby native text blocks were multi-line — the whole tail
        # after "Native chart text: " is the payload, exactly what
        # figures.py joined into it.
        match = _DESCRIPTOR_RE.match(text)
        if not match:
            output.append(block)
            continue
        stats["descriptors_seen"] += 1
        prefix = match.group("prefix")
        payload = match.group("payload")
        items = [item for item in payload.split(_ITEM_SEP) if _norm(item)]

        kept: list[str] = []
        figure_ledger: list[dict[str, Any]] = []
        for item in items:
            normalized = _norm(item)
            if normalized and normalized in body_corpus:
                stats["items_dropped"] += 1
                figure_ledger.append(
                    {"item": item, "action": ACTION_DROPPED}
                )
            else:
                kept.append(item)
                stats["items_kept"] += 1
                figure_ledger.append({"item": item, "action": ACTION_KEPT})
        ledger.append(
            {
                "figure_evidence_id": block.get("evidence_id"),
                "decisions": figure_ledger,
            }
        )

        if kept:
            rewritten = f"{prefix} Native chart text: {_ITEM_SEP.join(kept)}"
        else:
            # Every item had a body witness: the descriptor keeps its
            # structural prefix (figure marker / caption) and loses the
            # payload tail.
            rewritten = prefix
        if rewritten == text:
            output.append(block)
            continue
        payload_dict = dict(block.get("raw_payload") or {})
        payload_dict["chart_text_arbitration"] = {
            "descriptor_before": text,
            "descriptor_after": rewritten,
            "decisions": figure_ledger,
        }
        output.append({**block, "text": rewritten, "raw_payload": payload_dict})

    info = {
        **stats,
        "ledger": ledger,
        "note": (
            "native chart text items with a body-stream witness are dropped "
            "from figure descriptors (whitespace-normalized substring "
            "match); items without a body witness are kept — the descriptor "
            "channel may be the only witness of content the body lost"
        ),
    }
    return output, info


def _empty_info() -> dict[str, Any]:
    return {
        "descriptors_seen": 0,
        "items_dropped": 0,
        "items_kept": 0,
        "ledger": [],
        "note": "no body text to arbitrate against; descriptors unchanged",
    }
