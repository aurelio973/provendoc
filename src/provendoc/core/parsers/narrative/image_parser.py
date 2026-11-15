# ============================================================
# Narrative Domain
# Image Input Parser + OCR Four-Exit Routing
# (design log §19 routing-honesty #127 → §20 OCR implementation v1)
# ============================================================
"""Image-input parser: magic-number self-verification + a four-outcome OCR route.

Background (#127 minimal loop → §20 OCR implementation, approved 2026-10-13):

- #127 (routing honesty v1): eight extensions including PNG previously had no route registered — the SDK
  falls back to ``TxtParser`` for unknown extensions, decoding image binaries as text; the scenario chain
  spun empty end-to-end for pure-image input, with no structured marker explaining "why the text layer is empty";
- this batch (specs/ocr_image_route_prereg_v1.md, FROZEN) implements on top of it
  an OCR route for pure-image input: pymupdf single-page ``get_textpage_ocr`` (default
  300dpi + a 20MP pixel budget against giant-image blowups — following the pdf ``_effective_dpi_for``
  dpi-reduction discipline, frozen locally). Four probe rounds of evidence (9 real corpus samples + synthetic
  images) are in prereg §0: of the six families pymupdf cannot open webp (outcome ③ honest registration,
  PIL transcode bridging deferred to another batch); the 0079863 specimen is beyond even OCR's rescue (an honest null is
  a terminal state — OCR is not a panacea).

Behavior contract (prereg §1, four outcomes):

- Magic-number self-verification unchanged (six families + the honest-failure discipline untouched; same as ``.dot``×
  Graphviz: a suffix claiming image while the bytes match no known magic number →
  ``parse_failed``, never guessing text out of binary; empty files follow the same rule);
- ① Backend unavailable (pymupdf missing / tessdata unresolvable) → empty body +
  ``OCR_ROUTE_UNAVAILABLE`` unsupported tier (the #127 behavior kept as-is;
  out of the capability domain, not a parse failure);
- ② OCR ran but output is empty → empty body + ``OCR_EMPTY_OUTPUT``
  unresolved tier (reusing an existing registered code, exact semantic fit, zero re-definition);
- ③ OCR execution failure (pymupdf cannot open the container [webp] / rendering exception) →
  empty body + ``OCR_EXECUTION_FAILED`` unresolved tier (**new code**, vocab
  double-table RECOVERY category / loss None — same family as OCR_EMPTY_OUTPUT);
- ④ Success non-empty → ``Document.text = OCR text`` (the downstream Txt narrative channel and
  the scenario chain activate naturally — pure-image input no longer spins empty), ``metadata["image_ocr"]``
  registers channel facts (backend/language/dpi/tessdata/chars/blocks + quality
  proxies replacement_density / nonprintable_rate — the D4 GT bucket has zero thresholds that can be
  frozen, **fact registration is not a gate**), and no ocr_unresolved record is written
  (no unresolved observation).

tessdata resolution chain (prereg §1): explicit argument → in-repo ``.tessdata`` →
system ``fitz.get_tessdata()``; explicitly given but unusable → outcome ① (no silent
downgrade overriding the explicit configuration — honest attribution). Language defaults to ``chi_sim+eng`` (bilingual-corpus
evidence + both packages present in the in-repo .tessdata; overridable via constructor).

2026-10-16 increment (scenario wiring): outcome ④ additionally stamps
``metadata["domain_signals"]``（``detect_domain_signals(...)``
same convention as the 7 narrative parsers) — this module's contract claimed from day one that "the scenario chain
activates naturally", but SDK scenario routing reads ``metadata.domain_signals``, and without the stamp
it spins empty (docstring promise vs implementation gap; blind-test report filed, wired up in this batch).
What gets stamped is the factual evidence of the OCR text; failure outcomes ①②③ still stamp nothing (an empty body has no
evidence to classify — absence is not failure).

2026-10-16 increment 2 (CJK newline-split-word normalization, prereg
image_ocr_cjk_newline_join_prereg_v1): the outcome-④ OCR text first passes
``_cjk_newline_join`` (merging a single newline between two CJK characters, zero character rewrites)
before entering the body and being stamped — OCR line boxes cutting "纳税人识别号" into "纳税\n人识别号"
(evidenced by the batch-1 pre-survey; zero evidence for the inserted-space hypothesis) no longer presses down on the marker words;
wrong glyphs (号→与) are not fixed — as-is. The _INVOICE_MARKERS frozen layer is untouched.

Honest boundary (prereg §2, hard-written, do not cross): mixed-form PDF image-region re-fishing is not in
this batch (needs the invoice prereg D6 unified path + channel arbitration design); webp has no
PIL transcode (pillow is not a declared dependency; no dependency added for a rare format); zero network (tessdata
in-repo/system, no downloads); multi-frame animated images read the first frame only in v1.

Layering contract: parsers do not import core.pipeline (one-way dependency
pipeline→parsers), so the magic-number table and the dpi-reduction discipline are frozen locally rather than reusing the pdf
evidence module (which is bound to the PDFPage contract); nor is the format_verification
container-family table reused (it has no image-family entries; image extensions go through ``no_magic_expected``
pass-through, self-verified by this parser). The ``BaseParser._read_and_diagnose`` shared
pipeline is not used — that pipeline is a **text** pipeline of read→decode→diagnose; force-decoding binary
images would repeat the old fallback-to-TxtParser mistake; sha256 / binary_suspected
facts are still registered as-is (attributable-failure discipline).
"""

import hashlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
    stamp_unresolved_registry,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.text import is_binary_suspected

logger = logging.getLogger(__name__)

# Magic-number self-verification table (v1 frozen: six families, prefix matching; TIFF/WebP take the OR of two signatures).
# Suffix-independent — any file with a registered image extension is verified as an image when any of the
# six magic numbers hits (a .jpg renamed to .png is handled honestly as a format-level fact;
# image_profile.suffix records the declared suffix as-is, no rejection).
_IMAGE_MAGIC_SIGNATURES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("png", (b"\x89PNG\r\n\x1a\n",)),
    ("jpeg", (b"\xff\xd8\xff",)),
    ("gif", (b"GIF87a", b"GIF89a")),
    ("bmp", (b"BM",)),
    ("tiff", (b"II*\x00", b"MM\x00*")),
    ("webp", (b"RIFF",)),  # RIFF container needs a second WEBP-segment check, see _detect_format
)

# ocr_unresolved reason_code frozen list (add-only, no semantic changes; all three codes are
# registered in sync in the vocab_registry double table — OCR_EXECUTION_FAILED is the §20
# new code, the other two reuse existing registrations):
# - OCR_ROUTE_UNAVAILABLE  ① backend unavailable (unsupported tier, out of capability domain)
# - OCR_EMPTY_OUTPUT       ② OCR ran, output empty (unresolved tier,
#   reusing the pdf domain's existing registered code, zero re-definition)
# - OCR_EXECUTION_FAILED   ③ execution failure — container unopenable / rendering exception
#   (unresolved tier, new code: RECOVERY category / loss None)
REASON_CODE_OCR_ROUTE_UNAVAILABLE = "OCR_ROUTE_UNAVAILABLE"
REASON_CODE_OCR_EMPTY_OUTPUT = "OCR_EMPTY_OUTPUT"
REASON_CODE_OCR_EXECUTION_FAILED = "OCR_EXECUTION_FAILED"

# OCR backend identifier (image_ocr.backend; same string as the pdf-domain ocr.py).
_OCR_BACKEND = "pymupdf_tesseract"

# CJK newline-split-word normalization (image_ocr_cjk_newline_join_prereg_v1, 2026-10-16):
# OCR line boxes cut marker words across lines (measured "纳税\n人识别号" ×4, "价税\n合计"
# — characters all correct, only the line break), so the inline regexes of detect_domain_signals / KIE anchors
# cannot cross that \n. Only "a single newline whose previous and next neighbors are both CJK characters" is merged: zero character
# rewrites; \n\n paragraphs are naturally preserved (the first \n's next neighbor is \n, not CJK — no match);
# ASCII text untouched. Applies only to the image-OCR outcome; the PDF text layer (real layout
# semantics) and the _INVOICE_MARKERS frozen layer (4,533 calibration) are untouched.
_CJK_CLASS = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_CJK_NEWLINE_RE = re.compile(
    rf"(?<=[{_CJK_CLASS}])\r?\n(?=[{_CJK_CLASS}])"
)

# tessdata resolution chain, tier 2: in-repo .tessdata (walking up 6 levels from this file in the src layout
# reaches the repo root; in installed form the directory does not exist → the chain naturally falls to the system tier).
_REPO_TESSDATA = Path(__file__).resolve().parents[5] / ".tessdata"

# D4 quality-proxy charset (prereg §0 measured range across 9 samples: 0.0002–0.0164;
# GT bucket is zero → thresholds cannot be frozen; v1 registers facts only, no gating). Same form as the four probe rounds:
# common OCR replacement characters + non-printable control characters.
_OCR_REPLACEMENT_CHARS = frozenset("\ufffd`~|")
_OCR_NONPRINTABLE_CTRL = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f"
)

# Failure-outcome warning texts (one each for ①②③; outcome ④ produces no warning).
_OCR_WARNING_BY_REASON: dict[str, str] = {
    REASON_CODE_OCR_ROUTE_UNAVAILABLE: (
        "image OCR backend unavailable (pymupdf/tessdata unresolvable); "
        "no text extracted (see metadata.ocr_unresolved)"
    ),
    REASON_CODE_OCR_EMPTY_OUTPUT: (
        "image OCR completed but produced no text "
        "(see metadata.ocr_unresolved)"
    ),
    REASON_CODE_OCR_EXECUTION_FAILED: (
        "image OCR execution failed (container unsupported by backend "
        "or render error; see metadata.ocr_unresolved)"
    ),
}


def _import_fitz() -> Any:
    """Lazily import pymupdf (older distributions fall back to ``fitz``; both missing → None → outcome ①)."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError:
            return None
    return fitz


def _repo_tessdata() -> Path | None:
    """In-repo .tessdata tier (usable only when the directory exists and holds traineddata packages)."""
    if _REPO_TESSDATA.is_dir() and any(_REPO_TESSDATA.glob("*.traineddata")):
        return _REPO_TESSDATA
    return None


def _quality_proxies(text: str) -> dict[str, float]:
    """D4 quality proxies (fact registration, not a gate — same form as the four probe rounds)."""
    if not text:
        return {"replacement_density": 0.0, "nonprintable_rate": 0.0}
    total = len(text)
    replacement = sum(1 for ch in text if ch in _OCR_REPLACEMENT_CHARS)
    nonprintable = sum(1 for ch in text if ch in _OCR_NONPRINTABLE_CTRL)
    return {
        "replacement_density": round(replacement / total, 4),
        "nonprintable_rate": round(nonprintable / total, 4),
    }


def _cjk_newline_join(text: str) -> str:
    """OCR line-box typographic-noise normalization: merge a single newline between two CJK characters.

    as-is boundary (prereg §1): only the newline is removed; no character touched, no glyph fixed,
    no word guessed; \n\n paragraphs, ASCII newlines, and non-CJK-adjacent newlines are all preserved as-is.
    """
    return _CJK_NEWLINE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Perspective-rescue geometry block (image_ocr_perspective_rescue_prereg_v1, 2026-10-16 batch 2)
# ---------------------------------------------------------------------------

# Optional-dependency gating (prereg §1): when opencv-python (Apache-2.0, via the pdf-layout
# extra or the image-ocr extra / installed by the consumer) is absent the rescue is skipped wholesale; the debug
# log keeps an audit record — the rescue is a bonus path, not a route precondition. numpy is always
# present alongside cv2, imported under the same gate; a three-way cache avoids repeated import overhead.
_rescue_modules: tuple[Any, Any] | None = None
_rescue_modules_resolved = False

# quad-detection parameters (prereg §2 frozen, probe-evidenced calibration):
# Gaussian blur 5×5 → Canny 50/150 → 3×3 dilate → top-8 contours by RETR_LIST area,
# area ∈ (20%, 99%) of the frame, approxPolyDP 0.02·perimeter, convex quadrilateral;
# warp target side length = max of opposite-edge norms; any edge < 50px counts as degenerate and gives up outright.
_QUAD_MIN_AREA_RATIO = 0.2
_QUAD_MAX_AREA_RATIO = 0.99
_QUAD_TOP_CONTOURS = 8
_QUAD_APPROX_EPSILON = 0.02
_WARP_MIN_SIDE = 50


def _import_rescue_modules() -> tuple[Any, Any] | None:
    """Lazily import cv2 + numpy (absent → None → rescue skipped, audit via debug)."""
    global _rescue_modules, _rescue_modules_resolved
    if not _rescue_modules_resolved:
        try:
            import cv2
            import numpy
        except Exception:  # pragma: no cover - environment dependent
            _rescue_modules = None
        else:
            _rescue_modules = (cv2, numpy)
        _rescue_modules_resolved = True
    if _rescue_modules is None:
        logger.debug(
            "perspective rescue skipped: opencv-python not installed "
            "(optional rescue path, prereg "
            "image_ocr_perspective_rescue_prereg_v1)"
        )
    return _rescue_modules


def _find_document_quad(cv2: Any, np: Any, image: Any):
    """Document quadrilateral detection (prereg §2 frozen parameters; None when nothing found)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.dilate(
        cv2.Canny(blurred, 50, 150), np.ones((3, 3), dtype=np.uint8)
    )
    contours, _ = cv2.findContours(
        edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None
    frame_area = float(image.shape[0]) * float(image.shape[1])
    for contour in sorted(
        contours, key=cv2.contourArea, reverse=True
    )[:_QUAD_TOP_CONTOURS]:
        area = cv2.contourArea(contour)
        if not (
            _QUAD_MIN_AREA_RATIO * frame_area
            < area
            < _QUAD_MAX_AREA_RATIO * frame_area
        ):
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(
            contour, _QUAD_APPROX_EPSILON * perimeter, True
        )
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2)
    return None


def _order_quad_points(np: Any, points: Any):
    """Four-point ordering: min row-sum = top-left / max = bottom-right; min row-diff = top-right / max = bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1)
    rect[0] = points[np.argmin(sums)]
    rect[2] = points[np.argmax(sums)]
    rect[1] = points[np.argmin(diffs)]
    rect[3] = points[np.argmax(diffs)]
    return rect


def _warp_perspective(cv2: Any, np: Any, image: Any, quad: Any):
    """Perspective correction (target rectangle side lengths take the max opposite-edge norm; degenerate → None)."""
    rect = _order_quad_points(np, quad.astype("float32"))
    tl, tr, br, bl = rect
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if width < _WARP_MIN_SIDE or height < _WARP_MIN_SIDE:
        return None
    matrix = cv2.getPerspectiveTransform(
        rect,
        np.array(
            [
                [0, 0],
                [width - 1, 0],
                [width - 1, height - 1],
                [0, height - 1],
            ],
            dtype="float32",
        ),
    )
    return cv2.warpPerspective(image, matrix, (width, height))


def _clahe_gray(cv2: Any, image: Any):
    """CLAHE grayscale (clip 2.0, 8×8 — variant-ladder tier 2, robust to lighting gradients)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(
        gray
    )


def _total_voting_kinds(signals: dict) -> int:
    """Total voting kinds across families (the rescue keep criterion, prereg §2)."""
    return sum(
        family["voting_kinds"] for family in signals["families"].values()
    )


def _detect_image_format(raw_bytes: bytes) -> str | None:
    """Magic-number self-verification: return the matched image family name, or None on no match.

    WebP special case: a RIFF header alone is not proof (RIFF is a generic container family; WAV/AVI
    share the header) — the ``WEBP`` segment at offset 8 must confirm.
    """
    for format_name, signatures in _IMAGE_MAGIC_SIGNATURES:
        for signature in signatures:
            if raw_bytes.startswith(signature):
                if format_name == "webp":
                    if len(raw_bytes) >= 12 and raw_bytes[8:12] == b"WEBP":
                        return "webp"
                    continue
                return format_name
    return None


class _OCROutcome(NamedTuple):
    """Triple result of one OCR attempt (the input fact for the four-outcome route)."""

    text: str                 # ④ OCR text on success; "" for every other outcome
    image_ocr: dict | None    # channel facts on ④ success; None for the other outcomes
    failure_reason: str | None  # failure code for ①②③; None on ④ success


class ImageParser(BaseParser):
    """Image-input parser (magic-number self-verification + four-outcome OCR route).

    The success outcome (④) produces OCR text + channel facts + a ``domain_signals``
    stamp (2026-10-16: same convention as the 7 narrative parsers; the scenario chain
    is activated by it — the downstream Txt narrative channel + scenario routing no longer spin empty for pure-image input);
    the three failure outcomes (①②③) produce **observation registration**: empty body +
    a single ``ocr_unresolved`` record, no ``domain_signals`` /
    ``heading_candidates`` (an empty body has no evidence to classify — the scenario chain
    absence is not failure).
    """

    def __init__(
        self,
        *,
        language: str = "chi_sim+eng",
        dpi: int = 300,
        tessdata: str | Path | None = None,
        max_page_pixels: int = 20_000_000,
    ):
        """Configure OCR language / resolution / tessdata / pixel budget (prereg §1).

        Args:
            language: Tesseract language string (``+`` combines languages; default
                ``chi_sim+eng`` — bilingual-corpus evidence + both packages
                present in the in-repo .tessdata).
            dpi: rendering resolution (default 300).
            tessdata: explicit tessdata directory; ``None`` = follow the resolution chain
                (in-repo .tessdata → system fitz.get_tessdata()).
            max_page_pixels: per-page pixel budget (default 20MP; over the limit the dpi is
                scaled down proportionally, following the pdf ``_effective_dpi_for`` discipline).
        """
        self.language = language
        self.dpi = int(dpi)
        self.tessdata = str(tessdata) if tessdata is not None else None
        self.max_page_pixels = max_page_pixels

    def parse(self, file_path) -> Document:
        """Parse an image file (magic-number self-verification → four-outcome OCR route)."""
        path = Path(file_path)
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "image"
        start_time = time.perf_counter()

        try:
            raw_bytes = path.read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
            metadata["binary_suspected"] = is_binary_suspected(raw_bytes)

            if not raw_bytes:
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    "empty file: no bytes to verify against any image "
                    "magic signature"
                )
                return self._finalize(metadata, start_time)

            image_format = _detect_image_format(raw_bytes)
            if image_format is None:
                # Same discipline as .dot×Graphviz: the suffix claims image but the bytes are
                # not any known image format → honest failure; no guessing, no patching.
                metadata["parse_failed"] = True
                metadata["parse_error"] = (
                    f"declared image suffix {path.suffix.lower()!r} but "
                    "bytes match no known image magic signature "
                    "(png/jpeg/gif/bmp/tiff/webp)"
                )
                return self._finalize(metadata, start_time)

            metadata["image_profile"] = {
                "format": image_format,
                "suffix": path.suffix.lower(),
                "byte_length": len(raw_bytes),
            }

            # Four-outcome OCR routing (prereg §1).
            outcome = self._run_ocr(path, image_format)
            if outcome.failure_reason is None:
                # ④ Success: the OCR text (after CJK newline-split-word normalization) enters the body (the
                # downstream Txt narrative channel); channel facts enter metadata; no unresolved
                # observation → no ocr_unresolved record, no warning.
                metadata["image_ocr"] = outcome.image_ocr
                # 2026-10-16 (newline-split-word normalization, prereg
                # image_ocr_cjk_newline_join_prereg_v1): marker words cut by
                # line boxes ("纳税\n人识别号") are broken only at the layout layer; the characters are all correct
                # — the same normalized text feeds both the stamp and the body, no second caliber;
                # the image_ocr channel facts keep the raw OCR caliber, no back-fill.
                text = _cjk_newline_join(outcome.text)
                signals = detect_domain_signals(text)
                # 2026-10-16 (perspective rescue, prereg
                # image_ocr_perspective_rescue_prereg_v1 batch 2): texts with zero
                # families present try geometric rescue — quad detection +
                # perspective warp (+CLAHE) re-OCR; only a strict gain in cross-family
                # total voting kinds replaces the body; families present (including all
                # active) structurally never trigger; channel-fact discipline: sha256 is always
                # over the original file bytes, and the whole rescue chain goes into
                # the image_ocr.perspective_rescue side-channel facts.
                if not any(
                    family["present"]
                    for family in signals["families"].values()
                ):
                    (
                        rescue_facts,
                        rescue_text,
                        rescue_signals,
                        rescue_image_ocr,
                    ) = self._perspective_rescue(raw_bytes, signals)
                    if rescue_facts is not None:
                        if rescue_image_ocr is not None:
                            rescue_facts["original_ocr"] = (
                                outcome.image_ocr
                            )
                        # The ④ success outcome always carries channel facts; the emptiness check is only type narrowing —
                        # on a theoretical None, the side-channel fact write is skipped rather than crashing.
                        base_ocr = (
                            rescue_image_ocr
                            if rescue_image_ocr is not None
                            else outcome.image_ocr
                        )
                        if base_ocr is not None:
                            channel = dict(base_ocr)
                            channel["perspective_rescue"] = rescue_facts
                            metadata["image_ocr"] = channel
                        if rescue_text is not None:
                            text = rescue_text
                            signals = rescue_signals
                # 2026-10-16 (scenario wiring): the stamp activates the scenario chain —
                # the same convention as the 7 narrative parsers; SDK scenario routing
                # reads metadata.domain_signals — without the stamp the whole chain spins empty. What is stamped is
                # the factual evidence of the OCR text, not a gate.
                metadata["domain_signals"] = signals
                return self._finalize(metadata, start_time, text=text)

            # ①②③ failure: empty body + a single ocr_unresolved registration
            # (marker contract §1, the three shared-foundation primitives); failures degrade silently
            # (side-channel fact discipline) and never escalate an OCR failure into parse_failed.
            resolution = (
                "unsupported"
                if outcome.failure_reason == REASON_CODE_OCR_ROUTE_UNAVAILABLE
                else "unresolved"
            )
            metadata["warnings"] = [
                _OCR_WARNING_BY_REASON[outcome.failure_reason]
            ]
            try:
                record = build_unresolved_record(
                    object_type="image_text",
                    object_id="document",
                    resolution=resolution,
                    reason_code=outcome.failure_reason,
                    source=path.name,
                    placeholder_text=None,
                    format=image_format,
                    suffix=path.suffix.lower(),
                )
                stamp_unresolved_registry(
                    metadata, "ocr_unresolved",
                    [record] if record is not None else None,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug(
                    "image ocr_unresolved registry skipped: %s", exc
                )

            return self._finalize(metadata, start_time)

        except Exception as error:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(error)
            return self._finalize(metadata, start_time)

    # ------------------------------------------------------------------
    # OCR subsystem (prereg ocr_image_route_prereg_v1)
    # ------------------------------------------------------------------

    def _run_ocr(self, path: Path, image_format: str) -> _OCROutcome:
        """Run one OCR attempt and classify it into one of the four outcomes.

        The image is a single-page carrier (multi-frame animated images read the first frame only in v1; the honest boundary does not widen);
        any backend open/render exception goes to outcome ③, never escalated to parse_failed.
        """
        tessdata = self._resolve_tessdata()
        fitz = _import_fitz()
        if tessdata is None or fitz is None:
            # ① Backend unavailable: #127 behavior kept as-is (unsupported tier).
            return _OCROutcome("", None, REASON_CODE_OCR_ROUTE_UNAVAILABLE)

        try:
            document = fitz.open(str(path))
        except Exception as error:
            # ③ Execution failure: probes confirmed pymupdf cannot open the webp container;
            # PIL transcode bridging is another batch (pillow is not a declared dependency).
            logger.debug(
                "image OCR open failed (format=%s): %s", image_format, error
            )
            return _OCROutcome("", None, REASON_CODE_OCR_EXECUTION_FAILED)

        try:
            page = document[0]
            effective_dpi = self._effective_dpi(page)
            try:
                text_page = page.get_textpage_ocr(
                    flags=0,
                    language=self.language,
                    dpi=effective_dpi,
                    full=True,
                    tessdata=tessdata,
                )
                text = page.get_text(textpage=text_page)
                raw_page = page.get_text(
                    "dict", textpage=text_page, sort=False
                )
            except Exception as error:
                logger.debug("image OCR render failed: %s", error)
                return _OCROutcome(
                    "", None, REASON_CODE_OCR_EXECUTION_FAILED
                )
        finally:
            document.close()

        if not text.strip():
            # ② OCR ran, output empty (reusing the existing registered code, zero re-definition).
            return _OCROutcome("", None, REASON_CODE_OCR_EMPTY_OUTPUT)

        blocks = sum(
            1
            for block in raw_page.get("blocks", [])
            if block.get("type") == 0
        )
        image_ocr: dict[str, object] = {
            "backend": _OCR_BACKEND,
            "language": self.language,
            "dpi": effective_dpi,
            "tessdata": tessdata,
            "chars": len(text),
            "blocks": blocks,
        }
        image_ocr.update(_quality_proxies(text))
        # ④ Success: OCR text + channel facts (quality-proxy facts, no gating).
        return _OCROutcome(text, image_ocr, None)

    def _run_ocr_png(self, png_bytes: bytes) -> _OCROutcome:
        """Rescue-round OCR: PNG bytes to a temp file, reusing the four-outcome OCR subsystem.

        Same tessdata resolution chain / effective-dpi pixel budget / outcome
        classification as the first round (prereg §2: a failed rescue OCR only records "no gain for this variant",
        never escalating any error). The temp file is deleted immediately after use.
        """
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            handle.write(png_bytes)
            handle.close()
            return self._run_ocr(Path(handle.name), "png")
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def _perspective_rescue(
        self, raw_bytes: bytes, first_signals: dict
    ):
        """Geometric rescue for zero-family text (prereg §2, 2026-10-16 batch 2).

        Args:
            raw_bytes: original image bytes (sha256 caliber unchanged; the rescue transforms
                in memory only).
            first_signals: the first-round text's (mupdf-decoded OCR + join applied)
                domain_signals — the baseline end of the trigger gate and keep criterion.

        Returns:
            (facts, text, signals, image_ocr): with cv2 present, facts is always a
            dict (rescue audit facts); text/signals/image_ocr describe the kept
            rescue round (all None when not kept). With cv2/numpy absent, returns
            (None, None, None, None) — absence is an environment fact, not a document fact:
            no channel facts left, audit via the debug log.
        """
        modules = _import_rescue_modules()
        if modules is None:
            return None, None, None, None
        cv2, np = modules
        facts: dict[str, Any] = {
            "triggered": True,
            "quad_found": False,
            "variants": [],
            "kept": False,
            "kept_variant": None,
        }
        image = cv2.imdecode(
            np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if image is None:
            return facts, None, None, None
        quad = _find_document_quad(cv2, np, image)
        if quad is None:
            return facts, None, None, None
        facts["quad_found"] = True
        warped = _warp_perspective(cv2, np, image, quad)
        if warped is None:
            facts["variants"].append(
                {"variant": "warp", "note": "degenerate quad geometry"}
            )
            return facts, None, None, None
        # Variant ladder (prereg §2): warp → if no strict gain, try warp+CLAHE;
        # strict-improvement keep criterion: the cross-family total
        # voting kinds must be strictly greater than the first round; ties or worse always keep the original.
        first_total = _total_voting_kinds(first_signals)
        best_total = first_total
        best_label = None
        best_text = None
        best_signals = None
        best_image_ocr = None
        for label, do_clahe in (("warp", False), ("warp_clahe", True)):
            candidate = _clahe_gray(cv2, warped) if do_clahe else warped
            encoded_ok, encoded = cv2.imencode(".png", candidate)
            if not encoded_ok:
                facts["variants"].append(
                    {"variant": label, "note": "png encode failed"}
                )
                continue
            outcome = self._run_ocr_png(encoded.tobytes())
            if (
                outcome.failure_reason is not None
                or not outcome.text.strip()
            ):
                facts["variants"].append(
                    {
                        "variant": label,
                        "voting_kinds": 0,
                        "note": outcome.failure_reason or "empty output",
                    }
                )
                continue
            text = _cjk_newline_join(outcome.text)
            signals = detect_domain_signals(text)
            total = _total_voting_kinds(signals)
            facts["variants"].append(
                {"variant": label, "voting_kinds": total}
            )
            if total > best_total:
                best_total = total
                best_label = label
                best_text = text
                best_signals = signals
                best_image_ocr = outcome.image_ocr
            if best_image_ocr is not None:
                # Stop at the first tier hit (prereg §2: descend tiers only without a strict gain).
                break
        if best_image_ocr is None:
            return facts, None, None, None
        facts["kept"] = True
        facts["kept_variant"] = best_label
        return facts, best_text, best_signals, best_image_ocr

    def _resolve_tessdata(self) -> str | None:
        """tessdata resolution chain (prereg §1): explicit → in-repo → system.

        Any unresolvable tier → ``None`` (outcome ①); **explicitly given but unusable
        is not downgraded** — never silently overriding the explicit configuration; honest attribution to outcome ①.
        """
        if self.tessdata:
            explicit = Path(self.tessdata)
            if explicit.is_dir() and any(explicit.glob("*.traineddata")):
                return str(explicit)
            logger.debug(
                "explicit tessdata %r unusable; OCR route unavailable",
                self.tessdata,
            )
            return None

        repo = _repo_tessdata()
        if repo is not None:
            return str(repo)

        fitz = _import_fitz()
        if fitz is None or not hasattr(fitz.Page, "get_textpage_ocr"):
            return None
        try:
            system = fitz.get_tessdata()
        except Exception:
            return None
        if system and Path(str(system)).is_dir():
            return str(system)
        return None

    def _effective_dpi(self, page: Any) -> int:
        """Rendering dpi within the pixel budget (pdf ``_effective_dpi_for`` discipline, frozen locally).

        pymupdf renders ``width/72 * dpi`` pixels per side; a fixed 300dpi on huge
        images would explode to hundreds of megapixels — the dpi is scaled down proportionally until within budget
        (floor 72, same as the pdf floor).
        """
        if not self.max_page_pixels or self.max_page_pixels <= 0:
            return self.dpi
        width = max(1.0, float(page.rect.width))
        height = max(1.0, float(page.rect.height))
        pixels = (width * self.dpi / 72.0) * (height * self.dpi / 72.0)
        if pixels <= self.max_page_pixels:
            return self.dpi
        scale = (self.max_page_pixels / pixels) ** 0.5
        return max(72, int(self.dpi * scale))

    @staticmethod
    def _finalize(
        metadata: dict, start_time: float, text: str = ""
    ) -> Document:
        """Unified finish: timing + document (④ carries the OCR text; other outcomes an empty body)."""
        metadata["parse_duration_ms"] = round(
            (time.perf_counter() - start_time) * 1000, 3
        )
        return Document(text=text, metadata=metadata)
