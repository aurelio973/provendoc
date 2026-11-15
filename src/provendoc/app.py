"""Provendoc — thin CLI entry point.

This module is a thin wrapper around ProvendocRun. All ingestion
logic lives in runtime/provendoc_run.py. This file only provides
a convenient `python app.py` entry point.

Usage:
    python app.py                                    # run with defaults
    python app.py ./my_dataset                        # custom dataset
    python app.py ./my_dataset "desc"                # custom description
    python app.py ./my_dataset --mode benchmark       # benchmark mode
    python app.py ./my_dataset --mode debug           # debug mode
    python app.py --config provendoc.toml                  # load config from TOML
    python app.py --list-modes                        # show mode presets
    python app.py ./my_dataset --workers 8            # parallel workers
    python app.py ./my_dataset --threshold 1048576    # huge file threshold
    python app.py ./my_dataset --no-cache             # disable parse cache
    python app.py ./my_dataset --no-parallel          # sequential processing
    python app.py ./my_dataset --no-async-export      # disable async file writes
    python app.py --clear-cache                       # clear parse cache and exit

Run modes:
    benchmark  — minimal exports, skip evaluation/debug, for speed measurement
    analysis   — default; balanced exports + profiler on
    debug      — full exports, no huge-file skipping, all diagnostics

Config file (TOML):
    Create a provendoc.toml file with a [provendoc] section to persist
    settings.  CLI flags override config file values.

    Example::

        [provendoc]
        dataset_dir = "./data"
        runtime_mode = "benchmark"
        parallel_workers = 4
        huge_file_threshold = 1048576
        enable_async_export = true
        progress_interval = 25
"""

import argparse
import logging
import os
import sys

from provendoc.runtime.config import RunConfig
from provendoc.runtime.parse_cache import ParseCache
from provendoc.runtime.provendoc_run import ProvendocRun


def _configure_logging() -> None:
    """Entry-point logging setup (library code stays NullHandler-silent).

    Defaults to WARNING so honest-failure facts (parse_failed etc.)
    reach stderr audibly without cluttering the CLI's normal print
    output; PROVENDOC_LOG_LEVEL overrides (e.g. DEBUG).
    """
    level = os.environ.get("PROVENDOC_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )


def _print_modes():
    """Print available runtime modes and their preset descriptions."""
    modes = [
        ("benchmark", "Single-process minimal exports for fast scale/quality measurement"),
        ("development", "Single-process full exports and HTML report for debug/development"),
        ("batch", "Multi-process worker disk-offloading for large-scale production batching"),
        ("analysis", "Default balanced mode — evaluation + markdown/json exports + reports"),
        ("debug", "Full exports, no huge-file skipping, all diagnostics enabled"),
    ]
    print("Runtime modes:")
    for name, desc in modes:
        print(f"  {name:12s} — {desc}")
    print()
    print("Preset details:")
    print("  benchmark:    parallel=F (1 proc), exports=F, report=F, fast-path=512KB")
    print("  development:  parallel=F (1 proc), exports=T, report=T, debug=T")
    print("  batch:        parallel=T (multi-proc), exports=T, report=F, thin-IPC=T")
    print("  analysis:     parallel=T (multi-proc), exports=T, report=T, thin-IPC=T")
    print("  debug:        parallel=T, all exports on, huge_file_skip_*=False")


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser (argparse, replaces hand-rolled argv walking).

    Semantics match the legacy parser: positional dataset_dir /
    description, explicit flags map onto the same RunConfig fields,
    and flags beat TOML (_build_config). Unknown flags / missing
    values now fail honestly via argparse (exit 2) instead of
    silently landing in the positional arguments.
    """
    parser = argparse.ArgumentParser(
        prog="provendoc",
        description=(
            "Evidence-first, domain-aware document ingestion batch runtime. "
            "Runs the dataset through per-domain pipelines and writes "
            "runs/{run_id}/ artifacts (raw / representation / evaluation / reports)."
        ),
        epilog=(
            "run modes: benchmark / development / batch / analysis (default) / debug — "
            "see --list-modes. Config file values are overridden by CLI flags."
        ),
    )
    parser.add_argument(
        "dataset_dir", nargs="?", default=None,
        help="dataset directory (default: ./tests/datasets/txt under the repo root)",
    )
    parser.add_argument(
        "description", nargs="?", default=None,
        help="run description recorded in the summary",
    )
    parser.add_argument(
        "--mode", dest="runtime_mode", default=None,
        help="runtime mode preset (benchmark/development/batch/analysis/debug)",
    )
    parser.add_argument(
        "--workers", dest="parallel_workers", type=int, default=None,
        help="parallel worker count",
    )
    parser.add_argument(
        "--threshold", dest="huge_file_threshold", type=int, default=None,
        help="huge file skip threshold in bytes",
    )
    parser.add_argument(
        "--config", dest="_config_file", default=None,
        help="load RunConfig from a TOML file (CLI flags still override)",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="clear the parse cache and exit",
    )
    parser.add_argument(
        "--no-cache", dest="enable_parse_cache", action="store_false",
        default=None,
        help="disable the parse cache for this run",
    )
    parser.add_argument(
        "--no-parallel", action="store_true",
        help="run sequentially (single process)",
    )
    parser.add_argument(
        "--no-async-export", dest="enable_async_export", action="store_false",
        default=None,
        help="disable async artifact file writes",
    )
    parser.add_argument(
        "--progress-interval", dest="progress_interval", type=int, default=None,
        help="progress print interval (files)",
    )
    parser.add_argument(
        "--list-modes", action="store_true",
        help="print runtime mode presets and exit",
    )
    return parser


def _parse_args(argv: list) -> dict:
    """Parse CLI arguments (argv[0] = script name) into RunConfig kwargs.

    Defaults are applied only when no config file is supplied.  Otherwise
    they would incorrectly overwrite values explicitly stored in TOML.
    Action flags (--clear-cache / --list-modes) print and exit here,
    mirroring the legacy behavior.
    """
    args = _build_arg_parser().parse_args(argv[1:])

    if args.clear_cache:
        count = ParseCache.clear()
        print(f"[App] Cleared {count} cache entries from .cache/parse/")
        sys.exit(0)
    if args.list_modes:
        _print_modes()
        sys.exit(0)

    kwargs = {}
    if args.dataset_dir is not None:
        kwargs["dataset_dir"] = args.dataset_dir
    if args.description is not None:
        kwargs["description"] = args.description
    for dest in (
        "runtime_mode", "parallel_workers", "huge_file_threshold",
        "_config_file", "enable_parse_cache", "enable_async_export",
        "progress_interval",
    ):
        value = getattr(args, dest)
        if value is not None:
            kwargs[dest] = value
    if args.no_parallel:
        kwargs["enable_parallel"] = False
        kwargs["parallel_workers"] = 1

    if "_config_file" not in kwargs:
        kwargs.setdefault("dataset_dir", _default_dataset_dir())
        kwargs.setdefault("description", "App-level unified run")

    return kwargs


def _default_dataset_dir() -> str:
    """Resolve the default dataset directory.

    Prefers the CWD-relative path (legacy behavior when run from the
    repo root); falls back to the repo root's tests/datasets/txt so
    any working directory (e.g. an IDE launch from src/) still runs.
    """
    from pathlib import Path

    cwd_candidate = Path("./tests/datasets/txt")
    if cwd_candidate.exists():
        return str(cwd_candidate)
    repo_candidate = (
        Path(__file__).resolve().parents[2] / "tests" / "datasets" / "txt"
    )
    if repo_candidate.exists():
        return str(repo_candidate)
    return str(cwd_candidate)


def _build_config(kwargs: dict) -> RunConfig:
    """Build runtime config while preserving explicit TOML values."""
    config_file = kwargs.get("_config_file")
    cli_kwargs = {
        key: value
        for key, value in kwargs.items()
        if not key.startswith("_")
    }

    if not config_file:
        cli_mode = cli_kwargs.pop("runtime_mode", None)
        cfg = RunConfig(runtime_mode=cli_mode or "analysis")
        for key, value in cli_kwargs.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    cfg = RunConfig.from_toml(config_file)
    cli_mode = cli_kwargs.pop("runtime_mode", None)
    if cli_mode is not None and cli_mode != cfg.runtime_mode:
        # A changed CLI-selected mode deliberately refreshes the preset.  Apply
        # all other explicit CLI arguments afterwards so they remain authoritative.
        cfg.runtime_mode = cli_mode
        cfg.__post_init__()

    for key, value in cli_kwargs.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def main():
    """Create an ProvendocRun with sensible defaults and execute it."""
    _configure_logging()
    cfg = _build_config(_parse_args(sys.argv))

    run = ProvendocRun(cfg)
    summary = run.execute()

    mode_label = cfg.runtime_mode.upper()
    print(f"\n[App] {mode_label} mode — {summary.success_files}/{summary.total_files} files OK")
    print(f"[App] Report: {run.run_dir / 'reports' / 'index.html'}")
    # int exit code: the console script runs sys.exit(main());
    # returning a non-int object would be interpreted as exit code 1.
    return 0 if summary.success_files >= summary.total_files else 1


if __name__ == "__main__":
    main()
