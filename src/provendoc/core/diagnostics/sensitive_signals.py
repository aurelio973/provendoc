# ============================================================
# Diagnostics Layer
# Sensitive-data candidacy detection (identify only, never mask)
# ============================================================
"""Sensitive-information candidacy for text: phone numbers, email,
national IDs, bank cards, credential assignments, private IPs.

Contract (register-driven, mirrors the project-wide discipline):

- **identify, never mask**: the parser records candidates and their
  locations; masking / redaction / removal is a downstream Policy
  decision (``redacted`` is always ``False`` here).  The document text
  is never modified by this module;
- **never store the value**: locations and counts only — a leaked
  registry would itself be a leak, so matched strings are discarded
  the moment they are counted;
- **candidacy, not judgment**: pattern hits are candidates.  Bank
  cards get a Luhn check to suppress the worst false positives, but a
  passing Luhn check still only means "looks like a card number";
- **bounded**: locations are capped per type; totals are always exact.

Output shape (``metadata["sensitive_candidates"]``, written only when
at least one candidate exists)::

    {
        "present": True,
        "redacted": False,
        "total_count": int,
        "types": {
            "cn_mobile": {
                "count": int,
                "locations": [{"line_start": n, "line_end": n}, ...],
                "locations_truncated": bool,
            },
            ...
        },
        "note": "identification only; masking is downstream policy",
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict

# Locations registered per type (counts stay exact beyond the cap).
_MAX_LOCATIONS_PER_TYPE = 20

_PATTERNS = {
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "email": re.compile(
        r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
    ),
    "cn_national_id": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card_candidate": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "private_ip": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
    "credential_assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|pwd)"
        r"\b\s*[:=]\s*[\"']?\S{4,}"
    ),
    # Standalone JWT shape: three base64url segments, the header always
    # starts with eyJ (base64 of {"); the assignment form is already
    # covered by credential_assignment — only free-standing tokens here.
    "jwt_token": re.compile(
        r"(?<![\w.-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        r"\.[A-Za-z0-9_-]{3,}(?![\w.-])"
    ),
    # Patient ID: label word + number.
    "patient_id": re.compile(
        r"(?i)(?:patient[_-]?id|病历号|住院号|门诊号|病案号)"
        r"\s*[:：#]?\s*[A-Za-z0-9-]{4,}"
    ),
}


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum; suppresses non-card 16-19 digit runs."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_sensitive_candidates(text: str) -> Dict[str, Any] | None:
    """Scan ``text`` line by line; return the registry or ``None``.

    Runs on the structured view; line numbers are 1-based against it.
    Values are never retained — only type, count and line locations.
    """
    types: Dict[str, Dict[str, Any]] = {}
    total = 0

    for line_no, line in enumerate(text.splitlines(), 1):
        for type_name, pattern in _PATTERNS.items():
            for match in pattern.finditer(line):
                if type_name == "bank_card_candidate" and not _luhn_ok(
                    match.group(0)
                ):
                    continue
                entry = types.setdefault(
                    type_name, {"count": 0, "locations": []}
                )
                entry["count"] += 1
                total += 1
                if len(entry["locations"]) < _MAX_LOCATIONS_PER_TYPE:
                    entry["locations"].append(
                        {"line_start": line_no, "line_end": line_no}
                    )

    if not types:
        return None

    for entry in types.values():
        entry["locations_truncated"] = (
            len(entry["locations"]) < entry["count"]
        )

    return {
        "present": True,
        "redacted": False,
        "total_count": total,
        "types": types,
        "note": (
            "identification only; masking/removal is a downstream "
            "policy decision, the parser never rewrites content"
        ),
    }


def scan_sensitive_types(text: str) -> list[str]:
    """Distinct sensitive-candidate type names hit anywhere in ``text``.

    Read-only reuse entry over the same pattern library: a contract
    entry-level scan that returns only type names (values are never
    stored); the ``_PATTERNS`` vocabulary, the Luhn guard and
    detect_sensitive_candidates share one source. Central stamping
    behavior is unchanged — this function is a purely additive read
    surface and alters no existing pattern or behavior.
    """
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for type_name, pattern in _PATTERNS.items():
        for match in pattern.finditer(text):
            if type_name == "bank_card_candidate" and not _luhn_ok(
                match.group(0)
            ):
                continue
            found.append(type_name)
            break
    return found
