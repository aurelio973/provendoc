# ============================================================
# Structure Layer
# Text-subtype candidate detection for extension-routed .txt files
# ============================================================
"""Subtype candidacy for text-claimed files: JSON / NDJSON / XML / HTML /
CSV / TSV / log / Markdown / code.

A ``.txt`` file frequently *carries* a structured subtype (a JSON dump
saved as .txt, a log export, a CSV paste).  Extension-based routing
already sent the file to TxtParser; this detector records what the
content actually looks like as **candidate evidence only**.

Fact boundaries (documented, not guessed):

- the result is a candidacy note, never a routing decision:
  ``delegated`` is always ``False`` and no parser is invoked from here —
  delegation would need a prereg'd policy, which is out of scope;
- detection is signal-based and size-guarded: strict parsing is
  attempted only for JSON-family candidates within a bounded size, and
  every parse is fully exception-guarded;
- security posture: observation only — no execution of detected code,
  no entity expansion of detected XML, no YAML parsing at all (alias
  bombs); regex signals are linear-time patterns over bounded probes.

Output shape (``metadata["detected_subtype"]``, written only when a
medium/high-confidence candidate exists)::

    {
        "subtype": "json" | "ndjson" | "xml" | "html" | "csv" | "tsv"
                   | "log" | "markdown" | "code",
        "confidence": "high" | "medium",
        "signals": [str, ...],        # the evidence that fired
        "candidate_only": True,
        "delegated": False,
    }
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Strict JSON parsing is attempted only up to this size (characters) —
# a multi-GB dump pasted as .txt must not be fed to the parser twice.
# Public name: the parse_config fact block records this cap verbatim.
SUBTYPE_PROBE_CHAR_CAP = 1_000_000
_MAX_PROBE_CHARS = SUBTYPE_PROBE_CHAR_CAP

# Probe windows for line-based signals.
_NDJSON_PROBE_LINES = 20
_CSV_PROBE_LINES = 10
_LOG_PROBE_LINES = 50

_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_FENCE_RE = re.compile(r"^\s*```")
_XML_DECL_RE = re.compile(r"^\s*<\?xml[\s?]", re.IGNORECASE)
_HTML_RE = re.compile(r"^\s*(?:<!doctype\s+html|<html[\s>])", re.IGNORECASE)
_ROOT_TAG_RE = re.compile(r"^\s*<([A-Za-z_][\w:.-]*)(?:\s[^<>]*)?>")
_LOG_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"           # ISO-ish timestamp
    r"|\[\d{4}-\d{2}-\d{2}"                       # [2026-09-28 ...
    r"|\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}"       # Apache access log
    r")"
)
_CODE_FEATURE_RE = re.compile(
    r"^\s*(?:"
    r"def\s+\w+\(|class\s+\w+|import\s+\w|from\s+\w+\s+import"
    r"|#include\s*[<\"]|package\s+\w+|using\s+\w+"
    r"|(?:public|private|protected|static|final|const|let|var|function)\s"
    r"|(?:if|for|while)\s*\(.*\)\s*\{?"
    r"|(?:if|elif|for|while)\s+.*:$"               # Python-style trailing colon
    r"|return\s|}\s*$"
    r"|\w+\s*=\s*(?:function|async|\()"
    r")"
)


def _non_empty_lines(text: str, limit: int) -> List[str]:
    lines: List[str] = []
    for ln in text.splitlines():
        if ln.strip() != "":
            lines.append(ln)
            if len(lines) >= limit:
                break
    return lines


def _json_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    if len(text) > _MAX_PROBE_CHARS:
        return ("json", "medium", ["json_like_start_probe_size_exceeded"])
    try:
        json.loads(stripped)
        return ("json", "high", ["strict_json_parse_ok"])
    except Exception:
        return None  # a lone "{" opening is too weak to register


def _ndjson_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    if len(text) > _MAX_PROBE_CHARS:
        return None
    lines = _non_empty_lines(text, _NDJSON_PROBE_LINES)
    if len(lines) < 3:
        return None
    parsed = 0
    for ln in lines:
        if ln.lstrip()[:1] not in "{[":
            continue
        try:
            json.loads(ln)
            parsed += 1
        except Exception:
            continue
    if parsed >= 3 and parsed / len(lines) >= 0.8:
        return ("ndjson", "high", [f"ndjson_line_parse_ratio:{parsed}/{len(lines)}"])
    return None


def _xml_html_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    if _XML_DECL_RE.match(text):
        return ("xml", "high", ["xml_declaration"])
    if _HTML_RE.match(text):
        return ("html", "high", ["html_doctype_or_root_tag"])
    if _ROOT_TAG_RE.match(text):
        return ("xml", "medium", ["xml_like_root_tag"])
    return None


def _delimited_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    lines = _non_empty_lines(text, _CSV_PROBE_LINES)
    if len(lines) < 3:
        return None
    for subtype, delimiter in (("tsv", "\t"), ("csv", ",")):
        counts = [ln.count(delimiter) for ln in lines]
        if min(counts) >= 1 and len(set(counts)) == 1:
            return (
                subtype,
                "medium",
                [f"consistent_delimiter_columns:{counts[0] + 1}x{len(lines)}"],
            )
    return None


def _log_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    lines = _non_empty_lines(text, _LOG_PROBE_LINES)
    if len(lines) < 5:
        return None
    hits = sum(1 for ln in lines if _LOG_PREFIX_RE.match(ln))
    if hits / len(lines) >= 0.3:
        return ("log", "medium", [f"timestamp_prefix_ratio:{hits}/{len(lines)}"])
    return None


def _markdown_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    lines = text.splitlines()
    headings = sum(1 for ln in lines if _ATX_HEADING_RE.match(ln))
    fences = sum(1 for ln in lines if _FENCE_RE.match(ln))
    signals: List[str] = []
    if headings >= 2:
        signals.append(f"atx_heading_lines:{headings}")
    if fences >= 2:
        signals.append(f"code_fence_lines:{fences}")
    if headings >= 2:
        return ("markdown", "medium", signals)
    return None


def _code_candidate(text: str) -> Optional[Tuple[str, str, List[str]]]:
    lines = _non_empty_lines(text, _LOG_PROBE_LINES)
    if len(lines) < 5:
        return None
    hits = sum(1 for ln in lines if _CODE_FEATURE_RE.match(ln))
    if hits >= 5 and hits / len(lines) >= 0.4:
        return ("code", "medium", [f"code_feature_line_ratio:{hits}/{len(lines)}"])
    return None


# ---------------------------------------------------------------------------
# Delegation probe (2026-09-29 batch)
# ---------------------------------------------------------------------------

# Delegates re-run the actual stdlib parser for the detected subtype, so
# "looks like X" is never confused with "successfully parsed as X".
# Guards: size-capped, fully exception-guarded, observation only — the
# document text is never replaced by the delegate's output; a failed
# delegation falls back to plain text and registers the reason.
_DELEGATE_SIZE_CAP = _MAX_PROBE_CHARS


def _delegate_json(text: str) -> None:
    json.loads(text.strip())


def _delegate_ndjson(text: str) -> None:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        json.loads(ln)


def _delegate_delimited(delimiter: str):
    def _run(text: str) -> None:
        import csv

        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
        if not rows:
            raise ValueError("no rows parsed")

    return _run


def _delegate_xml(text: str) -> None:
    # stdlib ElementTree never resolves external entities and CPython's
    # expat caps internal entity amplification; size is capped on top.
    import xml.etree.ElementTree as ET

    ET.fromstring(text.strip())


_DELEGATES = {
    "json": ("stdlib.json", _delegate_json),
    "ndjson": ("stdlib.json-lines", _delegate_ndjson),
    "csv": ("stdlib.csv", _delegate_delimited(",")),
    "tsv": ("stdlib.csv", _delegate_delimited("\t")),
    "xml": ("stdlib.ElementTree", _delegate_xml),
}


def probe_delegation(
    subtype_record: Dict[str, Any], text: str
) -> Dict[str, Any]:
    """Attempt real delegation for a detected subtype; register the outcome.

    Output shape (``metadata["delegation"]``)::

        {
            "subtype": str,
            "attempted": bool,
            "delegate": str | None,       # e.g. "stdlib.json"
            "status": "ok" | "failed" | "not_attempted",
            "reason": str | None,          # why not attempted / error class
            "fallback": "plain_text" | None,
        }

    Security posture: delegates are stdlib parsers only, size-capped,
    no network/entity fetching, no execution of detected code, and the
    delegate's output is discarded — this is a viability probe, never a
    content replacement.
    """
    subtype = str(subtype_record.get("subtype"))
    base: Dict[str, Any] = {
        "subtype": subtype,
        "fallback": None,
    }

    entry = _DELEGATES.get(subtype)
    if entry is None:
        base.update({
            "attempted": False,
            "delegate": None,
            "status": "not_attempted",
            "reason": "no_delegate_registered",
        })
        return base

    delegate_name, delegate = entry
    base["delegate"] = delegate_name
    if len(text) > _DELEGATE_SIZE_CAP:
        base.update({
            "attempted": False,
            "status": "not_attempted",
            "reason": "size_cap_exceeded",
        })
        return base

    try:
        delegate(text)
    except Exception as exc:
        base.update({
            "attempted": True,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
            "fallback": "plain_text",
        })
        return base

    base.update({
        "attempted": True,
        "status": "ok",
        "reason": None,
    })
    return base


def detect_subtype(text: str) -> Optional[Dict[str, Any]]:
    """Return the best subtype candidacy for ``text``, or ``None``.

    Probes run in specificity order and the first medium/high-confidence
    hit wins; everything is exception-guarded and side-effect free.
    """
    if not text.strip():
        return None

    probes = (
        _json_candidate,
        _ndjson_candidate,
        _xml_html_candidate,
        _delimited_candidate,
        _log_candidate,
        _markdown_candidate,
        _code_candidate,
    )
    for probe in probes:
        try:
            hit = probe(text)
        except Exception:
            continue  # observation must never break parsing
        if hit is None:
            continue
        subtype, confidence, signals = hit
        return {
            "subtype": subtype,
            "confidence": confidence,
            "signals": signals,
            "candidate_only": True,
            "delegated": False,
        }
    return None
