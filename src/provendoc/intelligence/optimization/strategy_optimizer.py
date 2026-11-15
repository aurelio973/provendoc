"""Strategy Optimizer — deterministic policy search for optimal chunking.

This is the "strategy optimization layer" — the intelligence core
that closes the loop between chunker, quality evaluation, and
strategy policies.

Architecture:

    document
        |
    profile --> baseline strategy (profile-first)
        |
    controlled candidates --> chunker --> evaluation --> objective
        |                                  + failure benchmark
        |
    hard-constraint gate (violations disqualify)
        |
    rank (canonical quality composite + penalties) --> best policy + trials history

Design principles (2026-09-07 decision, open question 3):
1. The chunker code NEVER changes. Only policies do.
2. POLICY-SPACE search, not parameter-space. The behavioral levers
   are boundary strategy, integrity rules, and context propagation
   — chunk_size/overlap alone produce near-identical strategies
   (proven by the cqi_validity experiment) and are no longer searched.
3. Profile-first, no full-space search. Each profile declares a
   baseline strategy; candidates are an ENUMERATED neighborhood of
   that baseline (controlled boundaries × adjacent scale tiers ×
   merge toggle), never a continuous or exhaustive search.
4. Hard constraints REJECT, they do not discount. A candidate that
   splits a protected block, crosses unrelated sections, exceeds
   the hard limit, or falls below a profile structural threshold is
   disqualified outright. Cross-sheet merging without evidence is
   N/A for narrative/config domains and enforced once the table
   domain joins the optimizer.
5. Lexicographic total order over candidates (2026-09-11,
   re_migration_plan §3/§5 step 4 — replaces the additive
   objective; a high composite can no longer "buy off"
   structural damage):
       (a) disqualified (hard-constraint violations, HC1-5)
       (b) gate rank (gate_v1 three states: passed < warned < blocked;
           not_evaluated ranks with blocked — no failure data
           cannot be rewarded)
       (c) failure rate (severity-weighted confirmed instances,
           chunk-normalized, UN-capped)
       (d) quality composite DESC (invariant_v1 canonical:
           0.8*invariant_composite + 0.2*context_self_containment)
       (e) advisory preference DESC (profile-baseline bonus)
       (f) complexity penalty ASC (per lever deviating).
   The retired additive terms: HARD_FAILURE_PENALTY_CAP (a cap
   let high-scoring candidates buy off critical damage) and the
   profile prioritize 1.5x re-weighting (retired with the legacy
   canonical; min_thresholds remain hard constraints, the
   baseline anchoring / advisory / complexity terms are kept).
6. Deterministic search — no RL, no LLM, no adaptive.
7. Quality evaluation is a diagnostic, not a semantic authority.
   The optimizer proposes; humans decide between runs
   (cold-path discipline).

Module organization (2026-09 split, verbatim method moves):
- strategy_optimizer_profiles     OptimizationProfile, search-space
                                  constants, policy_spec /
                                  spec_to_policy
- strategy_optimizer_search       controlled / grid / hierarchical
                                  search + _evaluate_strategy
- strategy_optimizer_evaluation   baseline evaluation, hard
                                  constraints, lexicographic
                                  objective, output builders
- strategy_optimizer (this module)  StrategyOptimizer facade:
                                  parser mapping, public API
                                  (optimize / optimize_document),
                                  re-exports for import stability
"""

from pathlib import Path

from provendoc.core.parsers.config.toml_parser import TomlParser
from provendoc.core.parsers.config.yaml_parser import YamlParser
from provendoc.core.parsers.narrative.markdown_parser import MarkdownParser
from provendoc.core.parsers.narrative.txt_parser import TxtParser
from provendoc.core.pipeline.representation_registry import (
    representation_factory,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
)
from provendoc.integrity.failure_modes import (
    RetrievalFailureBenchmark,
)
from provendoc.intelligence.diagnostics.document_quality import DocumentQualityReport
from provendoc.intelligence.optimization.strategy_optimizer_evaluation import (
    StrategyOptimizerEvaluationMixin,
)
from provendoc.intelligence.optimization.strategy_optimizer_profiles import (
    ADVISORY_PREFERENCE_BONUS,
    BOUNDARY_OPTIONS,
    COMPLEXITY_PENALTY_PER_LEVER,
    CROSS_SECTION_MODES,
    DEFAULT_STRATEGY_SPACE,
    HARD_FAILURE_SEVERITY_WEIGHTS,
    HIERARCHICAL_PHASES,
    PROTECTED_BLOCK_MODES,
    SCALE_ORDER,
    SCALE_TIERS,
    OptimizationProfile,
    policy_spec,
    spec_to_policy,
)
from provendoc.intelligence.optimization.strategy_optimizer_search import (
    StrategyOptimizerSearchMixin,
)
from provendoc.intelligence.rse.narrative_quality import NarrativeQuality
from provendoc.intelligence.rse.rse_report import canonical_quality

__all__ = [
    "StrategyOptimizer",
    "OptimizationProfile",
    "policy_spec",
    "spec_to_policy",
    "BOUNDARY_OPTIONS",
    "SCALE_TIERS",
    "SCALE_ORDER",
    "DEFAULT_STRATEGY_SPACE",
    "HIERARCHICAL_PHASES",
    "PROTECTED_BLOCK_MODES",
    "CROSS_SECTION_MODES",
    "HARD_FAILURE_SEVERITY_WEIGHTS",
    "ADVISORY_PREFERENCE_BONUS",
    "COMPLEXITY_PENALTY_PER_LEVER",
]


# ==================================
# StrategyOptimizer
# ==================================


class StrategyOptimizer(
    StrategyOptimizerSearchMixin,
    StrategyOptimizerEvaluationMixin,
):
    """Deterministic policy search for optimal chunking strategy.

    Usage:
        optimizer = StrategyOptimizer()

        # Simple — auto profile, controlled search (profile-first
        # enumerated neighborhood + hard constraints)
        result = optimizer.optimize("./data/doc.txt")

        # Advanced — custom profile, narrowed controlled space
        result = optimizer.optimize(
            "./data/doc.md",
            strategy_space={
                "boundary": ["discourse_unit", "heading_boundary"],
                "scale": ["medium", "large"],
            },
            profile=OptimizationProfile.markdown(),
        )

        # Full Cartesian product (opt-in experimental mode)
        result = optimizer.optimize("./data/doc.md", mode="grid")

        print(result["best_strategy"])
        # -> {"boundary": "heading_boundary", "scale": "medium", ...}
        print(result["best_score"])
        print(result["objective_components"])
        print(len(result["trials"]))
    """

    # ==================================
    # parser / representation mapping
    # ==================================

    # Note: .html/.htm intentionally absent — the domain registry routes
    # HTML to the code domain (tolerant AST/component parsing), so the
    # narrative optimizer never handles it.
    _PARSER_MAP = {
        ".txt": TxtParser,
        ".md": MarkdownParser,
        ".markdown": MarkdownParser,
        # markdown-family unification (markdown_family_routing_prereg_v1 F1)
        ".mdown": MarkdownParser,
        ".mkdn": MarkdownParser,
        ".mdwn": MarkdownParser,
        ".mdx": MarkdownParser,
        ".rmd": MarkdownParser,
        ".qmd": MarkdownParser,
        ".toml": TomlParser,
        ".yaml": YamlParser,
        ".yml": YamlParser,
    }

    # The single source of truth for suffix → representation has moved to
    # core.pipeline.representation_registry
    # (pipeline_consolidation_prereg_v1 D1) — the old _REPR_MAP copy
    # (including the ppt CFB/odp family deferred-backfill notes) was retired with this batch;
    # read sites obtain a factory with the unified strategy= signature via
    # representation_factory(suffix); unregistered suffixes fall back to
    # TxtNarrativeRepresentation (same semantics as the old .get default).

    # ==================================
    # early exit threshold
    # ==================================
    # Documents shorter than this are not worth
    # optimizing — no parameter combination can
    # meaningfully improve chunking on a document
    # this small. The optimizer should skip and
    # return the default strategy as-is.
    # ==================================

    _EARLY_EXIT_THRESHOLD = 150  # chars

    # ==================================
    # initialization
    # ==================================

    def __init__(self, evaluator=None, failure_benchmark=None):
        """Injects the evaluator and the failure benchmark (default implementations)."""
        self.evaluator = evaluator or NarrativeQuality()
        self._doc_quality = DocumentQualityReport()
        self._failure_benchmark = (
            failure_benchmark or RetrievalFailureBenchmark()
        )

    # ==================================
    # public API
    # ==================================

    def optimize(
        self,
        file_path,
        strategy_space=None,
        profile=None,
        mode="controlled",
        custom_phases=None,
    ):
        """Run deterministic policy search.

        Args:
            file_path: path to the file to optimize for. The parsing
                facade only supports the suffixes wired in
                ``_PARSER_MAP`` (txt / markdown-family / yaml / yml /
                toml); any other suffix — code, structured, or a
                narrative-family suffix whose parser is not wired
                here — returns an honest ``not_applicable`` result
                instead of silently mis-parsing the file as plain
                text (optimizer_code_bypass_prereg_v1 P1, D1=B).
                Pre-built Documents go through
                :meth:`optimize_document`.
            strategy_space: dict of policy knob -> list of values
                {"boundary": ["discourse_unit", "heading_boundary"],
                 "scale": ["medium", "large"]}
                In controlled mode this NARROWS the profile-anchored
                space; in grid mode it overrides DEFAULT_STRATEGY_SPACE.
            profile: OptimizationProfile for document-type-aware
                scoring. If None, uses auto-detect from extension.
            mode: "controlled" (default — profile-first enumerated
                neighborhood + hard constraints),
                "grid" (full Cartesian product, opt-in), or
                "hierarchical" (phase-by-phase greedy, opt-in)
            custom_phases: list of phase dicts for hierarchical mode
                [{"param": "boundary", "values": [...]}]
                Overrides HIERARCHICAL_PHASES.

        Returns:
            dict (OptimizationResult):
                file: input file path
                profile: profile name used
                mode: search mode used
                best_strategy: winning policy spec dict
                    {boundary, scale, structural_relations,
                     soft_limit, hard_limit}; None = no candidate qualifies
                    below the quality floor, so keep the default baseline (best_score reports
                    the baseline score, optimized_output is None, sanity_flags explains)
                best_score: objective score (float); the adoption contract guarantees
                    best_score >= baseline_score (quality floor,
                    effective since 2026-10-03) — improvement is always non-negative
                best_evaluation: full evaluation result for winner
                objective_components: canonical / penalty / advisory /
                    complexity breakdown for the winner
                disqualified_count: candidates rejected by hard
                    constraints
                hard_constraints: the constraint set applied
                baseline_score: objective score of the default policy
                better_than_baseline: qualified strategies that beat
                    the baseline
                trials: list of {strategy, score, delta_vs_baseline,
                    disqualified, violations} sorted by score desc
                    (qualified first)
                total_tested: number of unique configurations tested
                phases_trace: list of phase results (hierarchical only)
                sanity_flags: list of warnings if best strategy
                    deviates suspiciously from baseline
                optimized_output: dict with the actual chunks
                    built using the best strategy, plus a
                    baseline comparison (default vs optimized).
        """
        # resolve profile
        if profile is None:
            profile = OptimizationProfile.auto(file_path)

        # ==================================
        # domain guard (optimizer_code_bypass_prereg_v1 P1, D1=B)
        # ==================================
        # The parsing facade only supports _PARSER_MAP suffixes.
        # Everything else used to fall through to a silent TxtParser
        # fallback — corpus evidence (2026-10-25): optimize() on .py
        # files parsed code as plain text and emitted confident-
        # looking wrong-domain recommendations; provendoc_run fed
        # unfiltered file sets into this path. Refuse honestly
        # instead (early_exit-shaped result, no exception).
        # ==================================
        suffix = Path(file_path).suffix.lower()
        if suffix not in self._PARSER_MAP:
            return self._not_applicable_result(
                file_path, profile, suffix,
            )

        # parse document once (strategy-independent)
        parser_cls = self._PARSER_MAP[suffix]
        parser = parser_cls()
        document = parser.parse(file_path)

        return self.optimize_document(
            document, suffix,
            strategy_space=strategy_space,
            profile=profile,
            mode=mode,
            custom_phases=custom_phases,
            file_label=str(file_path),
        )

    def _not_applicable_result(self, file_path, profile, suffix):
        """Honest refusal for suffixes the parsing facade cannot parse.

        optimizer_code_bypass_prereg_v1 P1 (D1=B, 2026-10-25):
        mirrors the ``_early_exit_result`` key contract so consumers
        see one structure — same keys, mode ``not_applicable``, no
        search, no exception (callers' try/except flows unaffected).
        ``best_strategy`` is None: no narrative baseline is
        meaningful for a file this facade cannot parse; the reason
        and the optimize_document pointer ride in sanity_flags.
        """
        try:
            from provendoc.core.domain.domain_registry import (
                classify_domain,
            )

            domain = classify_domain(suffix)
        except Exception:
            domain = "unknown"
        return {
            "file": str(file_path),
            "profile": profile.name,
            "mode": "not_applicable",
            "best_strategy": None,
            "best_score": 0.0,
            "best_evaluation": None,
            "objective_components": None,
            "disqualified_count": 0,
            "hard_constraints": {
                "note": (
                    "optimization not applicable — suffix not "
                    "supported by the optimizer parsing facade"
                ),
                "min_thresholds": profile.min_thresholds,
            },
            "baseline_score": 0.0,
            "better_than_baseline": [],
            "trials": [],
            "total_tested": 0,
            "phases_trace": None,
            "sanity_flags": [
                f"suffix '{suffix}' ({domain} domain) is not wired "
                f"into the optimizer parsing facade — running it "
                f"through the narrative optimizer would silently "
                f"mis-parse the file as plain text (TxtParser "
                f"fallback); the {domain} domain's representation "
                f"is size-parameter-independent by design; "
                f"pre-built Documents go through "
                f"optimize_document()",
            ],
            "optimized_output": None,
        }

    # ==================================
    # optimize_document — core search logic
    # ==================================
    #
    # Accepts a pre-built Document instead of a file_path.
    # This lets chunk() pass its synthetic Document directly
    # without writing to disk.
    # ==================================

    def optimize_document(
        self,
        document,
        suffix,
        strategy_space=None,
        profile=None,
        mode="controlled",
        custom_phases=None,
        file_label="(direct_input)",
    ):
        """Run deterministic parameter search on a pre-built Document.

        Same logic as optimize(), but skips file parsing — the
        caller provides the Document directly.

        Args:
            document: pre-built Document object
            suffix: file type suffix (".txt", ".md", ".yaml")
            strategy_space: param -> list of values
                (narrows the profile-anchored space in controlled
                mode; overrides DEFAULT_STRATEGY_SPACE in grid mode)
            profile: OptimizationProfile (if None, auto from suffix)
            mode: "controlled" (default), "grid", or "hierarchical"
            custom_phases: phase dicts (hierarchical mode only)
            file_label: label for the "file" field in result

        Returns:
            same dict as optimize()
        """

        # resolve profile
        if profile is None:
            if suffix in (
                ".md", ".markdown",
                ".mdown", ".mkdn", ".mdwn", ".mdx", ".rmd", ".qmd",
            ):
                profile = OptimizationProfile.markdown()
            elif suffix in (".yaml", ".yml", ".toml"):
                profile = OptimizationProfile.plain_text()
            else:
                profile = OptimizationProfile.plain_text()

        # document quality (for evaluation confidence)
        dqr = self._doc_quality.evaluate(document)

        # ==================================
        # early exit: document too short for optimization
        # ==================================
        # If the total document text is below the threshold,
        # no parameter combination can meaningfully improve
        # chunking. Return the default strategy with a flag.
        # ==================================
        total_text_length = len(document.text) if hasattr(
            document, "text"
        ) else sum(
            len(p) for p in getattr(document, "paragraphs", [])
        )

        if total_text_length < self._EARLY_EXIT_THRESHOLD:
            return self._early_exit_result(
                document, dqr, profile, suffix,
                file_label, total_text_length,
            )

        # Representation factory: representation_registry is the single source of truth
        # (pipeline_consolidation_prereg_v1 D1); unregistered suffixes fall back to Txt
        # (same semantics as the old _REPR_MAP.get default). Factories share the unified
        # strategy= call signature; representation classes that do not accept strategy are adapted by the registry.
        repr_cls = representation_factory(suffix) or TxtNarrativeRepresentation

        # run search
        if mode == "hierarchical":
            results, phases_trace = self._hierarchical_search(
                document, dqr, repr_cls, suffix,
                profile, custom_phases,
            )
        elif mode == "controlled":
            results, phases_trace = self._controlled_search(
                document, dqr, repr_cls, suffix,
                profile, strategy_space,
            ), None
        else:
            results, phases_trace = self._grid_search(
                document, dqr, repr_cls, suffix,
                profile, strategy_space,
            ), None

        # rank: qualified first, then the lexicographic total order
        # (gate rank → failure rate → quality → advisory → complexity)
        results.sort(
            key=lambda r: (
                r.get("disqualified", False), r["rank_key"],
            ),
        )

        # deduplicate
        seen = set()
        unique = []
        for r in results:
            key = self._params_key(r["strategy"])
            if key not in seen:
                seen.add(key)
                unique.append(r)

        # ==================================
        # baseline evaluation (done early: the quality floor and
        # better_than_baseline both depend on it)
        # ==================================
        # Evaluate the baseline (default strategy) under the SAME
        # lexicographic order, so we can tell which trials actually
        # beat it (rank_key comparison, not raw score deltas).
        # ==================================
        baseline_evaluation, baseline_score, baseline_rank_key = \
            self._evaluate_baseline(
                document, dqr, repr_cls, profile,
            )

        # ==================================
        # hard-constraint gate + quality floor + fallback
        # ==================================
        # Quality floor: the lexicographic order allows trading quality for a better
        # gate/failure rank, but the adoption contract requires improvement = best_score − baseline
        # ≥ 0 — quality-regressing strategies are never adopted; above the floor the
        # best rank_key wins. When nobody qualifies below the floor we fall back to the
        # profile baseline and announce it loudly, never silently adopting a fallback.
        # ==================================
        sanity_flags = []
        quality_floor = baseline_score - 1e-9
        qualified = [
            r for r in unique
            if not r.get("disqualified") and r["score"] >= quality_floor
        ]

        if qualified:
            best = qualified[0]
        elif unique:
            base = profile.resolved_baseline()
            fallback_spec = policy_spec(
                base["boundary"], base["scale"],
                merge_short_siblings=base["merge_short_siblings"],
            )
            fallback_key = self._params_key(fallback_spec)
            best = next(
                (r for r in unique
                 if self._params_key(r["strategy"]) == fallback_key),
                None,
            )
            if best is None:
                best = self._evaluate_strategy(
                    fallback_spec, document, dqr,
                    repr_cls, suffix, profile,
                )
            sanity_flags.append(
                "no candidate satisfied the hard constraints and the "
                "quality floor (score >= baseline) — fell back to the "
                "profile baseline strategy; review violations in "
                "trials before adopting"
            )
        else:
            best = None

        # sanity check
        sanity_flags.extend(self._sanity_check(best, unique))

        # Adoption-contract backstop: winners produced by any path (including profile-baseline
        # fallback) must not fall below the quality floor; if one does, switch to "keep the default baseline" —
        # emit no optimized_output, report the baseline score as best_score,
        # keep improvement at 0, and never silently deliver a quality regression.
        kept_default_baseline = False
        if best is not None and best["score"] < quality_floor:
            best = None
            kept_default_baseline = True
            sanity_flags.append(
                "winner fell below the baseline quality floor — "
                "kept the default strategy unchanged (no optimized "
                "output emitted)"
            )

        # build list of strategies that beat baseline
        # (Lexicographic total-order comparison: smaller rank_key = better; raw score
        # deltas no longer gate admission — a high score cannot buy off structural damage;
        # consistent with the adoption contract, candidates whose quality falls below the floor do not count as "better")
        better_than_baseline = []
        for r in unique:
            if r.get("disqualified"):
                continue
            if r["score"] < quality_floor:
                continue
            if r["rank_key"] < baseline_rank_key:
                better_than_baseline.append({
                    "strategy": r["strategy"],
                    "score": r["score"],
                    "gate_status": r["objective_components"]["gate_status"][
                        "status"
                    ],
                    "quality_overall": canonical_quality(r["evaluation"]),
                    "improvement": round(
                        r["score"] - baseline_score, 4
                    ),
                })

        # build optimized output
        if best:
            optimized_output = self._build_optimized_output(
                best, document, dqr,
                repr_cls, profile,
            )
        else:
            optimized_output = None

        return {
            "file": file_label,
            "profile": profile.name,
            "mode": mode,
            "best_strategy": (
                best["strategy"] if best else None
            ),
            "best_score": (
                best["score"] if best
                else (
                    round(baseline_score, 4)
                    if kept_default_baseline else 0.0
                )
            ),
            "best_evaluation": (
                best["evaluation"] if best
                else (
                    baseline_evaluation
                    if kept_default_baseline else None
                )
            ),
            "objective_components": (
                best.get("objective_components") if best else None
            ),
            "disqualified_count": sum(
                1 for r in unique if r.get("disqualified")
            ),
            "hard_constraints": {
                "protected_block": "confirmed critical/error "
                    "structure breaks disqualify",
                "cross_section": "confirmed semantic drift disqualifies",
                "hard_limit": "chunks over the hard limit disqualify "
                    "(single protected blocks exempt)",
                "min_thresholds": profile.min_thresholds,
                "cross_sheet_merge": "N/A for narrative/config domains "
                    "(enforced when the table domain joins)",
            },
            "baseline_score": round(baseline_score, 4),
            "better_than_baseline": better_than_baseline,
            "trials": unique,
            "trials_summary": self._trials_summary(
                unique, baseline_score
            ),
            "total_tested": len(unique),
            "phases_trace": phases_trace,
            "sanity_flags": sanity_flags,
            "optimized_output": optimized_output,
        }
