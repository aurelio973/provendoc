"""Runtime integrity benchmark — retrieval failure modes.

Migrated from ``benchmarks/representation_integrity/failure_modes.py`` because
the runtime layer (ProvendocRun evaluation ``failure_modes`` dimension) depends on it at
runtime.  The remainder of ``benchmarks/`` stays at the repository root as
development tooling and is not part of the installed distribution.
"""

from provendoc.integrity.failure_modes import (
    FailureMode,
    RetrievalFailureBenchmark,
)

__all__ = ["FailureMode", "RetrievalFailureBenchmark"]
