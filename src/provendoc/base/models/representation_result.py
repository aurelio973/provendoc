from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Union

from provendoc.base.models.representation_unit import RepresentationUnit

if TYPE_CHECKING:
    # Type-only import: MultiGranularityOutput (the coarse/base/fine
    # container used by narrative representations) is part of the
    # declared ``content`` union. Runtime import would invert the
    # base→core layering, so it stays behind TYPE_CHECKING.
    from provendoc.core.representations.narrative.granularity import (
        MultiGranularityOutput,
    )


@dataclass
class RepresentationResult:
    """Build result of a single representation.

    ``representation_type`` is the representation name ("semantic" /
    "document" / "optimized_semantic" etc.); the canonical shape of
    ``content`` is ``List[RepresentationUnit]`` — some representations
    (e.g. document) use a dict, narrative ones use the three-granularity
    container ``MultiGranularityOutput``.
    """
    representation_type: str  # "semantic", "document", "optimized_semantic", etc.
    content: Union[List[RepresentationUnit], Dict[str, Any], "MultiGranularityOutput"]  # canonical: List[RepresentationUnit]
    metadata: Dict[str, Any] = field(default_factory=dict)
