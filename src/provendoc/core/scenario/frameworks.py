"""Scenario framework registry (L3) — deterministic label → profile map.

Implements scenario_framework_prereg_v1 §2 (decision D-B settled: module
lives in ``core/scenario/``; domain = format, scenario = semantics). Each
framework carries NO logic, only SELECTIONS — every field is a named
reference to an already-existing strategy/layer, so adding a scenario
= registering one row + landing its profile, never pipeline edits.

Decision (land immediately, not gated on the freeze): the
registry lands now ahead of the prereg freeze; the prereg's §2.3
placeholder table and this registry must be reconciled at freeze time.

Two independent gates (prereg §2.2):

* ``FRAMEWORKS`` lookup — a label MISSING from the registry falls
  back to the ``general_prose`` framework (framework fallback);
* watchlist families never emit labels in the first place (label
  fallback lives in the resolver) — so even a misfired watchlist
  label would land on the general framework, not a scenario chain.

Honest boundaries:

* the registry is a pure dict — no plugin loading, no runtime
  mutation, frozen import order (family-name asc) for determinism;
* ``rse_profile`` references the invariant-set names of
  ``intelligence/rse/scenario_invariants.py``; a name with no
  invariant set means "no additional invariants" (fallback is never
  penalized);
* ``scenario_chain`` is the metadata key the scenario chain (if any)
  stamps its ledger under — empty string means attach-only (no
  scenario chain runs for that label yet; the label is still
  recorded for audit).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from provendoc.core.scenario.resolver import (
LABEL_ACADEMIC_PAPER,
LABEL_ANNOUNCEMENT,
LABEL_CONTRACT,
LABEL_EMAIL,
LABEL_EXAM,
LABEL_GENERAL_PROSE,
LABEL_GOVERNMENT_DOCUMENT,
LABEL_INVOICE,
LABEL_MEDICAL_REPORT,
LABEL_TECH_STANDARD,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScenarioFramework:
    """One scenario's frozen selection of existing strategies.

    parser_profile:        L1/L3 normalization strategy name
    cleaning_profile:      scenario-era cleaning level (a named
                           CleaningLevel, never a new rule set)
    representation_profile: representation/chunk strategy name (an
                           existing strategy_optimizer candidate)
    rse_profile:           scenario invariant-set name (§3.2; empty
                           string = no additional invariants)
    scenario_chain:        metadata key the scenario chain stamps
                           ("" = no chain yet, label still audited)
    notes:                 known boundaries, human-readable
    """

    parser_profile: str
    cleaning_profile: str
    representation_profile: str
    rse_profile: str
    scenario_chain: str
    notes: str


FRAMEWORKS: dict[str, ScenarioFramework] = {
    # invoice: the fully-landed chain (A–E) + PDF KIE field
    # extraction; chunking is line-boundary-only with amount-line
    # atomicity (see invoice_chunking / scenario_invariants).
    LABEL_INVOICE: ScenarioFramework(
        parser_profile="kie_invoice_v1",
        cleaning_profile="standard",
        representation_profile="line_boundary_v1",
        rse_profile="invoice_v1",
        scenario_chain="invoice_scenario",
        notes=(
            "chain landed (salvage→anchors→arbitration→amount check→"
            "line items→line-boundary chunking); DQR surfaces the "
            "ledger verbatim, never scored"
        ),
    ),
    # academic_paper: v1.8 — real-corpus audit fixes (prereg
    # academic_paper_math_regions_prereg_v1 D8f/D8g): parameter-
    # delimiter alias defs (\def\ba#1\ea{\begin{align}...})
    # registered as begin/end pairs with cross-env close, and
    # whitespace tolerated between alias name and def body;
    # v1.7 — structure-fact ledger PLUS chunk
    # integration (formula blocks and References entries atomic,
    # academic_paper_chunking, prereg
    # academic_paper_chunk_atomicity_prereg_v1) PLUS shape-guarded
    # section-heading keyword locators (Chinese 目录/引言/结论/致谢/
    # 附录/正文 headings, prereg
    # academic_paper_section_headings_prereg_v1) PLUS
    # whole-region display math and special-character census
    # (prereg academic_paper_math_regions_prereg_v1); the math
    # scan is comment-aware (% to EOL), $-stateful ($$ only
    # counts outside inline math) and skips macro-definition
    # bodies (brace-depth tracked, blank-line bounded); a
    # post-References appendix heading ends the references region.
    LABEL_ACADEMIC_PAPER: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="heading_aware_v1",
        rse_profile="academic_paper_v1",
        scenario_chain="academic_paper_scenario",
        notes=(
            "v1.8 = real-corpus audit fixes: parameter-delimiter "
            "alias defs (\\def\\ba#1\\ea{\\begin{align}#1\\end{align}}) "
            "registered as begin/end pairs — the delimiter token "
            "closes any open display region (1701.00528's \\bea/\\ea "
            "mixed convention); whitespace between alias name and "
            "def body tolerated (1801.00728); "
            "v1.7 = whole-region display math (equation/align/"
            "gather/multline/eqnarray environment families, \\[..\\] "
            "and $$ blocks — interior lines protected, chunker fed "
            "the uncapped list; comment-aware, $-stateful and "
            "macro-definition-body-guarded against phantom regions; "
            "environment alias pairs (\\bea/\\eea, the HEP-template "
            "convention) tracked as regions) + TeX special-character "
            "census "
            "(sub/superscript, symbol and accent line counts); "
            "v1.6 = structure-fact ledger + atomic chunks (formula "
            "blocks and References entries never split; shared "
            "check_chunk_invariants verdict in the ledger) + "
            "shape-guarded section-heading keyword locators; a "
            "post-References appendix heading ends the references "
            "region; chunk-level semantics beyond atomicity is v2"
        ),
    ),
    # tech_standard: v1 chain landed 2026-11-10 — a
    # structure-fact LEDGER like the email chain: clause-numbering
    # hierarchy (flush-left numbered headings, per-depth census,
    # Appendix X. headings, TOC entries counted separately) + RFC
    # 2119 keyword census (ten UPPERCASE requirement levels with
    # bounded samples and citation count). RFC header identity
    # facts already live in domain_signals (identification layer
    # stays calibration-locked, T0 independence).
    LABEL_TECH_STANDARD: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="clause_aware_v1",
        rse_profile="",
        scenario_chain="tech_standard_scenario",
        notes="v1 = structure-fact ledger (T1 clause numbering with "
              "depth census + Appendix + TOC separation, T2 RFC 2119 "
              "keyword census); shape-based clause ledger (numbered "
              "list lines are declared-limitation clauses); no "
              "clause-aware chunking in v1; marker family stays "
              "calibration-frozen",
    ),
    # email: v1 chain landed 2026-10-18 (prereg
    # specs/email_scenario_prereg_v1_draft.md) — a structure-fact
    # LEDGER like the academic chain: header fields, boundary,
    # quote blocks, signature and attachments located as line
    # coordinates; chunks keep header/quote/signature blocks
    # atomic. The identification layer stays untouched (marker
    # family uncalibrated-locked, E0 independence).
    LABEL_EMAIL: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="email_scenario",
        notes="v1 = structure-fact ledger (E1 header fields with "
              "folds, E2 boundary, E3 quote blocks + attribution "
              "hats, E4 RFC 3676 signature, E5 attachments + MIME "
              "boundaries) + atomic chunking; no MIME decoding, no "
              "thread-tree reconstruction, no spam/sender "
              "classification; marker family stays calibration-frozen",
    ),
    # exam: v6 identification layer landed 2026-11-08
    # (domain_signals v6 marker, resolver v5 eligible) — label
    # recorded for audit; question-type structure facts (quorum,
    # question-type families, subitem letters, points) are the
    # first candidates when the chain lands.
    LABEL_EXAM: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="label-only for now; v6 marker + resolver eligible "
              "(2026-11-08); no extraction chain yet — question-type "
              "structure ledger is the first candidate",
    ),
    # government_document: v1 chain landed 2026-11-14 — the
    # article-level structure ledger that the p8 batch registered
    # as the first chain candidate: G1 条文层级 (章/条/cn-section
    # lines, verbatim numerals, parsed ordinals, gist brackets,
    # sequence anomaly markers) + G2 公文块 (gazette numbers,
    # main-recipient lines, preamble openings, drafting-statement
    # headings, consultative-draft annotations, effective
    # clauses). Identification layer stays calibration-locked
    # (G0 independence).
    LABEL_GOVERNMENT_DOCUMENT: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="label-only for now; v7 marker + resolver v6 eligible "
              "(2026-11-11, samr 376-file calibration); no extraction "
              "chain yet — chapter/article hierarchy ledger is the "
              "first candidate",
    ),
    # announcement: v1 label + convention observation (特此公告
    # closing markers etc.); no chain yet.
    LABEL_ANNOUNCEMENT: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="announcement conventions observed by domain_signals_v2; "
              "no extraction chain yet",
    ),
    # contract: v8 identification layer landed 2026-11-11
    # (domain_signals v8 insurance marker table, resolver v7
    # eligible; iachina 44-clause GT bucket + negative-bucket
    # calibration) — label recorded for audit; clause-level
    # structure facts (第X条 numbering, 责任免除/犹豫期
    # sections, 投保人/被保险人 party blocks) are the first
    # candidates when the chain lands.
    LABEL_CONTRACT: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="label-only for now; v8 marker + resolver v7 eligible "
              "(2026-11-11, iachina 44-clause GT + negative-bucket "
              "calibration); no extraction chain yet — clause "
              "hierarchy ledger is the first candidate",
    ),
    # medical_report: v9 identification layer landed 2026-11-13
    # (domain_signals v9 English lab-report markers, resolver v8
    # eligible; Mendeley 260-scan GT, image carrier — the first
    # image-carrier GT, stress-testing the OCR front end) — label
    # recorded for audit; report-level structure facts (test/result
    # table rows, reference-range columns, abnormal flags) are the
    # first candidates when the chain lands.
    LABEL_MEDICAL_REPORT: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="label-only for now; v9 marker + resolver v8 eligible "
              "(2026-11-13, Mendeley 260-scan GT, image carrier); no "
              "extraction chain yet — test/result table ledger is the "
              "first candidate",
    ),
    # general_prose: the fallback framework — deliberately carries
    # nothing; the fallback label is a legal outcome, never a
    # penalty (no invariants: penalizing fallback would invite
    # score-gaming by tuning, per prereg §3.2).
    LABEL_GENERAL_PROSE: ScenarioFramework(
        parser_profile="narrative_default",
        cleaning_profile="standard",
        representation_profile="narrative_default",
        rse_profile="",
        scenario_chain="",
        notes="fallback framework; no additional invariants by design",
    ),
}


def get_framework(label: str) -> ScenarioFramework:
    """Deterministic label → framework lookup with general-prose fallback.

    An unknown label (including any watchlist family that might
    misfire a label someday) lands on the general framework — the
    framework fallback and the label fallback are two independent
    gates (prereg §2.2). Pure function; never raises.
    """
    return FRAMEWORKS.get(label, FRAMEWORKS[LABEL_GENERAL_PROSE])


def attach_scenario_chain(document: Any) -> None:
    """Attach the registered scenario chain's ledger, if any.

    Replaces the hardcoded ``if label == "invoice"`` dispatch in
    ``universal_parser`` (the registry is now the single dispatch
    point). Dispatch discipline:

    * unknown chain key / no chain registered → no-op (absence is
      not failure);
    * chain modules are imported lazily so a missing optional
      module degrades to a debug log, never a parse failure (the
      same silent-degradation discipline as provenance /
      doc_identity);
    * the chain stamp is attach-only — document text fields are
      never touched.
    """
    label = ""
    scenario = getattr(document, "metadata", {}).get("scenario")
    if isinstance(scenario, dict):
        label = str(scenario.get("label", ""))
    framework = get_framework(label)
    if not framework.scenario_chain:
        return
    if framework.scenario_chain == "invoice_scenario":
        from provendoc.core.scenario.invoice_scenario_dqr import (
            attach_invoice_scenario,
        )
        attach_invoice_scenario(document)
    elif framework.scenario_chain == "academic_paper_scenario":
        from provendoc.core.scenario.academic_paper_scenario_dqr import (
            attach_academic_paper_scenario,
        )
        attach_academic_paper_scenario(document)
    elif framework.scenario_chain == "email_scenario":
        from provendoc.core.scenario.email_scenario_dqr import (
            attach_email_scenario,
        )
        attach_email_scenario(document)
    elif framework.scenario_chain == "tech_standard_scenario":
        from provendoc.core.scenario.tech_standard_scenario_dqr import (
            attach_tech_standard_scenario,
        )
        attach_tech_standard_scenario(document)
    # any other registered chain key is a registry/code mismatch —
    # recorded honestly by leaving the metadata absent (absence is
    # not failure), never a silent wrong stamp.


def stamp_scenario(document, *, include_understanding: bool = False) -> None:
    """Unified L2 scenario stamping (scenario_framework prereg §1,
    decision D-C).

    When domain_signals has already been stamped by the narrative
    parser, it is resolved to a single label → metadata["scenario"],
    and the L3 registry then dispatches the scenario chain
    (attach_scenario_chain). Non-narrative domains / parsers that
    stamp no domain_signals → no scenario key (absence is not
    failure). Failures degrade silently (auditable via debug logs),
    the same discipline as provenance / doc_identity.

    2026-10-15 wiring fix: SDK ``client.parse`` and
    ``UniversalParser.parse`` are two parallel pipelines; the
    scenario stamp previously existed only in the latter — real
    invoice samples parsed via the SDK were missing the scenario
    key (unit tests covered the universal path, so they stayed
    green). This function is the single source of truth: both
    paths must call it; idempotent (repeated calls re-resolve the
    same domain_signals with deterministically identical
    results).

    2026-11-08 wiring: the L2 soft view (scenario_view, the first
    slice of the 2026-11-04 three-layer soft-semantics direction)
    was previously referenced only by its own tests and sat on no
    runtime path. It is now stamped alongside the hard decision
    as ``metadata["scenario_view"]`` — a read-only projection of
    the same ``(domain_signals, decision)``, additive-only (no
    domain_signals means no key, clean zero-diff); consumers can
    ignore it entirely.

    2026-11-08 wiring (task-layer on-demand slice): when
    ``include_understanding=True``, the L2 understanding-layer
    candidate-field IR is appended as
    ``metadata["scenario_understanding"]`` (see
    ``_stamp_understanding``). Off by default = clean zero-diff;
    the understanding layer scans the full text for evidence, and
    that cost is paid only by the explicit requester (11-04
    direction: the task layer enables scenario structure on
    demand).
    """
    try:
        from provendoc.core.scenario.resolver import (
            decision_to_dict,
            resolve_scenario,
        )

        signals = getattr(document, "metadata", {}).get("domain_signals")
        if isinstance(signals, dict):
            decision = resolve_scenario(signals)
            document.metadata["scenario"] = decision_to_dict(decision)
            from provendoc.core.scenario.view import scenario_view

            document.metadata["scenario_view"] = scenario_view(
                signals, decision
            ).to_dict()
        attach_scenario_chain(document)
    except Exception as exc:
        from provendoc.core.pipeline.log_hygiene import sanitize_log_value
        from provendoc.core.pipeline.pipeline_stamps import (
            record_degradation,
        )

        record_degradation(document, "scenario", exc)
        _log.debug(
            "Scenario resolver failed for %s",
            sanitize_log_value(
                getattr(document, "metadata", {}).get("file_path", "<unknown>")
            ),
            exc_info=True,
        )
    if include_understanding:
        _stamp_understanding(document)


def _stamp_understanding(document) -> None:
    """Task-layer opt-in understanding stamp — the understanding-layer
    slice of the 11-04 three-layer direction.

    Calls ``interpret_document(text, filename)``; when it yields a
    result it lands as ``metadata["scenario_understanding"]``
    (a dict projection of detection + candidate-field IR).
    Discipline:

    * absence is not failure: the understanding layer carries its
      own evidence threshold — with no scenario evidence it
      returns None and the key never lands (same convention as
      the scenario key);
    * independent of the hard decision: the understanding layer
      uses its own score-based detectors (recognizing only the
      invoice / academic_paper / order skeletons) and does not
      reuse ``resolve_scenario``'s labels — the two decision
      layers are deliberately independent calibration views;
      disagreement is not a bug;
    * failures degrade silently, and the degradation event is
      registered under the separate
      ``scenario_understanding`` component name, keeping the
      scenario ledger unpolluted.
    """
    try:
        from provendoc.core.scenario.understanding import interpret_document

        metadata = getattr(document, "metadata", None)
        if not isinstance(metadata, dict):
            return
        text = getattr(document, "text", None) or ""
        file_path = metadata.get("file_path")
        filename = (
            os.path.basename(file_path) if isinstance(file_path, str) else None
        )
        interpretation = interpret_document(text, filename)
        if interpretation is not None:
            metadata["scenario_understanding"] = interpretation.to_dict()
    except Exception as exc:  # noqa: BLE001 — 观测盖章绝不拖垮解析
        from provendoc.core.pipeline.pipeline_stamps import (
            record_degradation,
        )

        record_degradation(document, "scenario_understanding", exc)
        _log.debug("Scenario understanding stamp failed", exc_info=True)


__all__ = [
    "FRAMEWORKS",
    "ScenarioFramework",
    "attach_scenario_chain",
    "get_framework",
    "stamp_scenario",
]
