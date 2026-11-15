"""Search mixin for StrategyOptimizer.

Contains (split from strategy_optimizer.py in 2026-09, verbatim relocation):
- Controlled search (default mode)  _adjacent_scales /
                        _controlled_candidates /
                        _controlled_search
- Grid search (opt-in)    _grid_search
- Hierarchical search (opt-in)    _hierarchical_search
- Single-strategy evaluation    _evaluate_strategy

For the design constitution and overall conventions see the provendoc.intelligence.
optimization.strategy_optimizer module docstring; profile and search-space
constants in strategy_optimizer_profiles; objective functions and hard constraints in
strategy_optimizer_evaluation. Verbatim relocation, no behavior change.
"""

import itertools

from provendoc.intelligence.optimization.strategy_optimizer_profiles import (
    BOUNDARY_OPTIONS,
    DEFAULT_STRATEGY_SPACE,
    HIERARCHICAL_PHASES,
    SCALE_ORDER,
    SCALE_TIERS,
    policy_spec,
    spec_to_policy,
)
from provendoc.intelligence.rse.rse_report import (
    chunk_structure_applicability,
)


class StrategyOptimizerSearchMixin:
    """Search machinery: controlled / grid / hierarchical + trials.

    Method bodies are relocated verbatim from StrategyOptimizer; mixin composition
    preserves the original method resolution order and behavior (class names and
    method signatures unchanged, so tests calling private methods directly keep working).
    """

    # ==================================
    # controlled search (profile-first, default mode)
    # ==================================

    def _adjacent_scales(self, baseline_scale):
        """Baseline scale tier and its immediate neighbors only."""
        try:
            index = SCALE_ORDER.index(baseline_scale)
        except ValueError:
            return [baseline_scale]
        return [
            SCALE_ORDER[i]
            for i in (index - 1, index, index + 1)
            if 0 <= i < len(SCALE_ORDER)
        ]

    def _controlled_candidates(self, profile, strategy_space=None):
        """Enumerate the profile-anchored candidate specs.

        Controlled space = profile boundaries (baseline first)
        × ADJACENT scale tiers × merge toggle (only meaningful
        for heading_boundary). An explicit ``strategy_space`` can
        narrow each axis (it can also re-admit the mechanical
        token_limit control — that is an explicit opt-in).
        """
        base = profile.resolved_baseline()

        if strategy_space:
            boundaries = strategy_space.get(
                "boundary", profile.controlled_boundaries(),
            )
            scales = strategy_space.get(
                "scale", self._adjacent_scales(base["scale"]),
            )
            merges = strategy_space.get("merge_short_siblings")
            if merges is None:
                merges = (
                    [False, True]
                    if base["boundary"] == "heading_boundary"
                    else [False]
                )
        else:
            boundaries = profile.controlled_boundaries()
            scales = self._adjacent_scales(base["scale"])
            merges = (
                [False, True]
                if base["boundary"] == "heading_boundary"
                else [False]
            )

        specs = [
            policy_spec(b, s, merge_short_siblings=m)
            for b, s, m in itertools.product(boundaries, scales, merges)
        ]

        # baseline first (deterministic ordering)
        baseline_key = self._params_key(
            policy_spec(
                base["boundary"], base["scale"],
                merge_short_siblings=base["merge_short_siblings"],
            )
        )
        specs.sort(
            key=lambda spec: (
                self._params_key(spec) != baseline_key
            )
        )

        # dedupe after normalization: merge_short_siblings collapses
        # to False for non-heading boundaries, so e.g. (structural_
        # block, medium, True) is the same policy as (structural_
        # block, medium, False) — never evaluate it twice.
        seen = set()
        unique_specs = []
        for spec in specs:
            key = self._params_key(spec)
            if key not in seen:
                seen.add(key)
                unique_specs.append(spec)
        return unique_specs

    def _controlled_search(
        self, document, dqr, repr_cls, suffix,
        profile, strategy_space,
    ):
        """Profile-first enumerated neighborhood search."""
        results = []
        for spec in self._controlled_candidates(
            profile, strategy_space,
        ):
            trial = self._evaluate_strategy(
                spec, document, dqr, repr_cls, suffix, profile,
            )
            if trial is not None:
                results.append(trial)
        return results

    # ==================================
    # grid search (Cartesian product, opt-in)
    # ==================================

    def _grid_search(
        self, document, dqr, repr_cls, suffix,
        profile, strategy_space,
    ):
        """Full Cartesian product of all policy combinations."""
        space = strategy_space or DEFAULT_STRATEGY_SPACE

        boundaries = space.get("boundary", BOUNDARY_OPTIONS)
        scales = space.get("scale", list(SCALE_TIERS.keys()))
        merge_options = space.get("merge_short_siblings", [False])

        results = []
        for boundary, scale, merge_short_siblings in itertools.product(
            boundaries, scales, merge_options,
        ):
            spec = policy_spec(
                boundary, scale,
                merge_short_siblings=merge_short_siblings,
            )

            trial = self._evaluate_strategy(
                spec, document, dqr, repr_cls, suffix, profile,
            )
            if trial is not None:
                results.append(trial)

        return results

    # ==================================
    # hierarchical search (greedy phases)
    # ==================================

    def _hierarchical_search(
        self, document, dqr, repr_cls, suffix,
        profile, custom_phases,
    ):
        """Phase-by-phase greedy search.

        Each phase searches one policy knob, fixing the best
        value from previous phases. Much faster than grid
        but may miss interaction effects.
        """
        phases = custom_phases or HIERARCHICAL_PHASES

        current = {
            "boundary": "discourse_unit",
            "scale": "medium",
            "merge_short_siblings": False,
        }
        all_results = []
        phases_trace = []

        for phase in phases:
            param = phase["param"]
            values = phase["values"]

            phase_results = []

            for value in values:
                test = dict(current)
                test[param] = value
                spec = policy_spec(
                    test["boundary"], test["scale"],
                    merge_short_siblings=test.get(
                        "merge_short_siblings", False
                    ),
                )

                trial = self._evaluate_strategy(
                    spec, document, dqr, repr_cls,
                    suffix, profile,
                )
                if trial is not None:
                    phase_results.append(trial)

            if not phase_results:
                continue

            # sort by the lexicographic total order — a disqualified
            # phase result is never adopted
            phase_results.sort(
                key=lambda r: (
                    r.get("disqualified", False), r["rank_key"],
                ),
            )

            best = phase_results[0]
            if best.get("disqualified"):
                # every value in this phase violated a hard
                # constraint — keep the current (previous-phase)
                # value and record why
                phases_trace.append({
                    "phase": param,
                    "values_tested": values,
                    "best_value": current[param],
                    "best_score": None,
                    "note": "all phase candidates violated hard "
                            "constraints — previous value retained",
                })
                all_results.extend(phase_results)
                continue

            best_value = best["strategy"][param]

            # fix best value for next phase
            current[param] = best_value

            phases_trace.append({
                "phase": param,
                "values_tested": values,
                "best_value": best_value,
                "best_score": best["score"],
                "best_confidence": best["evaluation"]["confidence"],
            })

            all_results.extend(phase_results)

        # ensure the fully-optimized combination is in results
        # (it may not be if all phases had ties and a different
        # config with same score was evaluated in an earlier phase)
        final_spec = policy_spec(
            current["boundary"], current["scale"],
            merge_short_siblings=current["merge_short_siblings"],
        )
        final_key = self._params_key(final_spec)
        if not any(
            self._params_key(r["strategy"]) == final_key
            for r in all_results
        ):
            final_trial = self._evaluate_strategy(
                final_spec, document, dqr,
                repr_cls, suffix, profile,
            )
            if final_trial is not None:
                all_results.append(final_trial)

        return all_results, phases_trace

    # ==================================
    # single strategy evaluation
    # ==================================

    def _evaluate_strategy(
        self, spec, document, dqr,
        repr_cls, suffix, profile,
    ):
        """Build chunks for one policy spec, run the quality
        evaluation + the failure benchmark, check hard constraints,
        compute the objective.

        Returns None if chunking produces empty results. A trial
        dict carries ``disqualified`` + ``violations`` when a hard
        constraint fails — disqualified trials stay in the history
        for transparency but can never win.
        """
        strategy = spec_to_policy(spec)

        # build representation
        semantic_repr = self._build_representation(
            repr_cls, strategy, suffix,
        )
        if semantic_repr is None:
            return None

        result = semantic_repr.build(document)

        # run the quality evaluation
        evaluation = self.evaluator.evaluate(
            document, result, strategy, dqr,
        )

        # run the retrieval failure benchmark on the actual chunks
        chunks = self._extract_chunks(result.content)
        failure_report = self._run_failure_benchmark(chunks)

        # hard-constraint gate
        violations = self._check_hard_constraints(
            spec, evaluation, chunks, failure_report, profile,
        )

        # objective composition (lexicographic, not additive)
        quality = self._quality_composite(evaluation, chunks, document, strategy)
        advisory = self._advisory_preference(spec, profile)
        complexity = self._complexity_penalty(spec, profile)
        rank_key = self._rank_key(
            failure_report, chunks, quality,
            advisory_preference=advisory,
            complexity_penalty=complexity,
        )
        components = {
            "quality_composite": quality,
            "gate_status": self._gate_for(failure_report, chunks),
            "failure_rate": self._failure_rate(
                failure_report,
                len(chunks) if chunks else 0,
                chunk_structure_applicability(chunks),
            ),
            "advisory_preference": advisory,
            "complexity_penalty": complexity,
        }

        confirmed = failure_report.confirmed_failures()\
            if failure_report else []

        return {
            "strategy": spec,
            "score": quality,
            "rank_key": rank_key,
            "evaluation": evaluation,
            "disqualified": bool(violations),
            "violations": violations,
            "failure_summary": {
                "confirmed": len(confirmed),
                "suspected": (
                    len(failure_report.suspected_failures())
                    if failure_report else 0
                ),
                "by_mode": (
                    failure_report.summary if failure_report else {}
                ),
            },
            "objective_components": {
                k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in components.items()
            },
        }
