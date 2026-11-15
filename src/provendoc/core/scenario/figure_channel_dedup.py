"""Figure-channel arbitration (prereg invoice_scenario_prereg_v1 §1.A.3).

The PDF reconstruction layer emits figure descriptors carrying native
chart text (``[Figure on page N] ... Native chart text: a; b; c`` —
source: pdf/reconstruction/figures.py, items joined by ``"; "``, capped
at 6). The same content typically reappears in the body text stream,
so an invoice document carries the chart text twice.

This module arbitrates: a payload item whose content demonstrably
exists in the body is dropped from the descriptor; an item with no
body witness is KEPT (content-first: the channel may be the only
witness of content the body lost). Every decision is ledger-recorded.

Matching semantics (v1, deliberately conservative):

* whitespace-normalized substring check against the whitespace-
  collapsed body text — tolerant of the payload joining several body
  lines into one item ("ABNOLD & POBTEB NEW YORK" == two body lines);
* run AFTER invoice_salvage.salvage_numbers so digit-damage variants
  ("1,920_00" vs "1,920 .00") unify before matching;
* when every item is dropped the descriptor keeps its structural
  prefix (figure marker / caption) and loses the payload tail.

Honest boundaries: no fuzzy matching, no OCR-variant tolerance beyond
salvage unification — a channel item that differs from its body copy
by anything salvage cannot fix is kept and flagged, never guessed away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ACTION_DROPPED = "dropped_duplicate"
ACTION_KEPT = "kept_no_counterpart"

_DESCRIPTOR_RE = re.compile(
    r"^(?P<prefix>\[Figure on page \d+\].*?) Native chart text: (?P<payload>.*)$"
)
_ITEM_SEP = "; "


@dataclass(frozen=True)
class ChannelEdit:
    """One arbitration decision on a native-chart-text payload item."""

    line: int  # 1-based
    item: str
    action: str  # ACTION_DROPPED | ACTION_KEPT


@dataclass(frozen=True)
class ChannelDedupResult:
    """Arbitration output: rewritten text + decision ledger + stats."""

    text: str
    edits: tuple[ChannelEdit, ...]
    stats: dict[str, int]

    @property
    def edit_count(self) -> int:
        """Number of arbitration decisions recorded."""
        return len(self.edits)


def _norm(text: str) -> str:
    """Whitespace collapse — the only tolerance the matcher allows."""
    return " ".join(text.split())


def dedup_native_chart_text(text: str) -> ChannelDedupResult:
    """Drop native-chart-text payload items duplicated in the body.

    Idempotent: kept items stay kept; nothing new matches on a second
    pass. Pure function; no I/O; no exception paths on user data.
    """
    lines = text.split("\n")
    descriptor_hits: list[tuple[int, re.Match[str]]] = []
    body_parts: list[str] = []

    for lineno, line in enumerate(lines, start=1):
        match = _DESCRIPTOR_RE.match(line)
        if match:
            descriptor_hits.append((lineno, match))
        else:
            body_parts.append(line)

    if not descriptor_hits:
        return ChannelDedupResult(text=text, edits=(), stats={
            "descriptors_seen": 0,
            "items_dropped": 0,
            "items_kept": 0,
        })

    body_corpus = _norm(" ".join(body_parts))

    edits: list[ChannelEdit] = []
    stats = {
        "descriptors_seen": len(descriptor_hits),
        "items_dropped": 0,
        "items_kept": 0,
    }

    for lineno, match in descriptor_hits:
        prefix = match.group("prefix")
        payload = match.group("payload")
        items = [item for item in payload.split(_ITEM_SEP) if _norm(item)]

        kept: list[str] = []
        for item in items:
            if _norm(item) and _norm(item) in body_corpus:
                edits.append(ChannelEdit(lineno, item, ACTION_DROPPED))
                stats["items_dropped"] += 1
            else:
                kept.append(item)
                edits.append(ChannelEdit(lineno, item, ACTION_KEPT))
                stats["items_kept"] += 1

        if kept:
            lines[lineno - 1] = f"{prefix} Native chart text: {_ITEM_SEP.join(kept)}"
        else:
            lines[lineno - 1] = prefix

    return ChannelDedupResult(
        text="\n".join(lines), edits=tuple(edits), stats=stats
    )
