# ============================================================
# core/parsers/narrative/txt_encoding_segments.py
# ============================================================
"""Mixed-encoding segment-level candidate evidence (observation-only; never re-decodes the body text).

Discipline (txt_recovery_evidence_prereg_v1 §3):

- A segment = a contiguous interval of byte lines that **strictly fail to
  decode** under the main decoding (``encoding_used``); candidate encodings
  = a ranked statistical guess list over the segment bytes
  (charset_normalizer, the same library as the main pipeline — no new
  dependencies) — a single best guess is unstable on short segments
  (gbk/big5 decode the same bytes both ways); a ranked list is the honest
  shape of statistical evidence;
- Only segment boundaries and candidate encodings are registered; the body
  text is never re-decoded with a candidate encoding;
- Single-byte codecs (iso8859-*/cp125*/mac_* families — every byte decodes
  strictly) have no observable anchor → not detected (honest exclusion of
  c3-type invisible mojibake; prefer honest absence to fabrication);
- Caps: MAX_BYTES (bounded re-read) / MAX_SEGMENTS (bounded evidence,
  excess truncated and registered); no candidates → None (write-non-empty-only
  discipline).
"""

import codecs
import logging
from functools import lru_cache

from charset_normalizer import from_bytes

logger = logging.getLogger(__name__)

EXTRACTOR_ID = "encoding_segment_candidates_v1"

MAX_BYTES = 4 * 1024 * 1024
MAX_SEGMENTS = 10
# Byte cap for statistical probing of a single segment (bounded evidence; the excess never affects segment boundaries).
MAX_PROBE_BYTES = 64 * 1024


@lru_cache(maxsize=32)
def _is_single_byte_codec(encoding):
    """Single-byte codec determination: single-byte iff all 256 byte values decode strictly.

    Runtime probing instead of a maintained list — codec aliases/families are
    adjudicated by the codecs registry, avoiding drift between list and behavior.
    """
    try:
        name = codecs.lookup(encoding).name
    except LookupError:
        return False
    for value in range(256):
        try:
            bytes([value]).decode(name, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
    return True


def _probe_candidate_encodings(segment_bytes, max_candidates=5):
    """Statistical encoding guess over segment bytes (ranked list, best first).

    Admission rule: all candidates **chaos-tied** with the best are admitted
    (cap ``max_candidates``) — short-segment CJK multi-byte encodings are
    statistically indistinguishable (observed: gbk/big5/cp949/euc_jis all
    chaos=0.0); truncating ties would fabricate discrimination. No results /
    failures return an empty list — a failed statistical probe does not
    disqualify; the segment-boundary evidence remains valid.
    """
    try:
        matches = from_bytes(segment_bytes[:MAX_PROBE_BYTES])
    except Exception:  # pragma: no cover - defensive guard
        return []
    candidates = []
    best_chaos = None
    for match in matches:
        if len(candidates) >= max_candidates:
            break
        encoding = getattr(match, "encoding", None)
        if not encoding:
            continue
        try:
            chaos = round(float(match.chaos), 4)
        except (TypeError, ValueError):
            chaos = None
        if best_chaos is None:
            best_chaos = chaos
        elif chaos != best_chaos:
            break  # tie admission ends here — worse guesses are not registered
        candidates.append({"encoding": encoding, "chaos": chaos})
    return candidates


def detect_encoding_segment_candidates(
    file_path,
    encoding_used,
    max_bytes=MAX_BYTES,
    max_segments=MAX_SEGMENTS,
):
    """Register byte-line segments that strictly fail under the main decoding (candidate evidence).

    Args:
        file_path: original file path (bytes re-read in bounded fashion).
        encoding_used: the decoding encoding actually used by the main
            pipeline (``metadata["encoding_used"]``).
        max_bytes: re-read cap; excess is not detected (prefer honest absence
            until an anchor-based scalable approach exists).
        max_segments: evidence cap; excess is truncated and ``truncated`` is registered.

    Returns:
        dict or None: None when there are no candidates or it does not apply.
    """
    if not encoding_used:
        return None
    try:
        codecs.lookup(encoding_used)
    except LookupError:
        return None
    if _is_single_byte_codec(encoding_used):
        # Single-byte codecs decode every byte strictly — no observable anchor,
        # honest exclusion of c3-type invisible mojibake (prereg §3 rule 2).
        return None
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        logger.debug("encoding segment scan read skipped: %s", exc)
        return None
    if len(raw) > max_bytes:
        logger.debug(
            "encoding segment scan skipped: file exceeds %d bytes",
            max_bytes,
        )
        return None

    # Strict per-line decoding; failing lines are candidate lines (1-based line numbers, byte offsets tracked in sync).
    failing = []
    pos = 0
    for line_no, line_bytes in enumerate(raw.split(b"\n"), start=1):
        start, end = pos, pos + len(line_bytes)
        pos = end + 1
        try:
            line_bytes.decode(encoding_used, errors="strict")
        except (UnicodeDecodeError, ValueError):
            failing.append((line_no, start, end))
    if not failing:
        return None

    # Consecutive failing lines are grouped into segments.
    groups = [[failing[0]]]
    for entry in failing[1:]:
        if entry[0] == groups[-1][-1][0] + 1:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    truncated = len(groups) > max_segments
    groups = groups[:max_segments]

    segments = []
    for group in groups:
        byte_start, byte_end = group[0][1], group[-1][2]
        segments.append(
            {
                "line_start": group[0][0],
                "line_end": group[-1][0],
                "byte_start": byte_start,
                "byte_end": byte_end,
                "strict_fail_lines": len(group),
                "candidate_encodings": _probe_candidate_encodings(raw[byte_start:byte_end]),
            }
        )

    result = {
        "extractor": EXTRACTOR_ID,
        "encoding_used": encoding_used,
        "segments": segments,
        "note": "candidacy only - no re-decode applied to document text",
    }
    if truncated:
        result["truncated"] = True
    return result
