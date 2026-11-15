"""Scenario decision layer (L2) — resolve domain_signals into one label.

Implements scenario_framework_prereg_v1 §1 with D-A~D-D settled:
D-A runner_up margin = absolute count dict (voting_kinds diff +
total_count diff — auditable, no pseudo-precision); D-B module lives
in ``core/scenario/`` (domain = format, scenario = semantics); D-C
injection unified at universal_parser (this module never imports
parsers); D-D extractor version coexists (``scenario_resolver_v2``
stamped on every decision; old labels never invalidated in place).

v2 (adds email and announcement): email and
announcement join the eligible label set — their marker tables are
honest-uncalibrated (see domain_signals_v2 note); the resolution
algorithm itself is UNCHANGED from v1.

v3 (2026-10-15, corpus-driven fix): strong-vs-strong ties are now
broken by IDENTITY over CONTENT.  A document-identity declaration
(an RFC header line, the invoice's own invoice-number field) beats
content-convention evidence (email example headers quoted inside an
RFC discussing mail formats).  Observed co-occurrence driving the
rule: rfc2046 / rfc5322 / rfc977 carried email voting_kinds 5 >
tech_standard's 4 and flipped to email — the exact case prereg §4
rule 2's priority table was left open for.  The identity/content
classification is re-derived from the ``identity`` flag on each
``quorum.strong_kinds`` entry (emitted by domain_signals v3; absent
flag = content, so v2-era payloads still resolve deterministically).

v5 (2026-11-08): exam joins the eligible label set — the family's
marker table was calibrated 2026-11-08 (tmp_survey/exam_marker_probe
v0-v3: GT 21/26 + 22/22, questionnaire/FP faces all zero; full corpus
pass registered in specs/domain_signals_calibration README round7).
The resolution algorithm itself is UNCHANGED from v4.

v7 (2026-11-11, p10 contract promotion): contract leaves the watchlist and
joins the eligible label set.  Watchlist→eligible promotion follows
the "GT bucket first, then unfreeze" discipline (master plan §195): the
contract_iachina_zh GT bucket (44 association model clauses, iachina.cn
col94 census) landed before this change; the insurance marker table
(insurance_parties / insurance_terms in domain_signals v8) was
calibrated against the negative buckets (_adv_tmp/p10_negative_survey).
The resolution algorithm itself is UNCHANGED from v6 — strong-pool
exclusivity, identity > content, voting_kinds desc, total_count desc,
family asc.

v8 (2026-11-13, p11 medical promotion): medical_report leaves the
watchlist and joins the eligible label set — the second full
watchlist→eligible instance of the discipline.  The
medical_report_mendeley_en GT bucket (260 English-body lab-report
scans, image carrier — the first image-carrier GT) landed first; the
English marker table (reference_range OCR abbreviations,
lab_departments, result_table_headers in domain_signals v9) was
calibrated two-sided (positive census + full-chain negative census).
The same round demotes the English signature kinds to weak for
contract (7 letter-novel / sphinx-doc quorum flips found by the first
full-chain negative pass; contract recall is strong-path only).
The resolution algorithm itself is UNCHANGED from v7.

The layer does NO new detection — zero new markers, zero calibration
debt. Its only job is resolving the eleven evidence families into a
single label. Strong qualification is re-derived from the
``quorum.strong_kinds`` table inside the domain_signals payload plus
``marker_counts`` (single source of truth — the private tables of
``core.structure.domain_signals`` are never re-imported here).

Resolution algorithm (frozen, framework prereg §1.3):

1. collect families with ``present == True``;
2. empty set → ``general_prose`` (fallback is a legal label, not a
   failure);
3. strong beats quorum (specificity principle); within the same
   basis, rank by ``voting_kinds`` desc, then ``total_count`` desc;
4. every step's rule / families / count snapshot goes into
   ``evidence`` (rule references + counts only — no text excerpts;
   domain_signals already carries bounded samples);
5. the strongest loser enters ``runner_up`` with its absolute-count
   margin.

Honest boundaries:

* watchlist families (drug_instruction / financial_statement)
  record evidence but never emit a label and
  never enter runner_up — their ``present`` is an observation, not
  competition eligibility; drug_instruction has strong kinds
  (国药准字 drug-approval numbers), so a present watchlist family is
  recorded per-family in
  evidence under ``watchlist_family_observed``;
* a full tie (equal voting_kinds AND equal total_count) has not been
  observed in the v1 corpus; the code path stays deterministic via
  family-name ascending order and files the tie in evidence under
  ``tie_broken_deterministically`` — never a silent pick;
* malformed input (no ``families`` dict) degrades to general_prose
  with an ``input_unrecognized`` evidence entry — never raises into
  the injection point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BASIS_STRONG = "strong"
BASIS_QUORUM = "quorum"
BASIS_FALLBACK = "fallback"

LABEL_ACADEMIC_PAPER = "academic_paper"
LABEL_ANNOUNCEMENT = "announcement"
LABEL_CONTRACT = "contract"
LABEL_EMAIL = "email"
LABEL_EXAM = "exam"
LABEL_GOVERNMENT_DOCUMENT = "government_document"
LABEL_INVOICE = "invoice"
LABEL_MEDICAL_REPORT = "medical_report"
LABEL_TECH_STANDARD = "tech_standard"
LABEL_GENERAL_PROSE = "general_prose"

# scenario_low_confidence_governance_prereg_v1 §2.0: a non-fallback
# label whose runner-up voting_kinds margin is <= this threshold is
# the mislabel high-risk zone (frozen from the established
# close-margin audit criterion — tuning requires its own prereg).
SCENARIO_CLOSE_MARGIN_THRESHOLD = 1

# v2 label set: the three GT families + the two 2026-10-14 user-added
# families (email / announcement, honest-uncalibrated markers) +
# fallback. v5 (2026-11-08): exam joins (calibrated marker table,
# probe v0-v3 + corpus scan round7). v6 (2026-11-11):
# government_document joins (calibrated in _adv_tmp/p8 probes on
# the samr 376-file corpus: gov-narrative slice 256/278 on the
# production chain, chinese
# negative buckets FP=0, announcement/table/regulation-explainer
# boundary data in the
# bucket note). Algorithm unchanged — strong-pool exclusivity,
# identity > content, voting_kinds desc, total_count desc, family
# asc. v7 (2026-11-11, p10): contract joins — watchlist promotion
# with the GT bucket landed first (contract_iachina_zh 44 association
# model clauses, split frozen 2026-11-11) and the insurance marker
# table calibrated on the negative buckets
# (_adv_tmp/p10_negative_survey). v8 (2026-11-13, p11): medical_report
# joins — same discipline, medical_report_mendeley_en 260-scan GT
# landed first, English marker table calibrated two-sided. Algorithm
# unchanged. Remaining watchlist families (drug_instruction /
# financial_statement) keep
# evidence but emit no label (framework prereg §1.4, updated accordingly).
_ELIGIBLE_FAMILIES = frozenset(
    {
        LABEL_ACADEMIC_PAPER,
        LABEL_ANNOUNCEMENT,
        LABEL_CONTRACT,
        LABEL_EMAIL,
        LABEL_EXAM,
        LABEL_GOVERNMENT_DOCUMENT,
        LABEL_INVOICE,
        LABEL_MEDICAL_REPORT,
        LABEL_TECH_STANDARD,
    }
)

# Sorted once so watchlist evidence (and any tie-break) is stable.
# v7 (2026-11-11, p10): contract promoted out of the watchlist.
# v8 (2026-11-13, p11): medical_report promoted out of the watchlist.
_WATCHLIST_FAMILIES = (
    "drug_instruction",
    "financial_statement",
)


@dataclass(frozen=True)
class RunnerUp:
    """Strongest losing eligible family, with its absolute margin.

    D-A (settled): margin is the RAW count difference against the
    winner — ``voting_kinds`` diff and ``total_count`` diff as plain
    integers. No normalization: absolute counts are auditable.
    """

    family: str
    basis: str  # BASIS_STRONG | BASIS_QUORUM
    margin: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (margin copied, never aliased)."""
        return {
            "family": self.family,
            "basis": self.basis,
            "margin": dict(self.margin),
        }


@dataclass(frozen=True)
class ScenarioDecision:
    """One L2 scenario decision: label + basis + traceable evidence."""

    label: str
    basis: str  # strong | quorum | fallback
    runner_up: RunnerUp | None  # quorum/strong wins: strongest loser
    evidence: list[dict[str, Any]] = field(default_factory=list)


def _counts(facts: dict[str, Any]) -> dict[str, int]:
    """Count snapshot for evidence (no samples, no marker tables)."""
    return {
        "voting_kinds": int(facts.get("voting_kinds", 0)),
        "total_count": int(facts.get("total_count", 0)),
    }


def _strong_eligible(
    families: dict[str, Any], strong_kinds: list[dict[str, Any]]
) -> tuple[dict[str, str], set[str]]:
    """Re-derive strong qualification from the payload quorum table.

    Returns ({family: strongest marker label}, identity families) —
    ELIGIBLE families only; watchlist strong families (e.g.
    drug_instruction's 国药准字 drug-approval numbers) are recorded
    in evidence, not here.
    An entry carrying ``identity: true`` marks a document-identity
    declaration (v3 tie-break input); entries without the flag are
    content conventions (v2-era payloads degrade to all-content,
    which is the previous v2 behavior — deterministic, never a raise).
    """
    strong: dict[str, str] = {}
    identity: set[str] = set()
    for entry in strong_kinds:
        family = entry.get("family")
        marker = entry.get("marker")
        min_count = entry.get("min_count", 1)
        if family not in _ELIGIBLE_FAMILIES or not marker:
            continue
        facts = families.get(family) or {}
        counts = facts.get("marker_counts") or {}
        if counts.get(marker, 0) >= min_count:
            # keep the first (table order) strongest marker per family
            strong.setdefault(family, str(marker))
            if entry.get("identity"):
                identity.add(family)
    return strong, identity


def resolve_scenario(domain_signals: dict[str, Any]) -> ScenarioDecision:
    """Resolve the seven family evidences into a single scenario label.

    Pure function over the ``metadata["domain_signals"]`` payload;
    deterministic; never raises on malformed shapes.
    """
    evidence: list[dict[str, Any]] = []

    families = domain_signals.get("families")
    if not isinstance(families, dict):
        evidence.append({"rule": "input_unrecognized"})
        return ScenarioDecision(
            label=LABEL_GENERAL_PROSE,
            basis=BASIS_FALLBACK,
            runner_up=None,
            evidence=evidence,
        )

    present = {
        name: facts
        for name, facts in families.items()
        if isinstance(facts, dict) and facts.get("present")
    }
    evidence.append(
        {
            "rule": "collect_present_families",
            "families": sorted(present),
            "counts": {name: _counts(facts) for name, facts in sorted(present.items())},
        }
    )

    # Watchlist families present: observation only, no competition.
    watchlist_present = [
        name for name in _WATCHLIST_FAMILIES if name in present
    ]
    if watchlist_present:
        evidence.append(
            {
                "rule": "watchlist_family_observed_not_eligible",
                "families": watchlist_present,
                "counts": {
                    name: _counts(present[name]) for name in watchlist_present
                },
            }
        )

    eligible = {
        name: facts
        for name, facts in present.items()
        if name in _ELIGIBLE_FAMILIES
    }
    if not eligible:
        evidence.append(
            {
                "rule": "fallback_no_eligible_family",
                "eligible_families": [],
            }
        )
        return ScenarioDecision(
            label=LABEL_GENERAL_PROSE,
            basis=BASIS_FALLBACK,
            runner_up=None,
            evidence=evidence,
        )

    # v4 (2026-11-01, explainer_docs evidence): quoted-region
    # withdrawal. When ALL of a family's markers fall inside blocks
    # explicitly framed as "examples", those markers prove "this
    # document explains the format", not "this document IS the
    # format". Such a family exits the competition — no label, no
    # runner_up slot — it was never a legitimate competitor in the
    # first place. Withdrawal requires that ALL markers fall in the
    # quoted region; a single marker in the body region keeps the
    # evidence complete.
    quoted_only = sorted(
        name for name, facts in eligible.items() if facts.get("quoted_only")
    )
    if quoted_only:
        evidence.append(
            {
                "rule": "quoted_only_family_withdrawn",
                "families": quoted_only,
                "quoted_regions": len(
                    domain_signals.get("quoted_sample_regions") or []
                ),
                "reason": "all markers occur inside explicitly delimited "
                "sample blocks; the document quotes the format it describes",
            }
        )
        eligible = {
            name: facts for name, facts in eligible.items() if name not in quoted_only
        }
        if not eligible:
            evidence.append(
                {
                    "rule": "fallback_quoted_sample_only",
                    "eligible_families": [],
                }
            )
            return ScenarioDecision(
                label=LABEL_GENERAL_PROSE,
                basis=BASIS_FALLBACK,
                runner_up=None,
                evidence=evidence,
            )

    # Strong re-derivation from the payload quorum table (no private
    # table imports — single source of truth stays in domain_signals).
    quorum = domain_signals.get("quorum")
    strong_kinds = (
        quorum.get("strong_kinds")
        if isinstance(quorum, dict)
        else None
    ) or []
    strong, identity_strong = _strong_eligible(eligible, strong_kinds)
    evidence.append(
        {
            "rule": "strong_kinds_derived",
            "strong_families": sorted(strong),
            "strong_markers": dict(strong),
            "identity_strong_families": sorted(identity_strong),
            "source": "domain_signals.quorum.strong_kinds",
        }
    )

    # v3 tie-break: identity strong beats content strong (prereg §4
    # rule 2's priority table, first frozen entries 2026-10-15).  A
    # document-identity declaration ("this document IS an RFC") wins
    # over content conventions ("this document QUOTES email headers").
    if (
        len(strong) > 1
        and identity_strong
        and identity_strong != set(strong)
    ):
        evidence.append(
            {
                "rule": "identity_strong_preferred",
                "identity_families": sorted(identity_strong),
                "content_families": sorted(set(strong) - identity_strong),
            }
        )

    if strong:
        pool = {name: eligible[name] for name in strong}
        basis = BASIS_STRONG
    else:
        pool = eligible
        basis = BASIS_QUORUM

    # Rank: identity strong first (v3), then voting_kinds desc,
    # total_count desc, family asc (the last key only ever fires on a
    # full tie — v1 corpus has none).
    ranked = sorted(
        pool.items(),
        key=lambda kv: (
            0 if kv[0] in identity_strong else 1,
            -kv[1].get("voting_kinds", 0),
            -kv[1].get("total_count", 0),
            kv[0],
        ),
    )
    evidence.append(
        {
            "rule": "ranked_by_voting_kinds_then_total_count",
            "basis": basis,
            "order": [name for name, _ in ranked],
            "counts": {name: _counts(facts) for name, facts in ranked},
        }
    )

    winner_name, winner_facts = ranked[0]
    winner_counts = _counts(winner_facts)

    # Runner-up comes from ALL eligible losers — not just the same
    # basis pool (framework prereg §1.3 rule 6). The RFC overlap
    # case is exactly where this matters: academic_paper may carry
    # MORE voting kinds than the strong tech_standard winner and
    # still lose on specificity — the negative margin is the
    # auditable fact that a count-stronger family was overridden.
    losers = [
        (name, facts)
        for name, facts in eligible.items()
        if name != winner_name
    ]
    losers.sort(
        key=lambda kv: (
            0 if kv[0] in identity_strong else (1 if kv[0] in strong else 2),
            -kv[1].get("voting_kinds", 0),
            -kv[1].get("total_count", 0),
            kv[0],
        )
    )
    runner_up: RunnerUp | None = None
    if losers:
        loser_name, loser_facts = losers[0]
        loser_counts = _counts(loser_facts)
        # Full-tie honesty (counts equal to the winner): filed in
        # evidence, never a silent pick.
        if loser_counts == winner_counts:
            evidence.append(
                {
                    "rule": "tie_broken_deterministically",
                    "tied": sorted([winner_name, loser_name]),
                    "tie_break": (
                        "identity_strong_then_voting_kinds"
                        "_then_family_name_asc"
                    ),
                }
            )
        runner_up = RunnerUp(
            family=loser_name,
            basis=(
                BASIS_STRONG if loser_name in strong else BASIS_QUORUM
            ),
            margin={
                "voting_kinds": (
                    winner_counts["voting_kinds"]
                    - loser_counts["voting_kinds"]
                ),
                "total_count": (
                    winner_counts["total_count"]
                    - loser_counts["total_count"]
                ),
            },
        )
        evidence.append(
            {
                "rule": "runner_up_recorded",
                "runner_up": runner_up.to_dict(),
            }
        )

    return ScenarioDecision(
        label=winner_name,
        basis=basis,
        runner_up=runner_up,
        evidence=evidence,
    )


def decision_to_dict(decision: ScenarioDecision) -> dict[str, Any]:
    """JSON-safe projection for ``metadata["scenario"]`` (D-D: the
    extractor version is part of the contract — resolver iterations
    coexist, old labels are never invalidated in place)."""
    return {
        "extractor": "scenario_resolver_v8",
        "label": decision.label,
        "basis": decision.basis,
        "runner_up": (
            decision.runner_up.to_dict()
            if decision.runner_up is not None
            else None
        ),
        "evidence": decision.evidence,
    }


def is_close_margin_label(scenario: Any) -> bool:
    """Single-source low-confidence verdict over a scenario payload.

    scenario_low_confidence_governance_prereg_v1 §2.0. True ⟺ the
    payload is a dict carrying a non-None label that is neither the
    fallback (``general_prose``) nor margin-free, and the runner-up
    ``voting_kinds`` margin is an int <=
    ``SCENARIO_CLOSE_MARGIN_THRESHOLD``. Malformed payloads and
    ``runner_up=None`` (no opponent / incomparable) return False —
    degradation is itself a judgment, so "cannot compare" never
    triggers it (conservative no-op). Defensive: never raises.

    Consumers (keep this THE single source): rse_report confidence
    cap (§2.1), scenario chunking behavioral fallback (§2.2), and
    scenario_quality_batch's runner_up_close_margin audit list.
    """
    if not isinstance(scenario, dict):
        return False
    label = scenario.get("label")
    if not isinstance(label, str) or not label:
        return False
    if label == LABEL_GENERAL_PROSE:
        return False
    runner_up = scenario.get("runner_up")
    if not isinstance(runner_up, dict):
        return False
    margin = runner_up.get("margin")
    if not isinstance(margin, dict):
        return False
    diff = margin.get("voting_kinds")
    return (
        isinstance(diff, int)
        and diff <= SCENARIO_CLOSE_MARGIN_THRESHOLD
    )


__all__ = [
    "BASIS_FALLBACK",
    "BASIS_QUORUM",
    "BASIS_STRONG",
    "LABEL_ACADEMIC_PAPER",
    "LABEL_ANNOUNCEMENT",
    "LABEL_CONTRACT",
    "LABEL_EMAIL",
    "LABEL_EXAM",
    "LABEL_GENERAL_PROSE",
    "LABEL_INVOICE",
    "LABEL_TECH_STANDARD",
    "RunnerUp",
    "SCENARIO_CLOSE_MARGIN_THRESHOLD",
    "ScenarioDecision",
    "decision_to_dict",
    "resolve_scenario",
]
