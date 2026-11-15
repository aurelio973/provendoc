"""Evaluation and objective-function mixin for StrategyOptimizer.

Contains (split from strategy_optimizer.py in 2026-09, verbatim relocation):
- Early-exit result builder     _early_exit_result
- Baseline evaluation   _evaluate_baseline / _extract_chunks /
                      _run_failure_benchmark
- Sanity checks         _sanity_check (incl. _BASELINE_SCALE)
- Output builders       _build_optimized_output / _trials_summary
- Hard constraints      _is_single_protected_block /
                      _check_hard_constraints (HC1-5)
- Lexicographic objective       _gate_for / _failure_rate /
                      _quality_composite / _rank_key /
                      _advisory_preference / _complexity_penalty
- Representation building and utilities   _build_representation / _clean_params /
                      _params_key

For the design constitution and overall conventions see the provendoc.intelligence.
optimization.strategy_optimizer module docstring; profile and search-space constants in
strategy_optimizer_profiles. Verbatim relocation, no behavior change.
"""

from provendoc.core.pipeline.representation_registry import (
    representation_factory,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
)
from provendoc.core.strategies.representation_policy import (
    RepresentationPolicy,
)
from provendoc.intelligence.optimization.strategy_optimizer_profiles import (
    _REJECTION_SEVERITIES,
    ADVISORY_PREFERENCE_BONUS,
    COMPLEXITY_PENALTY_PER_LEVER,
    CROSS_SECTION_MODES,
    HARD_FAILURE_SEVERITY_WEIGHTS,
    PROTECTED_BLOCK_MODES,
    policy_spec,
    spec_to_policy,
)
from provendoc.intelligence.rse.rse_report import (
    GATE_RANK,
    GATE_VERSION,
    chunk_structure_applicability,
    compute_gate_v1,
    compute_quality_score,
    gate_exempt,
)


class StrategyOptimizerEvaluationMixin:
    """Evaluation, hard-constraint and objective machinery.

    Method bodies are relocated verbatim from StrategyOptimizer; mixin composition
    preserves the original method resolution order and behavior (class names and
    method signatures unchanged, so tests calling private methods directly keep working).
    """

    # ==================================
    # early exit result builder
    # ==================================

    def _early_exit_result(
        self, document, dqr, profile, suffix,
        file_label, total_text_length,
    ):
        """Build a result for documents too short to optimize.

        Returns the profile baseline strategy as best with a flag
        indicating the optimization was skipped.
        """
        baseline_evaluation, baseline_score, _ = self._evaluate_baseline(
            document, dqr,
            # representation_registry is the single source of truth
            # (pipeline_consolidation_prereg_v1 D1); the Txt fallback matches the old
            # _REPR_MAP.get default semantics
            representation_factory(suffix) or TxtNarrativeRepresentation,
            profile,
        )
        base = profile.resolved_baseline()

        return {
            "file": file_label,
            "profile": profile.name,
            "mode": "early_exit",
            "best_strategy": policy_spec(
                base["boundary"], base["scale"],
                merge_short_siblings=base["merge_short_siblings"],
            ),
            "best_score": round(baseline_score, 4),
            "best_evaluation": baseline_evaluation,
            "objective_components": None,
            "disqualified_count": 0,
            "hard_constraints": {
                "note": "optimization skipped (document too short)",
                "min_thresholds": profile.min_thresholds,
            },
            "baseline_score": round(baseline_score, 4),
            "better_than_baseline": [],
            "trials": [],
            "total_tested": 0,
            "phases_trace": None,
            "sanity_flags": [
                f"document too short ({total_text_length} chars < "
                f"{self._EARLY_EXIT_THRESHOLD} threshold) — "
                f"optimization skipped, profile baseline strategy used"
            ],
            "optimized_output": None,
        }

    # ==================================
    # baseline evaluation (default policy, same objective)
    # ==================================

    def _evaluate_baseline(self, document, dqr, repr_cls, profile):
        """Evaluate the default RepresentationPolicy.

        Uses the same lexicographic order as candidates minus the
        advisory/complexity terms (the default policy is not a
        controlled candidate, so baseline-relative bonuses do not
        apply). Returns (evaluation_result, quality_score, rank_key).
        """
        baseline_strategy = RepresentationPolicy()
        baseline_repr = repr_cls(strategy=baseline_strategy)
        baseline_result = baseline_repr.build(document)
        baseline_evaluation = self.evaluator.evaluate(
            document, baseline_result,
            baseline_strategy, dqr,
        )

        baseline_chunks = self._extract_chunks(baseline_result.content)
        failure_report = self._run_failure_benchmark(baseline_chunks)

        quality = self._quality_composite(
            baseline_evaluation, baseline_chunks, document, baseline_strategy,
        )
        rank_key = self._rank_key(
            failure_report, baseline_chunks, quality,
            advisory_preference=0.0,
            complexity_penalty=0.0,
        )
        return baseline_evaluation, quality, rank_key

    # ==================================
    # chunk extraction (MultiGranularityOutput compatible)
    # ==================================

    @staticmethod
    def _extract_chunks(content):
        """Extract the base chunk list from a representation result."""
        if hasattr(content, "base"):
            return content.base
        if hasattr(content, "all_units"):
            return content.all_units()
        return content

    def _run_failure_benchmark(self, chunks):
        """Run the retrieval failure benchmark on a chunk list."""
        if not chunks:
            return None
        try:
            return self._failure_benchmark.run(
                list(chunks), document_id="strategy_optimizer",
            )
        except Exception:
            # The benchmark is a second opinion, never a crash risk
            # for the optimizer itself.
            return None

    # ==================================
    # sanity check
    # ==================================
    #
    # The composite is a proxy metric, not truth. The optimizer
    # maximizes it, which means it can exploit metric weaknesses —
    # e.g. fewer, larger chunks inflate cohesion and
    # hierarchy_preservation without actually improving retrieval.
    #
    # Sanity flags for the policy space:
    #   - mechanical token_limit winning on a structured doc
    #     (suspicious: usually means metric gaming)
    #   - extreme scale (small on long docs, large on short)
    #   - single chunk (everything in one basket)
    #   - score jump unusually large vs runner-up
    #
    # It does NOT reject the result. It just warns.
    # The final decision is always the user's.
    # ==================================

    _BASELINE_SCALE = "medium"

    def _sanity_check(self, best, all_trials):
        """Flag suspicious optimization results.

        Returns a list of warning strings. Empty list = clean.
        """
        flags = []

        if not best:
            return flags

        strategy = best["strategy"]
        score = best["score"]

        # check 1: mechanical baseline winning
        boundary = strategy.get("boundary")
        if boundary == "token_limit":
            flags.append(
                "mechanical token_limit policy won — on structured "
                "documents this usually means metric gaming; "
                "verify chunks manually before adopting"
            )

        # check 2: extreme scale
        scale = strategy.get("scale", self._BASELINE_SCALE)
        metadata = best.get("evaluation", {}).get("metadata", {})
        chunk_count = metadata.get("chunk_count", 0)
        total_len = metadata.get("total_length", 0) or 0
        if scale == "large" and chunk_count <= 2 and total_len > 3000:
            flags.append(
                "scale=large produced very few chunks on a long "
                "document — oversized chunks may inflate cohesion"
            )
        elif scale == "small" and chunk_count > 40:
            flags.append(
                f"scale=small produced {chunk_count} chunks — "
                "may fragment discourse units"
            )

        if chunk_count <= 1:
            suitability = metadata.get("single_chunk_suitability") or {}
            if suitability.get("suitable"):
                flags.append(
                    "best policy produces one legitimate self-contained "
                    "chunk — accepted by single_chunk_suitability; "
                    "verify retrieval granularity if the use case changes"
                )
            else:
                flags.append(
                    "best policy produces only 1 chunk, but the document "
                    "is not a legitimate single-chunk terminal state — "
                    "treat the high composite score as metric gaming"
                )

        # check 3: score gap too large (qualified trials only —
        # disqualified trials sit at the bottom of the ranking
        # and would fake a gap)
        qualified = [
            r for r in all_trials if not r.get("disqualified")
        ]
        if len(qualified) >= 2 and qualified[0] is best:
            runner_up = qualified[1]["score"]
            gap = score - runner_up
            if gap > 0.2:
                flags.append(
                    f"score gap between #1 and #2 is {gap:.3f} "
                    f"— large gaps may indicate metric exploit "
                    f"rather than genuine improvement"
                )

        # check 4 (the old confidence check was retired with the six-dimension confidence
        # assembly layer in batch 3 — the invariant track no longer emits a top-level confidence)

        return flags

    # ==================================
    # optimized output builder
    # ==================================
    #
    # After the search finds the best strategy, this method
    # rebuilds the actual chunks with that strategy and
    # produces a baseline comparison (default strategy vs
    # optimized), so the user gets not just "the best params"
    # but the actual optimized output ready to use.
    # ==================================

    def _build_optimized_output(
        self, best, document, dqr,
        repr_cls, profile,
    ):
        """Build final optimized output: chunks + baseline comparison.

        Returns a dict with:
            strategy: best strategy params
            chunks: actual Chunk objects from best strategy
            chunk_count, avg_chunk_length
            quality_composite: invariant composite for optimized chunks
            baseline: default strategy stats for comparison
            improvement: score delta (optimized - baseline)
        """

        # --- optimized chunks ---

        best_strategy = spec_to_policy(best["strategy"])

        semantic_repr = repr_cls(strategy=best_strategy)
        optimized_result = semantic_repr.build(document)
        optimized_chunks = self._extract_chunks(
            optimized_result.content,
        )

        # --- baseline (default strategy) ---

        baseline_strategy = RepresentationPolicy()
        baseline_evaluation, baseline_score, _baseline_rank_key = (
            self._evaluate_baseline(document, dqr, repr_cls, profile)
        )
        baseline_repr = repr_cls(strategy=baseline_strategy)
        baseline_chunks = self._extract_chunks(
            baseline_repr.build(document).content,
        )

        return {
            "strategy": best["strategy"],
            "chunks": optimized_chunks,
            "chunk_count": len(optimized_chunks),
            "avg_chunk_length": round(
                sum(
                    len(c.text)
                    for c in optimized_chunks
                ) / max(len(optimized_chunks), 1),
                2,
            ),
            "quality_composite": best["evaluation"].get(
                "invariant_composite"
            ),
            "objective_components": best.get("objective_components"),
            "violations": best.get("violations", []),
            "baseline": {
                "strategy": {
                    "boundary": baseline_strategy.boundary_preference.primary,
                    "soft_limit": baseline_strategy.packaging.size_soft_limit,
                    "hard_limit": baseline_strategy.packaging.size_hard_limit,
                },
                "chunk_count": len(baseline_chunks),
                "quality_composite": baseline_evaluation.get(
                    "invariant_composite"
                ),
                "score": round(baseline_score, 4),
            },
            "optimized_score": best["score"],
            "improvement": round(
                best["score"] - baseline_score, 4,
            ),
        }

    # ==================================
    # trials summary (lightweight, for debug output)
    # ==================================

    def _trials_summary(self, unique_results, baseline_score):
        """Build a lightweight summary of all trials for debug.

        Returns a list of dicts with just the essential info:
        strategy params, score, delta vs baseline, and the
        disqualification verdict. Sorted by score descending.
        """
        summary = []
        for r in unique_results:
            summary.append({
                "strategy": r["strategy"],
                "score": r["score"],
                "delta_vs_baseline": round(
                    r["score"] - baseline_score, 4
                ),
                "disqualified": r.get("disqualified", False),
                **(
                    {"violations": r["violations"]}
                    if r.get("violations") else {}
                ),
            })
        return summary

    # ==================================
    # hard constraints (violations disqualify)
    # ==================================

    @staticmethod
    def _is_structural_evidence_atom(chunk) -> bool:
        """True if the chunk is a single unsplittable structural atom
        from an evidence-anchored reconstruction layer (currently: pdf
        base units).

        Same precedent as _is_single_protected_block: structural
        completeness beats the hard limit by design. The pdf
        reconstruction layer defines each long source block as an atom
        — no chunk-size strategy could split it without breaking
        evidence continuity, so HC3 (which exists to catch chunks a
        strategy could have made smaller) must not disqualify on it.
        Merged units (pdf_merged_blocks > 1) remain enforceable: they
        were assembled from splittable short blocks.
        """
        metadata = getattr(chunk, "metadata", None) or {}
        if not isinstance(metadata, dict):
            return False
        if metadata.get("source_kind") != "pdf_reconstruction":
            return False
        return int(metadata.get("pdf_merged_blocks") or 1) <= 1

    @staticmethod
    def _is_single_protected_block(text):
        """True if the chunk is exactly one fenced code block or
        one markdown table — structural completeness beats the
        hard limit for these by design."""
        stripped = text.strip()
        if (
            stripped.startswith("```")
            and stripped.endswith("```")
            and stripped.count("```") == 2
        ):
            return True
        lines = [line for line in stripped.split("\n") if line.strip()]
        if (
            lines
            and lines[0].lstrip().startswith("|")
            and "|---" in stripped
        ):
            return True
        return False

    def _check_hard_constraints(
        self, spec, evaluation, chunks, failure_report, profile,
    ):
        """Evaluate the hard-constraint set for one candidate.

        Returns a list of violation dicts; empty list = qualified.
        """
        violations = []

        confirmed = (
            failure_report.confirmed_failures()
            if failure_report else []
        )

        # HC1: protected blocks must never be split
        for failure in confirmed:
            if (
                failure.mode in PROTECTED_BLOCK_MODES
                and failure.severity in _REJECTION_SEVERITIES
            ):
                violations.append({
                    "constraint": "protected_block",
                    "failure": failure.mode.value,
                    "severity": failure.severity,
                    "affected_chunks": failure.affected_chunks,
                })
                break

        # HC2: no crossing into unrelated sections
        for failure in confirmed:
            if failure.mode in CROSS_SECTION_MODES:
                violations.append({
                    "constraint": "cross_section",
                    "failure": failure.mode.value,
                    "affected_chunks": failure.affected_chunks,
                })
                break

        # HC3: respect the hard size limit
        hard_limit = spec.get("hard_limit")
        if hard_limit and chunks:
            oversized = [
                index for index, chunk in enumerate(chunks)
                if len(
                    chunk.text if hasattr(chunk, "text") else str(chunk)
                ) > hard_limit
                and not self._is_single_protected_block(
                    chunk.text if hasattr(chunk, "text") else str(chunk)
                )
                and not self._is_structural_evidence_atom(chunk)
            ]
            if oversized:
                violations.append({
                    "constraint": "hard_limit",
                    "hard_limit": hard_limit,
                    "oversized_chunks": oversized[:10],
                })

        # HC4: invariant scores above profile thresholds
        # (batch 4: key names moved to the invariant track; the old effective_profile/profile
        # six-dimension view was deleted in batch 3)
        invariants = evaluation.get("invariants") or {}
        for dim, threshold in profile.min_thresholds.items():
            view = invariants.get(dim) or {}
            actual = view.get("score")
            if (
                view.get("applicable")
                and actual is not None
                and actual < threshold
            ):
                violations.append({
                    "constraint": "min_threshold",
                    "dimension": dim,
                    "actual": round(actual, 4),
                    "threshold": threshold,
                })

        # HC5: no cross-sheet merging without evidence — N/A for
        # narrative/config domains; enforced when the table domain
        # joins the optimizer (documented in the result dict).

        return violations

    # ==================================
    # objective composition (lexicographic, 2026-09-11 step 4)
    # ==================================

    def _gate_for(self, failure_report, chunks):
        """failure report → gate three states (same single source of truth as the
        report layer's compute_gate_v1; includes applicability exemptions and density
        normalization; the version constant GATE_VERSION is shared, so a gate_v2 bump has zero drift)."""
        if failure_report is None:
            return {
                "gate_version": GATE_VERSION,
                "status": "not_evaluated",
                "critical_count": 0,
                "error_count": 0,
                "issues": [],
                "note": "failure benchmark not run — gate cannot "
                        "aggregate; ranks with blocked (conservative)",
            }
        confirmed_views = [
            {"mode": f.mode.value, "severity": f.severity}
            for f in failure_report.confirmed_failures()
        ]
        return compute_gate_v1(
            confirmed_views,
            len(chunks) if chunks else 0,
            chunk_structure_applicability(chunks),
        )

    @staticmethod
    def _failure_rate(failure_report, total_chunks, applicability=None):
        """Severity-weighted confirmed-instance density (chunk-normalized, no cap).

        Lexicographic level 2: fine-grained ordering within the same gate state.
        Shares the exemption convention with compute_gate_v1 (gate_exempt is the
        single source of truth) — applicability-exempted failures enter neither the
        gate count nor the density, so the two levels never contradict. The retired cap
        (HARD_FAILURE_PENALTY_CAP) let high scores buy off structural damage; it was deleted. warnings/info weigh 0 (advisory role, excluded from ordering).
        """
        if failure_report is None:
            return 0.0
        total_weight = 0.0
        for f in failure_report.confirmed_failures():
            mode_value = getattr(f.mode, "value", f.mode)
            if gate_exempt(mode_value, applicability):
                continue
            total_weight += HARD_FAILURE_SEVERITY_WEIGHTS.get(
                f.severity, 0.0,
            )
        if total_weight <= 0:
            return 0.0
        return round(
            total_weight / max(total_chunks, 1), 6,
        )

    def _quality_composite(self, evaluation, chunks, document, strategy):
        """Canonical term: invariant_v1 four-dimension composite (= invariant_composite).

        csc was returned to the candidate slot per the admission prereg
        (admission_calibration_v1 §2.3) and does not enter the canonical term (the same
        pure function as the report layer: compute_quality_score). If the invariant
        track degrades (should not happen) this returns 0.0: the old six-dimension
        overall was deleted in batch 3, so degraded candidates rank last (honestly sortable).
        """
        inv = evaluation.get("invariant_composite")
        quality = compute_quality_score(inv)
        if quality is None:
            # Invariant-track degradation: 0.0 keeps candidates sortable (should not happen)
            quality = 0.0
        return quality

    def _rank_key(
        self, failure_report, chunks, quality,
        advisory_preference, complexity_penalty,
    ):
        """Lexicographic total-order key (lower wins; migration doc §3, five levels):

        1. gate_rank      gate_v1 three states (passed<warned<blocked;
                          not_evaluated ranks with blocked — no failure
                          data can be rewarded)
        2. failure_rate   severity-weighted density (within-gate-state tiebreak)
        3. -quality       canonical composite (new five dimensions), descending
        4. -advisory      baseline preference, descending
        5. complexity     complexity, ascending

        High scores cannot buy off structural damage: levels 1/2 are unaffected by quality.
        """
        applicability = chunk_structure_applicability(chunks)
        gate = self._gate_for(failure_report, chunks)
        failure_rate = self._failure_rate(
            failure_report, len(chunks) if chunks else 0, applicability,
        )
        return (
            GATE_RANK[gate["status"]],
            failure_rate,
            -float(quality),
            -float(advisory_preference),
            float(complexity_penalty),
        )

    @staticmethod
    def _advisory_preference(spec, profile):
        """Bonus for keeping the profile baseline levers."""
        base = profile.resolved_baseline()
        bonus = 0.0
        if spec.get("boundary") == base["boundary"]:
            bonus += ADVISORY_PREFERENCE_BONUS["boundary"]
        if spec.get("scale") == base["scale"]:
            bonus += ADVISORY_PREFERENCE_BONUS["scale"]
        return bonus

    @staticmethod
    def _complexity_penalty(spec, profile):
        """Cost per lever deviating from the profile baseline."""
        base = profile.resolved_baseline()
        deviations = sum((
            spec.get("boundary") != base["boundary"],
            spec.get("scale") != base["scale"],
            bool(spec.get("merge_short_siblings"))
            != bool(base["merge_short_siblings"]),
        ))
        return deviations * COMPLEXITY_PENALTY_PER_LEVER

    # ==================================
    # representation builder
    # ==================================
    #
    # repr_cls is supplied by representation_registry.representation_factory
    # (pipeline_consolidation_prereg_v1 D1): classes that accept strategy are returned
    # as-is; those that do not (log/jsonl/code) are adapted by the registry to the
    # unified ``(strategy=None)`` signature — no type branching at call sites.
    # ==================================

    def _build_representation(self, repr_cls, strategy, suffix):
        """Create a representation instance with strategy.

        All three representations (Txt, Markdown, Html) now
        accept a unified ``strategy`` parameter.
        """
        return repr_cls(strategy=strategy)

    # ==================================
    # canonical term (invariant_v1, rewritten in step 4)
    # ==================================
    #
    # The old profile-priority 1.5x reweighting was retired with the legacy canonical term.
    # min_thresholds has been a hard constraint since 2026-09-07 (HC4, unchanged);
    # baseline anchoring (advisory) and complexity remain at lexicographic
    # levels 4/5. The canonical body = compute_quality_score (five-dimension
    # composite, shared with the report layer); see _quality_composite.
    # ==================================

    # ==================================
    # helpers
    # ==================================

    @staticmethod
    def _clean_params(params):
        """Remove internal keys, keep user-facing ones."""
        return {
            k: v for k, v in params.items()
            if k != "name"
        }

    # kept for backward compatibility with older callers

    def _params_key(self, params):
        """Create hashable key from params dict for dedup."""
        return tuple(sorted(params.items()))
