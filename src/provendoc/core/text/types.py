from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DecodeResult:
    """Return value of safe_decode, carrying the full decode history."""
    text: str
    encoding_used: str = ""
    encoding_fallback: bool = False
    encoding_detection: dict = field(default_factory=dict)
    decode_trace: list[str] = field(default_factory=list)
    detected_encoding: str | None = None
