"""Run-level aggregation of PDF runtime and reliability metrics.

This module is pure accounting over per-document runtime facts produced by the
PDF pipeline.  It never computes, adjusts, or interprets quality scores: the
``narrative_input_reliability`` three-state distribution reported here is the
same external input-reliability context the pipeline already exposes per
document, aggregated so a run summary can answer operational questions like
"what share of documents ran OCR", "did MinerU succeed", and "how long did
each pipeline stage take".

Per-file input shape (all keys optional except ``document_id``)::

    {
        "document_id": str,
        "actual_runtime": str,
        "recommended_runtime": str,
        "ocr_status": str | None,
        "layout_status": str | None,
        "reliability_status": str | None,
        "page_count": int,
        "ocr_used_pages": [int, ...],
        "ocr_unavailable_pages": [int, ...],
        "ocr_page_timings": [{"page": int, "duration_ms": float, ...}, ...],
        "stages": {"probe": {"duration_ms": float, "status": str}, ...},
    }
"""

from __future__ import annotations

from typing import Any


def extract_pdf_run_metrics(document_metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the per-file facts needed for run aggregation from parser metadata.

    Returns ``None`` when the metadata does not look like a PDF document, so
    callers can blindly apply it to every parsed file.
    """
    runtime = document_metadata.get("pdf_runtime")
    if not isinstance(runtime, dict):
        return None
    routing = document_metadata.get("pdf_routing_decision") or {}
    quality_for_evaluation = document_metadata.get("pdf_quality_for_evaluation") or {}
    reliability = quality_for_evaluation.get("narrative_input_reliability") or {}

    return {
        "document_id": document_metadata.get("source_file")
        or document_metadata.get("file_name")
        or "",
        "actual_runtime": runtime.get("actual_runtime"),
        "recommended_runtime": routing.get("recommended_runtime")
        if isinstance(routing, dict)
        else None,
        "ocr_status": runtime.get("ocr_status"),
        "layout_status": runtime.get("layout_status"),
        "reliability_status": reliability.get("status")
        if isinstance(reliability, dict)
        else None,
        "page_count": document_metadata.get("page_count", 0),
        "ocr_used_pages": list(runtime.get("ocr_used_pages") or []),
        "ocr_unavailable_pages": list(runtime.get("ocr_unavailable_pages") or []),
        "ocr_failed_pages": list(runtime.get("ocr_failed_pages") or []),
        "ocr_budget_exhausted_pages": list(
            runtime.get("ocr_budget_exhausted_pages") or []
        ),
        "ocr_empty_output_pages": list(runtime.get("ocr_empty_output_pages") or []),
        "ocr_page_timings": list(runtime.get("ocr_page_timings") or []),
        "stages": dict(runtime.get("stages") or {}),
    }


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 3)
    rank = max(1, min(len(sorted_values), round(fraction * len(sorted_values))))
    return round(sorted_values[rank - 1], 3)


def _distribution(values: list[str]) -> dict[str, dict[str, float]]:
    counts: dict[str, int] = {}
    for value in values:
        if value is None:
            value = "unknown"
        counts[value] = counts.get(value, 0) + 1
    total = sum(counts.values()) or 1
    return {
        name: {"count": count, "pct": round(count * 100.0 / total, 1)}
        for name, count in sorted(counts.items())
    }


def _stage_duration_summary(
    stages_by_file: list[dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Aggregate per-stage durations (ms) with p50/p95/max across files."""
    by_stage: dict[str, list[float]] = {}
    failed: dict[str, int] = {}
    for stages in stages_by_file:
        for name, info in (stages or {}).items():
            if not isinstance(info, dict):
                continue
            duration = info.get("duration_ms")
            if isinstance(duration, (int, float)) and duration >= 0:
                by_stage.setdefault(name, []).append(float(duration))
            if str(info.get("status", "")).startswith("failed"):
                failed[name] = failed.get(name, 0) + 1
    summary: dict[str, Any] = {}
    for name, durations in by_stage.items():
        ordered = sorted(durations)
        summary[name] = {
            "count": len(ordered),
            "p50_ms": _percentile(ordered, 0.50),
            "p95_ms": _percentile(ordered, 0.95),
            "max_ms": round(ordered[-1], 3),
            "total_ms": round(sum(ordered), 3),
            "failed_count": failed.get(name, 0),
        }
    return summary


def aggregate_pdf_run_metrics(per_file: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-file PDF runtime facts into a run-level summary block."""
    files = [entry for entry in per_file if isinstance(entry, dict)]
    document_count = len(files)
    if not document_count:
        return {"document_count": 0}

    actual_runtimes = [str(f.get("actual_runtime") or "unknown") for f in files]
    recommended = [str(f.get("recommended_runtime") or "unknown") for f in files]
    reliability = [str(f.get("reliability_status") or "unknown") for f in files]

    ocr_status_values = [
        str(f.get("ocr_status")) for f in files if f.get("ocr_status") is not None
    ]
    layout_status_values = [
        str(f.get("layout_status")) for f in files if f.get("layout_status") is not None
    ]

    ocr_pages_used = sum(len(f.get("ocr_used_pages") or []) for f in files)
    ocr_pages_unavailable = sum(
        len(f.get("ocr_unavailable_pages") or []) for f in files
    )
    ocr_pages_failed = sum(len(f.get("ocr_failed_pages") or []) for f in files)
    ocr_pages_budget_exhausted = sum(
        len(f.get("ocr_budget_exhausted_pages") or []) for f in files
    )
    ocr_pages_empty_output = sum(
        len(f.get("ocr_empty_output_pages") or []) for f in files
    )
    ocr_page_attempts = ocr_pages_used + ocr_pages_unavailable

    page_timings = sorted(
        float(ms)
        for f in files
        for t in (f.get("ocr_page_timings") or [])
        if isinstance(t, dict)
        and isinstance(ms := t.get("duration_ms"), (int, float))
    )

    return {
        "document_count": document_count,
        "runtime_distribution": _distribution(actual_runtimes),
        "recommended_runtime_distribution": _distribution(recommended),
        "ocr": {
            "status_distribution": _distribution(ocr_status_values),
            "pages_used": ocr_pages_used,
            "pages_unavailable": ocr_pages_unavailable,
            "pages_failed": ocr_pages_failed,
            "pages_budget_exhausted": ocr_pages_budget_exhausted,
            "pages_empty_output": ocr_pages_empty_output,
            "page_attempt_count": ocr_page_attempts,
            "page_level_ocr_rate": (
                round(ocr_pages_used * 100.0 / ocr_page_attempts, 1)
                if ocr_page_attempts
                else 0.0
            ),
            "page_timing_p50_ms": _percentile(page_timings, 0.50),
            "page_timing_p95_ms": _percentile(page_timings, 0.95),
        },
        "layout": {
            "status_distribution": _distribution(layout_status_values),
        },
        "narrative_input_reliability_distribution": _distribution(reliability),
        "stage_durations_ms": _stage_duration_summary(
            [f.get("stages") or {} for f in files]
        ),
    }


__all__ = [
    "aggregate_pdf_run_metrics",
    "extract_pdf_run_metrics",
]
