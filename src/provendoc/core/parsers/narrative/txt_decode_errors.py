# ============================================================
# Narrative Domain
# TXT per-byte decode error registry (observation only)
# ============================================================
"""Byte-level decode error facts for TXT files.

Frozen rules (txt_engineering_gaps_prereg_v1 G4): re-decode the raw
file bytes strictly with the final ``encoding_used``; each
``UnicodeDecodeError`` yields byte-exact evidence (start/end/reason/
raw hex).  Character offsets are unstable under mojibake and
multibyte encodings — byte offsets into the source file are the
honest coordinates.

Observation-only discipline: no re-decoding is applied to the
document, the text is never touched, entries are capped, and any
failure skips the whole fact silently.
"""

from pathlib import Path
from typing import Any, Dict, Optional

# Entry cap: beyond this the fact stays bounded and says so.
_MAX_ENTRIES = 20
# Hex evidence cap per entry (bytes).
_MAX_HEX_BYTES = 16
# Scan cap: strict re-decode is O(file) per error round; huge files
# only get their prefix scanned and the fact says so honestly.
_MAX_SCAN_BYTES = 16 * 1024 * 1024

# Stateful / pair-aligned codecs where resuming mid-stream after an
# error is meaningless — record the first error and stop.
_STOP_ON_FIRST_ERROR_CODECS = {"utf-16", "utf-16-le", "utf-16-be", "utf-32"}


def collect_decode_errors(
    file_path: str,
    encoding_used: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Register byte-level decode errors, or None when none exist.

    Args:
        file_path: source file (bytes are re-read; never written).
        encoding_used: the codec the parse actually decoded with.

    Returns:
        ``{"encoding_used", "count", "truncated", "entries"}`` when at
        least one strict-decode error exists; ``None`` otherwise
        (absent-key honesty: clean files do not carry the field).
    """
    if not encoding_used:
        return None
    codec = encoding_used.lower()
    try:
        raw = Path(file_path).read_bytes()
    except OSError:
        return None

    scanned = raw[:_MAX_SCAN_BYTES]
    scan_truncated = len(raw) > _MAX_SCAN_BYTES

    entries: list = []
    pos = 0
    while pos < len(scanned) and len(entries) < _MAX_ENTRIES:
        try:
            scanned[pos:].decode(encoding_used, errors="strict")
            break
        except UnicodeDecodeError as exc:
            start = pos + exc.start
            end = pos + exc.end
            entries.append(
                {
                    "byte_start": start,
                    "byte_end": end,
                    "reason": exc.reason,
                    "raw_bytes_hex": raw[start:end][:_MAX_HEX_BYTES].hex(),
                }
            )
            if codec in _STOP_ON_FIRST_ERROR_CODECS:
                break
            pos = end if end > start else start + 1
        except (LookupError, ValueError):
            # unknown codec / surprising decoder state — skip silently
            return None

    if not entries:
        return None

    truncated = (
        len(entries) >= _MAX_ENTRIES
        or scan_truncated
        or (codec in _STOP_ON_FIRST_ERROR_CODECS and pos < len(scanned))
    )
    return {
        "encoding_used": encoding_used,
        "count": len(entries),
        "truncated": truncated,
        "entries": entries,
    }
