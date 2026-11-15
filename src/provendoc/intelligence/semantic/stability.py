"""Representation Stability — how stable are invariants across strategies?

The core question this module answers:

    "If we chunk the same document with different strategies, do the
     semantic invariants give consistent signals?"

A stable invariant is one that:
    - Scores high for a GOOD chunking (regardless of strategy)
    - Scores low for a BAD chunking (regardless of strategy)
    - Doesn't wildly fluctuate across reasonable strategies

An unstable invariant might:
    - Score high for one strategy and low for another on the SAME document
    - Be sensitive to chunk_size / overlap rather than to semantic quality

==================================
Why this matters
==================================

In the old CQI system, we had a single strategy evaluation. The optimizer
would maximize CQI for one strategy, which could overfit to the metric's
specific heuristics.

With stability analysis, we can:
    1. Detect metric gaming — if an invariant improves only for one
       strategy, it's probably gaming the metric, not improving quality.
    2. Identify robust vs. fragile invariants — robust invariants
       correlate with genuine quality across strategies.
    3. Drive optimization toward stable quality, not single-strategy
       quality.

==================================
Algorithm
==================================

Given:
    - A document D
    - A set of chunking strategies S1, S2, ..., Sn
    - A set of invariants I1, I2, ..., Im

For each invariant Ij:
    1. Compute scores: sj1, sj2, ..., sjn (one per strategy)
    2. Stability metrics:
       - Mean: μ(sj) — average satisfaction across strategies
       - Std:  σ(sj) — variation across strategies
       - CV:   σ/μ   — coefficient of variation (normalized instability)
       - Min:  min(sj) — worst-case satisfaction
    3. Stability score = f(μ, σ) — high mean + low std = stable

The stability score is NOT the same as the invariant score. An invariant
can be stable but low (consistently violated) or unstable but high
(varies but generally good).

==================================
Interpretation
==================================

    stability > 0.8  → robust invariant, trustworthy signal
    stability 0.5-0.8 → moderate, use with caution
    stability < 0.5  → fragile, may be strategy-sensitive

Fragile invariants are NOT useless — they may detect strategy-specific
pathologies. But they should carry lower weight in optimization.
"""

import statistics
from dataclasses import dataclass, field
from typing import Dict, List

from provendoc.intelligence.semantic.invariant import (
    InvariantRegistry,
    InvariantResult,
)


@dataclass
class InvariantStability:
    """Stability metrics for a single invariant across strategies.

    Attributes:
        invariant_name:   which invariant this measures
        scores:           raw scores per strategy
        mean:             average score across strategies
        std:              standard deviation
        cv:               coefficient of variation (std/mean)
        min_score:        worst-case score
        max_score:        best-case score
        stability:        composite stability score [0, 1]
        classification:   "robust" | "moderate" | "fragile"
    """
    invariant_name: str
    scores: List[float] = field(default_factory=list)
    mean: float = 0.0
    std: float = 0.0
    cv: float = 0.0
    min_score: float = 0.0
    max_score: float = 0.0
    stability: float = 0.0
    classification: str = "unknown"

    def to_dict(self) -> dict:
        """Serialize into a dict."""
        return {
            "invariant_name": self.invariant_name,
            "scores": [round(s, 3) for s in self.scores],
            "mean": round(self.mean, 3),
            "std": round(self.std, 3),
            "cv": round(self.cv, 3),
            "min_score": round(self.min_score, 3),
            "max_score": round(self.max_score, 3),
            "stability": round(self.stability, 3),
            "classification": self.classification,
        }


@dataclass
class StabilityReport:
    """Full stability analysis across all invariants and strategies.

    Attributes:
        domain:             which domain was analyzed
        strategy_labels:    labels for each strategy evaluated
        invariant_stability: per-invariant stability metrics
        global_stability:    aggregate stability across all invariants
        robust_invariants:   names of invariants with stability > 0.8
        fragile_invariants:  names of invariants with stability < 0.5
    """
    domain: str = ""
    strategy_labels: List[str] = field(default_factory=list)
    invariant_stability: Dict[str, InvariantStability] = field(default_factory=dict)
    global_stability: float = 0.0
    robust_invariants: List[str] = field(default_factory=list)
    fragile_invariants: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize into a dict."""
        return {
            "domain": self.domain,
            "strategy_labels": self.strategy_labels,
            "invariant_stability": {
                k: v.to_dict()
                for k, v in self.invariant_stability.items()
            },
            "global_stability": round(self.global_stability, 3),
            "robust_invariants": self.robust_invariants,
            "fragile_invariants": self.fragile_invariants,
        }


class StabilityScorer:
    """Compute representation stability across chunking strategies.

    Usage:
        scorer = StabilityScorer(registry)
        report = scorer.analyze(
            document=document,
            strategy_results={
                "fixed_300": (chunks_300, strategy_300),
                "recursive_512": (chunks_512, strategy_512),
                "semantic": (chunks_sem, strategy_sem),
            }
        )
    """

    def __init__(self, registry: InvariantRegistry):
        """Inject the domain-invariant registry."""
        self.registry = registry

    def analyze(
        self,
        document,
        strategy_results: Dict[str, tuple],
    ) -> StabilityReport:
        """Analyze invariant stability across multiple strategies.

        Args:
            document: the Document being analyzed
            strategy_results: dict of strategy_label → (chunks, strategy)

        Returns:
            StabilityReport with per-invariant and global stability
        """
        labels = list(strategy_results.keys())
        n_strategies = len(labels)

        if n_strategies == 0:
            return StabilityReport(domain=self.registry.domain)

        # Evaluate invariants for each strategy
        per_strategy_results: Dict[str, Dict[str, InvariantResult]] = {}
        for label, (chunks, strategy) in strategy_results.items():
            results = self.registry.evaluate_all(
                chunks, document, strategy
            )
            per_strategy_results[label] = results

        # Compute per-invariant stability
        report = StabilityReport(
            domain=self.registry.domain,
            strategy_labels=labels,
        )

        all_stability_scores = []

        for inv_name in self.registry.invariant_names:
            scores = []
            for label in labels:
                result = per_strategy_results[label].get(inv_name)
                if result and result.applicable:
                    scores.append(result.score)
                # N/A results are excluded, not counted as 0 or 1

            if len(scores) < 2:
                # Need at least 2 data points for stability
                stability = InvariantStability(
                    invariant_name=inv_name,
                    scores=scores,
                    mean=scores[0] if scores else 0.0,
                    std=0.0,
                    cv=0.0,
                    min_score=scores[0] if scores else 0.0,
                    max_score=scores[0] if scores else 0.0,
                    stability=scores[0] if scores else 0.0,
                    classification="insufficient_data",
                )
                report.invariant_stability[inv_name] = stability
                continue

            mean_score = statistics.mean(scores)
            std_score = statistics.pstdev(scores) if len(scores) > 1 else 0.0
            cv = std_score / max(mean_score, 0.01)
            min_s = min(scores)
            max_s = max(scores)

            # Stability = high mean + low variation
            # We want: mean close to 1.0 (invariant is satisfied)
            #          AND std close to 0.0 (consistent across strategies)
            # Stability = mean * (1 - cv_clamped)
            cv_factor = max(0.0, 1.0 - min(cv, 1.0))
            stability_score = mean_score * cv_factor

            # Also penalize large range
            score_range = max_s - min_s
            range_penalty = max(0.0, 1.0 - score_range)
            stability_score = 0.7 * stability_score + 0.3 * range_penalty

            if stability_score > 0.8:
                classification = "robust"
            elif stability_score > 0.5:
                classification = "moderate"
            else:
                classification = "fragile"

            stability = InvariantStability(
                invariant_name=inv_name,
                scores=scores,
                mean=round(mean_score, 3),
                std=round(std_score, 3),
                cv=round(cv, 3),
                min_score=round(min_s, 3),
                max_score=round(max_s, 3),
                stability=round(stability_score, 3),
                classification=classification,
            )
            report.invariant_stability[inv_name] = stability
            all_stability_scores.append(stability_score)

        # Global stability = mean of all invariant stability scores
        if all_stability_scores:
            report.global_stability = round(
                statistics.mean(all_stability_scores), 3
            )

        # Classify invariants
        for name, stab in report.invariant_stability.items():
            if stab.classification == "robust":
                report.robust_invariants.append(name)
            elif stab.classification == "fragile":
                report.fragile_invariants.append(name)

        return report
