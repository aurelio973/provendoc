from __future__ import annotations

from charset_normalizer import from_bytes

from provendoc.core.text.types import DecodeResult


def is_binary_suspected(raw_bytes: bytes) -> bool:
    """Return True when null bytes exceed 1% of the payload."""
    if not raw_bytes:
        return False
    null_ratio = raw_bytes.count(b"\x00") / len(raw_bytes)
    return null_ratio > 0.01


def _detect_encoding(raw_bytes: bytes) -> tuple[str | None, dict]:
    """BOM first, statistical detection as fallback. Returns
    (encoding, detection_info)."""

    # UTF-16 LE / BE
    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        return "utf-16", {"method": "bom", "encoding": "utf-16"}

    # UTF-8 BOM
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", {"method": "bom", "encoding": "utf-8-sig"}

    # Statistical detection. CJK single-byte ranges overlap heavily:
    # the same GBK text can be misdetected as CP949 — both decode
    # without errors. When GB18030 (a GBK superset) is also among the
    # candidates, prefer it explicitly to avoid silent mojibake.
    candidates = list(from_bytes(raw_bytes))
    if not candidates:
        return None, {"method": "statistical", "success": False}

    detected = candidates[0]
    candidate_encodings = [
        candidate.encoding.lower()
        for candidate in candidates
        if candidate.encoding
    ]
    if detected.encoding and detected.encoding.lower() in {"cp949", "euc_kr"}:
        gb18030 = next(
            (
                candidate
                for candidate in candidates
                if candidate.encoding and candidate.encoding.lower() == "gb18030"
            ),
            None,
        )
        if gb18030 is not None:
            detected = gb18030

    chaos = getattr(detected, "chaos", None)
    percent_chaos = getattr(detected, "percent_chaos", None)

    info = {
        "method": "statistical",
        "encoding": detected.encoding,
        "chaos": chaos,
        "percent_chaos": percent_chaos,
        "candidates": candidate_encodings[:5],
    }

    # reject high-chaos guesses
    if chaos is not None and chaos > 0.2:
        info["rejected"] = True
        return None, info

    return detected.encoding, info


def safe_decode(
    raw_bytes: bytes,
    encoding: str = "utf-8",
    tolerant: bool = True,
) -> DecodeResult:
    """Safe decode with a fallback chain.

    Attempt order: declared encoding → statistically detected encoding
    → UTF-8 with replacement as the last resort.
    """
    decode_trace = []

    try:
        decode_trace.append(encoding)
        text = raw_bytes.decode(encoding)
        return DecodeResult(
            text=text,
            encoding_used=encoding,
            decode_trace=decode_trace,
        )
    except UnicodeDecodeError:
        if not tolerant:
            raise

    detected_encoding, detection_info = _detect_encoding(raw_bytes)

    if detected_encoding:
        decode_trace.append(f"detected:{detected_encoding}")
        text = raw_bytes.decode(detected_encoding, errors="replace")
        return DecodeResult(
            text=text,
            encoding_used=detected_encoding,
            encoding_detection=detection_info,
            decode_trace=decode_trace,
            detected_encoding=detected_encoding,
        )

    # last resort: utf-8 replace
    decode_trace.append("utf8_replace_fallback")
    text = raw_bytes.decode("utf-8", errors="replace")
    return DecodeResult(
        text=text,
        encoding_used="utf-8",
        encoding_fallback=True,
        encoding_detection=detection_info,
        decode_trace=decode_trace,
    )
