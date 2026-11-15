"""Invoice-scenario L1 salvage (prereg invoice_scenario_prereg_v1 §1.A).

Deterministic, ledger-recorded numeric damage repair for OCR-extracted
invoice text. Every rewrite is recorded as a SalvageEdit (line number,
rule, before, after) so the whole transform is reversible and
auditable; no edit is ever silent.

Rules (v1, validated against the Arnold & Porter corpse sample):

* R1 underscore decimal: ``1,920_00`` -> ``1,920.00``
  (underscore between digits is OCR damage for the decimal point).
* R2 space tighten: ``7,907 .80`` -> ``7,907.80`` and ``1. 5`` -> ``1.5``
  (spaces squeezed around an inter-digit dot).

Honest boundaries (prereg §1.A / §2):

* alphabetic name damage (ABNOLD & POBTEB) is NOT touched here --
  cross-channel voting and header lexicon repair are separate rules
  (prereg §1.A.5), never silent;
* digit-context O/0 confusion and injected spaces inside identifiers
  are v1 backlog, not this module;
* these rules are invoice-scenario gated, not global: R2 would also
  tighten prose like ``v1. 2`` -- acceptable inside an invoice, wrong
  in general prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_UNDERSCORE_DECIMAL = "R1_underscore_decimal"
RULE_SPACE_TIGHTEN_BEFORE = "R2a_space_tighten_before"
RULE_SPACE_TIGHTEN_AFTER = "R2b_space_tighten_after"

_UNDERSCORE_DECIMAL_RE = re.compile(r"(?<=\d)_(?=\d)")
_SPACE_BEFORE_DOT_RE = re.compile(r"(?<=\d) \.(?=\d)")
_SPACE_AFTER_DOT_RE = re.compile(r"(?<=\d)\. (?=\d)")


@dataclass(frozen=True)
class SalvageEdit:
    """One auditable rewrite: what rule, which line, before/after."""

    line: int  # 1-based
    rule: str
    before: str
    after: str


@dataclass(frozen=True)
class SalvageResult:
    """Salvage output: repaired text + complete edit ledger."""

    text: str
    edits: tuple[SalvageEdit, ...]

    @property
    def edit_count(self) -> int:
        """Number of ledger-recorded rewrites applied."""
        return len(self.edits)


def salvage_numbers(text: str) -> SalvageResult:
    """Apply R1/R2 numeric damage repairs, ledger-recorded per line.

    Idempotent: applying twice produces zero second-pass edits.
    Pure function; no I/O; no exception paths on user data.
    """
    edits: list[SalvageEdit] = []
    out_lines: list[str] = []

    for lineno, line in enumerate(text.split("\n"), start=1):
        current = line

        if _UNDERSCORE_DECIMAL_RE.search(current):
            repaired = _UNDERSCORE_DECIMAL_RE.sub(".", current)
            edits.append(
                SalvageEdit(lineno, RULE_UNDERSCORE_DECIMAL, current, repaired)
            )
            current = repaired

        if _SPACE_BEFORE_DOT_RE.search(current):
            repaired = _SPACE_BEFORE_DOT_RE.sub(".", current)
            edits.append(
                SalvageEdit(lineno, RULE_SPACE_TIGHTEN_BEFORE, current, repaired)
            )
            current = repaired

        if _SPACE_AFTER_DOT_RE.search(current):
            repaired = _SPACE_AFTER_DOT_RE.sub(".", current)
            edits.append(
                SalvageEdit(lineno, RULE_SPACE_TIGHTEN_AFTER, current, repaired)
            )
            current = repaired

        out_lines.append(current)

    return SalvageResult(text="\n".join(out_lines), edits=tuple(edits))
