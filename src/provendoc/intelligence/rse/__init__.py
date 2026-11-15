"""Evaluation layer — domain-specific quality evaluation.

Universal Ingestion Evaluation Architecture:

    Evaluation Framework
    ├── Shared Evaluation Foundation     (core/evaluation/foundation/)
    ├── Narrative      (NarrativeQuality, invariant_v1)   ✓ implemented
    ├── Config Quality (ConfigQuality)          ✓ implemented (config_quality_v1.4)
    ├── Table Quality  (TableQuality)           ✓ implemented (table_quality_v2.0)
    ├── Event Quality  (EventQuality)           ✓ implemented (event_quality_v2.0)
    └── Code Quality   (CodeQuality)            ✓ implemented (code_quality_v3)

Each domain has its own:
    - representation philosophy
    - evaluation philosophy
    - optimization philosophy

"Good chunk" is NOT universal:
    Narrative  = semantic continuity
    Config     = hierarchy-preserving paths
    Table      = structure-preserving row groups (cell-traceable)
    Event      = incident-local event groups
    Code       = AST-complete units
"""

from provendoc.intelligence.rse.code_quality import CodeQuality
from provendoc.intelligence.rse.config_quality import ConfigQuality
from provendoc.intelligence.rse.event_quality import EventQuality
from provendoc.intelligence.rse.narrative_quality import NarrativeQuality
from provendoc.intelligence.rse.table_quality import TableQuality
from provendoc.intelligence.rse.unified_result import build_unified_evaluation

__all__ = [
    "NarrativeQuality",
    "ConfigQuality",
    "TableQuality",
    "EventQuality",
    "CodeQuality",
    "build_unified_evaluation",
]
