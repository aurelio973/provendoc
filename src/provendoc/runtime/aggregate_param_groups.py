"""Aggregate human-confirmed param groups into per-category policy recommendations.

Consumes the dashboard-exported param_groups_{run_id}.json, aggregates
"human-approved parameter groups" by file category, and derives sensible
policy ranges — closing the last mile of the human feedback loop.

Usage:
    python runtime/aggregate_param_groups.py param_groups_*.json
    python runtime/aggregate_param_groups.py --scan          # scan cwd for all exports
    python runtime/aggregate_param_groups.py file.json -o rec.json
"""

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _mode(vals: List[str]) -> Optional[str]:
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def _category_of(file_path: str) -> str:
    """Same bucketing as the report's _category_of: first two directory levels."""
    parts = Path(file_path.replace("\\", "/")).parts
    if len(parts) <= 1:
        return "root"
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def aggregate(files_groups: Dict[str, List[dict]]) -> Dict:
    """Aggregate param groups by category.

    files_groups: {file_path: [{soft, hard, ctx, boundary, integrity, density, ts}, ...]}
    """
    by_cat: Dict[str, List[dict]] = {}
    for file_path, groups in files_groups.items():
        cat = _category_of(file_path)
        by_cat.setdefault(cat, []).extend(groups)

    cat_recs: Dict[str, Dict] = {}
    all_softs, all_hards = [], []
    for cat, groups in sorted(by_cat.items()):
        softs = [g["soft"] for g in groups if "soft" in g]
        hards = [g["hard"] for g in groups if "hard" in g]
        densities = [g["density"] for g in groups if g.get("density")]
        all_softs.extend(softs)
        all_hards.extend(hards)
        cat_recs[cat] = {
            "n_groups": len(groups),
            "n_files": sum(1 for fp, gs in files_groups.items()
                           if _category_of(fp) == cat and gs),
            "soft": {"min": min(softs), "median": _median(softs), "max": max(softs)} if softs else None,
            "hard": {"min": min(hards), "median": _median(hards), "max": max(hards)} if hards else None,
            "density_median": _median(densities),
            "ctx_mode": _mode([g["ctx"] for g in groups if g.get("ctx")]),
            "boundary_mode": _mode([g["boundary"] for g in groups if g.get("boundary")]),
            "integrity_mode": _mode([g["integrity"] for g in groups if g.get("integrity")]),
        }

    # Global recommendation: median across all groups (RunConfig is global;
    # per-category differences are provided as reference)
    global_rec = None
    soft_med = _median(all_softs)
    hard_med = _median(all_hards)
    if soft_med is not None:
        global_rec = {
            "packaging_size_soft_limit": int(soft_med),
            "packaging_size_hard_limit": int(hard_med) if hard_med is not None else None,
            "note": "写入 RunConfig 的建议值；category 间分歧大时考虑 per-type policy",
        }

    # Divergence detection: flag when category medians differ by > 40%
    divergent = []
    if len(cat_recs) > 1:
        soft_meds = {}
        for cat, rec in cat_recs.items():
            soft = rec["soft"]
            if soft:
                soft_meds[cat] = soft["median"]
        if soft_meds:
            vals = list(soft_meds.values())
            if max(vals) / max(min(vals), 1) > 1.4:
                divergent = sorted(soft_meds.items(), key=lambda kv: kv[1])

    return {
        "total_groups": sum(len(g) for g in files_groups.values()),
        "total_files": sum(1 for g in files_groups.values() if g),
        "categories": cat_recs,
        "global_recommendation": global_rec,
        "divergent_categories": divergent,
    }


def print_report(rec: Dict) -> None:
    """Print the aggregation result as a readable report (per-category ranges + global recommendation + divergence hints)."""
    print(f"\n=== Param Groups Aggregation: {rec['total_groups']} groups / {rec['total_files']} files ===\n")
    for cat, r in rec["categories"].items():
        s = r["soft"]
        h = r["hard"]
        print(f"[{cat}]  {r['n_groups']} groups / {r['n_files']} files")
        if s:
            print(f"  soft: {s['min']} – {s['max']}  (median {s['median']})")
        if h:
            print(f"  hard: {h['min']} – {h['max']}  (median {h['median']})")
        if r["density_median"]:
            print(f"  density median: {r['density_median']}")
        modes = []
        for k in ("ctx_mode", "boundary_mode", "integrity_mode"):
            if r[k]:
                modes.append(f"{k.replace('_mode','')}={r[k]}")
        if modes:
            print(f"  modes: {' '.join(modes)}")
        print()
    g = rec["global_recommendation"]
    if g:
        print("→ Global RunConfig suggestion:")
        print(f"    packaging_size_soft_limit = {g['packaging_size_soft_limit']}")
        print(f"    packaging_size_hard_limit = {g['packaging_size_hard_limit']}")
        print(f"    ({g['note']})")
    if rec["divergent_categories"]:
        print("⚠ Category divergence detected (soft median spread > 40%):")
        for cat, med in rec["divergent_categories"]:
            print(f"    {cat}: {med}")


def main():
    """CLI entry: parse args → load exported JSON → aggregate → print/write."""
    # Windows GBK console compatibility: force UTF-8 output
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="*", help="param_groups_*.json files")
    ap.add_argument("--scan", action="store_true", help="scan cwd for param_groups_*.json")
    ap.add_argument("-o", "--output", help="write result JSON to this path")
    args = ap.parse_args()

    paths = list(args.inputs)
    if args.scan:
        paths.extend(glob.glob("param_groups_*.json"))
    if not paths:
        ap.error("no input files (pass paths or --scan)")

    files_groups: Dict[str, List[dict]] = {}
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for fp, groups in (data.get("groups") or {}).items():
            files_groups.setdefault(fp, []).extend(groups)

    if not files_groups:
        print("no param groups found in inputs", file=sys.stderr)
        sys.exit(1)

    rec = aggregate(files_groups)
    print_report(rec)

    if args.output:
        Path(args.output).write_text(
            json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
