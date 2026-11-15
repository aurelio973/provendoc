# ============================================================
# Image Assets — dimension facts
# ============================================================
"""Pixel-dimension facts parsed from image byte headers.

Evidence-only: dimensions are read from the stream headers the producer
wrote (PNG IHDR, JPEG SOF, GIF/BMP logical screen / header fields) —
never decoded, never re-encoded, never guessed.  ``dpi_from_points`` /
``dpi_from_cm`` are plain arithmetic over two recorded facts (pixel
size and placed size), so a "low-resolution placement" stays a
verifiable number instead of an adjective.

Honesty rules:

- an unparseable or truncated header yields ``(None, None)`` — a fact
  gap, never an exception and never a fabricated size;
- JPEG scans SOF0/1/2/… markers (C0–CF except C4/C8/CC) so progressive
  and lossless variants resolve too;
- ``effective_dpi`` returns None whenever either input or the placed
  size is missing or non-positive.
"""

from __future__ import annotations

import struct

# JPEG SOF markers that carry frame dimensions (C0-CF minus restart,
# DAC and JPG/private reserved slots).
_JPEG_SOF_MARKERS = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def pixel_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Read (width, height) from a PNG/JPEG/GIF/BMP byte header.

    Returns ``(None, None)`` when the format is unrecognized or the
    header is too short — the caller records the gap honestly.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
        return None, None
    data = bytes(data)

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        # IHDR sits at a fixed offset: 8 (signature) + 4 (length) + 4 (IHDR)
        if len(data) < 24:
            return None, None
        width, height = struct.unpack(">II", data[16:24])
        return width, height

    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)

    if data.startswith((b"GIF87a", b"GIF89a")):
        if len(data) < 10:
            return None, None
        width, height = struct.unpack("<HH", data[6:10])
        return width, height

    if data.startswith(b"BM"):
        if len(data) < 26:
            return None, None
        width, height = struct.unpack("<ii", data[18:26])
        return abs(width), abs(height)

    return None, None


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Walk JPEG markers until a SOF frame header is found."""
    offset = 2  # skip the SOI marker
    while offset + 4 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            # standalone markers: no length field
            offset += 2
            continue
        if offset + 4 > len(data):
            break
        segment_length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if marker in _JPEG_SOF_MARKERS:
            if offset + 9 <= len(data) and segment_length >= 7:
                height, width = struct.unpack(
                    ">HH", data[offset + 5:offset + 9]
                )
                return width, height
            return None, None
        if marker == 0xDA:  # SOS: pixel data follows, no SOF found
            return None, None
        offset += 2 + segment_length
    return None, None


def dpi_from_points(
    pixel_size: int | None, placed_size_pt: float | None
) -> float | None:
    """Rendering DPI for a PDF placement (PDF points are 1/72 inch)."""
    if pixel_size is None or placed_size_pt is None or placed_size_pt <= 0:
        return None
    if pixel_size <= 0:
        return None
    return round(pixel_size / (placed_size_pt / 72.0), 1)


def dpi_from_cm(
    pixel_size: int | None, placed_size_cm: float | None
) -> float | None:
    """Rendering DPI for a DOCX placement (extent converted to cm)."""
    if pixel_size is None or placed_size_cm is None or placed_size_cm <= 0:
        return None
    if pixel_size <= 0:
        return None
    return round(pixel_size / (placed_size_cm / 2.54), 1)


__all__ = ["pixel_dimensions", "dpi_from_points", "dpi_from_cm"]
