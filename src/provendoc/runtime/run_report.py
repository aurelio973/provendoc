"""Run-level consolidated report — runs/{run_id}/reports/run_report.md.

Positioning:
    - index.html is the interactive observatory; run_report.md is the
      **run-level consolidated report** — a full plain-text fact
      snapshot of one run, from collection (archive expansion
      included) through parsing, evaluation, and optimization
      advice: diffable, archivable, and paste-ready for PRs and
      weekly reports.
    - All data comes from the in-memory RunSummary (produced when
      execute() finishes); single-file artifacts are never re-read,
      guaranteeing the same accounting as summary.json.

Content skeleton:
    1. Run metadata (id / time / mode / dataset / elapsed)
    2. Overview stats (success / failure / quality)
    3. Domain distribution + corpus stratification
    4. Archive expansion facts (per-archive member/skip counts +
       per-archive member-parse aggregation)
    5. Failed-file list + failure-mode distribution
    6. Quality aggregation + optimization advice (advisory)
    7. Artifact index
"""
from pathlib import Path
from typing import Any, Dict, List

# Defensive truncation: the report is a snapshot, not a dataset —
# over-long lists fold into counts
_FAILED_FILES_LIMIT = 20
_ARCHIVE_FAILED_LIMIT = 10
_FAILURE_MODES_LIMIT = 15

_DOMAIN_ORDER = [
    "narrative", "structured_config", "spreadsheet", "code",
    "event", "table", "binary", "unknown",
]


def _esc(text: Any) -> str:
    """Escape a markdown table cell (pipes and newlines)."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{part / whole * 100:.1f}%"


def generate_run_report(run_dir: Path, summary, elapsed_total: float) -> Path:
    """Generate the run-level consolidated report; returns the reports/run_report.md path.

    Args:
        run_dir: this run's directory (runs/{run_id}).
        summary: RunSummary (in-memory object, carries files and archive_expansion).
        elapsed_total: total wall time of execute() (seconds).
    """
    lines: List[str] = []
    cfg = summary.config

    # ── 1. Run metadata ────────────────────────────────────
    lines.append("# Provendoc Run 报告")
    lines.append("")
    lines.append(f"- **Run ID**: `{summary.run_id}`")
    lines.append(f"- **时间**: {summary.timestamp}")
    lines.append(f"- **模式**: {cfg.runtime_mode.upper()}")
    lines.append(f"- **策略**: {cfg.policy_name}")
    lines.append(f"- **数据集**: `{cfg.dataset_dir}`")
    if getattr(cfg, "description", ""):
        lines.append(f"- **描述**: {cfg.description}")
    lines.append(f"- **耗时**: {elapsed_total:.1f}s")
    lines.append("")

    # ── 2. Overview stats ──────────────────────────────────
    s = summary
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 文件总数 | {s.total_files} |")
    lines.append(f"| 成功 | {s.success_files} ({_pct(s.success_files, s.total_files)}) |")
    lines.append(f"| 失败 | {s.failed_files} ({_pct(s.failed_files, s.total_files)}) |")
    if s.avg_quality_overall:
        lines.append(f"| 平均质量分 | {s.avg_quality_overall:.3f} |")
    if s.avg_quality_confidence:
        lines.append(f"| 平均置信度 | {s.avg_quality_confidence:.3f} |")
    filt = s.quality_filtered or {}
    filt_overall = filt.get("overall")
    if filt_overall is not None:
        lines.append(
            f"| Filtered 质量分（仅 meaningful） | {filt_overall:.3f}"
            f"（{filt.get('meaningful_count', 0)} meaningful） |"
        )
    lines.append("")

    # ── 3. Domain distribution + corpus stratification ─────
    domain_stats: Dict[str, Dict[str, int]] = {}
    for f in s.files:
        entry = domain_stats.setdefault(
            f.domain, {"total": 0, "success": 0, "failed": 0},
        )
        entry["total"] += 1
        entry["success" if f.success else "failed"] += 1
    if domain_stats:
        lines.append("### 域分布")
        lines.append("")
        lines.append("| 域 | 文件数 | 成功 | 失败 | 成功率 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for domain in sorted(
            domain_stats,
            key=lambda d: (_DOMAIN_ORDER.index(d) if d in _DOMAIN_ORDER else 99, d),
        ):
            st = domain_stats[domain]
            lines.append(
                f"| {_esc(domain)} | {st['total']} | {st['success']} | "
                f"{st['failed']} | {_pct(st['success'], st['total'])} |"
            )
        lines.append("")

    tier_snapshot = s.corpus_tier_snapshot or {}
    tiers = tier_snapshot.get("tiers") or {}
    if tiers:
        lines.append("### 语料分层")
        lines.append("")
        lines.append("| tier | 数量 | 占比 |")
        lines.append("| --- | --- | --- |")
        for tier, info in sorted(
            tiers.items(), key=lambda kv: -kv[1].get("count", 0),
        ):
            lines.append(
                f"| {_esc(tier)} | {info.get('count', 0)} | {info.get('pct', 0):.1f}% |"
            )
        lines.append("")

    # ── 4. Archive expansion (collection-stage facts) ──────
    ae = s.archive_expansion or {}
    archives = ae.get("archives") or []
    lines.append("## Archive 展开（采集期）")
    lines.append("")
    if not archives:
        lines.append("本次运行没有压缩包输入。")
        lines.append("")
    else:
        lines.append(
            f"共 {len(archives)} 个压缩包 → 提取 "
            f"{ae.get('extracted_members', 0)} 个成员文件流入各域解析"
            f"（深度停止 {ae.get('depth_stops', 0)} 次）。"
        )
        lines.append("")
        lines.append("| 压缩包 | 格式 | 成员 | 提取 | 跳过(不支持/超限/不安全/重复) | 备注 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in archives:
            skipped = a.get("skipped") or {}
            skip_cell = "/".join(
                str(skipped.get(k, 0))
                for k in ("unsupported", "oversize", "unsafe", "duplicate")
            )
            notes = "; ".join(a.get("notes") or [])
            lines.append(
                f"| `{_esc(a.get('archive', '?'))}` | {_esc(a.get('format'))} "
                f"| {a.get('members', 0)} | {a.get('extracted', 0)} "
                f"| {skip_cell} | {_esc(notes) or '—'} |"
            )
        lines.append("")

        # Per-archive member-parse aggregation (merged output: members
        # parse as usual, results grouped for presentation)
        member_prefix = {
            a.get("archive", ""): a for a in archives
        }
        per_archive: Dict[str, Dict[str, Any]] = {
            name: {"ok": 0, "failed": 0, "domains": {}, "errors": []}
            for name in member_prefix
        }
        for f in s.files:
            rel = str(f.relative_path).replace("\\", "/")
            for name in member_prefix:
                if name and rel.startswith(name + "/"):
                    bucket = per_archive[name]
                    if f.success:
                        bucket["ok"] += 1
                    else:
                        bucket["failed"] += 1
                        if len(bucket["errors"]) < _ARCHIVE_FAILED_LIMIT:
                            bucket["errors"].append((rel, f.error))
                    bucket["domains"][f.domain] = (
                        bucket["domains"].get(f.domain, 0) + 1
                    )
                    break
        lines.append("### 成员解析结果（合并输出）")
        lines.append("")
        for name, bucket in per_archive.items():
            parsed = bucket["ok"] + bucket["failed"]
            domain_cell = ", ".join(
                f"{d} {c}" for d, c in sorted(bucket["domains"].items())
            ) or "—"
            lines.append(
                f"- **`{_esc(name)}`**：{parsed} 个成员已解析 — "
                f"✅ {bucket['ok']} / ❌ {bucket['failed']}（{domain_cell}）"
            )
            for rel, err in bucket["errors"]:
                lines.append(f"  - `{_esc(rel)}`: {_esc(err)}")
        lines.append("")

    # ── 5. Failed files + failure modes ────────────────────
    failed_files = [f for f in s.files if not f.success]
    lines.append("## 失败与告警")
    lines.append("")
    if failed_files:
        shown = failed_files[:_FAILED_FILES_LIMIT]
        lines.append(f"失败文件 {len(failed_files)} 个（显示前 {len(shown)}）：")
        lines.append("")
        lines.append("| 文件 | 错误 |")
        lines.append("| --- | --- |")
        for f in shown:
            lines.append(f"| `{_esc(f.relative_path)}` | {_esc(f.error)} |")
        if len(failed_files) > len(shown):
            lines.append("")
            lines.append(f"… 其余 {len(failed_files) - len(shown)} 个略。")
        lines.append("")
    else:
        lines.append("全部文件解析成功，无失败项。")
        lines.append("")

    mode_dist: Dict[str, Dict[str, int]] = {}
    for f in s.files:
        seen = set()
        for mode in (f.failure_modes or []):
            entry = mode_dist.setdefault(mode, {"files": 0, "count": 0})
            entry["count"] += 1
            if mode not in seen:
                entry["files"] += 1
                seen.add(mode)
    if mode_dist:
        lines.append("### Failure Modes 分布")
        lines.append("")
        lines.append("| 模式 | 受影响文件 | 总次数 |")
        lines.append("| --- | --- | --- |")
        ranked = sorted(mode_dist.items(), key=lambda kv: -kv[1]["files"])
        for mode, entry in ranked[:_FAILURE_MODES_LIMIT]:
            lines.append(f"| {_esc(mode)} | {entry['files']} | {entry['count']} |")
        if len(ranked) > _FAILURE_MODES_LIMIT:
            lines.append("")
            lines.append(
                f"… 其余 {len(ranked) - _FAILURE_MODES_LIMIT} 种模式略。"
            )
        lines.append("")

    # ── 6. Quality aggregation + optimization advice ───────
    dims = s.quality_filtered_dimensions or {}
    if dims:
        lines.append("## 质量维度聚合（filtered 口径）")
        lines.append("")
        lines.append("| 维度 | 均值 |")
        lines.append("| --- | --- |")
        for dim, val in sorted(dims.items()):
            lines.append(f"| {_esc(dim)} | {val:.3f} |")
        lines.append("")

    opt = s.optimization_summary or {}
    rec = opt.get("global_recommendation") or {}
    if rec:
        lines.append("## 优化建议（advisory，run 间采纳）")
        lines.append("")
        lines.append(
            f"- boundary_mode: **{rec.get('boundary_mode') or 'n/a'}**"
            f"（票数 {rec.get('boundary_votes') or {}}）"
        )
        lines.append(f"- scale_median: **{rec.get('scale_median') or 'n/a'}**")
        per_cat = opt.get("per_category") or {}
        if per_cat:
            lines.append(
                f"- 覆盖 {len(per_cat)} 个类目"
                f"（抽样 {opt.get('sampled_files', 0)} 个文件）"
            )
        if opt.get("errors"):
            lines.append(f"- ⚠️ {len(opt['errors'])} 个文件搜索失败")
        lines.append("")

    # ── 7. Artifact index ──────────────────────────────────
    lines.append("## 产物索引")
    lines.append("")
    lines.append(f"run 目录：`{Path(run_dir).resolve()}`")
    lines.append("")
    lines.append("| 产物 | 路径 |")
    lines.append("| --- | --- |")
    lines.append("| 交互观测台（主入口） | `reports/index.html` |")
    lines.append("| 聚合摘要 | `summary.json` |")
    lines.append("| 解析快照（raw + DQR） | `raw/` |")
    lines.append("| 结构化输出（三粒度） | `representation/` |")
    lines.append("| 呈现层 markdown | `markdown/` |")
    lines.append("| 统一评价（质量评估 + failure modes） | `evaluation/` |")
    lines.append("| 策略搜索结论 | `optimization/optimization_summary.json` |")
    lines.append("| RuntimeProfiler 聚合 | `optimization/profile_summary.json` |")
    lines.append("| Run 级 trace（debug 开启时） | `debug/run_trace.json` |")
    lines.append("")

    report_path = Path(run_dir) / "reports" / "run_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
