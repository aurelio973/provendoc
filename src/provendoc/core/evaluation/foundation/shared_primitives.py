"""Shared Quality Primitives — universal ingestion health signals.

These primitives are domain-agnostic. They measure properties of the
ingestion process itself (not the chunk quality), and every domain
quality evaluator can reference them as baseline signals.

Separation principle:
    Domain quality evaluators (NarrativeQuality, ConfigQuality,
    etc.) answer:
        "are the chunks well-cut for this domain's retrieval philosophy?"

    Shared primitives answer:
        "was the ingestion itself clean and complete?"

A perfectly chunked dirty document should get:
    high domain quality + low shared primitive scores.

Current state:
    Many primitives already exist in scattered locations:
    - core/text/diagnostics.py      → printable_ratio, empty_line_ratio
    - core/text/decoding.py         → replacement_chars, encoding_fallback
    - core/diagnostics/             → control_char, unicode, quality, semantic
    - base/models/document.py       → metadata.diagnostics dict

    This module will become the unified interface for all of them.
    For now it documents the contract and provides the registry.
"""

from dataclasses import dataclass
from typing import Dict

# ==================================
# primitive signal names
# ==================================

PARSE_SUCCESS = "parse_success"
DIAGNOSTICS = "diagnostics"
CONFIDENCE = "confidence"
WARNINGS = "warnings"
CORRUPTION_SIGNALS = "corruption_signals"
MALFORMED_RECOVERY = "malformed_recovery"
EXTRACTION_COMPLETENESS = "extraction_completeness"
METADATA_CONSISTENCY = "metadata_consistency"

ALL_PRIMITIVES = [
    PARSE_SUCCESS,
    DIAGNOSTICS,
    CONFIDENCE,
    WARNINGS,
    CORRUPTION_SIGNALS,
    MALFORMED_RECOVERY,
    EXTRACTION_COMPLETENESS,
    METADATA_CONSISTENCY,
]


# ==================================
# primitive result
# ==================================

@dataclass
class PrimitiveResult:
    """A single shared primitive signal.

    Attributes:
        name:        primitive identifier
        value:       numeric score 0.0–1.0 (or boolean for some)
        detail:      human-readable explanation or raw data
        source:      which module produced this signal
    """
    name: str
    value: float
    detail: str = ""
    source: str = ""


# ==================================
# shared foundation report
# ==================================

def extract_shared_signals(document):
    """Extract shared quality primitives from a Document's metadata.

    This reads the diagnostics and signals that parsers already
    populate during parse(). It does NOT re-compute anything —
    it consolidates what already exists into a uniform interface.

    Args:
        document: Document with text and metadata

    Returns:
        dict of primitive_name → PrimitiveResult
    """
    meta = document.metadata
    diagnostics = meta.get("diagnostics", {})
    text = document.text

    results = {}

    # --- parse_success ---
    parse_success = meta.get("parse_success", True)
    results[PARSE_SUCCESS] = PrimitiveResult(
        name=PARSE_SUCCESS,
        value=1.0 if parse_success else 0.0,
        detail="parser completed successfully" if parse_success
               else "parser reported failure",
        source="metadata.parse_success",
    )

    # --- diagnostics (consolidated) ---
    printable_ratio = diagnostics.get("printable_ratio", 1.0)
    suspicious_chars = diagnostics.get("suspicious_chars", 0)
    replacement_chars = diagnostics.get("replacement_chars", 0)
    diag_score = 1.0
    if printable_ratio < 0.8:
        diag_score -= (0.8 - printable_ratio) * 2
    if suspicious_chars > 0:
        diag_score -= min(suspicious_chars / max(len(text), 1), 1) * 3
    if replacement_chars > 0:
        diag_score -= min(replacement_chars / max(len(text), 1), 1) * 5
    results[DIAGNOSTICS] = PrimitiveResult(
        name=DIAGNOSTICS,
        value=max(diag_score, 0.0),
        detail=f"printable_ratio={printable_ratio:.3f}, "
               f"suspicious={suspicious_chars}, "
               f"replacement={replacement_chars}",
        source="metadata.diagnostics",
    )

    # --- corruption_signals ---
    hidden_unicode = diagnostics.get("hidden_unicode", [])
    has_ansi = diagnostics.get("contains_ansi_escape", False)
    corruption_count = len(hidden_unicode) + (1 if has_ansi else 0)
    results[CORRUPTION_SIGNALS] = PrimitiveResult(
        name=CORRUPTION_SIGNALS,
        value=1.0 - min(corruption_count / 10, 1.0),
        detail=f"hidden_unicode={len(hidden_unicode)}, "
               f"ansi_escape={has_ansi}",
        source="metadata.diagnostics",
    )

    # --- warnings ---
    warnings = meta.get("warnings", [])
    results[WARNINGS] = PrimitiveResult(
        name=WARNINGS,
        value=1.0 if not warnings else max(0.3, 1.0 - len(warnings) * 0.2),
        detail=f"{len(warnings)} warning(s): {warnings[:3]}",
        source="metadata.warnings",
    )

    # --- extraction_completeness ---
    # measures whether meaningful content was extracted
    if text and len(text.strip()) > 0:
        results[EXTRACTION_COMPLETENESS] = PrimitiveResult(
            name=EXTRACTION_COMPLETENESS,
            value=1.0,
            detail=f"extracted {len(text)} chars",
            source="document.text",
        )
    else:
        results[EXTRACTION_COMPLETENESS] = PrimitiveResult(
            name=EXTRACTION_COMPLETENESS,
            value=0.0,
            detail="empty or whitespace-only text",
            source="document.text",
        )

    # --- metadata_consistency ---
    # checks if expected metadata fields are present
    expected_fields = ["file_type", "encoding_used"]
    present = sum(1 for f in expected_fields if meta.get(f))
    results[METADATA_CONSISTENCY] = PrimitiveResult(
        name=METADATA_CONSISTENCY,
        value=present / len(expected_fields),
        detail=f"{present}/{len(expected_fields)} expected fields present",
        source="metadata",
    )

    # --- malformed_recovery ---
    encoding_fallback = meta.get("encoding_fallback")
    results[MALFORMED_RECOVERY] = PrimitiveResult(
        name=MALFORMED_RECOVERY,
        value=0.7 if encoding_fallback else 1.0,
        detail=f"encoding_fallback={encoding_fallback}"
               if encoding_fallback
               else "no fallback needed",
        source="metadata.encoding_fallback",
    )

    # --- confidence (aggregate) ---
    all_values = [r.value for r in results.values()]
    results[CONFIDENCE] = PrimitiveResult(
        name=CONFIDENCE,
        value=min(all_values) if all_values else 0.0,
        detail="min of all primitive scores",
        source="aggregate",
    )

    return results


def summarize_shared_signals(signals: Dict[str, PrimitiveResult]) -> dict:
    """Convert signals dict to a JSON-serializable summary."""
    return {
        name: {
            "value": round(s.value, 3),
            "detail": s.detail,
        }
        for name, s in signals.items()
    }
