"""Derived image pipeline (OpenCV, optional dependency).

Derived renditions are second-class by design: the original bytes are the
evidence (bit-exact, never modified), while every derived file records the
pipeline name, its parameter fingerprint and the pipeline version so any
rendition is reproducible from the original.

Operations are individually guarded — one bad image (corrupt stream,
extreme aspect ratio, all-white canvas) skips its operation with a
recorded reason instead of failing the export.  ``cv2`` is imported
lazily: without OpenCV the derived layer degrades to "not configured"
and originals still export.
"""

from __future__ import annotations

from typing import Any, Callable

PIPELINE_VERSION = "image_derived_v1"

# Lazy-loaded OpenCV handle; None until first successful import.
_cv2: Any | None = None
_cv2_unavailable: str | None = None


def _cv2_module():
    global _cv2, _cv2_unavailable
    if _cv2 is not None:
        return _cv2
    if _cv2_unavailable is not None:
        raise ImportError(_cv2_unavailable)
    try:
        import cv2
    except Exception as error:  # opencv absent or binary-incompatible
        _cv2_unavailable = f"opencv unavailable: {error}"
        raise ImportError(_cv2_unavailable)
    _cv2 = cv2
    return cv2


def opencv_available() -> bool:
    """Probe whether OpenCV is usable (lazy import, failure reason cached).

    Returns:
        bool: True means cv2 imports and the derived layer is active;
        False degrades the derived layer to "not configured" while
        originals still export.
    """
    try:
        _cv2_module()
        return True
    except ImportError:
        return False


def _decode(data: bytes):
    cv2 = _cv2_module()
    import numpy as np

    # Explicit annotation: type stubs infer frombuffer(dtype=np.uint8)
    # as partially Never-typed; the real semantics are a 1-D uint8 buffer.
    array: np.ndarray = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(array, cv2.IMREAD_UNCHANGED)


def _encode(image, ext: str = ".png") -> bytes:
    cv2 = _cv2_module()
    ok, buffer = cv2.imencode(ext, image)
    if not ok:
        raise ValueError("imencode failed")
    return buffer.tobytes()


def _to_bgr(image):
    """Normalize arbitrary channel counts to 3-channel BGR for processing."""
    if image.ndim == 2:
        return image
    channels = image.shape[2] if image.ndim == 3 else 1
    if channels == 3:
        return image
    if channels == 4:
        cv2 = _cv2_module()
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if channels == 1:
        cv2 = _cv2_module()
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    raise ValueError(f"unsupported channel count: {channels}")


def deskew(image, params: dict[str, Any]):
    """Straighten small rotation angles (scans typically 0.5–2 degrees).

    Line angles measured by a mini Hough transform; the median angle is
    applied only when it stays inside ``max_angle`` (default 5 degrees) —
    a genuinely tilted photo must not be "corrected".  Conservative by
    design: no measurable angle returns the image unchanged.
    """
    cv2 = _cv2_module()
    import numpy as np

    max_angle = float(params.get("max_angle", 5.0))
    gray = image if image.ndim == 2 else cv2.cvtColor(
        _to_bgr(image), cv2.COLOR_BGR2GRAY
    )
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=100,
        minLineLength=int(gray.shape[1] * 0.25), maxLineGap=8,
    )
    if lines is None:
        return image
    angles = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        # Keep near-horizontal text lines; near-vertical lines are column
        # rules whose angle is text-line angle + 90.
        if abs(angle) <= max_angle:
            angles.append(angle)
        elif abs(angle - 90) <= max_angle:
            angles.append(angle - 90)
        elif abs(angle + 90) <= max_angle:
            angles.append(angle + 90)
    if not angles:
        return image
    angle = float(np.median(angles))
    if abs(angle) < 0.05 or abs(angle) > max_angle:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D(
        (width / 2.0, height / 2.0), angle, 1.0
    )
    return cv2.warpAffine(
        image, matrix, (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def autocrop(image, params: dict[str, Any]):
    """Trim uniform borders while keeping a configurable padding.

    ``threshold`` (default 8) treats near-background rows/columns as
    margins; ``padding_fraction`` (default 0.02) keeps a 2% safety band
    so shadowed strokes near the edge are never clipped.
    """
    cv2 = _cv2_module()
    np = _np()

    threshold = int(params.get("threshold", 8))
    padding = float(params.get("padding_fraction", 0.02))
    gray = image if image.ndim == 2 else cv2.cvtColor(
        _to_bgr(image), cv2.COLOR_BGR2GRAY
    )
    background = int(_border_median(gray))
    diff = cv2.absdiff(gray, background)
    mask = diff > threshold
    rows = mask.any(axis=1)
    cols = mask.any(axis=0)
    if not rows.any() or not cols.any():
        return image  # all-background image: cropping would erase it
    top = int(np.argmax(rows))
    bottom = int(len(rows) - 1 - np.argmax(rows[::-1]))
    left = int(np.argmax(cols))
    right = int(len(cols) - 1 - np.argmax(cols[::-1]))
    pad_h = max(1, int(image.shape[0] * padding))
    pad_w = max(1, int(image.shape[1] * padding))
    top = max(0, top - pad_h)
    left = max(0, left - pad_w)
    bottom = min(image.shape[0] - 1, bottom + pad_h)
    right = min(image.shape[1] - 1, right + pad_w)
    if (bottom - top) < 8 or (right - left) < 8:
        return image
    return image[top:bottom + 1, left:right + 1]


def sharpen(image, params: dict[str, Any]):
    """Unsharp mask; ``amount`` (default 0.6) keeps artefacts low."""
    cv2 = _cv2_module()

    amount = float(params.get("amount", 0.6))
    base = _to_bgr(image)
    blurred = cv2.GaussianBlur(base, (0, 0), sigmaX=2.0)
    return cv2.addWeighted(base, 1.0 + amount, blurred, -amount, 0)


def thumbnail(image, params: dict[str, Any]):
    """Long-edge resize; ``max_edge`` (default 512) preserves aspect."""
    cv2 = _cv2_module()

    max_edge = int(params.get("max_edge", 512))
    height, width = image.shape[:2]
    scale = max_edge / float(max(height, width))
    if scale >= 1.0:
        return image
    return cv2.resize(
        image,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


# name -> (callable, default params, output extension)
OPERATIONS: dict[str, tuple[Callable, dict[str, Any], str]] = {
    "deskew": (deskew, {"max_angle": 5.0}, ".png"),
    "autocrop": (
        autocrop,
        {"threshold": 8, "padding_fraction": 0.02},
        ".png",
    ),
    "sharpen": (sharpen, {"amount": 0.6}, ".png"),
    "thumbnail": (thumbnail, {"max_edge": 512}, ".png"),
}

# Default chain: conservative geometric normalization + preview size.
DEFAULT_PIPELINE = ["deskew", "autocrop", "thumbnail"]


def _np():
    import numpy as np

    return np


def _border_median(gray):
    """Median of the four border strips — the assumed page background."""
    np = _np()
    edges = np.concatenate([
        gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1],
    ])
    return np.median(edges)


def build_derived(
    payloads: dict[str, bytes],
    pipeline: list[str] | None = None,
    params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the derived pipeline over extracted originals.

    Returns ``{asset_id: {filename, bytes, pipeline, params, version}}``
    for every asset whose operations produced output; failures are
    recorded per asset/operation and never raise.
    """
    if pipeline is None:
        pipeline = DEFAULT_PIPELINE
    params = params or {}

    results: dict[str, Any] = {}
    for asset_id, data in payloads.items():
        try:
            image = _decode(data)
            if image is None:
                # cv2.imdecode signals corrupt streams by returning None,
                # not by raising
                raise ValueError("imdecode returned None")
        except Exception as error:
            results[asset_id] = {
                "error": f"decode failed: {error}",
                "renditions": {},
            }
            continue

        renditions: dict[str, Any] = {}
        current = image
        applied: list[dict[str, Any]] = []
        for step in pipeline:
            if step not in OPERATIONS:
                applied.append({"operation": step, "status": "unknown_operation"})
                continue
            func, defaults, ext = OPERATIONS[step]
            step_params = {**defaults, **(params.get(step) or {})}
            try:
                current = func(current, step_params)
                applied.append(
                    {"operation": step, "status": "applied",
                     "params": step_params}
                )
            except Exception as error:
                applied.append(
                    {"operation": step, "status": "skipped",
                     "reason": str(error)}
                )
                continue
            # Chain semantics: each step's output is the next step's input;
            # every intermediate is kept (named by step) so renditions are
            # comparable and the final file is the last applied step.
            filename = f"{asset_id}_{step}{ext}"
            renditions[step] = {
                "filename": filename,
                "bytes": _encode(current, ext),
            }
        results[asset_id] = {
            "renditions": renditions,
            "applied": applied,
            "pipeline": list(pipeline),
            "pipeline_version": PIPELINE_VERSION,
        }
    return results


__all__ = [
    "PIPELINE_VERSION",
    "DEFAULT_PIPELINE",
    "OPERATIONS",
    "opencv_available",
    "build_derived",
]
