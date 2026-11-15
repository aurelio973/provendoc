"""Text processing subsystem public API.

Parsers import only from here, never from the submodules directly.

Layering contract:
    - the transform layer (core/text) only changes text, no warnings;
    - the observation layer (core/diagnostics) only observes, owns
      warnings;
    - the orchestration layer (base/parser) diagnoses first, then
      replaces, and records replacement stats in metadata.
"""

from .decoding import is_binary_suspected, safe_decode
from .normalization import normalize_newlines, sanitize_invisible_chars, strip_bom
from .types import DecodeResult

__all__ = [
    "is_binary_suspected",
    "safe_decode",
    "normalize_newlines",
    "strip_bom",
    "sanitize_invisible_chars",
    "DecodeResult",
]
