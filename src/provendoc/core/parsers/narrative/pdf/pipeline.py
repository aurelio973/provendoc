"""PDF ingestion pipeline — internal orchestration for the PDF runtime.

Flow:

    probe/classify → native page evidence (text / images / tables)
        → reading reconstruction → noise suppression → structural reconstruction
        → extraction/layout diagnostics

The pipeline never claims an unavailable OCR, MinerU or visual backend ran.
When local Tesseract is available, it enriches only pages without usable native
text; otherwise it records the native fallback and its reason explicitly.
"""

from __future__ import annotations

import importlib.metadata
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, cast

from provendoc.core.parsers.narrative.pdf.classifier import PDFClassifier
from provendoc.core.parsers.narrative.pdf.contracts import (
    PDFBlock,
    PDFExtractionResult,
    PDFRoutingDecision,
    PDFRuntimeLimits,
)
from provendoc.core.parsers.narrative.pdf.diagnostics import (
    ExtractionQualityProbe,
    LayoutQualityProbe,
    build_pdf_quality_for_evaluation,
)
from provendoc.core.parsers.narrative.pdf.enrichment import unconfigured_backend_metadata
from provendoc.core.parsers.narrative.pdf.evidence import (
    AnnotationExtractor,
    FigureEvidenceExtractor,
    FileIntegrityExtractor,
    ImageRegionExtractor,
    NativePDFExtractor,
    NativeTableExtractor,
    NavigationExtractor,
    OCRBackendUnavailable,
    OCRPDFExtractor,
    PageGeometryExtractor,
    PDFImageAssetExtractor,
    SharedTableFinder,
    SharedTableFinderConsumer,
    TextCharacterExtractor,
    WatermarkExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.navigation import (
    NavigationEvidence,
)
from provendoc.core.parsers.narrative.pdf.evidence.security import (
    extract_security_evidence,
)
from provendoc.core.parsers.narrative.pdf.governance import (
    PDFGovernanceEvaluator,
    PDFGovernanceProvider,
)
from provendoc.core.parsers.narrative.pdf.layout_overlay import (
    PDFLayoutBackend,
    apply_layout_overlay,
)
from provendoc.core.parsers.narrative.pdf.reconstruction import (
    ContentRoleAnnotator,
    DocumentContextAnnotator,
    FigureReconstructor,
    FootnoteSeparator,
    FormulaReconstructor,
    HeadingReconstructor,
    NavigationReconstructor,
    ParagraphReconstructor,
    ReadingOrderReconstructor,
    RepetitiveArtifactSuppressor,
    TableOverlapSuppressor,
    TableReconstructor,
    VisualRowAssembler,
    arbitrate_native_chart_text,
)
from provendoc.core.parsers.narrative.pdf.text_layer import (
    TextLayerAssessor,
    compare_layers,
)


def _pymupdf_version() -> str:
    for package in ("pymupdf", "PyMuPDF"):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "unknown"


def _valid_page_numbers(value: object, selected_pages: list[int]) -> list[int]:
    """Normalize extractor page lists while retaining only requested pages."""
    selected = set(selected_pages)
    if not isinstance(value, list):
        return []
    return sorted({page for page in value if isinstance(page, int) and page in selected})


def _page_has_text_evidence(page: Any) -> bool:
    """Return whether an OCR page has usable text blocks for replacement."""
    return any(
        block.block_type == "text" and bool(block.text.strip())
        for block in getattr(page, "blocks", [])
    )


def _page_joined_text(page: Any) -> str:
    """Join a page's text-block evidence for layer comparison."""
    return "\n".join(
        block.text for block in getattr(page, "blocks", []) if block.text
    )


# Design log #175 hole 1: scan-backed pages may also carry a usable embedded text layer (the product of some old OCR
# engine). For the new OCR to take over, its output length must cross this floor — and it must also
# pass a one-quarter length comparison against the embedded layer it replaces (see _run_ocr_enrichment).
SCAN_BACKED_MIN_OCR_ADOPT_CHARS = 200


def _ocr_status(
    *,
    attempted_pages: list[int],
    used_pages: list[int],
    failed_pages: list[int],
    budget_exhausted_pages: list[int],
    empty_output_pages: list[int],
) -> str:
    """Summarize OCR execution while preserving legacy partial-attempt semantics.

    ``used`` means the extractor ran for at least one selected page; consumers
    must inspect the page lists/routes to see whether any attempt actually
    replaced native evidence. This keeps partial failures compatible while
    avoiding an incorrect ``actual_runtime`` claim, which remains native unless
    at least one non-empty OCR page was adopted.
    """
    if attempted_pages or used_pages:
        return "used"
    if budget_exhausted_pages:
        return "budget_exhausted"
    if failed_pages:
        return "failed"
    if empty_output_pages:
        return "empty_output"
    return "failed"


@dataclass
class PDFPipelineResult:
    """The complete result of one PDF pipeline run."""

    routing: PDFRoutingDecision
    extraction: PDFExtractionResult
    blocks: list[dict] = field(default_factory=list)
    narrative_text: str = ""
    reconstruction: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    runtime_notes: list[str] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)
    governance_enforced: bool = False
    navigation: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    # Image-asset records (opt-in): per-placement facts carrying the
    # in-memory byte payload for the export layer; None = disabled.
    image_assets: dict[str, Any] | None = None


class PDFPipeline:
    """Orchestrate auditable native, optional OCR, and additive PDF evidence."""

    def __init__(
        self,
        classifier: PDFClassifier | None = None,
        native_extractor: NativePDFExtractor | None = None,
        ocr_extractor: OCRPDFExtractor | None = None,
        image_extractor: ImageRegionExtractor | None = None,
        table_extractor: NativeTableExtractor | None = None,
        reading_order: ReadingOrderReconstructor | None = None,
        visual_rows: VisualRowAssembler | None = None,
        table_overlap: TableOverlapSuppressor | None = None,
        repetitive: RepetitiveArtifactSuppressor | None = None,
        headings: HeadingReconstructor | None = None,
        footnotes: FootnoteSeparator | None = None,
        paragraphs: ParagraphReconstructor | None = None,
        tables: TableReconstructor | None = None,
        figures: FigureReconstructor | None = None,
        formulas: FormulaReconstructor | None = None,
        visual_enricher: Any | None = None,
        layout_backend: PDFLayoutBackend | None = None,
        navigation_extractor: NavigationExtractor | None = None,
        navigation_reconstructor: NavigationReconstructor | None = None,
        character_extractor: TextCharacterExtractor | None = None,
        watermark_extractor: WatermarkExtractor | None = None,
        geometry_extractor: PageGeometryExtractor | None = None,
        figure_evidence_extractor: FigureEvidenceExtractor | None = None,
        file_integrity_extractor: FileIntegrityExtractor | None = None,
        ocr_language: str = "eng",
        ocr_tessdata: str | None = None,
        governance_provider: PDFGovernanceProvider | None = None,
        enforce_governance: bool = False,
        context_annotator: DocumentContextAnnotator | None = None,
        extraction_quality: ExtractionQualityProbe | None = None,
        layout_quality: LayoutQualityProbe | None = None,
        text_layer_assessor: TextLayerAssessor | None = None,
        content_roles: ContentRoleAnnotator | None = None,
        limits: PDFRuntimeLimits | None = None,
    ):
        """Assemble the extractors and reconstructors (default implementation)."""
        self.classifier = classifier or PDFClassifier()
        self.limits = limits
        self.native_extractor = native_extractor or NativePDFExtractor()
        self.ocr_extractor = ocr_extractor or OCRPDFExtractor(
            language=ocr_language,
            tessdata=ocr_tessdata,
            max_page_pixels=limits.max_page_pixels if limits else None,
            time_budget_seconds=limits.ocr_time_budget_seconds if limits else None,
            parallel_workers=(limits.ocr_parallel_workers or 1) if limits else 1,
        )
        self.image_extractor = image_extractor or ImageRegionExtractor()
        self.table_extractor = table_extractor or NativeTableExtractor()
        self.character_extractor = character_extractor or TextCharacterExtractor()
        self.watermark_extractor = watermark_extractor or WatermarkExtractor()
        self.geometry_extractor = geometry_extractor or PageGeometryExtractor()
        self.figure_evidence_extractor = (
            figure_evidence_extractor or FigureEvidenceExtractor()
        )
        self.annotation_extractor = AnnotationExtractor()
        self.image_asset_extractor = PDFImageAssetExtractor(
            max_payload_bytes=(
                limits.max_image_asset_bytes if limits else None
            ),
        )
        self.file_integrity_extractor = (
            file_integrity_extractor or FileIntegrityExtractor()
        )
        self.reading_order = reading_order or ReadingOrderReconstructor()
        self.visual_rows = visual_rows or VisualRowAssembler()
        # Design log #125 L2: ruled-table overlap suppression runs before visual
        # row banding so suppressed fragments never join a band.
        self.table_overlap = table_overlap or TableOverlapSuppressor()
        self.repetitive = repetitive or RepetitiveArtifactSuppressor()
        self.headings = headings or HeadingReconstructor()
        self.footnotes = footnotes or FootnoteSeparator()
        self.paragraphs = paragraphs or ParagraphReconstructor()
        self.tables = tables or TableReconstructor()
        self.figures = figures or FigureReconstructor(visual_enricher=visual_enricher)
        self.formulas = formulas or FormulaReconstructor()
        self.layout_backend = layout_backend
        self.navigation_extractor = navigation_extractor or NavigationExtractor()
        self.navigation_reconstructor = (
            navigation_reconstructor or NavigationReconstructor()
        )
        self.governance = PDFGovernanceEvaluator(governance_provider)
        self.enforce_governance = enforce_governance
        self.context_annotator = context_annotator or DocumentContextAnnotator()
        self.extraction_quality = extraction_quality or ExtractionQualityProbe()
        self.layout_quality = layout_quality or LayoutQualityProbe()
        self.text_layer_assessor = text_layer_assessor or TextLayerAssessor()
        self.content_roles = content_roles or ContentRoleAnnotator()

    def run(
        self,
        file_path: str | Path,
        *,
        suppress_repetitive: bool = True,
        min_text_length: int = 1,
        enable_ocr: bool = True,
        enable_layout: bool = True,
        enable_image_assets: bool = True,
    ) -> PDFPipelineResult:
        """Run native evidence plus optional OCR enrichment for a single PDF.

        Native extraction always runs first. OCR is then attempted only for pages
        whose probe says native text is unavailable, allowing mixed documents to
        retain native provenance on digital pages while gaining OCR evidence on
        scans. A configured layout backend runs only when routing recommends the
        layout runtime; its semantic regions become an additive, evidence-bound
        reconstruction overlay rather than a replacement for immutable
        native/OCR evidence.

        Resource limits (``PDFRuntimeLimits``) are guards, not filters: an
        encrypted or oversized document still runs the cheap native path, but
        OCR and layout enrichment are skipped and the guard reason is recorded
        truthfully in ``runtime["guard"]``.
        """
        stages: dict[str, dict[str, Any]] = {}
        with self._stage(stages, "probe"):
            routing = self.classifier.classify(file_path, limits=self.limits)

        runtime_notes, guarded, encrypted, enable_ocr, enable_layout = (
            self._guard_notes(routing, enable_ocr, enable_layout)
        )

        with self._stage(stages, "native"):
            extraction = self._extract_native(file_path, encrypted)

        with self._stage(stages, "ocr"):
            runtime = self._run_ocr_enrichment(
                extraction=extraction,
                file_path=file_path,
                routing=routing,
                enable_ocr=enable_ocr,
                runtime_notes=runtime_notes,
            )

        with self._stage(stages, "additive_evidence"):
            self._merge_additive_evidence(extraction, file_path, runtime_notes)
            self._check_ocr_language_limitation(extraction, runtime, runtime_notes)
            self._note_page_geometry_limitations(extraction, runtime_notes)

        # Text-layer facts (spec engineering principle: layer-conflict registration + status classification + explainable panel).
        # All inputs come from existing fact sources (probe signals, OCR routing, page errors, hidden text layer,
        # layer-conflict records); this stage only classifies, it never re-reads the file.
        with self._stage(stages, "text_layer"):
            self._assess_text_layer(routing, runtime, extraction)

        with self._stage(stages, "navigation"):
            navigation_evidence = self._safe_extract_navigation(
                file_path, runtime_notes
            )

        with self._stage(stages, "security"):
            security_evidence = self._safe_extract_security(
                file_path, runtime_notes
            )

        with self._stage(stages, "layout"):
            layout_blocks, layout_info = apply_layout_overlay(
                pages=extraction.pages,
                file_path=file_path,
                layout_backend=self.layout_backend,
                enable_layout=enable_layout,
                routing_recommends_layout=(
                    routing.recommended_runtime == "layout" and not guarded
                ),
                runtime=runtime,
                runtime_notes=runtime_notes,
            )

        with self._stage(stages, "reconstruction"):
            ordered_content, reading_order_info, repetitive_info = (
                self._select_content_blocks(
                    extraction, routing, navigation_evidence,
                    layout_blocks, layout_info, suppress_repetitive,
                )
            )
            (
                blocks,
                heading_info,
                navigation_info,
                table_info,
                figure_info,
            ) = self._rebuild_structures(
                ordered_content, min_text_length, navigation_evidence,
                extraction, file_path,
            )
            (
                blocks,
                image_assets_result,
                formula_info,
                footnote_info,
                content_role_info,
                chart_arbitration_info,
            ) = self._enrich_blocks(
                blocks, extraction, file_path, enable_image_assets,
                table_info, figure_info,
            )
            # The arbitration ledger lands in the reconstruction dict alongside the figures rebuild info.
            figure_info = {
                **figure_info,
                "native_chart_text_arbitration": chart_arbitration_info,
            }
            blocks, narrative_text, context, governance_enforced = (
                self._apply_governance(
                    file_path, extraction, blocks, runtime_notes,
                )
            )

        with self._stage(stages, "quality"):
            quality, reconstruction = self._build_quality(
                extraction, narrative_text, blocks, runtime, routing,
                reading_order_info, repetitive_info, heading_info,
                navigation_info, footnote_info, table_info, figure_info,
                formula_info, content_role_info,
            )

        self._finalize_runtime(runtime, routing, stages)
        return PDFPipelineResult(
            routing=routing,
            extraction=extraction,
            blocks=blocks,
            narrative_text=narrative_text,
            reconstruction=reconstruction,
            quality=quality,
            context=context,
            runtime_notes=runtime_notes,
            runtime=runtime,
            governance_enforced=governance_enforced,
            navigation=(
                navigation_evidence.to_dict() if navigation_evidence is not None else {}
            ),
            security=security_evidence,
            image_assets=image_assets_result,
        )

    # ---- run() stage methods (2026-10-06 structural convergence: split off the run() orchestration body
    # with per-line equivalent behaviour; the stage timing boundaries stay inside run()) ----

    @staticmethod
    def _guard_notes(
        routing: PDFRoutingDecision,
        enable_ocr: bool,
        enable_layout: bool,
    ) -> tuple[list[str], bool, bool, bool, bool]:
        """Post-probe guard decision: when guarded, the OCR/layout enrichment paths are closed off.

        Returns (runtime_notes, guarded, encrypted, enable_ocr, enable_layout).
        Resource limits are guards, not filters: the native evidence is the honest fallback behind every
        guard reason.
        """
        runtime_notes: list[str] = []
        guarded = routing.recommended_runtime == "guarded"
        encrypted = bool(routing.signals.get("encrypted"))
        if guarded:
            for reason in routing.signals.get("guard_reasons", []):
                runtime_notes.append(f"runtime guard engaged: {reason}")
            # The expensive enrichment paths are exactly what a pathological
            # file must not monopolize; native evidence stays as the honest
            # fallback for every guard reason.
            enable_ocr = False
            enable_layout = False
        return runtime_notes, guarded, encrypted, enable_ocr, enable_layout

    def _extract_native(
        self, file_path: str | Path, encrypted: bool
    ) -> PDFExtractionResult:
        """Native extraction; when an encrypted document's native layer is unavailable this is registered honestly and is not fatal."""
        try:
            return self.native_extractor.extract(file_path)
        except Exception as error:
            if not encrypted:
                raise
            # A password-protected document may refuse even native text
            # access; record that fact instead of failing the whole run.
            return PDFExtractionResult(
                pages=[],
                extraction_method="native_text",
                metadata={
                    "guard_reason": (
                        f"native extraction unavailable for encrypted document: {error}"
                    )
                },
            )

    def _assess_text_layer(
        self,
        routing: PDFRoutingDecision,
        runtime: dict[str, Any],
        extraction: PDFExtractionResult,
    ) -> None:
        """Text-layer fact classification (classify only, never re-read the file); writes extraction.metadata
        and runtime in place."""
        text_layer = self.text_layer_assessor.assess(
            page_signals=routing.signals.get("pages") or [],
            ocr_page_routes=runtime.get("ocr_page_routes") or [],
            native_page_errors=extraction.metadata.get("pdf_native_page_errors"),
            invisible_text_pages=(
                extraction.metadata.get("pdf_text_characters") or {}
            ).get("invisible_text_pages"),
            layer_conflicts=runtime.get("text_layer_conflicts"),
            hidden_layer_comparisons=runtime.get("hidden_layer_comparisons"),
        )
        extraction.metadata["pdf_text_layer"] = text_layer
        runtime["text_layer"] = {
            "status_counts": text_layer["status_counts"],
            "layer_conflict_counts": text_layer["layer_conflict_counts"],
            "hidden_layer_comparison_counts": text_layer[
                "hidden_layer_comparison_counts"
            ],
            "hidden_text_layer_pages": text_layer["hidden_text_layer_pages"],
            "registration": "TEXT_LAYER_CONFLICT",
        }

    def _safe_extract_navigation(
        self, file_path: str | Path, runtime_notes: list[str]
    ) -> NavigationEvidence | None:
        """Navigation evidence (bookmarks / page labels); failures are recorded as notes and are not fatal."""
        try:
            return self.navigation_extractor.extract(file_path)
        except Exception as error:  # Navigation evidence is never fatal.
            runtime_notes.append(f"navigation evidence skipped: {error}")
            return None

    @staticmethod
    def _safe_extract_security(
        file_path: str | Path, runtime_notes: list[str]
    ) -> dict[str, Any]:
        """Security evidence; failures are registered honestly as available=False and are not fatal."""
        try:
            return extract_security_evidence(file_path)
        except Exception as error:  # Security evidence is never fatal.
            runtime_notes.append(f"security evidence skipped: {error}")
            return {"available": False, "error": str(error)}

    def _select_content_blocks(
        self,
        extraction: PDFExtractionResult,
        routing: PDFRoutingDecision,
        navigation_evidence: NavigationEvidence | None,
        layout_blocks: list | None,
        layout_info: dict[str, Any],
        suppress_repetitive: bool,
    ) -> tuple[list, dict[str, Any], dict[str, Any]]:
        """Reconstruction first half: reading-order rebuild + duplicate-content suppression + content-block screening
        + double page-number identity registration.

        Returns (ordered_content, reading_order_info, repetitive_info).
        """
        # Design log #175 hole 3: the probe column signals of OCR-replaced pages come from an empty/rotten native layer and are unusable.
        # Column evidence is re-derived from the finally-adopted OCR block geometry so column-priority ordering can take effect on these pages
        # (scanned two-column document shapes). The derived facts go into reading_order_info;
        # the probe-side original signals stay untouched (the comparison stays auditable).
        derived_column_signals = self._derive_ocr_column_signals(extraction)
        layout_signals = routing.signals
        if derived_column_signals:
            merged_page_signals = []
            for signal in routing.signals.get("pages", []):
                if not isinstance(signal, dict):
                    continue
                page_signal = dict(signal)
                override = derived_column_signals.get(
                    int(page_signal.get("page_number", -1))
                )
                if override:
                    page_signal.update(override)
                merged_page_signals.append(page_signal)
            layout_signals = {**routing.signals, "pages": merged_page_signals}
        ordered_blocks, reading_order_info = self.reading_order.reconstruct(
            extraction.pages,
            layout_signals=layout_signals,
        )
        if derived_column_signals:
            reading_order_info["column_evidence_source"] = {
                "pages": sorted(derived_column_signals),
                "source": "ocr_blocks",
            }
        if layout_blocks is not None:
            # MinerU's semantic region order supersedes the geometric fallback
            # while every derived block keeps links to immutable evidence.
            ordered_blocks = layout_blocks
            reading_order_info = {
                **reading_order_info,
                **layout_info,
                "superseded_by_layout_overlay": True,
            }
        if suppress_repetitive:
            content_blocks, repetitive_info = self.repetitive.suppress(extraction.pages)
        else:
            content_blocks = [
                block for page in extraction.pages for block in page.blocks
            ]
            repetitive_info = {
                "suppressed_block_count": 0,
                "suppressed_repetitive_samples": [],
                "edge_candidate_count": 0,
                "enabled": False,
            }

        content_ids = {block.evidence_id for block in content_blocks}
        if layout_blocks is not None:
            # The MinerU overlay already separates narrative regions from
            # page-edge artifacts, so the geometry-based native suppressor is
            # bypassed for content selection while its statistics stay recorded.
            content_ids = {block.evidence_id for block in ordered_blocks}
            repetitive_info = {
                **repetitive_info,
                "superseded_by_layout_overlay": True,
            }
        ordered_content = [
            block
            for block in ordered_blocks
            # Design log #175 hole 3: column-split sub-blocks use the ":col{n}" derived id, their lineage
            # points back to the original block — both paths can hit the content screening.
            if block.evidence_id in content_ids
            or (
                isinstance(block.raw_payload, dict)
                and block.raw_payload.get("split_from_evidence_id") in content_ids
            )
        ]
        # Design log #125 L2: ruled-table overlap suppression runs before visual-line banding;
        # suppressed fragments no longer take part in banding (prereg §1.B frozen semantics).
        # When the layout backend takes over the whole step is skipped and a skipped fact is registered (zero-diff discipline:
        # no ledger key is written when the geometry path has zero suppressions).
        if layout_blocks is not None:
            table_overlap_info = {
                "superseded_by_layout_overlay": True,
                "note": (
                    "table overlap suppression skipped: layout overlay "
                    "provides the semantic region order"
                ),
            }
        else:
            ordered_content, table_overlap_info = self.table_overlap.suppress(
                ordered_content
            )
        if table_overlap_info:
            repetitive_info = {
                **repetitive_info,
                "table_overlap_suppression": table_overlap_info,
            }
        # Design log #125: same-baseline visual-line reassembly — the two blocks of a label/value grid can be
        # split apart within the same visual line by draw order (value drawn before label); here they are recombined into one line by baseline x.
        # Runs only when geometric evidence is available; when the layout backend takes over
        # (semantic region order already replaced geometric order) the whole step is skipped.
        if layout_blocks is not None:
            visual_row_info = {
                "superseded_by_layout_overlay": True,
                "assembled_page_count": 0,
                "pages": {},
                "note": (
                    "visual row assembly skipped: layout overlay provides "
                    "the semantic region order"
                ),
            }
        else:
            ordered_content, visual_row_info = self.visual_rows.assemble(
                ordered_content,
                layout_signals=routing.signals,
            )
        reading_order_info = {**reading_order_info, "visual_rows": visual_row_info}
        # Spec engineering principle: double page numbering — physical page / declared page label / printed page number,
        # three fact sets registered side by side; the physical page number is the sole authority, the rest are registered facts.
        extraction.metadata["pdf_page_identity"] = {
            "physical_page_count": len(extraction.pages),
            "pdf_page_labels": self._page_labels(navigation_evidence),
            "visible_page_numbers": repetitive_info.get(
                "visible_page_numbers", {}
            ),
            "visible_page_number_ambiguous_pages": repetitive_info.get(
                "visible_page_number_ambiguous_pages", []
            ),
            "note": (
                "the physical page number is authoritative; declared page "
                "labels and printed page numbers are recorded facts and "
                "never remap block provenance"
            ),
        }
        return ordered_content, reading_order_info, repetitive_info

    def _derive_ocr_column_signals(
        self,
        extraction: PDFExtractionResult,
    ) -> dict[int, dict[str, Any]]:
        """Design log #175 hole 3: re-derive column signals for OCR-replaced pages from the final OCR block geometry.

        Probe column evidence only reflects the native layer — a pure-scan page probes an empty layer; after OCR
        replacement the column-structure evidence goes to zero and column-priority ordering fails with it. Here, for pages
        whose final adopted evidence is OCR, columns are re-derived from block geometry (the same column clustering as the probe), overriding only
        when >= 2 columns come out; otherwise it stays blank (honesty: form/single-column pages do not masquerade as multi-column).
        """
        ocr_pages = {
            int(page_number)
            for page_number in (extraction.metadata.get("ocr_pages") or [])
            if isinstance(page_number, int)
        }
        if not ocr_pages:
            return {}
        derived: dict[int, dict[str, Any]] = {}
        for page in extraction.pages:
            if page.page_number not in ocr_pages:
                continue
            text_blocks = [
                block
                for block in page.blocks
                if block.block_type == "text" and bool(block.text.strip())
            ]
            page_width = float(page.width or 0.0)
            if len(text_blocks) < 2 or page_width <= 0.0:
                continue
            columns = self.classifier.derive_column_signals(
                x_positions=[float(block.bbox[0]) for block in text_blocks],
                right_edges=[float(block.bbox[2]) for block in text_blocks],
                page_width=page_width,
                block_count=len(text_blocks),
            )
            if columns["column_count"] >= 2:
                derived[page.page_number] = {
                    "likely_multicolumn": True,
                    "column_count": columns["column_count"],
                    "column_boundaries": columns["boundaries"],
                    "column_widths": columns["widths"],
                    "irregular_columns": columns["irregular"],
                    "column_evidence_source": "ocr_blocks",
                }
        return derived

    def _rebuild_structures(
        self,
        ordered_content: list,
        min_text_length: int,
        navigation_evidence: NavigationEvidence | None,
        extraction: PDFExtractionResult,
        file_path: str | Path,
    ) -> tuple[list, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Reconstruction middle: heading/navigation/page-geometry/table/figure structure rebuild.

        Returns (blocks, heading_info, navigation_info, table_info, figure_info).
        """
        blocks, heading_info = self._recover_structures(
            ordered_content=ordered_content,
            min_text_length=min_text_length,
        )
        blocks, navigation_info = self.navigation_reconstructor.apply(
            blocks, navigation_evidence
        )
        self._attach_page_geometry(blocks, extraction)
        blocks, table_info = self.tables.reconstruct(blocks)
        blocks, figure_info = self.figures.reconstruct(blocks, file_path=file_path)
        return blocks, heading_info, navigation_info, table_info, figure_info

    def _enrich_blocks(
        self,
        blocks: list,
        extraction: PDFExtractionResult,
        file_path: str | Path,
        enable_image_assets: bool,
        table_info: dict[str, Any],
        figure_info: dict[str, Any],
    ) -> tuple[list, dict[str, Any] | None, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Reconstruction second half: image assets + formula/footnote/paragraph merging + content_role
        annotation + figure-channel arbitration.

        Returns (blocks, image_assets_result, formula_info, footnote_info,
        content_role_info, chart_arbitration_info).
        """
        # image assets (opt-in): original bytes for figure blocks; caption
        # evidence is reused from the figure reconstruction above — no
        # second caption search
        image_assets_result: dict[str, Any] | None = None
        if enable_image_assets:
            try:
                image_assets_result = self.image_asset_extractor.extract(
                    file_path, blocks
                )
            except Exception:
                # Asset extraction is additive evidence: a failure
                # here must never fail the whole parse.
                image_assets_result = {
                    "extractor": "pdf_image_assets_v1",
                    "records": [],
                    "not_extracted_reason": "extractor_failed",
                }
        page_heights = {
            page.page_number: page.height for page in extraction.pages
        }
        blocks, formula_info = self.formulas.reconstruct(
            blocks,
            page_heights=page_heights,
            evidence=[
                block for page in extraction.pages for block in page.blocks
            ],
        )
        blocks, footnote_info = self.footnotes.separate(
            blocks,
            page_heights,
        )
        blocks = self.paragraphs.merge(blocks)
        # Design log #125 (2): figure descriptor vs body dual-render arbitration — runs after
        # footnote separation and paragraph merging; the body corpus and the final narrative assembly
        # share the same shape; items in the native chart-text payload that have a witness in the body are dropped,
        # witness-less items are kept (that channel may be the only witness of body content otherwise lost);
        # every decision is recorded in the ledger.
        blocks, chart_arbitration_info = arbitrate_native_chart_text(blocks)
        # Spec engineering principle: body vs artifact classification never deletes — content_role is a
        # derived annotation; the block's type/text/evidence references all stay untouched.
        # P2 (spec §2/§5): watermark candidates and margin-note clusters are associated at block level here —
        # watermark blocks (text-type verbatim match) are tagged content_role=watermark,
        # margin clusters only register block evidence_id associations, roles unchanged.
        blocks, content_role_info = self.content_roles.assign(
            blocks,
            table_info=table_info,
            figure_info=figure_info,
            watermark_info=(extraction.metadata.get("pdf_watermarks") or {}),
            geometry_info=(extraction.metadata.get("pdf_page_geometry") or {}),
        )
        return (
            blocks,
            image_assets_result,
            formula_info,
            footnote_info,
            content_role_info,
            chart_arbitration_info,
        )

    def _apply_governance(
        self,
        file_path: str | Path,
        extraction: PDFExtractionResult,
        blocks: list,
        runtime_notes: list[str],
    ) -> tuple[list, str, dict[str, Any], bool]:
        """Reconstruction final stage: narrative text assembly + context annotation + external governance decisions.

        Returns (blocks, narrative_text, context, governance_enforced).
        """
        narrative_text = "\n\n".join(
            block["text"]
            for block in blocks
            if block.get("text") and block.get("type") != "footnote"
        )

        context = self.context_annotator.annotate(
            document_metadata=extraction.metadata,
            blocks=blocks,
        )
        governance = self.governance.evaluate(file_path=file_path, context=context)
        context["governance"] = governance
        context["policy_action"] = governance["policy_action"]
        governance_enforced = (
            self.enforce_governance
            and governance["policy_action"] in {"deny", "exclude"}
        )
        if governance["policy_action"] in {"deny", "exclude"}:
            if governance_enforced:
                runtime_notes.append(
                    "external governance decision is "
                    f"'{governance['policy_action']}'; reconstructed retrieval content was excluded while immutable evidence remains in metadata"
                )
                blocks = []
                narrative_text = ""
            else:
                runtime_notes.append(
                    "external governance decision is "
                    f"'{governance['policy_action']}'; parser preserves evidence and exposes the decision for downstream enforcement"
                )
        context["governance"]["enforced"] = governance_enforced
        context["governance"]["filtering_applied"] = governance_enforced
        context["filtering_applied"] = governance_enforced
        return blocks, narrative_text, context, governance_enforced

    def _build_quality(
        self,
        extraction: PDFExtractionResult,
        narrative_text: str,
        blocks: list,
        runtime: dict[str, Any],
        routing: PDFRoutingDecision,
        reading_order_info: dict[str, Any],
        repetitive_info: dict[str, Any],
        heading_info: dict[str, Any],
        navigation_info: dict[str, Any],
        footnote_info: dict[str, Any],
        table_info: dict[str, Any],
        figure_info: dict[str, Any],
        formula_info: dict[str, Any],
        content_role_info: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Quality stage: extraction/layout quality assessment + reconstruction evidence-dict assembly.

        Returns (quality, reconstruction).
        """
        quality = {
            "extraction": self.extraction_quality.assess(extraction, narrative_text),
            "layout": self.layout_quality.assess(
                blocks=blocks,
                reading_order_info=reading_order_info,
                repetitive_info=repetitive_info,
                heading_info=heading_info,
                table_info=table_info,
                figure_info=figure_info,
            ),
        }
        reconstruction = {
            "reading_order": reading_order_info,
            "repetitive": repetitive_info,
            "headings": heading_info,
            "navigation": navigation_info,
            "footnotes": footnote_info,
            "tables": table_info,
            "figures": figure_info,
            "formulas": formula_info,
            "content_roles": content_role_info,
        }
        # Keep PDF extraction reliability outside the narrative
        # evaluation. The final PDF representation remains narrative
        # text, so NarrativeQuality still evaluates the same invariants
        # as TXT; this payload only explains input reliability and is
        # never a scoring or optimization objective.
        quality["for_narrative_evaluation"] = build_pdf_quality_for_evaluation(
            quality=quality,
            runtime=runtime,
            routing=routing.as_dict(),
            reconstruction=reconstruction,
        )
        return quality, reconstruction

    @contextmanager
    def _stage(
        self, stages: dict[str, dict[str, Any]], name: str
    ) -> Iterator[None]:
        """Record per-stage duration and status into a stable schema."""
        started = time.perf_counter()
        status = "ok"
        try:
            yield
        except Exception as error:
            status = f"failed: {error}"
            raise
        finally:
            stages[name] = {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "status": status,
            }

    def _finalize_runtime(
        self,
        runtime: dict[str, Any],
        routing: PDFRoutingDecision,
        stages: dict[str, dict[str, Any]],
    ) -> None:
        """Attach guard, stage timings, and version fingerprints to runtime."""
        if routing.recommended_runtime == "guarded":
            runtime["guard"] = {
                "status": "guarded",
                "reasons": list(routing.signals.get("guard_reasons", [])),
            }
        runtime["stages"] = stages
        runtime["versions"] = {
            "pymupdf": _pymupdf_version(),
            "limits": self.limits.as_dict() if self.limits else None,
            "ocr_backend": runtime.get("ocr_backend"),
            "layout_backend": (
                runtime.get("layout_backend", {}).get("version")
                if isinstance(runtime.get("layout_backend"), dict)
                else None
            ),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _page_labels(navigation_evidence: NavigationEvidence | None) -> dict[str, str]:
        """Invert the declared label map to page → label (spec: double page numbering)."""
        if navigation_evidence is None:
            return {}
        try:
            label_page_map = navigation_evidence.label_page_map()
        except Exception:
            return {}
        return {
            str(page): label
            for label, page in sorted(
                label_page_map.items(), key=lambda item: item[1]
            )
        }

    @staticmethod
    def _register_text_layer_conflicts(
        *,
        extraction: PDFExtractionResult,
        replaced_native_pages: list[Any],
        ocr_by_page: dict[int, Any],
        page_routes: dict[int, dict[str, Any]],
        runtime: dict[str, Any],
        runtime_notes: list[str],
    ) -> None:
        """Compare replaced native text with the kept OCR text; preserve both.

        A page whose native evidence still had characters is compared with
        the OCR text that replaced it (word-multiset share).  The comparison
        is registered under ``TEXT_LAYER_CONFLICT`` and the replaced native
        page is preserved verbatim in ``pdf_replaced_native_pages`` so no
        native evidence is ever silently overwritten by an OCR adoption.
        """
        if replaced_native_pages:
            # Verbatim preservation: the same serialization shape as the
            # evidence layer uses, so consumers can audit what the OCR
            # adoption displaced on every replaced page.
            extraction.metadata["pdf_replaced_native_pages"] = [
                {
                    "page_number": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                    "blocks": [
                        {
                            "block_type": block.block_type,
                            "text": block.text,
                            "page_number": block.page_number,
                            "bbox": list(block.bbox),
                            "source_kind": block.source_kind,
                            "confidence": block.confidence,
                            "raw_payload_ref": block.raw_payload_ref,
                            "raw_payload": block.raw_payload,
                            "evidence_id": block.evidence_id,
                        }
                        for block in page.blocks
                    ],
                    "preserved_reason": (
                        "page replaced by OCR adoption; native evidence kept "
                        "verbatim for audit (TEXT_LAYER_CONFLICT discipline)"
                    ),
                }
                for page in replaced_native_pages
            ]

        conflicts: list[dict[str, Any]] = []
        for page in replaced_native_pages:
            page_number = page.page_number
            route = page_routes.get(page_number, {})
            if int(route.get("native_text_chars", 0) or 0) <= 0:
                continue
            ocr_page = ocr_by_page.get(page_number)
            if ocr_page is None:
                continue
            comparison = compare_layers(
                _page_joined_text(page),
                _page_joined_text(ocr_page),
            )
            conflicts.append(
                {
                    "page": page_number,
                    "registration": "TEXT_LAYER_CONFLICT",
                    "native_text_chars": int(route.get("native_text_chars", 0) or 0),
                    "action": "ocr_kept_native_preserved_in_metadata",
                    **comparison,
                }
            )
        if conflicts:
            runtime["text_layer_conflicts"] = conflicts
            counts: dict[str, int] = {}
            for conflict in conflicts:
                counts[conflict["classification"]] = (
                    counts.get(conflict["classification"], 0) + 1
                )
            pages = [conflict["page"] for conflict in conflicts]
            runtime_notes.append(
                "OCR replaced pages that still carried native text "
                f"({pages}); layer comparison registered ({counts}); the replaced "
                "native evidence is preserved in pdf_replaced_native_pages"
            )

        # -------------------------------------------------------------
        # P2 (spec engineering principle 2): hidden layer vs OCR direct token comparison.
        # Independent channel: the hidden layer is invisible text in the page content stream
        # (alpha==0, usually an overlay left behind by a previous OCR tool); comparing it against this project's OCR
        # result only registers facts — it influences no adoption decision and is not confused with the native vs
        # OCR per-page status.
        # -------------------------------------------------------------
        hidden_by_page = (
            (extraction.metadata.get("pdf_text_characters") or {}).get(
                "invisible_text_by_page"
            )
            or {}
        )
        hidden_comparisons: list[dict[str, Any]] = []
        for page_key, entry in sorted(hidden_by_page.items()):
            if not isinstance(entry, dict):
                continue
            hidden_text = str(entry.get("text") or "")
            if not hidden_text.strip():
                continue
            try:
                page_number = int(page_key)
            except (TypeError, ValueError):
                continue
            ocr_page = ocr_by_page.get(page_number)
            if ocr_page is None:
                # No OCR result for this page: nothing to compare against.
                # The hidden layer itself stays registered verbatim in
                # pdf_text_characters.invisible_text_by_page.
                continue
            comparison = compare_layers(
                hidden_text,
                _page_joined_text(ocr_page),
            )
            hidden_comparisons.append(
                {
                    "page": page_number,
                    "registration": "TEXT_LAYER_CONFLICT",
                    "layers": "hidden_vs_ocr",
                    "action": "comparison_registered_only",
                    "hidden_text_chars": int(entry.get("char_count", 0) or 0),
                    "hidden_text_truncated": bool(entry.get("truncated")),
                    **comparison,
                }
            )
        if hidden_comparisons:
            runtime["hidden_layer_comparisons"] = hidden_comparisons
            hidden_counts: dict[str, int] = {}
            for comparison_record in hidden_comparisons:
                hidden_counts[comparison_record["classification"]] = (
                    hidden_counts.get(comparison_record["classification"], 0) + 1
                )
            hidden_pages = [
                comparison_record["page"] for comparison_record in hidden_comparisons
            ]
            runtime_notes.append(
                "Hidden text layer compared directly against the OCR result "
                f"(pages {hidden_pages}); comparison registered ({hidden_counts}); "
                "adoption decisions are unaffected"
            )

    def _run_ocr_enrichment(
        self,
        *,
        extraction: PDFExtractionResult,
        file_path: str | Path,
        routing: PDFRoutingDecision,
        enable_ocr: bool,
        runtime_notes: list[str],
    ) -> dict[str, Any]:
        """Route low-native-text (and scan-backed) pages to OCR with factual fallbacks.

        OCR replaces a selected page only when it returns non-empty OCR text.
        This prevents a successful-but-empty OCR response from silently erasing
        native evidence. ``ocr_page_routes`` records the final source chosen for
        every page so a consumer can distinguish native text, OCR text, and a
        native fallback after a disabled, unavailable, failed, or empty OCR run.
        A page fully covered by a scan image that still extracts native text
        (design log #175 hole 1: legacy-OCR "sandwich") is also re-OCR'd; adoption is
        gated so thin OCR output cannot displace the embedded layer.
        """
        all_native_pages = [page.page_number for page in extraction.pages]
        page_signals = {
            int(signal["page_number"]): signal
            for signal in routing.signals.get("pages", [])
            if isinstance(signal, dict) and isinstance(signal.get("page_number"), int)
        }
        selected_pages = [
            page_number
            for page_number in all_native_pages
            if not page_signals.get(page_number, {}).get("native_text", False)
        ]
        # Design log #175 hole 1: full-page scan image + embedded text layer = legacy OCR sandwich (overprint/
        # carbon-copy strokes and digit-sticking all trace back to that layer). These pages also request fresh OCR instead of letting
        # a rotten embedded layer sail through inspection on native_text=True alone.
        scan_backed_reocr_pages = [
            page_number
            for page_number in all_native_pages
            if page_number not in selected_pages
            and bool(page_signals.get(page_number, {}).get("scan_backed", False))
            and bool(page_signals.get(page_number, {}).get("native_text", False))
        ]
        selected_pages = sorted(set(selected_pages) | set(scan_backed_reocr_pages))
        page_routes: dict[int, dict[str, Any]] = {
            page_number: {
                "page_number": page_number,
                "native_text_chars": int(
                    page_signals.get(page_number, {}).get("text_chars", 0) or 0
                ),
                "native_text_usable": bool(
                    page_signals.get(page_number, {}).get("native_text", False)
                ),
                "ocr_requested": page_number in selected_pages,
                "ocr_requested_reason": (
                    "scan_backed_suspected_text_layer"
                    if page_number in scan_backed_reocr_pages
                    else "no_usable_native_text"
                    if page_number in selected_pages
                    else None
                ),
                "ocr_attempted": False,
                "final_route": "native_text",
                "fallback_reason": None,
            }
            for page_number in all_native_pages
        }
        runtime: dict[str, Any] = {
            "actual_runtime": "native_text",
            "recommended_runtime": routing.recommended_runtime,
            "native_retained_pages": all_native_pages,
            "ocr_requested_pages": selected_pages,
            "scan_backed_reocr_pages": scan_backed_reocr_pages,
            "ocr_attempted_pages": [],
            "ocr_used_pages": [],
            "ocr_unavailable_pages": [],
            "ocr_failed_pages": [],
            "ocr_budget_exhausted_pages": [],
            "ocr_empty_output_pages": [],
            "ocr_page_timings": [],
            "ocr_page_routes": list(page_routes.values()),
            "enrichment_backends": unconfigured_backend_metadata(),
        }
        if not selected_pages:
            runtime["ocr_status"] = "not_needed"
            return runtime
        if not enable_ocr:
            runtime["ocr_status"] = "disabled"
            runtime["ocr_unavailable_pages"] = selected_pages
            for page_number in selected_pages:
                page_routes[page_number]["final_route"] = "native_fallback"
                page_routes[page_number]["fallback_reason"] = "ocr_disabled"
            runtime_notes.append(
                "OCR was disabled; retaining native extraction for pages without usable native text"
            )
            return runtime

        availability = self.ocr_extractor.availability()
        runtime["ocr_backend"] = availability.get("backend")
        runtime["ocr_capability"] = availability
        if not availability.get("available", False):
            runtime["ocr_status"] = "unavailable"
            runtime["ocr_unavailable_pages"] = selected_pages
            for page_number in selected_pages:
                page_routes[page_number]["final_route"] = "native_fallback"
                page_routes[page_number]["fallback_reason"] = "ocr_backend_unavailable"
            runtime_notes.append(
                "routing recommends 'ocr' runtime for pages "
                f"{selected_pages}; native fallback retained because {availability.get('reason', 'OCR backend is unavailable')}"
            )
            return runtime

        try:
            ocr_result = self.ocr_extractor.extract_pages(file_path, selected_pages)
        except OCRBackendUnavailable as error:
            runtime["ocr_status"] = "unavailable"
            runtime["ocr_unavailable_pages"] = selected_pages
            runtime["ocr_failure_reason"] = str(error)
            for page_number in selected_pages:
                page_routes[page_number]["final_route"] = "native_fallback"
                page_routes[page_number]["fallback_reason"] = "ocr_backend_unavailable"
            runtime_notes.append(
                f"OCR backend became unavailable for pages {selected_pages}; native fallback retained: {error}"
            )
            return runtime
        except Exception as error:  # OCR enrichment is never allowed to discard native evidence.
            runtime["ocr_status"] = "failed"
            runtime["ocr_failed_pages"] = selected_pages
            runtime["ocr_unavailable_pages"] = selected_pages
            runtime["ocr_failure_reason"] = str(error)
            for page_number in selected_pages:
                page_routes[page_number]["final_route"] = "native_fallback"
                page_routes[page_number]["fallback_reason"] = "ocr_extraction_failed"
            runtime_notes.append(
                f"OCR extraction failed for pages {selected_pages}; native fallback retained: {error}"
            )
            return runtime

        ocr_meta = ocr_result.metadata
        ocr_by_page = {page.page_number: page for page in ocr_result.pages}
        failed_pages = _valid_page_numbers(ocr_meta.get("ocr_failed_pages"), selected_pages)
        budget_exhausted_pages = _valid_page_numbers(
            ocr_meta.get("ocr_budget_exhausted_pages"), selected_pages
        )
        page_timings = list(ocr_meta.get("ocr_page_timings", []))
        attempted_pages = [
            page_number
            for page_number in selected_pages
            if page_number not in budget_exhausted_pages
        ]
        empty_output_pages = [
            page_number
            for page_number, page in ocr_by_page.items()
            if not _page_has_text_evidence(page)
        ]
        ocr_used_pages = sorted(
            page_number
            for page_number in ocr_by_page
            if page_number not in failed_pages and page_number not in empty_output_pages
        )
        ocr_used_page_set = set(ocr_used_pages)

        # Design log #175 hole 1: adoption gatekeeping for scan-backed pages. They originally had a usable
        # embedded layer; when the new OCR output is too thin (below the fixed floor, and under a quarter of the embedded
        # layer's length) it does not take over — the embedded layer stays the primary evidence.
        scan_backed_reocr_set = set(scan_backed_reocr_pages)
        thin_reocr_pages = [
            page_number
            for page_number in ocr_used_pages
            if page_number in scan_backed_reocr_set
            and len(_page_joined_text(ocr_by_page[page_number]).strip())
            < max(
                SCAN_BACKED_MIN_OCR_ADOPT_CHARS,
                page_routes[page_number]["native_text_chars"] // 4,
            )
        ]
        thin_reocr_set = set(thin_reocr_pages)
        if thin_reocr_pages:
            ocr_used_pages = [
                page_number
                for page_number in ocr_used_pages
                if page_number not in thin_reocr_set
            ]
            ocr_used_page_set = set(ocr_used_pages)
            runtime["ocr_thin_output_pages"] = thin_reocr_pages
            runtime_notes.append(
                f"scan-backed re-OCR output too thin for pages {thin_reocr_pages}; "
                "embedded text layer retained as primary evidence"
            )

        merged_pages = []
        replaced_native_pages = []
        for page in extraction.pages:
            page_number = page.page_number
            ocr_page = ocr_by_page.get(page_number)
            if page_number not in ocr_used_page_set or ocr_page is None:
                merged_pages.append(page)
                continue
            # The page object is replaced by its OCR counterpart, but the
            # native evidence is preserved verbatim (below) and compared
            # first (TEXT_LAYER_CONFLICT discipline: compare, register,
            # never silently overwrite).
            replaced_native_pages.append(page)
            merged_pages.append(ocr_page)
        extraction.pages = merged_pages

        self._register_text_layer_conflicts(
            extraction=extraction,
            replaced_native_pages=replaced_native_pages,
            ocr_by_page=ocr_by_page,
            page_routes=page_routes,
            runtime=runtime,
            runtime_notes=runtime_notes,
        )
        if ocr_used_pages:
            extraction.extraction_method = "hybrid_native_ocr"
        extraction.metadata.update(ocr_meta)
        native_retained_pages = [
            page_number for page_number in all_native_pages if page_number not in ocr_used_page_set
        ]
        extraction.metadata["native_text_pages"] = native_retained_pages
        extraction.metadata["ocr_pages"] = ocr_used_pages
        extraction.metadata["ocr_empty_output_pages"] = sorted(empty_output_pages)

        # B8: pages whose native evidence still had characters but were
        # replaced wholesale by OCR — the dropped native text may overlap
        # with (duplicate) or contradict the OCR text.  Recorded, never
        # silently resolved.
        replaced_with_native_text = sorted(
            page_number
            for page_number in ocr_used_pages
            if page_routes[page_number]["native_text_chars"] > 0
        )
        if replaced_with_native_text:
            runtime["ocr_replaced_pages_with_native_text"] = replaced_with_native_text
            runtime_notes.append(
                "OCR replaced pages that still carried some native text "
                f"({replaced_with_native_text}); the replaced native evidence "
                "may overlap or disagree with the OCR text"
            )

        runtime["ocr_status"] = _ocr_status(
            attempted_pages=attempted_pages,
            used_pages=ocr_used_pages,
            failed_pages=failed_pages,
            budget_exhausted_pages=budget_exhausted_pages,
            empty_output_pages=empty_output_pages,
        )
        runtime["ocr_attempted_pages"] = attempted_pages
        runtime["ocr_used_pages"] = ocr_used_pages
        runtime["native_retained_pages"] = native_retained_pages
        runtime["ocr_failed_pages"] = failed_pages
        runtime["ocr_budget_exhausted_pages"] = budget_exhausted_pages
        runtime["ocr_empty_output_pages"] = sorted(empty_output_pages)
        runtime["ocr_page_timings"] = page_timings
        runtime["ocr_unavailable_pages"] = [
            page_number
            for page_number in selected_pages
            if page_number not in ocr_used_page_set
            and page_number not in thin_reocr_set
        ]

        for page_number in selected_pages:
            route = page_routes[page_number]
            route["ocr_attempted"] = page_number in attempted_pages
            if page_number in ocr_used_page_set:
                route["final_route"] = "ocr"
                continue
            # Design log #175 hole 1: when a scan-backed page is held back by the gate, the embedded layer is not a
            # "fallback" — it stays in place as the primary evidence.
            route["final_route"] = (
                "native_text" if page_number in thin_reocr_set else "native_fallback"
            )
            if page_number in failed_pages:
                route["fallback_reason"] = "ocr_page_failed"
            elif page_number in budget_exhausted_pages:
                route["fallback_reason"] = "ocr_time_budget_exhausted"
            elif page_number in empty_output_pages:
                route["fallback_reason"] = "ocr_empty_output"
            elif page_number in thin_reocr_set:
                route["fallback_reason"] = "ocr_output_too_thin_for_scan_backed_page"
            else:
                route["fallback_reason"] = "ocr_page_not_returned"

        runtime["actual_runtime"] = (
            "hybrid_native_ocr"
            if native_retained_pages and ocr_used_pages
            else "ocr"
            if ocr_used_pages
            else "native_text"
        )
        if (
            runtime["ocr_failed_pages"]
            or runtime["ocr_budget_exhausted_pages"]
            or runtime["ocr_empty_output_pages"]
        ):
            runtime_notes.append(
                "OCR page-level issues recorded: "
                f"failed={runtime['ocr_failed_pages']}, "
                f"budget_exhausted={runtime['ocr_budget_exhausted_pages']}, "
                f"empty_output={runtime['ocr_empty_output_pages']}"
            )
        return runtime

    def _merge_additive_evidence(
        self,
        extraction: PDFExtractionResult,
        file_path: str | Path,
        runtime_notes: list[str],
    ) -> None:
        """Attach image/table/character/geometry evidence without changing native text evidence."""
        extractors = (
            (self.image_extractor, self.image_extractor.merge_into_pages, "image evidence"),
            (self.table_extractor, self.table_extractor.merge_into_pages, "table evidence"),
            (self.character_extractor, self.character_extractor.merge_into_pages, "character evidence"),
            (self.watermark_extractor, self.watermark_extractor.merge_into_pages, "watermark evidence"),
            (self.geometry_extractor, self.geometry_extractor.merge_into_pages, "page geometry evidence"),
            (self.figure_evidence_extractor, self.figure_evidence_extractor.merge_into_pages, "figure evidence"),
            (self.annotation_extractor, self.annotation_extractor.merge_into_pages, "annotation evidence"),
            (self.file_integrity_extractor, self.file_integrity_extractor.merge_into_pages, "file integrity evidence"),
        )
        # Table detection is the single most expensive additive step and is
        # needed by three consumers (tables, geometry, figures); run it once
        # per page through a shared finder instead of three times.
        table_finder = SharedTableFinder(file_path)
        try:
            for extractor, merger, label in extractors:
                try:
                    if getattr(extractor, "accepts_shared_table_finder", False):
                        additive = cast(
                            SharedTableFinderConsumer, extractor
                        ).extract(file_path, table_finder=table_finder)
                    else:
                        additive = extractor.extract(file_path)
                    extraction.pages = merger(extraction.pages, additive.pages)
                    extraction.metadata.update(additive.metadata)
                except Exception as error:  # Additive evidence is not allowed to be fatal.
                    runtime_notes.append(f"{label} skipped: {error}")
        finally:
            table_finder.close()

    @staticmethod
    def _note_page_geometry_limitations(
        extraction: PDFExtractionResult,
        runtime_notes: list[str],
    ) -> None:
        """Surface geometry facts that make extraction honestly incomplete.

        Box mismatches, mixed orientation and boundary-crossing text change
        what the reader sees versus what the parser can see; they are
        recorded as runtime notes, never silently absorbed.
        """
        geometry = extraction.metadata.get("pdf_page_geometry")
        if not isinstance(geometry, dict):
            return
        if geometry.get("box_mismatch_pages"):
            runtime_notes.append(
                "page boxes differ ("
                f"pages {geometry['box_mismatch_pages']}); content outside "
                "the CropBox is invisible to extraction"
            )
        if geometry.get("mixed_orientation"):
            runtime_notes.append(
                "document mixes portrait and landscape pages ("
                f"landscape pages {geometry.get('landscape_pages')}); "
                "page geometry stays per-page truthful"
            )
        if geometry.get("overflow_text_pages"):
            runtime_notes.append(
                "text blocks cross the visible page boundary (pages "
                f"{geometry['overflow_text_pages']}); recorded as evidence, "
                "never re-cropped"
            )

    # ------------------------------------------------------------
    # B8: OCR language vs detected scripts (multilingual-page honesty)
    # ------------------------------------------------------------
    _SCRIPT_TO_TESSERACT_LANGUAGE = {
        "arabic": "ara",
        "arabic_ext": "ara",
        "arabic_ext_a": "ara",
        "arabic_present_a": "ara",
        "arabic_present_b": "ara",
        "hebrew": "heb",
        "syriac": "syr",
        "cjk_unified": "chi_sim",
        "cjk_ext_a": "chi_sim",
        "cjk_ext_b_plus": "chi_sim",
        "cjk_compat": "chi_sim",
        "kana": "jpn",
        "kana_ext": "jpn",
        "hangul": "kor",
        "cyrillic": "rus",
        "greek": "ell",
        "thai": "tha",
        "devanagari": "hin",
    }

    def _check_ocr_language_limitation(
        self,
        extraction: PDFExtractionResult,
        runtime: dict[str, Any],
        runtime_notes: list[str],
    ) -> None:
        """Record when a single OCR language cannot cover detected scripts.

        Tesseract is configured with one language string (possibly
        "+"-combined).  When pages that actually went through OCR also
        carry non-Latin scripts, recognition quality silently degrades —
        the honest thing is an explicit runtime note, never a silent fix.
        """
        if not runtime.get("ocr_used_pages"):
            return
        characters = extraction.metadata.get("pdf_text_characters") or {}
        scripts = characters.get("scripts") or []
        language = (
            runtime.get("ocr_capability", {}).get("language")
            or "eng"
        )
        configured = {
            part.strip() for part in str(language).split("+") if part.strip()
        }
        missing = {
            script: required
            for script, required in self._SCRIPT_TO_TESSERACT_LANGUAGE.items()
            if script in scripts and required not in configured
        }
        if not missing:
            return
        runtime["ocr_language_limitation"] = {
            "configured_language": language,
            "scripts_without_language": missing,
            "ocr_pages_affected": list(runtime.get("ocr_used_pages", [])),
        }
        runtime_notes.append(
            f"OCR language configured as '{language}' but the document contains "
            f"scripts needing additional Tesseract languages "
            f"({', '.join(f'{script}→{required}' for script, required in sorted(missing.items()))}); "
            "multilingual pages may recognize incorrectly"
        )

    # Full-line 【…】 sectioning convention (Chinese drug instructions: 【成份】/【适应症】/【不良反应】).
    # Same shape as the txt-side _BRACKET_HEADING_PATTERN in semantic_diagnostics.
    _BRACKET_HEADING_RE = re.compile(r"^【(.+?)】$")
    _BRACKET_MAX_TEXT_LEN = 30

    def _recover_structures(
        self,
        *,
        ordered_content: list[PDFBlock],
        min_text_length: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        table_evidence = [block for block in ordered_content if block.block_type == "table"]
        text_evidence = [
            block
            for block in ordered_content
            if (
                block.block_type == "text"
                and len(block.text.strip()) >= min_text_length
                and not self._covered_by_native_table(block, table_evidence)
            )
        ]
        classified_text, heading_info = self.headings.classify(text_evidence)
        self._apply_layout_titles(classified_text, text_evidence)
        bracket_promoted = self._apply_bracket_headings(classified_text)
        if bracket_promoted:
            heading_info["heading_candidate_count"] += bracket_promoted
        text_by_id = {
            block.get("evidence_id"): block
            for block in classified_text
            if block.get("evidence_id")
        }

        reconstructed: list[dict[str, Any]] = []
        for block in ordered_content:
            if block.block_type == "text":
                classified = text_by_id.get(block.evidence_id)
                if classified is not None:
                    reconstructed.append(classified)
                continue
            reconstructed.append(self._structural_block(block))
        return reconstructed, heading_info

    @staticmethod
    def _apply_layout_titles(classified: list[dict[str, Any]], evidence: list[PDFBlock]) -> None:
        """Honour explicit layout-backend title labels over font heuristics.

        MinerU marks titles semantically; the font-size heuristic cannot see
        them because layout blocks carry no span evidence. This override never
        invents headings for native evidence without a backend label.

        A title label without ``text_level`` keeps the heading fact but does
        NOT fabricate a level: the level the font heuristic produced (if any)
        is retained, confidence is capped at candidate grade, and the block is
        marked ``heading_level_source="layout_title_unleveled"`` so consumers
        can tell an evidence-backed level from a fallback one.
        """
        payload_by_id = {
            block.evidence_id: (block.raw_payload or {}) for block in evidence
        }
        for item in classified:
            payload = payload_by_id.get(item.get("evidence_id")) or {}
            if payload.get("mineru_type") != "title":
                continue
            item["type"] = "heading"
            raw_level = payload.get("mineru_text_level")
            try:
                level = int(raw_level) if raw_level is not None else None
            except (TypeError, ValueError):
                level = None
            if level is None:
                # The heading fact comes from the layout backend with no level evidence — do not invent 1.
                item["heading_level"] = max(1, int(item.get("heading_level") or 1))
                item["confidence"] = max(float(item.get("confidence", 0.0)), 0.75)
                item["heading_level_source"] = "layout_title_unleveled"
                continue
            item["heading_level"] = max(1, level)
            item["confidence"] = max(float(item.get("confidence", 0.0)), 0.9)
            item["heading_level_source"] = "layout_title"

    # Sources beyond the font heuristics/layout annotations — they already provide their own level evidence,
    # the 【】 convention does not override these blocks (bookmarks are author declarations, stronger).
    _STRONGER_HEADING_SOURCES = {
        "bookmark",
        "bookmark_promotion",
        "layout_title",
        "layout_title_unleveled",
    }

    @classmethod
    def _apply_bracket_headings(cls, classified: list[dict[str, Any]]) -> int:
        """Promote full-line 【…】 convention sections to headings.

        Chinese drug instructions section with full-line brackets (【成份】/【适应症】). This is a text
        convention, not an author-declared structural marker; therefore:

        * only full-line bracket blocks that are short enough are promoted (instruction section titles
          rarely exceed a dozen or so characters); longer bracket paragraphs (quotations/paraphrases/warnings)
          stay body text — no content is lost;
        * newly promoted blocks get level=1 (instruction sectioning is flat) and record
          ``heading_level_source="bracket_convention"``, classified inferred;
        * full-line bracket blocks already judged heading by the font heuristics keep their original level — only endorse the
          confidence and add the source. Blocks with a stronger source (bookmarks/layout annotations) stay untouched.

        Returns:
            The number of blocks newly promoted to heading in this run (endorsements of existing headings not counted).
        """
        promoted = 0
        for item in classified:
            if item.get("type") not in {"text", "heading"}:
                continue
            text = str(item.get("text") or "").strip()
            if len(text) > cls._BRACKET_MAX_TEXT_LEN:
                continue
            if not cls._BRACKET_HEADING_RE.match(text):
                continue
            existing_source = item.get("heading_level_source")
            if item["type"] == "heading":
                if existing_source in cls._STRONGER_HEADING_SOURCES:
                    continue
                item["confidence"] = max(float(item.get("confidence", 0.0)), 0.8)
                item.setdefault("heading_level_source", "bracket_convention")
                continue
            item["type"] = "heading"
            item["heading_level"] = 1
            item["confidence"] = max(float(item.get("confidence", 0.0)), 0.8)
            item["heading_level_source"] = "bracket_convention"
            promoted += 1
        return promoted

    @staticmethod
    def _attach_page_geometry(blocks: list[dict[str, Any]], extraction: PDFExtractionResult) -> None:
        """Attach factual page dimensions needed by page-boundary table scoring."""
        page_heights = {page.page_number: page.height for page in extraction.pages}
        for block in blocks:
            payload = block.get("raw_payload")
            if not isinstance(payload, dict):
                continue
            page = block.get("page")
            if page in page_heights:
                payload.setdefault("page_height", page_heights[page])

    @staticmethod
    def _covered_by_native_table(text: PDFBlock, tables: list[PDFBlock]) -> bool:
        """Avoid duplicate text when a native table owns the same source area.

        We suppress only text evidence whose bounding box is at least 80% inside
        a detected table. A paragraph that merely surrounds a table is retained.
        """
        text_area = max(0.0, text.bbox[2] - text.bbox[0]) * max(0.0, text.bbox[3] - text.bbox[1])
        if text_area <= 0:
            return False
        for table in tables:
            if table.page_number != text.page_number:
                continue
            x0 = max(text.bbox[0], table.bbox[0])
            y0 = max(text.bbox[1], table.bbox[1])
            x1 = min(text.bbox[2], table.bbox[2])
            y1 = min(text.bbox[3], table.bbox[3])
            overlap = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            if overlap / text_area >= 0.8:
                return True
        return False

    @staticmethod
    def _structural_block(block: PDFBlock) -> dict[str, Any]:
        return {
            "text": block.text,
            "type": block.block_type,
            "page": block.page_number,
            "bbox": list(block.bbox),
            "source_kind": block.source_kind,
            "confidence": block.confidence,
            "evidence_id": block.evidence_id,
            "raw_payload_ref": block.raw_payload_ref,
            "raw_payload": block.raw_payload,
        }
