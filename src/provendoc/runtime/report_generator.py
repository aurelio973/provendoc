from pathlib import Path

"""ReportGenerator — Representation Observability Workspace.

Runtime Artifact Semantics：
    - 唯一主入口：runs/{run_id}/reports/index.html
    - 一次运行 = 一个可交互的观测/调参工作台

布局：
    ┌──────────────────────────────────────────────────────┐
    │  上方：分块策略参数面板（实时调整）                    │
    ├────────┬──────────────────┬──────────────────────────┤
    │ 左侧   │  中间            │  右侧                     │
    │ 文件   │  原始文档        │  输出层（默认 raw）       │
    │ 列表   │  （左右切换）    │  按管线顺序：raw →        │
    │        │                  │  representation → markdown│
    │        │                  │  → evaluation →           │
    │        │                  │  optimization → debug     │
    ├────────┴──────────────────┴──────────────────────────┤
    │  下方：Quality · Optimization · Diagnostics           │
    └──────────────────────────────────────────────────────┘

质量评估定位：观测 + 诊断 + 治理（不是语义裁判）。
人工标注 → 策略学习：收集"满意的参数组"，推导 policy 合理区间。

输出：
    runs/{run_id}/reports/index.html
"""

import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional

try:
    from provendoc.core.semantics.coherence import CoherenceConfig, CoherenceShiftDetector
    _coherence_detector = CoherenceShiftDetector(CoherenceConfig(min_confidence=0.25))
except Exception:
    _coherence_detector = None  # type: ignore


# Step 4 (RSE migration): the single source of truth switches to rse_report's invariant_v1 five-dimension basis
# (structure preservation / semantic continuity / granularity fit / boundary cleanliness / context self-containment),
# same keys as quality_profile.dimensions.
from provendoc.intelligence.rse.rse_report import (
    QUALITY_DIMENSION_LABELS,
    QUALITY_DIMENSION_THRESHOLDS,
)
from provendoc.runtime.warning_tiering import build_tiered_view

QUALITY_DIMENSION_THRESHOLDS = dict(QUALITY_DIMENSION_THRESHOLDS)
QUALITY_DIMENSION_LABELS = dict(QUALITY_DIMENSION_LABELS)

# Cap for embedding raw text into the report (keeps the HTML size sane)
RAW_TEXT_LIMIT = 20000
MARKDOWN_LIMIT = 30000

# Representation inlining cap: bounds the HTML size (fetch is unreliable under file://, content must be inlined)
REPR_UNIT_LIMIT = 200      # 每个粒度最多内嵌的 unit 数
REPR_TEXT_LIMIT = 2000     # 单个 unit 文本截断长度
# Keep only display-needed keys in unit.metadata (the rest may hold large arrays)
_REPR_META_KEYS = {
    "block_type", "unit_count", "oversized", "heading", "heading_path",
    "has_overlap_before", "has_overlap_after",
    # coarse context-pack observability
    "coarse_policy", "coarse_document_chars", "coarse_target_chars",
    "coarse_hard_max_chars", "coarse_actual_chars", "source_unit_count",
    "source_section_count", "source_base_range", "merge_reasons",
}

# ── evaluation/debug payload embedding sampling (8MB report governance) ────────────────────
# The debug artifact's contract is "full intermediate state": stages.parse embeds the full document.text,
# stages.representation embeds all units, and evaluation artifacts carry full evidence lists;
# embedding them verbatim (_read_json_payload) once pushed index.html past 8MB+.
# The frontend (report_template.html) actually consumes only the whitelisted keys below; full data still lives in
# the official artifact JSONs (the "Open ... JSON" links at the panel bottom); sampling facts recorded in
# payload["_sampling"] (dropped_keys / truncated_lists / original_byte_size).
# The whitelist is maintained in lockstep with the template: changing the template's consumption surface requires updating this too.
_EVALUATION_PAYLOAD_KEYS = (
"overall", "confidence", "applicability", "confidence_profile",
"metric_definitions", "rse",
)
_DEBUG_CONFIG_SCALARS_ONLY = True   # config 只内嵌标量（模板同口径）
_DEBUG_WARNING_CAP = 32            # warnings/errors 内嵌条数上限
_LIST_ITEM_CAP = 64                 # 白名单内递归列表条数上限
_LIST_STRING_CAP = 2000             # 白名单内递归字符串长度上限


class ReportGenerator:
    """Generate the Representation Observability Workspace report."""

    def __init__(self, run_dir: Path, summary):
        """Bind the run directory and summary data (generate() builds the report from them)."""
        self.run_dir = Path(run_dir)
        self.summary = summary
        # tiering_v1 tiered-view ingredients: collected in passing during the _files_payload walk
        # (one JSON read pass, no extra I/O); consumed by _build_html.
        self._tiering_records: List[Dict[str, Any]] = []

    def generate(self):
        """Generate reports/index.html."""
        reports_dir = self.run_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        output_path = reports_dir / "index.html"
        output_path.write_text(self._build_html(), encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------
    # Data preparation
    # ------------------------------------------------------------------

    def _success_rate(self) -> float:
        if self.summary.total_files == 0:
            return 0.0
        return (self.summary.success_files / self.summary.total_files) * 100

    def _optimization_summary_payload(self) -> Dict[str, Any]:
        """Prefer the in-memory run summary; fall back to reading the official optimization artifact when rebuilding a historical report."""
        summary = getattr(self.summary, "optimization_summary", {}) or {}
        if summary:
            return summary
        try:
            artifact = self.run_dir / "optimization" / "optimization_summary.json"
            if not artifact.exists():
                return {}
            data = json.loads(artifact.read_text(encoding="utf-8"))
            return data.get("optimization_summary", {}) if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _files_payload(self) -> List[Dict[str, Any]]:
        """Build the per-file data consumed by the frontend."""
        payload = []
        for f in self.summary.files:
            # Some historical summaries retain only artifact paths.  The raw
            # report must still show the parser output, so recover it from the
            # raw artifact when the summary text is absent.
            raw_text = f.raw_text or self._read_raw_text(f.raw_json_path)
            # Source Document panel contract: prefer the in-memory source text (the decoded original,
            # the "PyCharm view"); parallel workers drop overly long source_text, in which case
            # the full original is re-read from the raw artifact; the huge fast path has no artifact,
            # so it falls back to the parse snapshot — but the character count uses the worker's recorded true length.
            recovered_source = self._read_source_text(f.raw_json_path)
            recorded_char_count = getattr(f, "source_char_count", None)
            source_text = (
                getattr(f, "source_text", None)
                or recovered_source
                or raw_text
            )
            # When the full original can't be recovered but its true length is known (parallel huge fast path),
            # drive source_truncated / source_char_count with the true length, so the worker's in-memory
            # truncated text is never mistaken for the complete original.
            if (
                recovered_source
                and recorded_char_count is not None
                and recorded_char_count > len(recovered_source)
            ):
                source_text = recovered_source
            source_char_count = (
                recorded_char_count
                if (
                    recorded_char_count is not None
                    and recorded_char_count >= len(source_text)
                )
                else len(source_text)
            )
            markdown = f.markdown_content or ""
            # tiering_v1 ingredients: raw metadata and the evaluation rse layer (read early, shared
            # by the payload below and the tiered-view collection — no second I/O pass)
            raw_meta = self._read_raw_meta(f.raw_json_path)
            evaluation_payload = self._read_evaluation_payload(f.evaluation_json_path)

            payload.append({
                "file_path": f.file_path,
                "relative_path": f.relative_path,
                "success": f.success,
                "error": f.error,
                "chunk_count": f.chunk_count,
                "quality_overall": f.quality_overall,
                "quality_confidence": f.quality_confidence,
                "quality_dimensions": f.quality_dimensions or {},
                # issue-first: diagnostic issues + tuning advice shown before scores
                "quality_issues": getattr(f, "quality_issues", None) or [],
                # Dedup + count: giant files can produce tens of thousands of same-kind failures
                "failure_modes": self._dedupe_failures(f.failure_modes),
                "failure_total": len(f.failure_modes or []),
                "domain": f.domain,
                "ingestion_mode": f.ingestion_mode,
                "processing_time_ms": f.processing_time_ms,
                # Grouped navigation: bucket by "type/subdirectory" (e.g. txt/huge, markdown/malformed)
                "category": self._category_of(f.relative_path),
                "benchmark_tags": getattr(f, "benchmark_tags", None) or ["normal"],
                "file_size_bytes": getattr(f, "file_size_bytes", 0),
                "corpus_tier": getattr(f, "corpus_tier", "normal_narrative"),
                # Runtime Scale Policy: sampling and density-downgrade markers
                "sampling_meta": getattr(f, "sampling_meta", None),
                "density_adapted": getattr(f, "density_adapted", False),
                # Output-layer content
                "raw_text": self._truncate(raw_text, RAW_TEXT_LIMIT),
                "raw_truncated": len(raw_text) > RAW_TEXT_LIMIT,
                "raw_char_count": len(raw_text),
                # Source Document panel (center source column): the file's source text.
                # The frontend renders invisible characters as visible markers.
                "source_text": self._truncate(source_text, RAW_TEXT_LIMIT),
                "source_truncated": source_char_count > RAW_TEXT_LIMIT,
                "source_char_count": source_char_count,
                "markdown": self._truncate(markdown, MARKDOWN_LIMIT),
                "markdown_truncated": len(markdown) > MARKDOWN_LIMIT,
                # parser metadata (non-text fields of raw_json)
                "raw_meta": raw_meta,
                # representation display payload (embedded — fetch fails under file://)
                "representation": self._read_representation_payload(
                    f.representation_json_path
                ),
                # The internal report shows the official JSON artifacts directly; fetch is impossible under file://.
                # debug is not externally exposed, so the full intermediate state is kept rather than a digest or truncation.
                # (2026-09-14 sampling governance: evaluation/debug payloads became whitelist-embedded +
                #  _sampling facts; the full artifacts still live in the JSON files.)
                "evaluation_payload": evaluation_payload,
                "debug_payload": self._read_debug_payload(f.debug_json_path),
                # Output-layer paths (for "open the original file") — the seven-dimension official artifact contract
                # (chunks were folded into representation.chunks; failure_modes into evaluation)
                "paths": {
                    "raw": f.raw_json_path,
                    "markdown": getattr(f, "markdown_path", None),
                    "representation": f.representation_json_path,
                    "evaluation": f.evaluation_json_path,
                    "optimization": "optimization/optimization_summary.json",
                    "profile": "optimization/profile_summary.json",
                    "run_trace": "optimization/run_trace.json",
                    "debug": f.debug_json_path,
                },
                # diagnostics
                "doc_quality": f.doc_quality_result or {},
                "optimization": f.optimization_result or {},
                # coherence curve — computed from raw_text at report generation time
                "coherence_curve": self._coherence_curve(raw_text),
            })
            # tiering_v1 ingredient collection (warning_tiering_prereg_v1 §3: aggregation only
            # builds derived views on the report side; the tiering layer is unchanged; ingredients come from the same payload read)
            self._tiering_records.append({
                "file": str(f.relative_path),
                "warnings": raw_meta.get("warnings") or [],
                "rse": (evaluation_payload or {}).get("rse"),
            })
        return payload

    @staticmethod
    def _coherence_curve(raw_text: str) -> Optional[Dict[str, Any]]:
        """Compute coherence curve for a file's raw text.

        Returns None if detector is unavailable or text is too short.
        Returns CoherenceAnalysis.to_json_dict() otherwise.
        """
        if not _coherence_detector or not raw_text or len(raw_text) < 200:
            return None
        try:
            analysis = _coherence_detector.detect_with_curve(raw_text)
            if analysis.paragraph_count < 4:
                return None
            return analysis.to_json_dict()
        except Exception:
            return None

    @staticmethod
    def _category_of(relative_path: str) -> str:
        """Derive a group name from the relative path, e.g. 'txt/huge', 'markdown/malformed'.

        Takes the first two directory levels; one level if that's all; no directory → 'root'.
        """
        parts = Path(str(relative_path)).parts
        if len(parts) <= 1:
            return "root"
        return "/".join(parts[:2]) if len(parts) > 2 else parts[0]

    @staticmethod
    def _dedupe_failures(modes: Optional[List[str]]) -> List[Dict[str, Any]]:
        """Fold failure_modes into [{mode, count}], descending by count."""
        if not modes:
            return []
        counter: Dict[str, int] = {}
        for m in modes:
            counter[m] = counter.get(m, 0) + 1
        return [
            {"mode": m, "count": c}
            for m, c in sorted(counter.items(), key=lambda kv: -kv[1])
        ]

    def _read_raw_text(self, raw_json_path: Optional[str]) -> str:
        """Recover the parsed text from the raw artifact; compatible with slim summaries."""
        if not raw_json_path:
            return ""
        try:
            full_path = self.run_dir / raw_json_path
            if not full_path.exists():
                return ""
            data = json.loads(full_path.read_text(encoding="utf-8"))
            return str(data.get("text") or "") if isinstance(data, dict) else ""
        except Exception:
            return ""

    def _read_source_text(self, raw_json_path: Optional[str]) -> str:
        """Recover the file's source text (decoded original) from the raw artifact.

        Only newer run artifacts carry the ``source_text`` field; older artifacts and binary
        formats (pdf/docx/excel) return an empty string, and the caller falls back to the parse snapshot.
        """
        if not raw_json_path:
            return ""
        try:
            full_path = self.run_dir / raw_json_path
            if not full_path.exists():
                return ""
            data = json.loads(full_path.read_text(encoding="utf-8"))
            value = data.get("source_text") if isinstance(data, dict) else None
            return str(value) if value else ""
        except Exception:
            return ""

    def _read_raw_meta(self, raw_json_path: Optional[str]) -> Dict[str, Any]:
        """Read parser metadata from the raw_json file (text fields excluded to avoid duplication).

        raw-layer contract:
            - text is original_text (the pre-cleaning snapshot);
            - encoding is a conclusion-level object {encoding_used, encoding_fallback};
            - document_quality is the DQR (source-document quality assessment).
        """
        if not raw_json_path:
            return {}
        try:
            full_path = self.run_dir / raw_json_path
            if not full_path.exists():
                return {}
            data = json.loads(full_path.read_text(encoding="utf-8"))
            # Metadata fields only; the text field is already in raw_text
            return {
                "file_name": data.get("file_name"),
                "file_type": data.get("file_type"),
                "mime_type": data.get("mime_type"),
                "parser": data.get("parser"),
                "encoding": data.get("encoding"),
                "sha256": data.get("sha256"),
                "text_statistics": data.get("text_statistics", {}),
                "diagnostics": data.get("diagnostics", {}),
                "semantic_signals": data.get("semantic_signals", {}),
                "warnings": data.get("warnings", []),
                "headings": data.get("headings", []),
                "document_quality": data.get("document_quality"),
            }
        except Exception:
            return {}

    def _read_representation_payload(self, path: Optional[str]) -> Optional[Dict[str, Any]]:
        """Read the representation JSON and build a size-capped embedded display payload.

        When the report is opened under the file:// protocol, browsers forbid fetching sibling
        JSON files, so the three-granularity data the representation panel needs must be
        injected into RUN_DATA at generation time. Each unit keeps only display fields; each
        granularity embeds at most REPR_UNIT_LIMIT units, while unit_count keeps the true value so the frontend can show a truncation marker.
        """
        if not path:
            return None
        full_path = self.run_dir / path
        if not full_path.exists():
            return None
        try:
            data = json.loads(full_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        chunks = data.get("chunks") or {}
        cleaned_text = data.get("cleaned_text") or ""
        cleaning = data.get("cleaning") or {}
        cleaning_summary = cleaning.get("summary") or {}
        payload: Dict[str, Any] = {
            "selected_granularity": chunks.get("selected_granularity", "base"),
            "cleaned_text": self._truncate(cleaned_text, RAW_TEXT_LIMIT),
            "cleaned_char_count": len(cleaned_text),
            "cleaned_truncated": len(cleaned_text) > RAW_TEXT_LIMIT,
            "cleaning": {
                "applied": bool(cleaning.get("applied")),
                "cleaning_stats": cleaning_summary.get("cleaning_stats") or {},
                "cleaning_trace": (cleaning_summary.get("cleaning_trace") or [])[:12],
            },
            "granularities": {},
        }
        for g in ("coarse", "base", "fine"):
            gv = chunks.get(g) or {}
            units = gv.get("units") or []
            shown = []
            for u in units[:REPR_UNIT_LIMIT]:
                meta = u.get("metadata") or {}
                shown.append({
                    "text": (u.get("text") or "")[:REPR_TEXT_LIMIT],
                    "unit_type": u.get("unit_type"),
                    "granularity": u.get("granularity", g),
                    "heading_path": u.get("heading_path"),
                    "heading": u.get("heading"),
                    "context_from_previous": u.get("context_from_previous"),
                    "metadata": {k: v for k, v in meta.items() if k in _REPR_META_KEYS} or None,
                })
            payload["granularities"][g] = {
                "status": gv.get("status", "available"),
                "unit_count": gv.get("unit_count", len(units)),
                "character_count": gv.get("character_count", 0),
                "shown_count": len(shown),
                "truncated": len(units) > REPR_UNIT_LIMIT,
                "reason": gv.get("reason"),
                "units": shown,
            }
        return payload

    def _read_json_payload(self, artifact_path: Optional[str]) -> Optional[Dict[str, Any]]:
        """Read a run's official JSON artifact so the frontend can build structured digests and original-file links."""
        data, _size = self._load_json_with_size(artifact_path)
        return data

    def _load_json_with_size(self, artifact_path: Optional[str]):
        """Read a run's official JSON artifact, returning (data, original byte count).

        Missing/corrupt returns (None, 0); non-dict data is wrapped as {"value": data}
        (same contract as the legacy _read_json_payload).
        """
        if not artifact_path:
            return None, 0
        try:
            full_path = self.run_dir / artifact_path
            if not full_path.exists():
                return None, 0
            raw = full_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                data = {"value": data}
            return data, len(raw.encode("utf-8"))
        except Exception:
            return None, 0

    def _read_evaluation_payload(self, artifact_path: Optional[str]) -> Optional[Dict[str, Any]]:
        """Whitelist-embedded sampling view of the evaluation artifact (size governance + honest sampling facts)."""
        data, size = self._load_json_with_size(artifact_path)
        if data is None:
            return None
        return self._sample_payload(data, _EVALUATION_PAYLOAD_KEYS, size)

    def _read_debug_payload(self, artifact_path: Optional[str]) -> Optional[Dict[str, Any]]:
        """Whitelist-embedded sampling view of the debug artifact.

        The largest parts of the debug artifact are stages (parse embeds the full text,
        representation embeds all units, evaluation carries the full result) — the frontend
        debug panel doesn't consume them, so they are sampled away; warnings/errors keep
        their head entries with truncation facts recorded. The full data is always reachable via "Open per-file debug JSON".
        """
        data, size = self._load_json_with_size(artifact_path)
        if data is None:
            return None

        config = data.get("config") or {}
        scalar_config = {
            k: v for k, v in config.items()
            if isinstance(v, (str, int, float, bool))
        } if _DEBUG_CONFIG_SCALARS_ONLY else dict(config)
        dropped_config = [k for k in config if k not in scalar_config]

        raw_warnings = data.get("warnings") or []
        raw_errors = data.get("errors") or []
        sampled: Dict[str, Any] = {
            "layer": data.get("layer"),
            "file": data.get("file") or {},
            "config": scalar_config,
            "pipeline_trace": data.get("pipeline_trace"),
            "warnings": raw_warnings[:_DEBUG_WARNING_CAP],
            "errors": raw_errors[:_DEBUG_WARNING_CAP],
        }
        trimmed: Dict[str, Any] = {}
        for key, raw in (
            ("warnings", raw_warnings), ("errors", raw_errors),
        ):
            if len(raw) > _DEBUG_WARNING_CAP:
                trimmed[key] = {"original_length": len(raw)}
        for key in sampled:
            sampled[key] = self._cap_values(sampled[key], trimmed, key)

        dropped_keys = [k for k in data if k not in sampled]
        if dropped_config:
            dropped_keys.append("config.<non-scalar keys>")
        sampled["_sampling"] = {
            "dropped_keys": dropped_keys,
            "truncated_lists": trimmed,
            "original_byte_size": size,
        }
        return sampled

    def _sample_payload(self, data: Dict[str, Any], keys, size: int) -> Dict[str, Any]:
        """Whitelist projection + recursive list/string caps + honest sampling facts."""
        sampled = {k: data[k] for k in keys if k in data}
        trimmed: Dict[str, Any] = {}
        for key in sampled:
            sampled[key] = self._cap_values(sampled[key], trimmed, key)
        dropped_keys = [k for k in data if k not in sampled]
        if dropped_keys or trimmed:
            sampled["_sampling"] = {
                "dropped_keys": dropped_keys,
                "truncated_lists": trimmed,
                "original_byte_size": size,
            }
        return sampled

    @classmethod
    def _cap_values(cls, value, trimmed: Dict[str, Any], path: str = ""):
        """Recursively truncate over-long lists (entry count) and strings; the path records the original length/char count."""
        if isinstance(value, list):
            if len(value) > _LIST_ITEM_CAP:
                trimmed[path] = {"original_length": len(value)}
                value = value[:_LIST_ITEM_CAP]
            return [
                cls._cap_values(item, trimmed, f"{path}[{i}]")
                for i, item in enumerate(value)
            ]
        if isinstance(value, dict):
            return {
                k: cls._cap_values(v, trimmed, f"{path}.{k}" if path else k)
                for k, v in value.items()
            }
        if isinstance(value, str) and len(value) > _LIST_STRING_CAP:
            trimmed[path] = {"original_chars": len(value)}
            return value[:_LIST_STRING_CAP]
        return value

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Return a byte-for-byte faithful preview slice.

        The caller owns the truncation metadata and UI notice.  Appending a
        textual marker here would make a source preview look like the file
        itself, violating the source-text contract for editor-view rendering.
        """
        return text[:limit]

    def _config_payload(self) -> Dict[str, Any]:
        cfg = self.summary.config
        return asdict(cfg) if hasattr(cfg, "__dataclass_fields__") else dict(cfg)

    def _avg_dimensions(self) -> Dict[str, float]:
        """Global mean quality dimensions (radar-chart baseline)."""
        buckets: Dict[str, List[float]] = {}
        for f in self.summary.files:
            for dim, val in (f.quality_dimensions or {}).items():
                buckets.setdefault(dim, []).append(val)
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}

    def _failure_distribution(self) -> Dict[str, Dict[str, int]]:
        """Failure-mode distribution.

        Counts both "affected files" and "total occurrences":
        giant stress-test files can produce tens of thousands of same-kind failures, and
        occurrence counts alone would drown the real signal, so affected-file count is the primary basis.
        """
        dist: Dict[str, Dict[str, int]] = {}
        for f in self.summary.files:
            seen = set()
            for mode in (f.failure_modes or []):
                entry = dist.setdefault(mode, {"files": 0, "occurrences": 0})
                entry["occurrences"] += 1
                if mode not in seen:
                    entry["files"] += 1
                    seen.add(mode)
        return dict(
            sorted(dist.items(), key=lambda kv: -kv[1]["files"])
        )

    def _previous_run_snapshot(self) -> Dict[str, Any]:
        """Read the previous run's summary.json and extract the tier snapshot and filtered quality.

        The basis for cross-run comparison: brings the previous run's corpus composition and quality
        baseline into the current report so the frontend can render drift deltas without loading extra files.
        """
        runs_root = self.run_dir.parent
        if not runs_root.exists():
            return {}
        candidates = sorted(
            (d for d in runs_root.iterdir()
             if d.is_dir() and d.resolve() != self.run_dir.resolve()),
            key=lambda d: d.name,
        )
        for prev_dir in reversed(candidates):
            summary_path = prev_dir / "summary.json"
            if not summary_path.exists():
                continue
            try:
                prev = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            return {
                "run_id": prev.get("run_id"),
                "timestamp": prev.get("timestamp"),
                "total_files": prev.get("total_files"),
                "success_files": prev.get("success_files"),
                "corpus_tier_snapshot": prev.get("corpus_tier_snapshot", {}) or {},
                "quality_filtered": prev.get("quality_filtered", {}) or {},
                "avg_quality_overall": prev.get("avg_quality_overall"),
            }
        return {}

    # ------------------------------------------------------------------
    # HTML construction (template-injection mode)
    # ------------------------------------------------------------------

    def _build_html(self) -> str:
        """Read the template file + inject the RUN_DATA JSON.

        HTML/CSS/JS all live in runtime/report_template.html (a native file, free of
        Python string-escaping problems); this layer only injects data.
        """
        files_payload = self._files_payload()
        # tiering_v1 two-source tiered aggregate view (frozen prereg basis; ingredients
        # collected in passing by _files_payload; an empty view when there are no records)
        warning_tiering = build_tiered_view(self._tiering_records)
        data_blob = json.dumps({
            "run_id": self.summary.run_id,
            "timestamp": self.summary.timestamp,
            "config": self._config_payload(),
            "stats": {
                "total_files": self.summary.total_files,
                "success_files": self.summary.success_files,
                "failed_files": self.summary.failed_files,
                "success_rate": self._success_rate(),
                "avg_quality_overall": self.summary.avg_quality_overall,
                "avg_quality_confidence": self.summary.avg_quality_confidence,
                "corpus_distribution": getattr(self.summary, "corpus_distribution", {}) or {},
                "quality_filtered": getattr(self.summary, "quality_filtered", {}) or {},
                "quality_filtered_dimensions": getattr(self.summary, "quality_filtered_dimensions", {}) or {},
                "corpus_tier_snapshot": getattr(self.summary, "corpus_tier_snapshot", {}) or {},
                "optimization_summary": self._optimization_summary_payload(),
                "previous_run": self._previous_run_snapshot(),
                # Archive expansion: collection-time archive facts (same source as the run-level
                # consolidated report; the frontend can render an archive view from them)
                "archive_expansion": getattr(self.summary, "archive_expansion", {}) or {},
            },
            "avg_dimensions": self._avg_dimensions(),
            "failure_distribution": self._failure_distribution(),
            "dimension_labels": QUALITY_DIMENSION_LABELS,
            "dimension_thresholds": QUALITY_DIMENSION_THRESHOLDS,
            "warning_tiering": warning_tiering,
            "files": files_payload,
        }, ensure_ascii=False)

        # Prevent </script> literals inside the JSON from closing the <script> tag early
        data_blob = data_blob.replace("</", "<\\/")

        template_path = Path(__file__).resolve().parent / "report_template.html"
        template = template_path.read_text(encoding="utf-8")
        if "__RUN_DATA__" not in template:
            raise ValueError(f"template missing __RUN_DATA__ placeholder: {template_path}")

        return template.replace("__RUN_DATA__", data_blob)
