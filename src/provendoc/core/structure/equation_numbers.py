"""Equation-number evidence shared by the DOCX and PDF formula paths.

Display equations commonly carry their number as trailing text, either inside
the OMML paragraph (DOCX) or on the formula line itself (PDF):

    E = mc^2                        (3.1)
    f(x) = a + b                    (12)
    y = kx + b                     （式3-2）

The matcher is deliberately conservative.  It only reports a number when a
parenthesized number sits at the very end of the text, preceded by nothing,
whitespace, or an explicit label ("式" / "公式" / "Eq.").  Callers are
expected to apply it to blocks already identified as formulas; the number is
extracted as a fact, never rewritten into the text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Number forms: "3", "3.1", "3-1", "3–2", "3a", "B.12", "A.3".  An optional
# single uppercase letter prefix covers appendix numbering ("(A.12)"); a
# lowercase prefix is deliberately excluded because "(x2)" reads as a
# function argument, not a number.  The parenthesized group must terminate
# the text; full-width parentheses are normalized to half-width first.
# Requiring whitespace/label/start before the opening parenthesis keeps
# mid-formula parenthesized expressions ("f(2)") from matching.
_EQ_NUM_TAIL = re.compile(
    r"(?:(?<=\s)|^)"
    r"(?P<label>(?:式|公式|eq\.?|equation)\s*)?"
    r"\((?P<number>(?-i:[A-Z])?[.\-–]?\d+(?:[.\-–]\d+)*[a-zA-Z]?)\)\s*$",
    re.IGNORECASE,
)
_LABEL_FOR_SOURCE = {
    "式": "cjk_label",
    "公式": "cjk_label",
}
_MAX_TAIL_LENGTH = 40
"""A number farther than this from the end is part of the formula, not a label."""


@dataclass(frozen=True)
class EquationNumberMatch:
    """One factual equation-number observation extracted from formula text."""

    number: str
    source: str
    pattern: str


def match_equation_number(text: str) -> EquationNumberMatch | None:
    """Return the trailing equation number of ``text`` when one provably exists.

    Only the whitespace-trimmed tail of the text is examined.  A match is
    returned only for a parenthesized number at the end of the text; nothing
    is inferred about numbering schemes or sequence continuity.
    """
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text.strip())
    tail = normalized[-_MAX_TAIL_LENGTH:]
    match = _EQ_NUM_TAIL.search(tail)
    if match is None:
        return None
    label = (match.group("label") or "").strip().lower()
    source = "trailing_parentheses"
    if label:
        source = _LABEL_FOR_SOURCE.get(label.rstrip("."), "labelled_parentheses")
    return EquationNumberMatch(
        number=match.group("number"),
        source=source,
        pattern=match.group(0).strip(),
    )
