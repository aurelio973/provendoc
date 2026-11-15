# ============================================================
# Code Domain
# Per-byte decode error registry + UTF-8 corruption probe
# ============================================================
"""Byte-level decode error facts for code-domain files.

Frozen rules (code_engineering_facts_prereg_v1 P2, frozen 2026-10-25
D2=B): two observation tiers, both observation-only — the document is
never re-decoded, the text is never touched, and any failure skips the
whole fact silently.

Tier 1 (txt-engineering-gaps G4 contract, shared implementation):
re-decode the raw file bytes strictly with the final ``encoding_used``;
each ``UnicodeDecodeError`` yields byte-exact evidence.  Reuses
``txt_decode_errors.collect_decode_errors`` verbatim (single
implementation, no drift — same precedent as markdown reusing the code
backend for fenced blocks).

Tier 2 (``utf8_corrupt_suspected``, code-domain addition): corpus
evidence (2026-10-25 scan, 19/20 corrupt-byte files) shows statistical
detection routes corrupted bytes to exotic *single-byte* codecs
(cp775 / mac_iceland / hp_roman8 / …).  A single-byte codec decodes any
byte sequence without failing, so Tier 1 stays empty and the per-byte
corruption is silently masked.  When strict UTF-8 decoding fails, Tier
1 found nothing, and the detected codec is single-byte, the fact
registers the first UTF-8 error offset plus the detected codec name as
a *suspicion* — corruption cannot be falsified under a single-byte
codec, so certainty is never claimed.  Multibyte codecs that decode the
file cleanly (e.g. a legitimate gb18030 file) are skipped: valid
multibyte decoding is positive evidence of a real alternate encoding,
not of corruption.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from provendoc.core.parsers.narrative.txt_decode_errors import (
    collect_decode_errors,
)

_PROBE_NOTE = (
    "strict utf-8 decoding fails at this offset while the detected "
    "single-byte codec decodes every byte without error; a single-byte "
    "codec cannot falsify corruption, so this is registered as "
    "suspicion only — the text is never re-decoded or rewritten"
)


def _decodes_as_single_byte(codec: str, raw: bytes) -> bool:
    """True when ``codec`` decodes ``raw`` cleanly 1 byte = 1 char."""
    try:
        text = raw.decode(codec)
    except (UnicodeDecodeError, LookupError, ValueError):
        return False
    return len(text) == len(raw)


def _first_utf8_error_offset(raw: bytes) -> Optional[int]:
    """Byte offset of the first strict-UTF-8 violation, if any."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start
    return None


def collect_code_decode_errors(
    file_path: str,
    encoding_used: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Register byte-level decode errors and masked-corruption suspicion.

    Args:
        file_path: source file (bytes are re-read; never written).
        encoding_used: the codec the parse actually decoded with.

    Returns:
        Tier-1 shape ``{"encoding_used", "count", "truncated",
        "entries"}`` when strict re-decoding under ``encoding_used``
        fails; tier-2 shape ``{"encoding_used",
        "utf8_corrupt_suspected": {...}}`` when single-byte masking is
        suspected; ``None`` otherwise (absent-key honesty: clean files
        do not carry the field).
    """
    if not encoding_used:
        return None

    tier1 = collect_decode_errors(file_path, encoding_used)
    if tier1 is not None:
        return tier1

    try:
        raw = Path(file_path).read_bytes()
    except OSError:
        return None

    offset = _first_utf8_error_offset(raw)
    if offset is None:
        return None
    if not _decodes_as_single_byte(encoding_used, raw):
        return None

    return {
        "encoding_used": encoding_used,
        "utf8_corrupt_suspected": {
            "first_error_byte_offset": offset,
            "note": _PROBE_NOTE,
        },
    }
