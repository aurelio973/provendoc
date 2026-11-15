"""Optional PyMuPDF/Tesseract OCR evidence extraction.

OCR is deliberately capability-gated.  When the local PyMuPDF build cannot
locate a Tesseract ``tessdata`` directory, callers receive an explicit
``OCRBackendUnavailable`` error and can retain native evidence without
claiming that OCR happened.  When available, OCR blocks follow the same
immutable geometry/span contract as native blocks.
"""

from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import cast

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with older PyMuPDF distributions.
    import fitz  # type: ignore[no-redef]

from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFPage,
)
from provendoc.core.parsers.narrative.pdf.evidence.native import NativePDFExtractor


class OCRBackendUnavailable(RuntimeError):
    """Raised when the configured PyMuPDF OCR backend cannot be used locally."""


DEFAULT_OCR_LANGUAGE = "eng"
"""Fallback language string when no better option is resolvable locally."""


def _has_traineddata(tessdata: str | Path | None, language: str) -> bool:
    """Return True when ``tessdata`` physically contains ``language``'s model.

    Tesseract's failure mode for a missing language pack is not an
    exception: it renders the page anyway and returns garbage (a Chinese
    scan read with ``eng`` yields Latin-looking noise such as ``vvZF FA a
    He`` with zero ideographs).  That silent degradation is worse than a
    hard error, because the character count looks healthy to upstream
    stages.  Checking the file directly lets routing stay truthful.
    """
    if not tessdata:
        return False
    try:
        return (Path(str(tessdata)) / f"{language}.traineddata").is_file()
    except OSError:
        return False


def resolve_ocr_language(
    language: str | None,
    tessdata: str | Path | None,
) -> tuple[str, str | None]:
    """Resolve the effective OCR language string against local tessdata.

    Returns ``(effective_language, downgraded_from)``.  ``downgraded_from``
    is the requested language string when it had to be narrowed, else
    ``None`` — callers surface it as runtime evidence rather than losing
    the fact on the floor.

    Resolution rules:

    * Every requested part that has a local ``.traineddata`` is kept, so an
      explicit ``chi_sim+eng`` still works when both models are present.
    * If **no** requested part is installed, the request falls back to the
      default ``eng``.  This keeps legacy behaviour intact on machines
      without any language pack the previous code also could not load.
    * ``tessdata=None`` means the directory is unknown here (PyMuPDF
      resolves it later); the request is passed through unchanged and no
      downgrade is reported, preserving the historical contract.
    """
    requested = (language or DEFAULT_OCR_LANGUAGE).strip()
    if tessdata is None:
        return requested, None
    parts = [part.strip() for part in requested.split("+") if part.strip()]
    available = [part for part in parts if _has_traineddata(tessdata, part)]
    if available:
        effective = "+".join(available)
        return effective, (requested if effective != requested else None)
    if _has_traineddata(tessdata, DEFAULT_OCR_LANGUAGE):
        return DEFAULT_OCR_LANGUAGE, (
            requested if requested != DEFAULT_OCR_LANGUAGE else None
        )
    return requested, None


def _effective_dpi_for(
    page: "fitz.Page", dpi: int, max_page_pixels: int | None
) -> int:
    """Derive a render DPI that respects the configured pixel budget.

    PyMuPDF renders OCR input at ``width/72 * dpi`` pixels per side. For
    oversized pages (posters, CAD sheets) a fixed 300 DPI can explode the
    render into hundreds of megapixels; the DPI is scaled down until the
    total pixel count fits the budget instead.
    """
    if not max_page_pixels or max_page_pixels <= 0:
        return dpi
    width_points = max(1.0, float(page.rect.width))
    height_points = max(1.0, float(page.rect.height))
    pixels = (width_points * dpi / 72.0) * (height_points * dpi / 72.0)
    if pixels <= max_page_pixels:
        return dpi
    scale = (max_page_pixels / pixels) ** 0.5
    return max(72, int(dpi * scale))


def _render_ocr_page(
    page: "fitz.Page",
    page_number: int,
    *,
    language: str,
    dpi: int,
    full_page: bool,
    tessdata: str,
    max_page_pixels: int | None,
) -> PDFPage:
    """Render one page through PyMuPDF/Tesseract into immutable evidence.

    Module-level so both the serial in-process path and the process-pool
    worker path share the exact same extraction logic.
    """
    effective_dpi = _effective_dpi_for(page, dpi, max_page_pixels)
    try:
        text_page = page.get_textpage_ocr(
            flags=0,
            language=language,
            dpi=effective_dpi,
            full=full_page,
            tessdata=tessdata,
        )
        raw_page = page.get_text("dict", textpage=text_page, sort=False)
    except Exception as error:
        raise OCRBackendUnavailable(
            f"PyMuPDF/Tesseract OCR failed on page {page_number}: {error}"
        ) from error

    blocks: list[PDFBlock] = []
    for block_index, block in enumerate(raw_page.get("blocks", [])):
        if block.get("type") != 0:
            continue
        text, spans = NativePDFExtractor._block_text_and_spans(block)
        if not text:
            continue
        raw_bbox = tuple(float(value) for value in block["bbox"])
        bbox = (raw_bbox[0], raw_bbox[1], raw_bbox[2], raw_bbox[3])
        blocks.append(
            PDFBlock(
                block_type="text",
                text=text,
                page_number=page_number,
                bbox=bbox,
                source_kind="ocr",
                confidence=0.75,
                raw_payload_ref=f"ocr:page:{page_number}:block:{block_index}",
                raw_payload={
                    "block_index": block_index,
                    "line_count": len(block.get("lines", [])),
                    "spans": spans,
                    "ocr_backend": "pymupdf_tesseract",
                    "ocr_language": language,
                    "ocr_dpi": effective_dpi,
                },
                evidence_id=f"ocr:p{page_number}:b{block_index}",
            )
        )
    return PDFPage(
        page_number=page_number,
        width=float(page.rect.width),
        height=float(page.rect.height),
        rotation=int(page.rotation),
        blocks=blocks,
    )


def _ocr_page_task(task: tuple) -> dict:
    """Worker-process entry point: OCR exactly one page in isolation.

    Runs in a spawned process (MuPDF is not thread-safe, so parallelism is
    process-based): every task opens its own document handle, renders one
    page, and returns plain picklable data. All exceptions become a
    ``failed`` outcome so one bad page can never poison the pool — the same
    per-page isolation contract as the serial loop.
    """
    (
        file_path,
        page_number,
        language,
        dpi,
        full_page,
        tessdata,
        max_page_pixels,
    ) = task
    started = time.perf_counter()
    try:
        document = fitz.open(file_path)
        try:
            page = document.load_page(page_number - 1)
            extracted = _render_ocr_page(
                page,
                page_number,
                language=language,
                dpi=dpi,
                full_page=full_page,
                tessdata=tessdata,
                max_page_pixels=max_page_pixels,
            )
        finally:
            document.close()
    except Exception:
        return {
            "page": page_number,
            "status": "failed",
            "pdf_page": None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "char_count": 0,
        }
    return {
        "page": page_number,
        "status": "ok",
        "pdf_page": extracted,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "char_count": sum(len(block.text) for block in extracted.blocks),
    }


class OCRPDFExtractor:
    """Extract page-level OCR evidence through PyMuPDF and a local Tesseract backend.

    The extractor never installs, downloads, or silently switches OCR engines.
    ``available`` and the result metadata expose the exact runtime capability so
    the pipeline can make auditable per-page fallback choices.
    """

    extraction_method = "ocr"

    def __init__(
        self,
        *,
        language: str = "eng",
        dpi: int = 300,
        full_page: bool = True,
        tessdata: str | Path | None = None,
        max_page_pixels: int | None = None,
        time_budget_seconds: float | None = None,
        parallel_workers: int = 1,
    ):
        """Configure OCR language, resolution, time budget and parallelism."""
        self.language = language
        self.dpi = dpi
        self.full_page = full_page
        self.tessdata = str(tessdata) if tessdata is not None else None
        self._effective_language: str | None = None
        self._language_downgraded_from: str | None = None
        self.max_page_pixels = max_page_pixels
        self.time_budget_seconds = time_budget_seconds
        self.parallel_workers = max(1, int(parallel_workers))

    def availability(self) -> dict[str, object]:
        """Return factual local OCR capability without invoking OCR."""
        try:
            tessdata = self._resolve_tessdata()
        except OCRBackendUnavailable as error:
            return {
                "available": False,
                "backend": "pymupdf_tesseract",
                "reason": str(error),
            }
        return {
            "available": True,
            "backend": "pymupdf_tesseract",
            "tessdata": tessdata,
            "language": self.effective_language,
            "requested_language": self.language,
            "language_downgraded_from": self.language_downgraded_from,
            "dpi": self.dpi,
            "full_page": self.full_page,
        }

    def extract(self, file_path: str | Path) -> PDFExtractionResult:
        """OCR every page and return normalized immutable evidence."""
        tessdata = self._resolve_tessdata()
        document = fitz.open(file_path)
        try:
            pages = [
                self._extract_page(document[page_index], page_index + 1, tessdata)
                for page_index in range(int(document.page_count))
            ]
            return PDFExtractionResult(
                pages=pages,
                extraction_method=self.extraction_method,
                metadata={
                    "page_count": len(pages),
                    "source_kind": "ocr",
                    "ocr_backend": "pymupdf_tesseract",
                    "ocr_tessdata": tessdata,
                    "ocr_language": self.effective_language,
                    "ocr_requested_language": self.language,
                    "ocr_language_downgraded_from": self.language_downgraded_from,
                    "ocr_dpi": self.dpi,
                    "ocr_full_page": self.full_page,
                },
            )
        finally:
            document.close()

    def extract_pages(
        self,
        file_path: str | Path,
        page_numbers: set[int] | list[int] | tuple[int, ...],
    ) -> PDFExtractionResult:
        """OCR selected one-based pages with per-page failure isolation.

        A single page failing OCR never discards its neighbours: the failed
        page is recorded in metadata as ``ocr_failed_pages`` and the remaining
        pages still produce evidence. A document-level time budget, when
        configured, stops launching new page renders once exhausted; skipped
        pages are recorded as ``ocr_budget_exhausted_pages`` so the pipeline
        can keep telling the truth about what ran and what did not.
        """
        selected = [int(n) for n in sorted(set(page_numbers)) if int(n) > 0]
        tessdata = self._resolve_tessdata()
        if self.parallel_workers > 1 and len(selected) > 1:
            return self._extract_pages_parallel(file_path, selected, tessdata)
        document = fitz.open(file_path)
        try:
            pages: list[PDFPage] = []
            page_timings: list[dict[str, object]] = []
            failed_pages: list[int] = []
            budget_exhausted_pages: list[int] = []
            budget_started = time.perf_counter()
            for page_index in range(int(document.page_count)):
                page_number = page_index + 1
                if page_number not in selected:
                    continue
                if (
                    self.time_budget_seconds is not None
                    and (time.perf_counter() - budget_started)
                    >= self.time_budget_seconds
                ):
                    budget_exhausted_pages.append(page_number)
                    continue

                page_started = time.perf_counter()
                page = document[page_index]
                try:
                    extracted = self._extract_page(page, page_number, tessdata)
                except OCRBackendUnavailable:
                    failed_pages.append(page_number)
                    page_timings.append(
                        {
                            "page": page_number,
                            "duration_ms": round(
                                (time.perf_counter() - page_started) * 1000, 3
                            ),
                            "char_count": 0,
                            "status": "failed",
                        }
                    )
                    continue
                pages.append(extracted)
                page_timings.append(
                    {
                        "page": page_number,
                        "duration_ms": round(
                            (time.perf_counter() - page_started) * 1000, 3
                        ),
                        "char_count": sum(len(block.text) for block in extracted.blocks),
                        "status": "ok",
                    }
                )
            return PDFExtractionResult(
                pages=pages,
                extraction_method=self.extraction_method,
                metadata={
                    "page_count": len(pages),
                    "source_kind": "ocr",
                    "ocr_backend": "pymupdf_tesseract",
                    "ocr_tessdata": tessdata,
                    "ocr_language": self.effective_language,
                    "ocr_requested_language": self.language,
                    "ocr_language_downgraded_from": self.language_downgraded_from,
                    "ocr_dpi": self.dpi,
                    "ocr_full_page": self.full_page,
                    "ocr_selected_pages": list(selected),
                    "ocr_failed_pages": failed_pages,
                    "ocr_budget_exhausted_pages": budget_exhausted_pages,
                    "ocr_page_timings": page_timings,
                },
            )
        finally:
            document.close()

    def _extract_pages_parallel(
        self,
        file_path: str | Path,
        selected: list[int],
        tessdata: str,
    ) -> PDFExtractionResult:
        """OCR selected pages across worker processes with a sliding window.

        MuPDF is not thread-safe, so parallelism is process-based: every task
        opens its own document handle and renders exactly one page. The
        document time budget keeps its serial semantics — once exhausted no
        new page is launched, in-flight pages still finish, and never-launched
        pages are recorded in ``ocr_budget_exhausted_pages``. A sliding
        submission window (2x workers) keeps the pool saturated even when
        page durations vary.
        """
        workers = max(1, min(self.parallel_workers, len(selected)))

        def task_for(page_number: int) -> tuple:
            """Build the task parameters for a single-page OCR worker."""
            return (
                str(file_path),
                page_number,
                self.effective_language,
                self.dpi,
                self.full_page,
                tessdata,
                self.max_page_pixels,
            )

        budget_started = time.perf_counter()

        def budget_exhausted() -> bool:
            """Decide whether the document-level OCR time budget is exhausted."""
            return self.time_budget_seconds is not None and (
                time.perf_counter() - budget_started
            ) >= self.time_budget_seconds

        pages: list[PDFPage] = []
        page_timings: list[dict[str, object]] = []
        failed_pages: list[int] = []
        budget_exhausted_pages: list[int] = []
        in_flight_window = workers * 2

        # Tesseract defaults to OpenMP multithreading; under process
        # parallelism each worker is capped at 1 thread to avoid
        # oversubscription. The previous value is restored after the pool
        # is torn down so the surrounding process is left untouched.
        omp_previous = os.environ.get("OMP_THREAD_LIMIT")
        if "OMP_THREAD_LIMIT" not in os.environ:
            os.environ["OMP_THREAD_LIMIT"] = "1"
        try:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                queue = deque(selected)
                pending: dict = {}
                while queue and len(pending) < in_flight_window:
                    page_number = queue.popleft()
                    if budget_exhausted():
                        budget_exhausted_pages.append(page_number)
                        continue
                    pending[
                        executor.submit(_ocr_page_task, task_for(page_number))
                    ] = page_number
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        page_number = pending.pop(future)
                        outcome = future.result()
                        if outcome["status"] == "ok":
                            pages.append(outcome["pdf_page"])
                        else:
                            failed_pages.append(page_number)
                        page_timings.append(
                            {
                                "page": page_number,
                                "duration_ms": outcome["duration_ms"],
                                "char_count": outcome["char_count"],
                                "status": outcome["status"],
                            }
                        )
                    while queue and len(pending) < in_flight_window:
                        page_number = queue.popleft()
                        if budget_exhausted():
                            budget_exhausted_pages.append(page_number)
                            continue
                        pending[
                            executor.submit(_ocr_page_task, task_for(page_number))
                        ] = page_number
        finally:
            if omp_previous is None:
                os.environ.pop("OMP_THREAD_LIMIT", None)
            else:
                os.environ["OMP_THREAD_LIMIT"] = omp_previous

        pages.sort(key=lambda page: page.page_number)
        page_timings.sort(key=lambda timing: cast("int", timing["page"]))
        failed_pages.sort()
        budget_exhausted_pages.sort()
        return PDFExtractionResult(
            pages=pages,
            extraction_method=self.extraction_method,
            metadata={
                "page_count": len(pages),
                "source_kind": "ocr",
                "ocr_backend": "pymupdf_tesseract",
                "ocr_tessdata": tessdata,
                "ocr_language": self.effective_language,
                "ocr_requested_language": self.language,
                "ocr_language_downgraded_from": self.language_downgraded_from,
                "ocr_dpi": self.dpi,
                "ocr_full_page": self.full_page,
                "ocr_selected_pages": list(selected),
                "ocr_failed_pages": failed_pages,
                "ocr_budget_exhausted_pages": budget_exhausted_pages,
                "ocr_page_timings": page_timings,
                "ocr_parallel_workers": workers,
            },
        )

    def _resolve_tessdata(self) -> str:
        if not hasattr(fitz.Page, "get_textpage_ocr"):
            raise OCRBackendUnavailable(
                "PyMuPDF does not expose Page.get_textpage_ocr in this environment"
            )
        try:
            path = self.tessdata or fitz.get_tessdata()
        except Exception as error:
            raise OCRBackendUnavailable(
                "Tesseract tessdata is unavailable; configure TESSDATA_PREFIX or pass tessdata"
            ) from error
        if not path:
            raise OCRBackendUnavailable(
                "Tesseract tessdata is unavailable; configure TESSDATA_PREFIX or pass tessdata"
            )
        resolved = str(path)
        effective, downgraded = resolve_ocr_language(self.language, resolved)
        self._effective_language = effective
        self._language_downgraded_from = downgraded
        return resolved

    @property
    def effective_language(self) -> str:
        """Language string actually handed to Tesseract after local resolution.

        Falls back to the requested string when resolution has not run yet
        (``_resolve_tessdata`` not called), so the value is always usable.
        """
        return self._effective_language or self.language

    @property
    def language_downgraded_from(self) -> str | None:
        """Requested language string that local tessdata could not satisfy."""
        return self._language_downgraded_from

    def _extract_page(self, page: fitz.Page, page_number: int, tessdata: str) -> PDFPage:
        return _render_ocr_page(
            page,
            page_number,
            language=self.effective_language,
            dpi=self.dpi,
            full_page=self.full_page,
            tessdata=tessdata,
            max_page_pixels=self.max_page_pixels,
        )

    def _effective_dpi(self, page: fitz.Page) -> int:
        return _effective_dpi_for(page, self.dpi, self.max_page_pixels)
