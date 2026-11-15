# Provendoc Runtime Layer
# Unified orchestration layer: one run = one complete ingestion journey
#
# Public API:
#   from runtime import ProvendocRun, RunConfig, RunSummary
#
# Quick start:
#   config = RunConfig(dataset_dir="./data", runtime_mode="benchmark")
#   run = ProvendocRun(config)
#   summary = run.execute()
#
# Config file:
#   config = RunConfig.from_toml("provendoc.toml")
#   run = ProvendocRun(config)
#   summary = run.execute()

from provendoc.runtime.config import RunConfig
from provendoc.runtime.provendoc_run import ProvendocRun
from provendoc.runtime.run_models import FileResult, RunSummary

__all__ = ["ProvendocRun", "RunConfig", "RunSummary", "FileResult"]
