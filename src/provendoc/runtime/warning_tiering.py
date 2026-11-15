# -*- coding: utf-8 -*-
"""Two-source tiered warning aggregation view (presentation layer, tiering_v1).

Contract source: the warning_tiering_prereg_v1 prereg (frozen
2026-09-27, freeze declaration §5). A purely derived view: it only
reads run artifact fields and changes no warning layer (build_warnings
thresholds and gate_v1 rules are untouched).

Frozen scope:

- The DQR source has a single ``observe`` tier: the raw layer's
  ``metadata["warnings"]`` (fact broadcasts), with numbers and sample
  text normalized into templates before aggregation; at most
  ``SAMPLE_CAP`` representative samples per template (prereg §3
  anchors it at 3).
- The RE source reports the gate three-state (blocked/warned/passed,
  with not_evaluated recording honest absence) plus a note tier
  (capability limitations / evaluation_status anomalies /
  traceability unavailable / missing RE artifacts, aggregated by
  kind — the frozen dry run measured a 39/40 same-template flood,
  see prereg §2.2).
- Zero information loss (prereg §4.3 acceptance line 3): every
  template carries a ``by_doc`` count, and any instance can be traced
  back to a (document, original string in the raw artifact) pair;
  numbers inside templates are normalized, and the raw artifact is
  always authoritative for the original string.

Determinism (prereg §4.4): the same input records always yield a
byte-identical view — templates/notes sort by (-count, key), and
by_doc entries insert in input order.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

TIERING_VERSION = "tiering_v1"

# Frozen scope: per-template representative sample cap (prereg §3)
SAMPLE_CAP = 3

# Gate status enumeration (gate_v1 three-state + this layer's own
# honest-absence state)
_GATE_TIERS = ("blocked", "warned", "passed", "not_evaluated")

# Normalization rules (same normalization function as the frozen dry
# run, anchored by prereg §4.1): numbers → N; quoted samples →
# (sample: S); bare samples → (sample: S)
_NUM_RE = re.compile(r"\d+")
_SAMPLE_QUOTED_RE = re.compile(r'\(sample: ".*?"\)')
_SAMPLE_RE = re.compile(r"\(sample: [^)]*\)")


def normalize_warning_template(warning: str) -> str:
    """Normalize one DQR warning into its same-kind template.

    Presentation-layer normalization only (numbers and sample text
    are placeholderized); the original string is never changed — the
    raw artifact stays authoritative, this function only serves
    aggregation grouping.
    """
    t = _NUM_RE.sub("N", warning)
    t = _SAMPLE_QUOTED_RE.sub("(sample: S)", t)
    t = _SAMPLE_RE.sub("(sample: S)", t)
    return t


def _add_note(notes: Dict[tuple, List[str]], kind: str, detail: str,
              doc: str) -> None:
    key = (kind, detail)
    bucket = notes.get(key)
    if bucket is None:
        notes[key] = [doc]
    else:
        bucket.append(doc)


def build_tiered_view(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-file records into the two-source tiered view (pure, deterministic).

    Args:
        records: each element is
            ``{"file": relative path, "warnings": list[str] | None,
               "rse": dict | None}`` (``rse`` is the evaluation
            artifact's ``rse`` layer; pass None for a missing artifact
            and it is booked as honest absence).

    Returns:
        The tiering_v1 view dict, directly injectable into
        RUN_DATA.warning_tiering.
    """
    n_docs = len(records)
    templates: Dict[str, Dict[str, Any]] = {}
    gate_dist: Dict[str, int] = {t: 0 for t in _GATE_TIERS}
    gate_issue_docs: Dict[str, List[str]] = {}
    notes: Dict[tuple, List[str]] = {}
    docs_with_any_warning = 0
    total_strings = 0

    for rec in records:
        doc = str(rec.get("file") or "")
        raw_warnings = rec.get("warnings") or []
        warnings = [w for w in raw_warnings if isinstance(w, str)]
        if warnings:
            docs_with_any_warning += 1
        for w in warnings:
            total_strings += 1
            tmpl = normalize_warning_template(w)
            entry = templates.get(tmpl)
            if entry is None:
                entry = templates[tmpl] = {
                    "instances": 0, "by_doc": {}, "samples": [],
                }
            entry["instances"] += 1
            entry["by_doc"][doc] = entry["by_doc"].get(doc, 0) + 1
            if len(entry["samples"]) < SAMPLE_CAP:
                entry["samples"].append({"doc": doc, "text": w})

        rse_layer = rec.get("rse")
        if not isinstance(rse_layer, dict):
            gate_dist["not_evaluated"] += 1
            _add_note(notes, "rse_unavailable", "no evaluation artifact", doc)
            continue

        gate = rse_layer.get("gate_status") or {}
        status = gate.get("status")
        if status not in gate_dist:
            status = "not_evaluated"
        gate_dist[status] += 1

        for issue in gate.get("issues") or []:
            if isinstance(issue, str):
                gate_issue_docs.setdefault(issue, []).append(doc)

        # capability_evaluation_block_prereg_v1 §2.4: completeness now
        # lives under capability_profile.evaluation (the top-level key
        # is removed; straight migration, no dual read)
        capability = rse_layer.get("capability_profile") or {}
        evaluation = capability.get("evaluation") or {}
        eval_status = evaluation.get("status")
        if eval_status in ("partial", "skipped"):
            _add_note(notes, "evaluation_status", str(eval_status), doc)

        traceability = capability.get("traceability") or {}
        if traceability.get("available") is False:
            # prereg §6 decision record: "traceability unavailable" must
            # never be silently unremarked
            _add_note(notes, "traceability_unavailable",
                      "traceability unavailable", doc)
        for limitation in capability.get("limitations") or []:
            if isinstance(limitation, str):
                _add_note(notes, "limitation", limitation, doc)

    templates_out = [
        {
            "template": tmpl,
            "instances": e["instances"],
            "docs": len(e["by_doc"]),
            "by_doc": e["by_doc"],
            "samples": e["samples"],
        }
        for tmpl, e in sorted(
            templates.items(), key=lambda kv: (-kv[1]["instances"], kv[0])
        )
    ]
    notes_out = [
        {"kind": kind, "detail": detail, "docs": len(docs), "by_doc": docs}
        for (kind, detail), docs in sorted(
            notes.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    ]
    gate_issues_out = {
        mode: docs for mode, docs in sorted(
            gate_issue_docs.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    }

    tier_table = [
        {
            "source": "dqr", "tier": "observe",
            "instances": total_strings,
            "distinct": len(templates_out),
            "docs": n_docs,
        }
    ]
    for tier in _GATE_TIERS:
        if gate_dist[tier]:
            tier_table.append({
                "source": "rse", "tier": tier,
                "instances": gate_dist[tier],
                "distinct": gate_dist[tier],
                "docs": gate_dist[tier],
            })
    if notes_out:
        note_docs = len({doc for n in notes_out for doc in n["by_doc"]})
        tier_table.append({
            "source": "rse", "tier": "note",
            "instances": sum(n["docs"] for n in notes_out),
            "distinct": len(notes_out),
            "docs": note_docs,
        })

    return {
        "version": TIERING_VERSION,
        "n_docs": n_docs,
        "dqr": {
            "tier": "observe",
            "total_strings": total_strings,
            "distinct_templates": len(templates_out),
            "docs_with_any_warning": docs_with_any_warning,
            "templates": templates_out,
        },
        "rse": {
            "gate_dist": gate_dist,
            "gate_issue_docs": gate_issues_out,
            "notes": notes_out,
        },
        "tier_table": tier_table,
    }
