"""Unified Semantic System — signals, relations, and primitives.

Three modules:
    Signals  (signals.py)    — "what was observed" (format-level + discourse-level)
    Relations (relations.py)  — "which units have semantic dependencies"
    Primitives (primitives.py) — deterministic signal → semantic primitive mapping
"""

from provendoc.core.semantics.primitives import (
    MappedSemantics,
    SignalSemanticMapper,
)
from provendoc.core.semantics.relations import (
    RelationType,
    StructuralRelation,
    StructuralSemanticsAnalyzer,
    build_locality_boundaries,
)
from provendoc.core.semantics.signals import (
    DiscourseAwareBoundaryPolicy,
    DiscourseCueType,
    DiscourseSignal,
    DiscourseSignalDetector,
    SignalExtractor,
    SignalObservation,
    SignalType,
    analyze_discourse,
)

__all__ = [
    # signals
    "SignalType",
    "SignalObservation",
    "SignalExtractor",
    "DiscourseCueType",
    "DiscourseSignal",
    "DiscourseSignalDetector",
    "DiscourseAwareBoundaryPolicy",
    "analyze_discourse",
    # relations
    "RelationType",
    "StructuralRelation",
    "StructuralSemanticsAnalyzer",
    "build_locality_boundaries",
    # primitives
    "MappedSemantics",
    "SignalSemanticMapper",
]
