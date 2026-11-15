"""Shared base for domain quality evaluators (table / event).

table_quality.py and event_quality.py carried byte-identical copies of the
three-phase aggregation pipeline (dimensions → profile/applicability/
issues, canonical overall, confidence) and the issue / severity /
empty-result / confidence helpers.  This module owns them exactly
once; subclasses supply:

- WEIGHTS / PRIMARY_DIMENSIONS / SECONDARY_DIMENSIONS / PROFILE_VERSION
- the domain dimension implementations (each returning the
  ``(score, applicable, reason, issues)`` 4-tuple)
- CONTRACT_FACT_SOURCES: the fact sources backed by a real parse
  contract; any other source is treated as a metadata fallback and
  gets the 0.85 format_reliability discount in :meth:`_confidence`.

config_quality.py and code_quality.py carry their own
architecture-specific variants of these helpers (config: bare-float
dimensions + a separate applicability phase + ``level``-keyed issues;
code: active-weight normalization + parse-status confidence).  They
are semantic variants, not byte-identical copies — on 2026-10-25 the
"align onto this base" promise was formally retired
(evaluator_semantics_governance_prereg_v1 §2): forcing them onto
BaseQuality would either change behavior or produce
inherit-then-override ceremony with zero dedup value.  Cross-domain
semantic consistency is governed by contract tests instead
(tests/test_evaluator_semantics_governance.py).
"""

from typing import AbstractSet, Any, Dict


class BaseQuality:
    """Shared aggregation pipeline + helpers for domain quality evaluators."""

    PROFILE_VERSION = ""

    WEIGHTS: Dict[str, Any] = {}

    # The base class only declares a readable contract (covariant AbstractSet): subclasses may use set or frozenset
    # literals interchangeably; mypy no longer fights over the concrete container type.
    PRIMARY_DIMENSIONS: AbstractSet[str] = frozenset()

    SECONDARY_DIMENSIONS: AbstractSet[str] = frozenset()

    #: Fact sources backed by a real parse contract.  Anything else
    #: is a metadata fallback and gets the 0.85 discount in
    #: :meth:`_confidence`.
    CONTRACT_FACT_SOURCES: AbstractSet[str] = frozenset()

    #: Observation-layer dimension (v2.0 taxonomy refactor, dimension_taxonomy_refactor
    #: _prereg_v1): sub-check raw scores are exposed under their original names,
    #: included_in_overall=False — the invariant track's profile→observations
    #: fallback reads scores with zero drift, demoted-dimension issues stay in the gate.
    OBSERVATION_DIMS: AbstractSet[str] = frozenset()

    #: Demoted dimensions outside the scoring layer (evidence layer/deterministic assertions, prereg §2.3):
    #: numbers go to observations only, issues stay in the main issues list
    #: (gate input unchanged).
    DEMOTED_DIMS: AbstractSet[str] = frozenset()

    # ==================================
    # Phase 1: dimensions → profile / applicability / issues
    # ==================================

    def _aggregate_dimensions(self, dim_results):
        """Fold per-dimension 4-tuples into profile/applicability/issues.

        N/A dimensions are reported as ``None`` in the profile with
        their reason recorded in applicability; their issues are
        dropped (nothing was actually evaluated).
        """
        profile = {}
        applicability = {}
        issues = []
        for dim, (score, applicable, reason, dim_issues) in (
            dim_results.items()
        ):
            if applicable:
                profile[dim] = round(score, 3)
            else:
                profile[dim] = None
            applicability[dim] = {
                "applicable": applicable,
                "reason": "" if applicable else reason,
            }
            if applicable and dim_issues:
                issues.extend(dim_issues)
        return profile, applicability, issues

    # ==================================
    # Three-question composite-dimension assembly (v2.0 taxonomy refactor)
    # ==================================

    def _merge_subchecks(self, canonical, spec, subchecks):
        """Composite dimension = weighted mean of applicable sub-checks (re-normalized within the dimension).

        The within-dimension N/A policy is isomorphic with the formula_relation_integrity
        within-dimension mixing precedent: only applicable sub-checks enter the numerator and denominator (partial
        applicability re-normalizes within the dimension, all-N/A makes the dimension N/A). Sub-check issues attach to
        the composite dimension name (the dimension field is relabelled, the atomic check name stays in the
        subcheck field — invariant-track issue propagation matches by the original name
        with zero drift), severity is preserved verbatim — zero gate-input drift
        (prereg §2.4).
        """
        num = 0.0
        den = 0.0
        issues = []
        na_reasons = []
        for name, weight in spec:
            score, applicable, reason, sub_issues = subchecks[name]
            if not applicable:
                na_reasons.append(f"{name}: {reason}")
                continue
            num += score * weight
            den += weight
            for issue in sub_issues:
                relabeled = dict(issue)
                relabeled["dimension"] = canonical
                relabeled["subcheck"] = name
                issues.append(relabeled)
        if den <= 0:
            joined = "; ".join(na_reasons)
            return 0.0, False, joined or "no_applicable_subchecks", []
        return num / den, True, "", issues

    def _observations_from(self, subchecks):
        """Sub-check raw scores exposed under their original names (demoted dimensions included).

        QualityBackedInvariant._dim_value goes through the profile →
        observations fallback: the old dimension names in the invariant track's dimension_weights
        still read correctly — zero drift for the invariant track across the taxonomy refactor
        (isomorphic with the config v1.2/v1.3 demotion precedent).
        """
        return {
            name: (round(score, 3) if applicable else None)
            for name, (score, applicable, _reason, _issues) in (
                subchecks.items()
            )
        }

    # ==================================
    # Phase 2: canonical overall — N/A is not reallocated
    # overall = Σ(active × w) / Σ(all w)
    # ==================================

    def _canonical_overall(self, profile, applicability):
        """Canonical weighted overall; N/A weight is NOT redistributed."""
        total_weight = sum(self.WEIGHTS.values())
        active_dims = {
            d for d in self.WEIGHTS
            if applicability[d]["applicable"]
        }
        if total_weight > 0:
            return (
                sum(
                    profile[d] * self.WEIGHTS[d]
                    for d in active_dims
                )
                / total_weight
            )
        return 0.0

    # ==================================
    # helpers
    # ==================================

    @staticmethod
    def _issue(dim, severity, message):
        return {"dimension": dim, "severity": severity, "message": message}

    def _severity_for(self, dim):
        return "error" if dim in self.PRIMARY_DIMENSIONS else "warning"

    # ==================================
    # Phase 3: confidence (decomposed)
    # ==================================

    def _confidence(self, applicability, facts, document_quality):
        """confidence = dimension_coverage × evaluation_depth
        × format_reliability.

        evaluation_depth: all dimensions have real implementations
        (skeleton mode retired); format_reliability: non-contract
        fact sources get a 0.85 discount, document quality warnings
        penalize by 0.1 each (floor 0.5).
        """
        total_dims = len(self.WEIGHTS)
        active_dims = sum(
            1 for d in self.WEIGHTS
            if applicability[d]["applicable"]
        )
        dimension_coverage = active_dims / total_dims

        evaluation_depth = 1.0

        format_reliability = 1.0
        if facts["source"] not in self.CONTRACT_FACT_SOURCES:
            format_reliability *= 0.85
        if document_quality is not None:
            try:
                warnings = getattr(document_quality, "warnings", [])
                if warnings:
                    format_reliability = max(
                        0.5,
                        format_reliability - 0.1 * len(warnings),
                    )
            # On format-reliability adjustment failure keep the baseline value (report-layer fallback).
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
    # empty result
    # ==================================

    def _empty_result(self):
        return {
            "profile_version": self.PROFILE_VERSION,
            "profile": {
                dim: None for dim in self.WEIGHTS
            },
            "observations": {
                dim: None for dim in self.OBSERVATION_DIMS
            },
            "applicability": {
                dim: {
                    "applicable": False,
                    "reason": "no chunks to evaluate",
                }
                for dim in self.WEIGHTS
            },
            "overall": 0.0,
            "confidence": 0.0,
            "confidence_profile": {},
            "issues": [
                {
                    "dimension": "all",
                    "message": "no chunks to evaluate",
                }
            ],
            "metadata": {
                "chunk_count": 0,
                "avg_chunk_length": 0,
                "min_chunk_length": 0,
                "max_chunk_length": 0,
                "fact_source": "none",
                "weight_policy": "canonical_no_redistribution",
            },
        }
