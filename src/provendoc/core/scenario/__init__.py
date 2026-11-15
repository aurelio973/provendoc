"""Scenario layer modules (L2/L3 scenario-scoped machinery).

See specs/scenario_framework_prereg_v1.md for the layering
contract, specs/invoice_scenario_prereg_v1_draft.md for the invoice
scenario rules, and each module docstring for its honest boundaries.
v2 (2026-10-14): email / announcement families join the label set
(uncalibrated, honest); the L3 framework registry (frameworks.py)
replaces the hardcoded invoice dispatch; the academic-paper chain
lands as a structure-fact ledger.
"""

from provendoc.core.scenario.academic_paper_chunking import (
    chunk_academic_paper_text as chunk_academic_paper_text,
)
from provendoc.core.scenario.academic_paper_chunking import (
    normalize_regions as normalize_academic_regions,
)
from provendoc.core.scenario.academic_paper_scenario_dqr import (
    SCENARIO_METADATA_KEY as ACADEMIC_PAPER_SCENARIO_KEY,
)
from provendoc.core.scenario.academic_paper_scenario_dqr import (
    attach_academic_paper_scenario,
    build_academic_paper_payload,
)
from provendoc.core.scenario.email_chunking import (
    chunk_email_text as chunk_email_text,
)
from provendoc.core.scenario.email_scenario_dqr import (
    SCENARIO_METADATA_KEY as EMAIL_SCENARIO_KEY,
)
from provendoc.core.scenario.email_scenario_dqr import (
    attach_email_scenario,
    build_email_payload,
)
from provendoc.core.scenario.figure_channel_dedup import (
    ChannelDedupResult,
    ChannelEdit,
    dedup_native_chart_text,
)
from provendoc.core.scenario.frameworks import (
    FRAMEWORKS,
    ScenarioFramework,
    attach_scenario_chain,
    get_framework,
)
from provendoc.core.scenario.invoice_amount_check import (
    AmountCheck,
    AmountCheckResult,
    check_cn_amount_consistency,
    parse_cn_upper_amount,
)
from provendoc.core.scenario.invoice_anchors import (
    AnchorHit,
    AnchorResult,
    extract_invoice_anchors,
)
from provendoc.core.scenario.invoice_arbitration import (
    ArbitrationResult,
    FieldDecision,
    arbitrate_anchors,
)
from provendoc.core.scenario.invoice_arithmetic_check import (
    ArithmeticCheck,
    ArithmeticCheckResult,
    check_invoice_arithmetic,
)
from provendoc.core.scenario.invoice_chunking import (
    Chunk,
    ChunkInvariantResult,
    ChunkResult,
    check_chunk_invariants,
    chunk_invoice_text,
)
from provendoc.core.scenario.invoice_line_items import (
    LineItemHit,
    LineItemResult,
    extract_line_items,
)
from provendoc.core.scenario.invoice_salvage import (
    SalvageEdit,
    SalvageResult,
    salvage_numbers,
)
from provendoc.core.scenario.invoice_salvage_pipeline import (
    ScenarioSalvageResult,
    salvage_invoice_text,
)
from provendoc.core.scenario.invoice_scenario_dqr import (
    SCENARIO_METADATA_KEY,
    attach_invoice_scenario,
    build_invoice_scenario_payload,
)
from provendoc.core.scenario.invoice_scenario_pipeline import (
    InvoiceScenarioResult,
    run_invoice_scenario,
)
from provendoc.core.scenario.orphan_noise import (
    OrphanEdit,
    OrphanNoiseResult,
    remove_orphan_noise,
)
from provendoc.core.scenario.resolver import (
    BASIS_FALLBACK,
    BASIS_QUORUM,
    BASIS_STRONG,
    LABEL_ACADEMIC_PAPER,
    LABEL_ANNOUNCEMENT,
    LABEL_EMAIL,
    LABEL_GENERAL_PROSE,
    LABEL_INVOICE,
    LABEL_TECH_STANDARD,
    RunnerUp,
    ScenarioDecision,
    decision_to_dict,
    resolve_scenario,
)
from provendoc.core.scenario.tech_standard_scenario_dqr import (
    SCENARIO_METADATA_KEY as TECH_STANDARD_SCENARIO_KEY,
)
from provendoc.core.scenario.tech_standard_scenario_dqr import (
    attach_tech_standard_scenario,
    build_tech_standard_payload,
)

__all__ = [
    "ACADEMIC_PAPER_SCENARIO_KEY",
    "AmountCheck",
    "AmountCheckResult",
    "AnchorHit",
    "AnchorResult",
    "ArithmeticCheck",
    "ArithmeticCheckResult",
    "ArbitrationResult",
    "BASIS_FALLBACK",
    "BASIS_QUORUM",
    "BASIS_STRONG",
    "ChannelDedupResult",
    "ChannelEdit",
    "Chunk",
    "ChunkInvariantResult",
    "ChunkResult",
    "EMAIL_SCENARIO_KEY",
    "FieldDecision",
    "FRAMEWORKS",
    "InvoiceScenarioResult",
    "LABEL_ACADEMIC_PAPER",
    "LABEL_ANNOUNCEMENT",
    "LABEL_EMAIL",
    "LABEL_GENERAL_PROSE",
    "LABEL_INVOICE",
    "LABEL_TECH_STANDARD",
    "LineItemHit",
    "LineItemResult",
    "OrphanEdit",
    "OrphanNoiseResult",
    "SCENARIO_METADATA_KEY",
    "RunnerUp",
    "SalvageEdit",
    "SalvageResult",
    "ScenarioDecision",
    "ScenarioFramework",
    "ScenarioSalvageResult",
    "TECH_STANDARD_SCENARIO_KEY",
    "arbitrate_anchors",
    "attach_academic_paper_scenario",
    "attach_email_scenario",
    "attach_invoice_scenario",
    "attach_scenario_chain",
    "attach_tech_standard_scenario",
    "build_academic_paper_payload",
    "build_email_payload",
    "build_invoice_scenario_payload",
    "build_tech_standard_payload",
    "check_chunk_invariants",
    "check_cn_amount_consistency",
    "check_invoice_arithmetic",
    "chunk_academic_paper_text",
    "chunk_email_text",
    "chunk_invoice_text",
    "decision_to_dict",
    "dedup_native_chart_text",
    "extract_invoice_anchors",
    "extract_line_items",
    "get_framework",
    "normalize_academic_regions",
    "parse_cn_upper_amount",
    "remove_orphan_noise",
    "resolve_scenario",
    "run_invoice_scenario",
    "salvage_invoice_text",
    "salvage_numbers",
]
