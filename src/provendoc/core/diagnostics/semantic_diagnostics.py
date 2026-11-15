import re
from typing import Any, Dict

_BRACKET_HEADING_PATTERN = re.compile(r"^【(.+?)】$")

_NUMBERED_HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百千万\d]+条"),
    re.compile(r"^[一二三四五六七八九十]+[、.]"),
]

_ENUMERATION_PATTERNS = [
    re.compile(r"^[-*•]\s+"),
    re.compile(r"^\d+[.)、]\s+"),
]


def _split_lines(text):
    """Unified line-splitting scope.

    CR/CRLF is normalized to \n and split on \n; \x0c/\x0b/\u2028/
    \u2029/\x85 are no longer line boundaries (kept as in-line
    characters) — so form feed and friends cannot slice single-line
    text into virtual lines and partially resurrect structure signals
    that were already destroyed.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def extract_semantic_signals(text: str) -> dict:
    """Extract structural cues from plain text: headings, tables,
    enumerations.

    The returned signals are not quality scores but structure boundaries
    a chunking strategy can respect.
    """
    signals: Dict[str, Any] = {
        "sections": {
            "contains_section_headings": False,
            "heading_patterns": [],
            "section_headings": [],
        },
        "tables": {
            "contains_table_like_text": False,
        },
        "enumeration": {
            "contains_enumeration": False,
        },
    }

    lines = _split_lines(text)

    _detect_bracket_headings(lines, signals)
    _detect_numbered_headings(lines, signals)
    _detect_table_like_text(lines, signals)
    _detect_enumeration(lines, signals)

    signals["sections"]["heading_patterns"] = sorted(
        set(signals["sections"]["heading_patterns"])
    )

    return signals


def _detect_bracket_headings(lines, signals):
    """Bracket headings of the 【适应症】 form."""
    bracket_headings = []
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        match = _BRACKET_HEADING_PATTERN.match(stripped)
        if match:
            bracket_headings.append({
                "title": match.group(1),
                "line": line_index,
                "pattern": "bracket_heading",
            })

    if bracket_headings:
        signals["sections"]["contains_section_headings"] = True
        signals["sections"]["heading_patterns"].append("bracket_heading")
        signals["sections"]["section_headings"].extend(bracket_headings)


def _detect_numbered_headings(lines, signals):
    """Numbered headings: 第一条 / 一、 / 1. forms."""
    numbered_headings = []
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        for pattern in _NUMBERED_HEADING_PATTERNS:
            if pattern.match(stripped):
                numbered_headings.append({
                    "title": stripped,
                    "line": line_index,
                    "pattern": "numbered_heading",
                })
                break

    if numbered_headings:
        signals["sections"]["contains_section_headings"] = True
        signals["sections"]["heading_patterns"].append("numbered_heading")
        signals["sections"]["section_headings"].extend(numbered_headings)


def _detect_table_like_text(lines, signals):
    """A table is deemed present when the longest run of consecutive
    |-containing lines reaches >= 2.

    Inter-line continuity: scattered |-containing lines — link separators
    (English | 日本語), type-union syntax (Menu | null) — do not
    constitute a table signal; a table interrupted by a blank line is
    treated as structurally damaged and does not trigger.
    """
    best_run = 0
    current_run = 0
    for line in lines:
        if "|" in line:
            current_run += 1
            if current_run > best_run:
                best_run = current_run
        else:
            current_run = 0
    if best_run >= 2:
        signals["tables"]["contains_table_like_text"] = True


def _detect_enumeration(lines, signals):
    """An enumeration is deemed present when >= 2 lines match the
    - / * / bullet / N. / N) / N、 forms."""
    enumeration_count = 0
    for line in lines:
        stripped = line.strip()
        for pattern in _ENUMERATION_PATTERNS:
            if pattern.match(stripped):
                enumeration_count += 1
                break

    if enumeration_count >= 2:
        signals["enumeration"]["contains_enumeration"] = True
