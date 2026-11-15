"""Diagnostics subsystem public API.

Parsers import only from here, never from the submodules directly.
"""

from .control_char_diagnostics import count_replacement_chars, count_suspicious_chars
from .diagnostics_builder import build_diagnostics
from .quality_diagnostics import (
    blank_run_excess_ratio,
    emoji_ratio,
    empty_line_ratio,
    printable_ratio,
    symbol_ratio,
)
from .repetition_diagnostics import detect_repetition
from .runtime_profiler import ProfileAggregator, RuntimeProfiler
from .security_carrier import build_carrier_facts, detect_security_carriers
from .semantic_diagnostics import extract_semantic_signals
from .unicode_diagnostics import contains_ansi_escape, detect_hidden_unicode
from .warnings_builder import build_warnings

__all__ = [
    "count_replacement_chars",
    "count_suspicious_chars",
    "printable_ratio",
    "symbol_ratio",
    "empty_line_ratio",
    "blank_run_excess_ratio",
    "emoji_ratio",
    "detect_hidden_unicode",
    "contains_ansi_escape",
    "detect_repetition",
    "extract_semantic_signals",
    "build_diagnostics",
    "build_warnings",
    "detect_security_carriers",
    "build_carrier_facts",
    "RuntimeProfiler",
    "ProfileAggregator",
]
