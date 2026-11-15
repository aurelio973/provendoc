"""Core Strategies — public interface of the strategy layer.

Contains the core strategy components: semantic representation
policy, corpus stratification, stability observation, parameter
space, and more.

Public API:
    RepresentationPolicy     — 5-layer semantic representation policy
    RuntimePackagingPolicy   — runtime packaging policy
    ContextPropagation       — context propagation strategy
    ScoreObservation         — score observation data structure
    CorpusStratifier          — 4-tier corpus stratifier
    CorpusTier               — corpus tier enum
    SemanticStabilityObserver — semantic stability observer
    ParameterSpace           — parameter space definition and construction
"""

from provendoc.core.strategies.corpus_stratification import (
    CorpusStratifier,
    CorpusTier,
)
from provendoc.core.strategies.parameter_space import ParameterSpace
from provendoc.core.strategies.representation_policy import (
    ContextPropagation,
    RepresentationPolicy,
    RuntimePackagingPolicy,
    ScoreObservation,
)
from provendoc.core.strategies.stability_observer import SemanticStabilityObserver

__all__ = [
    "RepresentationPolicy",
    "RuntimePackagingPolicy",
    "ContextPropagation",
    "ScoreObservation",
    "CorpusStratifier",
    "CorpusTier",
    "SemanticStabilityObserver",
    "ParameterSpace",
]
