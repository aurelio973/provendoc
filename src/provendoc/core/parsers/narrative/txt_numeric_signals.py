# ============================================================
# Narrative Domain
# TXT date / amount candidate signals (observation only)
# ============================================================
"""Date and amount candidate registration for TXT text.

Frozen rules (txt_engineering_gaps_prereg_v1 G8): candidates are
*recognition only* — counts, bounded samples and capped line
locations are recorded; nothing is parsed into date/numeric types
and the text is never rewritten.  Samples are values (dates and
amounts are not PII), bounded like domain_signals samples.
"""

import bisect
import re
from typing import Any, Dict, List, Optional

_MAX_SAMPLES = 5
_MAX_SAMPLE_CHARS = 40
_MAX_LOCATIONS = 20

_DATE_PATTERNS = [
    re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
]

_AMOUNT_PATTERNS = [
    re.compile(r"[¥￥$€]\s?\d[\d,]*(?:\.\d{1,2})?"),
    re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b"),
    re.compile(r"\d+(?:\.\d+)?\s?(?:元|万元|亿元)"),
]


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    return starts


def _collect(
    patterns: List[re.Pattern],
    text: str,
    starts: List[int],
) -> Optional[Dict[str, Any]]:
    """Count matches, bounded samples and capped line locations."""
    count = 0
    samples: List[str] = []
    locations: List[Dict[str, int]] = []
    seen_lines = set()
    locations_truncated = False
    for pattern in patterns:
        for match in pattern.finditer(text):
            count += 1
            sample = match.group(0)[:_MAX_SAMPLE_CHARS]
            if sample not in samples and len(samples) < _MAX_SAMPLES:
                samples.append(sample)
            line_no = bisect.bisect_right(starts, match.start())
            if line_no not in seen_lines:
                if len(locations) < _MAX_LOCATIONS:
                    seen_lines.add(line_no)
                    locations.append({"line_start": line_no, "line_end": line_no})
                else:
                    locations_truncated = True
    if count == 0:
        return None
    return {
        "count": count,
        "samples": samples,
        "locations": locations,
        "locations_truncated": locations_truncated,
    }


def detect_numeric_signals(text: str) -> Optional[Dict[str, Any]]:
    """Register date/amount candidates, or None when neither exists.

    Args:
        text: the extracted narrative text (as-is; never rewritten).

    Returns:
        ``{"date_candidates": ..., "amount_candidates": ...}`` with
        only the non-empty classes present; ``None`` when both classes
        are empty (absent-key honesty).
    """
    if not text:
        return None
    starts = _line_starts(text)
    result: Dict[str, Any] = {}
    dates = _collect(_DATE_PATTERNS, text, starts)
    if dates is not None:
        result["date_candidates"] = dates
    amounts = _collect(_AMOUNT_PATTERNS, text, starts)
    if amounts is not None:
        result["amount_candidates"] = amounts
    return result or None
