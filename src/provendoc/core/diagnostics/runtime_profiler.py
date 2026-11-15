"""RuntimeProfiler — lightweight per-stage timing and complexity profiler.

Design principles:
    - Zero intrusion: when enabled=False, context manager yields immediately,
      no time.perf_counter() calls, zero overhead on production paths.
    - Per-stage timing: wrap any pipeline phase with `with profiler.stage("evaluation"):`.
    - Complexity metrics: record structural counts (unit_count, coherence_windows, etc.)
      alongside timing to reveal *where* complexity explodes, not just *how long* it takes.
    - Aggregation: ProfileAggregator collects per-file profiles and produces
      min/avg/p90/max/total statistics across a whole run.

Usage (standalone):
    profiler = RuntimeProfiler(enabled=True)

    with profiler.stage("parse"):
        document = parser.parse(file_path)

    with profiler.stage("evaluation"):
        evaluation = evaluator.evaluate(...)

    profiler.record("base_units", 42)
    profiler.record("fine_units", 183)

    print(profiler.format_text())
    data = profiler.to_dict()

Usage (ProvendocRun integrated):
    # ProvendocRun creates one profiler per file, feeds them to ProfileAggregator,
    # then prints the aggregated report at the end of the run.
    aggregator = ProfileAggregator()
    for f in files:
        p = RuntimeProfiler()
        # ... pipeline with p.stage(...) ...
        aggregator.add(str(f), p.to_dict())
    print(aggregator.format_text())
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

# ── helpers ──────────────────────────────────────────────

def _fmt_ms(ms: float) -> str:
    """Format milliseconds human-readably."""
    if ms < 1:
        return f"{ms:.2f} ms"
    if ms < 10:
        return f"{ms:.1f} ms"
    return f"{ms:.0f} ms"


def _fmt_int(n: int | float) -> str:
    """Format integer metric."""
    if isinstance(n, float) and n == int(n):
        n = int(n)
    return str(n)


def _dot_fill(label: str, value: str, width: int = 38) -> str:
    """Format a line: label........... value"""
    dots = max(3, width - len(label) - len(value))
    return f"{label}{'.' * dots}{value}"


# ── RuntimeProfiler ──────────────────────────────────────

class RuntimeProfiler:
    """Lightweight per-stage timing and complexity profiler.

    Attributes:
        enabled: When False, stage() is a no-op and record() is a no-op.
        stages:  Dict of stage_name -> elapsed_ms (accumulated).
        metrics: Dict of metric_name -> value.
    """

    def __init__(self, enabled: bool = True):
        """Initialize the profiler.

        Args:
            enabled: when False, stage()/record() become zero-overhead
                no-ops so production paths can turn profiling off.
        """
        self.enabled = enabled
        self._stages: Dict[str, float] = {}
        self._metrics: Dict[str, Any] = {}
        self._label: Optional[str] = None

    def reset(self):
        """Clear all recorded data for reuse on the next file."""
        self._stages.clear()
        self._metrics.clear()
        self._label = None

    def set_label(self, label: str):
        """Set a human-readable label (e.g. filename) for report output."""
        self._label = label

    # ── timing ────────────────────────────────────────────

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Context manager: time a pipeline stage.

        Usage:
            with profiler.stage("parse"):
                doc = parser.parse(path)

        When disabled, this is a zero-overhead no-op.
        """
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            self._stages[name] = self._stages.get(name, 0.0) + elapsed

    def add_stage_time(self, name: str, ms: float):
        """Manually add time to a stage (e.g. when timing is done externally)."""
        if self.enabled:
            self._stages[name] = self._stages.get(name, 0.0) + ms

    # ── complexity metrics ────────────────────────────────

    def record(self, metric: str, value: int | float):
        """Record a complexity metric (chunk count, matrix size, etc.)."""
        if self.enabled:
            self._metrics[metric] = value

    # ── output ────────────────────────────────────────────

    @property
    def total_ms(self) -> float:
        """Sum of all stage timings."""
        return sum(self._stages.values())

    def to_dict(self) -> Dict[str, Any]:
        """Return structured profile data for JSON serialization."""
        return {
            "label": self._label,
            "stages": dict(self._stages),
            "metrics": dict(self._metrics),
            "total_ms": round(self.total_ms, 2),
        }

    def format_text(self, width: int = 38) -> str:
        """Format as readable text report.

        Output:
        ==================================================
        Runtime Profile — myfile.txt
        ==================================================
        parse.................. 132 ms
        representation......... 418 ms
        evaluation............. 2410 ms
        save................... 47 ms
        TOTAL.................. 2997 ms

        --------------------------------------------------
        Complexity Metrics
        --------------------------------------------------
        file_size_bytes........ 12500
        char_count............. 12034
        base_units............. 42
        fine_units............. 183
        """
        lines: List[str] = []
        sep = "=" * 50
        lines.append(sep)
        label = self._label or "profile"
        lines.append(f"Runtime Profile — {label}")
        lines.append(sep)

        # stages
        for name, ms in self._stages.items():
            lines.append(_dot_fill(name, _fmt_ms(ms), width))
        total_str = _fmt_ms(self.total_ms)
        lines.append(_dot_fill("TOTAL", total_str, width))

        # metrics
        if self._metrics:
            lines.append("")
            lines.append("-" * 50)
            lines.append("Complexity Metrics")
            lines.append("-" * 50)
            for name, val in self._metrics.items():
                lines.append(_dot_fill(name, _fmt_int(val), width))

        return "\n".join(lines)


# ── ProfileAggregator ────────────────────────────────────

@dataclass
class _StageStats:
    """Per-stage aggregated statistics."""
    min: float = float("inf")
    max: float = 0.0
    total: float = 0.0
    count: int = 0
    values: List[float] = field(default_factory=list)

    def add(self, ms: float):
        """Record one stage-latency sample (ms), updating min/max/total."""
        self.values.append(ms)
        self.min = min(self.min, ms)
        self.max = max(self.max, ms)
        self.total += ms
        self.count += 1

    @property
    def avg(self) -> float:
        """Mean latency (ms); 0.0 with no samples."""
        return self.total / self.count if self.count else 0.0

    @property
    def p90(self) -> float:
        """P90 latency (ms, nearest-rank method); 0.0 with no samples."""
        if not self.values:
            return 0.0
        sorted_vals = sorted(self.values)
        idx = int(len(sorted_vals) * 0.9)
        if idx >= len(sorted_vals):
            idx = len(sorted_vals) - 1
        return sorted_vals[idx]


@dataclass
class _MetricStats:
    """Per-metric aggregated statistics (min/max/avg)."""
    values: List[float] = field(default_factory=list)

    def add(self, val: float):
        """Record one metric sample."""
        self.values.append(val)

    @property
    def total(self) -> float:
        """Sum of samples; 0.0 with no samples."""
        return sum(self.values)

    @property
    def avg(self) -> float:
        """Mean of samples; 0.0 with no samples."""
        return sum(self.values) / len(self.values) if self.values else 0.0


class ProfileAggregator:
    """Aggregate per-file profiles into a run-level summary.

    Usage:
        agg = ProfileAggregator()
        for f in files:
            p = RuntimeProfiler()
            # ... pipeline ...
            agg.add(str(f), p.to_dict())
        print(agg.format_text())
        data = agg.summary()  # for JSON
    """

    def __init__(self):
        """Initialize an empty aggregator (add per file, then emit a run-level summary)."""
        self._file_count: int = 0
        self._stage_stats: Dict[str, _StageStats] = {}
        self._metric_stats: Dict[str, _MetricStats] = {}
        self._file_totals: List[tuple[str, float, str]] = []  # (label, total_ms, top_stage)

    def add(self, label: str, profile: Dict[str, Any]):
        """Add a single file's profile data."""
        self._file_count += 1
        stages = profile.get("stages", {})
        for name, ms in stages.items():
            if name not in self._stage_stats:
                self._stage_stats[name] = _StageStats()
            self._stage_stats[name].add(ms)

        metrics = profile.get("metrics", {})
        for name, val in metrics.items():
            if name not in self._metric_stats:
                self._metric_stats[name] = _MetricStats()
            self._metric_stats[name].add(val)

        total = profile.get("total_ms", 0.0)
        top_stage = ""
        top_time = 0.0
        for name, ms in stages.items():
            if ms > top_time:
                top_time = ms
                top_stage = name
        self._file_totals.append((label, total, top_stage))

    def summary(self) -> Dict[str, Any]:
        """Return structured aggregation for JSON serialization."""
        stages = {}
        for name, s in self._stage_stats.items():
            stages[name] = {
                "min": round(s.min, 2),
                "avg": round(s.avg, 2),
                "p90": round(s.p90, 2),
                "max": round(s.max, 2),
                "total": round(s.total, 2),
                "count": s.count,
            }

        metrics = {}
        for name, m in self._metric_stats.items():
            metrics[name] = {
                "total": round(m.total, 2),
                "avg": round(m.avg, 2),
                "count": len(m.values),
            }

        sorted_files = sorted(self._file_totals, key=lambda t: t[1], reverse=True)
        top_slow = [
            {"label": label, "total_ms": round(total, 2), "top_stage": stage}
            for label, total, stage in sorted_files[:10]
        ]

        grand_total = sum(s.total for s in self._stage_stats.values())

        return {
            "file_count": self._file_count,
            "stages": stages,
            "metrics": metrics,
            "grand_total_ms": round(grand_total, 2),
            "top_slow_files": top_slow,
        }

    def format_text(self) -> str:
        """Format aggregated report.

        Output:
        ==================================================
        Runtime Profile — Aggregated (15 files)
        ==================================================
        Stage            min     avg     p90      max      total
        parse             12      89     210      340       1240
        representation   200     450     920     1800       6300
        evaluation        800    2410    4200     5400      33740
        save              15      35      60      120        525
        TOTAL                                           41805

        --------------------------------------------------
        Top 5 slowest files
        --------------------------------------------------
          1. huge_10mb.txt       8234 ms  (evaluation)
          2. mixed_everything.md 5400 ms  (evaluation)
          3. log_big.log         4200 ms  (evaluation)

        --------------------------------------------------
        Complexity Metrics (avg)
        --------------------------------------------------
        base_units............. 42.0
        fine_units............. 183.0
        """
        lines: List[str] = []
        sep = "=" * 60
        lines.append(sep)
        lines.append(f"Runtime Profile — Aggregated ({self._file_count} files)")
        lines.append(sep)

        # stage table
        header = f"{'Stage':<18} {'min':>8} {'avg':>8} {'p90':>8} {'max':>8} {'total':>10}"
        lines.append(header)
        lines.append("-" * 60)

        # sort stages by total descending
        sorted_stages = sorted(
            self._stage_stats.items(),
            key=lambda kv: kv[1].total,
            reverse=True,
        )
        grand_total = 0.0
        for name, s in sorted_stages:
            lines.append(
                f"{name:<18} {_fmt_ms(s.min):>8} {_fmt_ms(s.avg):>8} "
                f"{_fmt_ms(s.p90):>8} {_fmt_ms(s.max):>8} {_fmt_ms(s.total):>10}"
            )
            grand_total += s.total
        lines.append("-" * 60)
        lines.append(f"{'TOTAL':<18} {'':>8} {'':>8} {'':>8} {'':>8} {_fmt_ms(grand_total):>10}")

        # top slow files
        if self._file_totals:
            lines.append("")
            lines.append("-" * 60)
            top_n = min(5, len(self._file_totals))
            lines.append(f"Top {top_n} slowest files")
            lines.append("-" * 60)
            sorted_files = sorted(self._file_totals, key=lambda t: t[1], reverse=True)
            for i, (label, total, top_stage) in enumerate(sorted_files[:top_n], 1):
                lines.append(f"  {i}. {label:<40} {_fmt_ms(total):>10}  ({top_stage})")

        # metrics
        if self._metric_stats:
            lines.append("")
            lines.append("-" * 60)
            lines.append("Complexity Metrics (avg)")
            lines.append("-" * 60)
            for name, m in self._metric_stats.items():
                val = m.avg
                if val == int(val):
                    val = int(val)
                else:
                    val = round(val, 1)
                lines.append(_dot_fill(name, _fmt_int(val), 42))

        return "\n".join(lines)
