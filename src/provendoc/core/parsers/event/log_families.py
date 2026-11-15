# -*- coding: utf-8 -*-
"""Log/JSONL format-family detection — observation-fact layer of the event domain.

The observation layer of the three-tier structure: recognizes standard syntax
families on decoded raw lines/records and emits family names plus field-level
statistical facts (log_format_family_prereg_v1 frozen scope). Observe only,
never modify: never touches event text, never joins the sanitization chain —
the same discipline as invisible_carrier_v1.

Log families (line-start-anchored syntax, D1 majority vote >=60% with >=3
sampled lines):
    clf_access / w3c_extended / syslog_3164 / syslog_5424 / logfmt

JSONL envelope families (record-level key-set fingerprint, D3: >=2 dict
records with >=80% hits):
    aws_cloudtrail / elastic_ecs / otlp_json
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ============================================================
# Frozen thresholds (prereg §2)
# ============================================================

FAMILY_MAJORITY_RATIO = 0.6
FAMILY_MIN_LINES = 3
LOGFMT_TOKEN_RATIO = 0.7
LOGFMT_MIN_KV_TOKENS = 2
ENVELOPE_MIN_RECORDS = 2
ENVELOPE_RATIO = 0.8
MAX_DISTINCT_KEYS = 20
MAX_STATUS_CODES = 10

# ============================================================
# Line-level syntax (all line-start anchored)
# ============================================================

# NCSA Combined: host ident user [time] "req" status size "ref" "ua"
_CLF_COMBINED_RE = re.compile(
    r'^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) '
    r'\[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r'(?P<status>\d{3}|-) (?P<size>\d+|-) '
    r'"(?P<referer>[^"]*)" "(?P<agent>[^"]*)"'
)
# NCSA Common: no referer/UA tail
_CLF_COMMON_RE = re.compile(
    r'^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) '
    r'\[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r'(?P<status>\d{3}|-) (?P<size>\d+|-)$'
)
_W3C_FIELDS_RE = re.compile(r'^#Fields:\s+(.+)$')
_SYSLOG_3164_RE = re.compile(
    r'^<(?P<pri>\d{1,3})>'
    r'(?P<stamp>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+'
    r'(?P<host>\S+)\s+(?P<rest>.*)$'
)
_SYSLOG_5424_RE = re.compile(
    r'^<(?P<pri>\d{1,3})>1\s+(?P<stamp>\S+)\s+(?P<host>\S+)\s+'
    r'(?P<app>\S+)\s+(?P<procid>\S+)\s+(?P<msgid>\S+)'
    r'(?:\s+(?P<structured>\[[^\]]*\]|\S+|-))?\s*(?P<rest>.*)$'
)
_LOGFMT_TOKEN_RE = re.compile(r'^[A-Za-z_][\w.\-]*=\S+$')

# RFC 5424 semantic names (severity 0-7)
_SEVERITY_NAMES = (
    "emerg", "alert", "crit", "err",
    "warning", "notice", "info", "debug",
)

FAMILY_BOUNDARY_HINTS = (
    "clf_access", "syslog_3164", "syslog_5424", "logfmt",
)


def classify_log_line(line: str) -> Optional[str]:
    """Classify a single line into its log syntax family (line-start anchored; None on no match).

    Args:
        line: a stripped non-empty line.

    Returns:
        Family name or None (generic does not impersonate here).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if _CLF_COMBINED_RE.match(stripped) or _CLF_COMMON_RE.match(stripped):
        return "clf_access"
    if _SYSLOG_5424_RE.match(stripped):
        return "syslog_5424"
    if _SYSLOG_3164_RE.match(stripped):
        return "syslog_3164"
    tokens = stripped.split()
    if len(tokens) >= 2:
        kv = sum(1 for t in tokens if _LOGFMT_TOKEN_RE.match(t))
        if (
            kv >= LOGFMT_MIN_KV_TOKENS
            and kv / len(tokens) >= LOGFMT_TOKEN_RATIO
        ):
            return "logfmt"
    return None


# ============================================================
# Family fact construction (prereg §2.2, write only non-zero)
# ============================================================

def _clf_facts(lines: Sequence[str]) -> Dict[str, Any]:
    combined = common = 0
    methods: Dict[str, int] = {}
    classes: Dict[str, int] = {}
    clients: set[str] = set()

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        m = _CLF_COMBINED_RE.match(stripped)
        if m:
            combined += 1
        else:
            m = _CLF_COMMON_RE.match(stripped)
            if m is None:
                continue
            common += 1
        if m is None:
            continue
        clients.add(m.group("client"))
        request = m.group("request")
        parts = request.split()
        if parts:
            methods[parts[0]] = methods.get(parts[0], 0) + 1
        status = m.group("status")
        if status.isdigit():
            bucket = f"{status[0]}xx"
            classes[bucket] = classes.get(bucket, 0) + 1

    return {
        "variant": "combined" if combined >= common else "common",
        "http_method_distribution": methods,
        "status_class_distribution": classes,
        "distinct_client_count": len(clients),
    }


def _w3c_facts(
    fields: Sequence[str], lines: Sequence[str]
) -> Dict[str, Any]:
    data_lines = [
        s.strip() for s in lines
        if s.strip() and not s.strip().startswith("#")
    ]
    facts: Dict[str, Any] = {
        "fields": list(fields),
        "data_line_count": len(data_lines),
    }
    if "sc-status" in fields:
        idx = fields.index("sc-status")
        codes: Dict[str, int] = {}
        for line in data_lines:
            tokens = line.split()
            if len(tokens) == len(fields) and tokens[idx].isdigit():
                codes[tokens[idx]] = codes.get(tokens[idx], 0) + 1
        if codes:
            top = sorted(
                codes.items(), key=lambda kv: (-kv[1], kv[0])
            )[:MAX_STATUS_CODES]
            facts["sc_status_distribution"] = dict(top)
    return facts


def _syslog_facts(
    family: str, lines: Sequence[str]
) -> Dict[str, Any]:
    severities: Dict[str, int] = {}
    facilities: Dict[str, int] = {}
    hosts: set[str] = set()

    pattern = (
        _SYSLOG_5424_RE if family == "syslog_5424" else _SYSLOG_3164_RE
    )
    for raw in lines:
        m = pattern.match(raw.strip())
        if m is None:
            continue
        pri = int(m.group("pri"))
        if pri > 191:
            continue
        hosts.add(m.group("host"))
        sev = _SEVERITY_NAMES[pri % 8]
        severities[sev] = severities.get(sev, 0) + 1
        fac = str(pri // 8)
        facilities[fac] = facilities.get(fac, 0) + 1

    return {
        "protocol": family.split("_")[1],
        "severity_distribution": severities,
        "facility_distribution": facilities,
        "distinct_host_count": len(hosts),
    }


def _logfmt_facts(lines: Sequence[str]) -> Dict[str, Any]:
    keys: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if classify_log_line(stripped) != "logfmt":
            continue
        for token in stripped.split():
            if _LOGFMT_TOKEN_RE.match(token):
                keys.add(token.split("=", 1)[0])
    return {
        "distinct_keys": sorted(keys)[:MAX_DISTINCT_KEYS],
        "distinct_key_count": len(keys),
    }


# ============================================================
# Family adjudication (D1)
# ============================================================

def detect_log_format_family(
    lines: Iterable[str],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Majority-vote detection of the whole log's syntax family; returns (family, facts).

    W3C is decided by the mere presence of a ``#Fields:`` directive; the other
    families vote line-level — >=60% with >=3 sampled lines wins; no hit
    returns (None, {}) — generic does not impersonate.

    Args:
        lines: decoded raw line view (shared with event recovery).

    Returns:
        (family, facts): when family is None, no metadata key is written.
    """
    line_list: List[str] = list(lines)
    votes: Dict[str, int] = {}
    sampled = 0
    w3c_fields: Optional[List[str]] = None

    for raw in line_list:
        stripped = raw.strip()
        if not stripped:
            continue
        fields_match = _W3C_FIELDS_RE.match(stripped)
        if fields_match:
            w3c_fields = fields_match.group(1).split()
            continue
        if stripped.startswith("#"):
            continue
        sampled += 1
        family = classify_log_line(stripped)
        if family is not None:
            votes[family] = votes.get(family, 0) + 1

    if w3c_fields is not None and sampled >= FAMILY_MIN_LINES:
        return "w3c_extended", _w3c_facts(w3c_fields, line_list)

    if sampled >= FAMILY_MIN_LINES:
        best = max(votes.items(), key=lambda kv: kv[1], default=None)
        if best and best[1] / sampled >= FAMILY_MAJORITY_RATIO:
            family, _ = best
            builders = {
                "clf_access": _clf_facts,
                "logfmt": _logfmt_facts,
            }
            if family in builders:
                return family, builders[family](line_list)
            if family.startswith("syslog_"):
                return family, _syslog_facts(family, line_list)
    return None, {}


# ============================================================
# jsonl envelope families (D3)
# ============================================================

_ENVELOPE_SIGNATURES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("aws_cloudtrail", ("eventSource", "eventID")),
    ("elastic_ecs", ("ecs.version",)),
    ("otlp_json", ("resourceLogs",)),
    ("otlp_json", ("resourceSpans",)),
    ("otlp_json", ("resourceMetrics",)),
)


def detect_jsonl_envelope(records: Sequence[Any]) -> Optional[str]:
    """Record-level key-set fingerprint detection of the JSONL envelope family.

    >=2 dict records with >=80% hits on the same signature -> family name;
    otherwise None (no key written; generic does not impersonate).

    Args:
        records: JsonlParser's records list (each item holds a "record" key).

    Returns:
        Envelope family name or None.
    """
    dict_records = [
        rec for r in records
        if isinstance(r, dict) and isinstance(r.get("record"), dict)
        for rec in (r["record"],)
    ]
    if len(dict_records) < ENVELOPE_MIN_RECORDS:
        return None
    for name, required in _ENVELOPE_SIGNATURES:
        hits = sum(
            1 for rec in dict_records
            if set(required) <= set(rec.keys())
        )
        if hits / len(dict_records) >= ENVELOPE_RATIO:
            return name
    return None
