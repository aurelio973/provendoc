"""Semantic Cleaning Layer — the semantic purification stage between
Parser and Representation.

Design philosophy:
    Not "remove all whitespace", but "distinguish semantic whitespace
    from artifact whitespace". Blank lines are rhythm in poetry,
    logical separation in code, event boundaries in logs — those
    carry meaning. But parser-induced blank lines (PDF residue,
    the blank line after a BOM, paste-induced extra newlines) are
    artifacts and get cleaned.

Three cleaning levels:
    1. conservative: only obvious meaningless artifacts (3+ consecutive
       blank lines, leading/trailing blank lines)
    2. standard: conservative + structure awareness (blank lines kept
       between list items and between paragraphs)
    3. aggressive: standard + semantic inference (discourse patterns
       judge whether a blank line carries semantic value)

Output:
    CleanedDocument — same structure as Document plus cleaning_trace,
    recording every cleaning decision (for explainability and debug).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Literal


class CleaningLevel(Enum):
    """How aggressive the cleaning is."""
    CONSERVATIVE = "conservative"   # obvious artifacts only
    STANDARD = "standard"           # + structure awareness
    AGGRESSIVE = "aggressive"       # + semantic inference


class BlankLineSemantics(Enum):
    """Semantic classification of blank/furniture lines — for explainability."""
    ARTIFACT = "artifact"           # meaningless whitespace (PDF residue, paste, etc.)
    STRUCTURAL = "structural"       # structural separation (between paragraphs, sections)
    DISCOURSE = "discourse"         # discourse marker (between list items, code blocks)
    PRESERVATION_CANDIDATE = "preservation_candidate"  # whitespace that may need keeping
    PAGE_FURNITURE = "page_furniture"  # page furniture (header/footer/page number/TOC
    # lines) — the semantic class for non-empty-line artifacts, distinct
    # from the blank-line ARTIFACT class


@dataclass
class CleaningDecision:
    """Record of a single cleaning decision — structural explainability."""
    line_range: tuple              # (start_line, end_line) 0-based
    original_text: str             # original text snippet
    decision: Literal["removed", "collapsed", "preserved"]
    semantics: BlankLineSemantics  # semantic classification
    reason: str                    # human-readable reason
    confidence: str                # "high" | "medium" | "low"


@dataclass
class CleaningResult:
    """Cleaning result — replaces Document.text while keeping the trace."""
    text: str
    decisions: List[CleaningDecision] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)

    def to_document_metadata(self) -> Dict:
        """Convert into the format stored in Document.metadata."""
        return {
            "cleaning_applied": True,
            "cleaning_decision_count": len(self.decisions),
            "cleaning_stats": self.stats,
            "cleaning_trace": [
                {
                    "line_range": d.line_range,
                    "decision": d.decision,
                    "semantics": d.semantics.value,
                    "reason": d.reason,
                    "confidence": d.confidence,
                }
                for d in self.decisions
            ],
        }
