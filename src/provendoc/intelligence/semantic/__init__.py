"""Semantic Invariant framework — domain-polymorphic quality evaluation.

Two layers, explicitly separated:

Runtime Core (absorbed into representation behavior):
    SemanticInvariant  — abstract base: "this semantic property MUST hold"
    InvariantRegistry  — per-domain registry of active invariants
    StabilityScorer     — representation stability across chunking strategies

    These have dissolved into the runtime: invariant checks live inside
    quality evaluation, stability informs representation policies, and
    locality preservation is enforced by separability rules. They are
    not called as a separate stage — their semantics permeate the pipeline.

Research / Verification Layer (NOT in runtime path):
    RecoverabilityModel — 3-layer recoverability assessment

    This is a verification tool, not a runtime engine. It is called only
    from benchmarks/ and tests/ to validate that the system still
    preserves recoverability semantics. Runtime does not import or
    invoke it. Forcing it back into runtime would be redundant — its
    valuable parts (extraction resilience, representation stability,
    reconstruction quality) are already enforced by the runtime layers
    above.

Key design decisions:
    1. Invariants are NOT a giant unified IR. Each domain declares its
       own invariants. The base class provides the contract.
    2. The registry is per-domain, not global. Narrative invariants
       don't apply to code, and vice versa.
    3. Downstream consumers (optimizer, recommendations) read the
       invariant-track output directly (``invariants`` /
       ``invariant_composite``); the legacy six-dimension profile
       was retired with the old production line.
"""

from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    InvariantResult,
    SemanticInvariant,
)
from provendoc.intelligence.semantic.recoverability import RecoverabilityModel
from provendoc.intelligence.semantic.stability import StabilityScorer

__all__ = [
    "InvariantRegistry",
    "InvariantResult",
    "SemanticInvariant",
    "RecoverabilityModel",
    "StabilityScorer",
]
