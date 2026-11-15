"""Output-assembly methods of ConfigQuality (mixin).

Contains Phase 4/5 and the empty-result path (split from config_quality.py in 2026-09,
moved verbatim):
- _confidence          confidence decomposition (dimension_coverage × evaluation_depth
                        × format_reliability)
- _generate_issues     score-threshold issues + anomaly-risk exposure
- _count_sibling_groups / _count_references   counting helpers
- _empty_result        complete empty output when no units exist

The methods are composed via ConfigQuality (see provendoc.intelligence.rse.config_quality)
as a mixin; class constants (WEIGHTS / PROFILE_VERSION /
_INTERPOLATION_PATTERNS / _is_severe_loss) are provided by that facade class.
The move involves no runtime behavior change.
"""

import re
from typing import TYPE_CHECKING, Dict, List, Tuple

from provendoc.core.parsers.config import contracts as cc
from provendoc.intelligence.rse.config_quality_model import (
    _METRIC_DEFINITIONS,
    FormatCapabilityProfile,
    _FactView,
)

if TYPE_CHECKING:
    from typing import Callable


class ConfigQualityReportingMixin:
    """Collection of output-assembly methods of ConfigQuality (no fields, methods only).

    Class constants (WEIGHTS / PROFILE_VERSION / _INTERPOLATION_PATTERNS)
    and _is_severe_loss are provided by the facade class ConfigQuality; the annotations here
    only provide a contract for static checking.
    """

    if TYPE_CHECKING:
        WEIGHTS: Dict[str, float]
        PROFILE_VERSION: str
        _INTERPOLATION_PATTERNS: List[re.Pattern[str]]
        _is_severe_loss: Callable[[str], bool]

    # ==================================
    # Confidence
    # ==================================

    def _confidence(
        self,
        applicability: Dict[str, Dict],
        profile: FormatCapabilityProfile,
        document_quality,
        facts: _FactView,
    ) -> Tuple[float, Dict]:
        """Compute confidence and decomposed profile.

        confidence = dimension_coverage × evaluation_depth × format_reliability

        evaluation_depth: all six dimensions have real implementations (no deferred dimensions);
        format_reliability: document quality warnings + fact-source reliability
        (fallback fact sources get a 0.85 discount).
        """
        total_dims = len(self.WEIGHTS)
        active_dims = sum(
            1 for d in self.WEIGHTS
            if applicability[d]["applicable"]
        )

        # dimension_coverage
        dimension_coverage = active_dims / total_dims

        # evaluation_depth — all six dimensions implemented
        evaluation_depth = 1.0

        # format_reliability: document quality + fact-source reliability
        format_reliability = 0.95
        if facts.source != cc.CONFIG_ENTRY_CONTRACT_VERSION:
            format_reliability *= 0.85
        if document_quality is not None:
            try:
                warnings = getattr(document_quality, "warnings", [])
                if warnings:
                    format_reliability = max(
                        0.5,
                        format_reliability - 0.1 * len(warnings),
                    )
            # Keep the baseline value if the format-reliability adjustment fails (report-layer fallback).
            except Exception:
                pass

        confidence = (
            dimension_coverage
            * evaluation_depth
            * format_reliability
        )

        return round(confidence, 3), {
            "dimension_coverage": round(dimension_coverage, 3),
            "evaluation_depth": round(evaluation_depth, 3),
            "format_reliability": round(format_reliability, 3),
        }

    # ==================================
    # Issues (score problems + anomaly risks)
    # ==================================

    def _generate_issues(
        self,
        dimensions: Dict[str, float],
        applicability: Dict[str, Dict],
        profile: FormatCapabilityProfile,
        facts: _FactView,
    ) -> List[Dict]:
        """Generate issue list.

        Two source categories:
        1. Score thresholds (<0.8 warning / <0.5 error)
        2. Anomaly risks (source_anomaly_handling exclusive):
           duplicate keys present → an issue is always emitted (with evidence warning / without evidence error);
           severe parse anomalies → error. Scores are not additionally penalized for anomaly existence.
        """
        issues = []

        for dim, score in dimensions.items():
            if not applicability[dim]["applicable"]:
                continue
            if dim == "source_anomaly_handling":
                continue  # handled by the anomaly-risk logic

            if score < 0.5:
                issues.append({
                    "dimension": dim,
                    "level": "error",
                    "message": (
                        f"score={score:.3f}, significant quality issue"
                    ),
                })
            elif score < 0.8:
                issues.append({
                    "dimension": dim,
                    "level": "warning",
                    "message": f"score={score:.3f}, potential issue",
                })

        # ---- anomaly risk exposure (score-independent) ----
        duplicates = [e for e in facts.entries if e.duplicate]
        handled = [e for e in duplicates if e.override_chain]
        if duplicates:
            level = (
                "warning" if len(handled) == len(duplicates)
                else "error"
            )
            issues.append({
                "dimension": "source_anomaly_handling",
                "level": level,
                "message": (
                    f"{len(duplicates)} duplicate key(s) detected; "
                    f"override_chain evidence: "
                    f"{len(handled)}/{len(duplicates)}"
                ),
            })
        for r in facts.loss_reasons:
            if self._is_severe_loss(r):
                issues.append({
                    "dimension": "source_anomaly_handling",
                    "level": "error",
                    "message": f"parse/contract anomaly: {r}",
                })

        return issues

    # ==================================
    # Helpers
    # ==================================

    @staticmethod
    def _count_sibling_groups(facts) -> int:
        """Count sibling groups (same parent) with >= 2 members."""
        groups: Dict[str, int] = {}
        for e in facts.entries:
            segments = e.path_str.split(".")
            if len(segments) <= 1:
                continue
            parent = ".".join(segments[:-1])
            groups[parent] = groups.get(parent, 0) + 1
        return sum(1 for c in groups.values() if c >= 2)

    def _count_references(self, facts, fine_units, base_units) -> int:
        """Count references: contract refs first, regex fallback."""
        if facts.refs:
            return len(facts.refs)
        count = 0
        for unit in (fine_units or base_units):
            for pattern in self._INTERPOLATION_PATTERNS:
                count += len(pattern.findall(unit.text))
        return count

    # ==================================
    # Empty result
    # ==================================

    def _empty_result(self):
        return {
            "profile": {
                dim: None for dim in self.WEIGHTS
            },
            "profile_version": self.PROFILE_VERSION,
            # v1.2: downgraded observation slots also exist in the empty result (all N/A)
            "observations": {
                **{
                    dim: None
                    for dim in self.OBSERVATION_DIMENSIONS
                },
                "observation_policy": (
                    "diagnostic_only_not_in_overall"
                ),
            },
            "overall": 0.0,
            "confidence": 0.0,
            "confidence_profile": {
                "dimension_coverage": 0.0,
                "evaluation_depth": 1.0,
                "format_reliability": 1.0,
            },
            "issues": [
                {
                    "dimension": "all",
                    "level": "error",
                    "message": "no chunks to evaluate",
                }
            ],
            "applicability": {
                dim: {
                    "applicable": False,
                    "reason": "no chunks",
                }
                for dim in self.WEIGHTS
            },
            "format_capability": {
                "has_hierarchy": False,
                "hierarchy_depth": 0,
                "has_interpolation": False,
                "has_namespace_collision": False,
                "representation_flattened": False,
                "sibling_count": 0,
                "format_type": "unknown",
            },
            "metric_definitions": {
                dim: {
                    "purpose": defn.purpose,
                    "theoretical_basis": defn.theoretical_basis,
                    "limitations": defn.limitations,
                    "role": defn.role,
                    "weight": defn.weight,
                    "classification": defn.classification,
                    "sub_checks": list(defn.sub_checks),
                    "included_in_overall": self.INCLUDED_IN_OVERALL[dim],
                }
                for dim, defn in _METRIC_DEFINITIONS.items()
            },
            "metadata": {
                "chunk_count": 0,
                "avg_chunk_length": 0,
                "min_chunk_length": 0,
                "max_chunk_length": 0,
                "format": "unknown",
                "sibling_groups": 0,
                "reference_count": 0,
                "fact_source": "none",
                "entry_count": 0,
                "container_count": 0,
                "fine_unit_count": 0,
                "base_unit_count": 0,
                "weight_policy": "canonical_fixed",
                "active_dimension_count": 0,
                "dimension_breakdowns": {},
            },
        }
