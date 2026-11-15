"""Unified Primitives Layer — Signal → Semantic Primitive mapping.

This is the SINGLE truth source for "how observed signals map to semantic primitives".
No other module should define signal-to-primitive mapping tables.

Core responsibility:
    Map SignalObservation list → RepresentationUnit's SemanticPrimitives.

    "I see a code block"      → locality_scope = "code_block"
    "I see 'for example'"     → discourse_cues.append("for_example")

Design principles:
    - semantic_role stays minimal (8 primitives), no explosion to
      EXPLANATION/EVIDENCE/CLAIM sub-types
    - Mapping is deterministic: same signals → same SemanticPrimitives
    - No cross-unit inference (that's StructuralSemanticsAnalyzer's job)

Mapping rules:

    SignalType → locality_scope:
        CODE_SIGNAL         → "code_block"
        TABLE_SIGNAL        → "table"
        LIST_SIGNAL         → "list_group"
        HEADING_SIGNAL      → no locality_scope, sets hierarchy_path
        DISCOURSE_CUE       → no locality_scope, sets discourse_cues
        STRUCTURAL_BOUNDARY → no locality_scope, marks separability

    SignalType → semantic_role:
        HEADING_SIGNAL      → REFERENCE
        DISCOURSE_CUE (explanation_marker) → EXAMPLE or EXPLANATION
        DISCOURSE_CUE (transition_phrase)  → TRANSITION
        DISCOURSE_CUE (topic_sentence)     → DEFINITION (if definition pattern)
        DISCOURSE_CUE (continuation)       → UNKNOWN (keep primitive)
        CODE_SIGNAL         → UNKNOWN (code has no single semantic role)
        TABLE_SIGNAL        → UNKNOWN
        LIST_SIGNAL         → UNKNOWN

    SignalType → discourse_cues:
        DISCOURSE_CUE (explanation_marker) → "for_example", "such_as"
        DISCOURSE_CUE (transition_phrase)  → "however", "therefore", "moreover"
        DISCOURSE_CUE (continuation)       → "continuation", "then", "next"
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from provendoc.core.semantics.signals import SignalObservation, SignalType

# ==================================
# Mapping tables
# ==================================

_SIGNAL_TO_LOCALITY: Dict[SignalType, str] = {
    SignalType.CODE_SIGNAL: "code_block",
    SignalType.TABLE_SIGNAL: "table",
    SignalType.LIST_SIGNAL: "list_group",
    # COHERENCE_SHIFT does not set locality_scope — it contributes boundary_pressure only.
    # Its confidence is treated as boundary influence strength, not a locality classifier.
}

_DISCOURSE_CUE_SUBTYPE_MAP: Dict[str, str] = {
    # explanation_marker subtypes → discourse cue labels
    "explanation_marker": "for_example",
    # transition_phrase subtypes
    "transition_phrase": "transition",
    # topic_sentence subtypes
    "topic_sentence": "topic",
    # continuation subtypes
    "continuation": "continuation",
    # structural_boundary subtypes
    "structural_boundary": "boundary",
}

_DISCOURSE_CUE_TO_ROLE: Dict[str, str] = {
    "for_example": "example",
    "transition": "transition",
    "topic": "definition",  # topic sentences often introduce definitions
    "continuation": "unknown",  # keep primitive — continuation doesn't imply a specific role
    "boundary": "unknown",
}

# Heading level → hierarchy depth offset
_HEADING_SUBTYPE_PREFIX = "h"


class SignalSemanticMapper:
    """Signal → Semantic Primitive mapper.

    Input: List[SignalObservation]
    Output: MappedSemantics (locality_scope, discourse_cues, semantic_role, heading_level)

    Principle: semantic_role stays at 8 primitives, no explosion.
    """

    def map(self, signals: List[SignalObservation]) -> "MappedSemantics":
        """Map signal list to semantic primitives.

        Args:
            signals: SignalObservation list

        Returns:
            MappedSemantics containing mapped locality_scope, discourse_cues,
            semantic_role, heading_level, is_boundary_candidate
        """
        result = MappedSemantics()

        if not signals:
            return result

        # Track the strongest locality signal
        best_locality: Optional[Tuple[str, float]] = None

        for sig in signals:
            # --- locality_scope ---
            if sig.signal_type in _SIGNAL_TO_LOCALITY:
                scope = _SIGNAL_TO_LOCALITY[sig.signal_type]
                if best_locality is None or sig.confidence > best_locality[1]:
                    best_locality = (scope, sig.confidence)

            # --- heading level ---
            if sig.signal_type == SignalType.HEADING_SIGNAL:
                if sig.sub_type and sig.sub_type.startswith(_HEADING_SUBTYPE_PREFIX):
                    try:
                        result.heading_level = int(sig.sub_type[1:])
                    except ValueError:
                        result.heading_level = 1
                result.heading_text = sig.text

            # --- discourse cues ---
            if sig.signal_type == SignalType.DISCOURSE_CUE:
                cue_label = _DISCOURSE_CUE_SUBTYPE_MAP.get(
                    sig.sub_type or "", sig.sub_type or "unknown"
                )
                if cue_label not in result.discourse_cues:
                    result.discourse_cues.append(cue_label)

                # Map to semantic_role (only set if currently unknown)
                role = _DISCOURSE_CUE_TO_ROLE.get(cue_label, "unknown")
                if role != "unknown" and result.semantic_role == "unknown":
                    result.semantic_role = role

            # --- boundary candidates ---
            if sig.signal_type == SignalType.STRUCTURAL_BOUNDARY:
                result.boundary_positions.append(sig.position)

            # --- coherence shift: boundary pressure accumulation ---
            # COHERENCE_SHIFT does not set locality_scope or semantic_role.
            # It contributes to boundary_positions as a weak pressure signal.
            # confidence = boundary influence strength (0-1).
            if sig.signal_type == SignalType.COHERENCE_SHIFT:
                result.boundary_positions.append(sig.position)
                # Accumulate coherence pressure: if multiple coherence signals
                # land near the same position, their influence compounds.
                result.coherence_pressure = max(
                    result.coherence_pressure, sig.confidence
                )

        # Set locality_scope from best signal
        if best_locality:
            result.locality_scope = best_locality[0]

        return result

    def map_to_primitives_kwargs(
        self,
        signals: List[SignalObservation],
    ) -> Dict:
        """Map signal list to SemanticPrimitives constructor kwargs.

        Returns keyword arguments that can be directly used to construct
        SemanticPrimitives.
        """
        mapped = self.map(signals)

        return {
            "locality_scope": mapped.locality_scope,
            "semantic_role": mapped.semantic_role,
            "discourse_cues": mapped.discourse_cues,
            "hierarchy_path": mapped.hierarchy_path,
            "dependencies": [],
            "identity": "",
            "integrity_guarantees": [],
            "confidence": mapped.confidence,
        }


@dataclass
class MappedSemantics:
    """Signal mapping result.

    Attributes:
        locality_scope: Locality scope ("code_block", "table", "list_group", "unknown")
        discourse_cues: Discourse marker list
        semantic_role: Semantic role (one of 8 primitives)
        heading_level: Heading level (1-6, 0 means non-heading)
        heading_text: Heading text
        hierarchy_path: Hierarchy path (needs external context to fill)
        boundary_positions: Structural boundary position list
        confidence: Mapping confidence
        coherence_pressure: Accumulated boundary influence strength from COHERENCE_SHIFT
            signals (0-1). 0.0 means no coherence evidence. This is NOT a probability —
            it represents how strongly coherence signals push toward a boundary here.
    """
    locality_scope: str = "unknown"
    discourse_cues: List[str] = field(default_factory=list)
    semantic_role: str = "unknown"
    heading_level: int = 0
    heading_text: str = ""
    hierarchy_path: List[str] = field(default_factory=list)
    boundary_positions: List[int] = field(default_factory=list)
    confidence: float = 0.8
    coherence_pressure: float = 0.0
