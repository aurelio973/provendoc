"""RE three-layer envelope assembly — record_stream domain (EventQuality v2.0 canonical).

Domain: record_stream
Formats: log, jsonl, ndjson

The original implementation of the §10 delivery record (2026-09-14). After the generalization refactor (re_migration_plan
§11, multi-domain promotion), this module degenerates into a delegating shell of domain_rse_report — the public
API and the existing contract tests are untouched, preventing behavior drift; the full record of domain-specific
calibration decisions lives in re_migration_plan.md §10, and the generic implementation in domain_rse_report.py.

Domain-specific contract (now collected into DOMAIN_QUALITY_CONTRACTS["record_stream"]):
    - quality: direct projection of the EventQuality canonical (event_quality_v2.0,
      no legacy debt)
    - gate: EventQuality issue grading (error/warning, no critical →
      ceiling warned); the single source of truth for exemptions = EventQuality applicability
    - capability: event units carry SourceContext line spans (log
      event-level line spans / jsonl record line numbers) → non-zero coverage, complete
      reachable; only synthetic units lacking line numbers honestly fall back to source_span_missing
      → hard confidence cap + partial (the original §10 decision-3 ceiling has been raised)
"""

from typing import Any, Dict, Optional

from provendoc.intelligence.rse.domain_rse_report import (
    build_domain_rse_report,
)

DOMAIN = "record_stream"


def build_event_rse_report(
    event_quality_result: Optional[Dict[str, Any]],
    representation=None,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the record_stream-domain RE three-layer envelope from an EventQuality result.

    Delegates to build_domain_rse_report (driven by the domain contract table); the signature and the
    return shape are unchanged — see domain_rse_report.build_domain_rse_report.
    """
    return build_domain_rse_report(
        DOMAIN,
        event_quality_result,
        representation=representation,
        provenance=provenance,
    )
