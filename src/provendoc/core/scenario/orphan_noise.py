"""Orphan-line noise removal (prereg invoice_scenario_prereg_v1 §1.A.4).

Conservative, ledger-recorded removal of two noise shapes observed in
the Arnold & Porter corpse sample:

* an entire line consisting of a single punctuation character
  (``.`` alone, a lone quote alone) -- dropped whole;
* a stray trailing quote standing apart from any word
  (``Tobacco Control (non-FDA) '``) -- only the quote is stripped.

Deliberately NOT touched (honest boundaries):

* single alphanumeric lines (``a`` can be real content);
* possessive quotes attached to a word (``clients'``) -- even when
  unpaired, a quote glued to a word is not demonstrably noise;
* leading quotes, doubled quotes, mid-line quotes.

Every decision is a ledger entry; line drops also emit 1-based
inclusive removal spans consumable by
``reconciliation.clean_gate`` (the deletion ledger contract).
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_DROPPED_LINE = "dropped_line"
ACTION_STRIPPED_QUOTE = "stripped_stray_quote"

_ORPHAN_PUNCTUATION = {".", ",", ";", ":", "!", "?", "-", "_", "*", "'", '"', "`", "´", "’", "‘", "”", "“"}
_TRAILING_QUOTES = {"'", '"', "`", "´", "’", "”"}


@dataclass(frozen=True)
class OrphanEdit:
    """One auditable orphan-noise decision."""

    line: int  # 1-based
    action: str
    before: str
    after: str | None  # None for whole-line drops


@dataclass(frozen=True)
class OrphanNoiseResult:
    """Orphan-noise output: cleaned text + ledger + removal spans.

    ``removal_spans`` and ``substitutions`` follow the reconciliation
    ledger contract (1-based) and feed ``clean_gate`` directly.
    """

    text: str
    edits: tuple[OrphanEdit, ...]
    removal_spans: tuple[tuple[int, int], ...]
    substitutions: tuple[tuple[int, str], ...]
    stats: dict[str, int]

    @property
    def edit_count(self) -> int:
        """Number of ledger decisions recorded."""
        return len(self.edits)


def _is_orphan_punct_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) == 1 and stripped in _ORPHAN_PUNCTUATION


def _strip_stray_trailing_quote(line: str) -> str | None:
    """Return the line minus a stray trailing quote, or None if absent.

    Stray means: the quote is the last non-space character, it stands
    apart from the preceding word (whitespace before it), and its count
    in the line is odd (no closing partner to lose).
    """
    core = line.rstrip()
    if not core:
        return None
    last = core[-1]
    if last not in _TRAILING_QUOTES:
        return None
    if len(core) >= 2 and core[-2] not in (" ", "\t"):
        # glued to a word (possessive / closing after word): not noise
        return None
    if core.count(last) % 2 == 0:
        # paired somewhere in the line: closing quote, not stray
        return None
    return core[:-1].rstrip()


def remove_orphan_noise(text: str) -> OrphanNoiseResult:
    """Drop orphan punctuation lines and strip stray trailing quotes.

    Idempotent; pure; every decision ledger-recorded. Line drops emit
    removal spans in the reconciliation ledger format so the clean
    gate can verify these deletions are explained.
    """
    edits: list[OrphanEdit] = []
    spans: list[tuple[int, int]] = []
    subs: list[tuple[int, str]] = []
    stats = {"lines_dropped": 0, "quotes_stripped": 0}

    out_lines: list[str] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if _is_orphan_punct_line(line):
            edits.append(OrphanEdit(lineno, ACTION_DROPPED_LINE, line, None))
            spans.append((lineno, lineno))
            stats["lines_dropped"] += 1
            continue
        stripped = _strip_stray_trailing_quote(line)
        if stripped is not None:
            edits.append(OrphanEdit(lineno, ACTION_STRIPPED_QUOTE, line, stripped))
            subs.append((lineno, stripped))
            stats["quotes_stripped"] += 1
            out_lines.append(stripped)
            continue
        out_lines.append(line)

    return OrphanNoiseResult(
        text="\n".join(out_lines),
        edits=tuple(edits),
        removal_spans=tuple(spans),
        substitutions=tuple(subs),
        stats=stats,
    )
