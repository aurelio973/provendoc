"""Public PDF parser backed by the PDF Document Ingestion Runtime."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.diagnostics import (
    blank_run_excess_ratio,
    build_carrier_facts,
    build_diagnostics,
    build_warnings,
    contains_ansi_escape,
    count_replacement_chars,
    count_suspicious_chars,
    detect_hidden_unicode,
    detect_security_carriers,
    emoji_ratio,
    empty_line_ratio,
    printable_ratio,
    symbol_ratio,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.foundation.unresolved_registry import (
    build_unresolved_record,
    stamp_unresolved_registry,
)
from provendoc.core.kie.invoice import extract_invoice_fields
from provendoc.core.parsers.narrative.pdf.contracts import PDFRuntimeLimits
from provendoc.core.parsers.narrative.pdf.evidence.file_integrity import (
    FileIntegrityExtractor,
)
from provendoc.core.parsers.narrative.pdf.evidence.security import (
    build_security_warnings,
)
from provendoc.core.parsers.narrative.pdf.pipeline import PDFPipeline
from provendoc.core.parsers.narrative.pdf.reconstruction import ParagraphReconstructor
from provendoc.core.structure.domain_signals import detect_domain_signals

_log = logging.getLogger(__name__)

# KIE core-field frozen list v1 (design log #127): fields both template families (Chinese e-invoice /
# Western invoice) should structurally have — nulls in these fields are the "no evidence in the
# text layer" candidates; title (may be absent) and drawer/remark (family differences / honest boundary,
# value stays null) are not in the list and are not registered (better absent than fabricated).
_KIE_CORE_FIELDS = (
    "invoice_number",
    "issue_date",
    "buyer",
    "seller",
    "items",
    "totals",
    "grand_total",
)


class PdfParser(BaseParser):
    """Parse through probe → evidence → reconstruction → diagnostics.

    Native evidence always runs. When local Tesseract support is available,
    pages without usable native text are replaced by auditable OCR evidence.
    An explicitly injected MinerU layout backend (optional dependency) runs
    only when routing recommends the layout runtime and overlays provenance-
    bound regions on the unchanged native/OCR evidence; without it, the
    unavailable layout route remains an explicit, truthful fallback.
    """

    def __init__(
        self,
        merge_blocks: bool = True,
        detect_headers: bool = True,
        min_blcok_length: int = 3,
        min_block_length: int | None = None,
        enable_ocr: bool = True,
        ocr_language: str = "eng",
        ocr_tessdata: str | None = None,
        layout_backend: Any | None = None,
        enable_layout: bool = True,
        governance_provider: Any | None = None,
        enforce_governance: bool = False,
        limits: PDFRuntimeLimits | None = None,
        enable_image_assets: bool = True,
    ):
        """Configure PDF parsing options: OCR/layout/governance etc."""
        self.merge_blocks = merge_blocks
        self.detect_headers = detect_headers
        self.enable_ocr = enable_ocr
        # B8: Tesseract language string, "+"-combined for multilingual
        # documents (e.g. "eng+chi_sim"); recorded verbatim in OCR metadata.
        self.ocr_language = ocr_language
        # Explicit tessdata directory (e.g. project-local .tessdata with
        # chi_sim); None = PyMuPDF default resolution (TESSDATA_PREFIX or
        # system install).
        self.ocr_tessdata = ocr_tessdata
        self.layout_backend = layout_backend
        self.enable_layout = enable_layout
        self.governance_provider = governance_provider
        self.enforce_governance = enforce_governance
        self.limits = limits
        # Opt-in image-asset extraction: original bytes plus placement
        # facts, joined to the narrative via manifest asset ids.
        # Auto-trigger: extraction is a natural no-op for PDFs whose
        # reconstruction produced no figure blocks, so the default on
        # means "image detected → assets (+ derived OpenCV processing
        # at the run layer)".  False = explicit opt-out.
        self.enable_image_assets = enable_image_assets
        # Keep the historical misspelled keyword compatible with callers.
        self.min_block_length = (
            min_block_length if min_block_length is not None else min_blcok_length
        )

    def parse(self, file_path) -> Document:
        """Parse a PDF into a Document (probe → evidence → reconstruction → diagnostics)."""
        path = Path(file_path)
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "pdf"
        start_time = time.perf_counter()

        try:
            raw_bytes = path.read_bytes()
            metadata["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
            result = PDFPipeline(
                paragraphs=ParagraphReconstructor(merge_blocks=self.merge_blocks),
                layout_backend=self.layout_backend,
                governance_provider=self.governance_provider,
                enforce_governance=self.enforce_governance,
                limits=self.limits,
                ocr_language=self.ocr_language,
                ocr_tessdata=self.ocr_tessdata,
            ).run(
                path,
                suppress_repetitive=self.detect_headers,
                min_text_length=max(1, self.min_block_length),
                enable_ocr=self.enable_ocr,
                enable_layout=self.enable_layout,
                enable_image_assets=self.enable_image_assets,
            )

            extraction_quality = result.quality["extraction"]
            layout_quality = result.quality["layout"]
            generic_diagnostics = self._build_text_diagnostics(result.narrative_text)
            warnings = build_warnings(generic_diagnostics)
            warnings.extend(result.runtime_notes)
            if layout_quality["multicolumn_suspected"]:
                warnings.append("multi-column layout suspected")
            if extraction_quality["empty_text_pages"]:
                warnings.append(
                    f"{extraction_quality['empty_text_pages']} pages have no native text evidence"
                )
            # Visible fact: body text of /Annots annotations (sticky notes/highlights/stamps etc.) never comes
            # from the annotation body — same discipline as docx_review's annotation warning.
            annotations = result.extraction.metadata.get("pdf_annotations") or {}
            if annotations.get("total_annotation_count"):
                warnings.append(
                    f"document carries {annotations['total_annotation_count']} "
                    "PDF annotations (annotation bodies are not part of "
                    "the narrative text)"
                )
                if annotations.get("hidden_annotation_count"):
                    warnings.append(
                        f"{annotations['hidden_annotation_count']} annotations "
                        "carry the hidden (/F bit 2) flag"
                    )
            # pdf_security_semantics_prereg_v1 §6.4（Q3-A/Q4-A）：
            # Security/compliance signal fact codes surface upward — JS present, invisible text present,
            # redaction not removed, visual masking, partial ByteRange, CMap contradictions.
            # Deep facts are fully retained in the pdf_security evidence section; this is only a visibility
            # copy for the compliance consumer surface (same discipline as G5 carrier).
            warnings.extend(build_security_warnings(result.security))

            metadata.update(
                {
                    "page_count": len(result.extraction.pages),
                    "block_count": len(result.blocks),
                    "extraction_confidence": extraction_quality["extraction_confidence"],
                    "diagnostics": generic_diagnostics,
                    "warnings": list(dict.fromkeys(warnings)),
                    "pdf_blocks": result.blocks,
                    "pdf_evidence": self._serialize_evidence(result.extraction),
                    "pdf_navigation": result.navigation,
                    "pdf_security": result.security,
                    "pdf_routing_decision": result.routing.as_dict(),
                    # Spec-engineering principle (text-layer status / dual page numbers / layer-conflict preservation):
                    # top-level copy for consumers — the same facts are also fully retained
                    # in pdf_evidence.metadata (the extraction metadata channel);
                    # this is only a convenience visibility copy for consumers, not a second fact source.
                    "pdf_text_layer": result.extraction.metadata.get(
                        "pdf_text_layer"
                    ),
                    "pdf_page_identity": result.extraction.metadata.get(
                        "pdf_page_identity"
                    ),
                    # Spec-engineering principle: unified coordinates — every external bbox is
                    # top-left origin, unit pt; raw PDF user-space (bottom-left
                    # origin) values live only in raw_payload, preserved as-is.
                    "pdf_coordinate_system": {
                        "origin": "top_left",
                        "y_axis": "down",
                        "units": "points",
                        "raw_pdf_space": (
                            "bottom_left_origin; preserved verbatim in "
                            "raw_payload where the source reports it"
                        ),
                        "note": (
                            "all exported bbox values (pdf_blocks, evidence, "
                            "layout regions) use this normalized convention; "
                            "raw values are never rewritten"
                        ),
                    },
                    "pdf_reconstruction": result.reconstruction,
                    "pdf_quality": {
                        key: value
                        for key, value in result.quality.items()
                        if key != "for_narrative_evaluation"
                    },
                    # This is an external context for the PDF-derived
                    # narrative input, not an evaluation dimension or score.
                    "pdf_quality_for_evaluation": result.quality["for_narrative_evaluation"],
                    "pdf_context": result.context,
                    "pdf_governance_enforced": result.governance_enforced,
                    "pdf_runtime": {
                        **result.runtime,
                        "runtime_notes": result.runtime_notes,
                        "phase": 3,
                    },
                    "multi_column_suspected": layout_quality["multicolumn_suspected"],
                    # Top-level copy for consumers (same facts as
                    # pdf_evidence.metadata["pdf_annotations"]).
                    "pdf_annotations": annotations,
                    "domain_signals": detect_domain_signals(result.narrative_text),
                    "parse_duration_ms": round((time.perf_counter() - start_time) * 1000, 3),
                }
            )
            # Invoice KIE (core/kie/invoice.py, 2026-10-06; 2026-10-12 dual-template-
            # family dispatch, design log #126): when domain_signals' invoice family
            # quorum judges present, marker counts are upgraded to structured field values,
            # written into metadata["invoice_fields"]. The template family auto-dispatches by anchor evidence
            # (Chinese e-invoice / Western invoice), sharing one field envelope; each field carries
            # value/confidence/source/note; fields with no text-layer evidence (e.g. invoice number
            # in an image region) return null + reason, no guessing or backfilling (the cut-random-digit-stream-
            # fake-number tier has been removed).
            _domain_signals = metadata.get("domain_signals") or {}
            if (_domain_signals.get("families") or {}).get("invoice", {}).get("present"):
                metadata["invoice_fields"] = extract_invoice_fields(
                    result.narrative_text, filename=path.name
                )
                # #127 routing honesty (minimal closed loop v1): KIE core-field nulls are
                # upgraded from note text to field-level structured registration — for mixed forms (text layer
                # present but key fields in image regions) the absence no longer hides only in a note.
                # Shared foundation stamp (ocr_unresolved key, marker_contract §1);
                # silent degradation on failure (bypass fact-channel discipline).
                try:
                    stamp_unresolved_registry(
                        metadata,
                        "ocr_unresolved",
                        self._derive_kie_unresolved(
                            metadata["invoice_fields"], path.name
                        ),
                    )
                except Exception:
                    _log.debug(
                        "pdf kie ocr_unresolved registry skipped",
                        exc_info=True,
                    )
            # Object-level unresolved registration (marker_contract.md §1, 2026-09-28):
            # derived from existing degradation fact sources — pdf_native_page_errors (per-page
            # failures of the native channel) and runtime["ocr_page_routes"] (per-page
            # fallback_reason of the OCR channel) — existing keys untouched, no guessing or backfilling;
            # metadata["pdf_unresolved"] is written only when non-empty (narrative contract
            # unchanged; channel names share the vocabulary with the 13 extraction_method values).
            unresolved_records = self._derive_unresolved_records(
                result.extraction.metadata.get("pdf_native_page_errors"),
                result.runtime.get("ocr_page_routes"),
            )
            if unresolved_records:
                metadata["pdf_unresolved"] = {
                    "extractor": "pdf_unresolved_v1",
                    "records": unresolved_records,
                }
            if result.extraction.metadata.get("pdf_replaced_native_pages"):
                # TEXT_LAYER_CONFLICT discipline: native-page evidence adopted/replaced by OCR
                # must be visible at document level for verbatim audit.
                metadata["pdf_replaced_native_pages"] = (
                    result.extraction.metadata["pdf_replaced_native_pages"]
                )
            if result.image_assets is not None:
                from provendoc.core.image_assets.schema import (
                    image_assets_metadata,
                )

                asset_entry, asset_payloads = image_assets_metadata(
                    result.image_assets.get("records") or [],
                    result.image_assets.get(
                        "extractor", "pdf_image_assets_v1"
                    ),
                )
                # G11: pass through the byte-budget truncation facts (payload_budget_bytes /
                # payload_bytes / payloads_truncated /
                # budget_skipped_placements) — image_assets_metadata only assembles
                # records; the budget facts live at the extractor top level and would be lost without pass-through.
                for budget_key in (
                    "payload_budget_bytes",
                    "payload_bytes",
                    "payloads_truncated",
                    "budget_skipped_placements",
                ):
                    if budget_key in result.image_assets:
                        asset_entry[budget_key] = result.image_assets[budget_key]
                metadata["pdf_image_assets"] = asset_entry
                if asset_payloads:
                    metadata["image_asset_payloads"] = asset_payloads

            # Security-carrier census (invisible_carrier_v1,
            # security_carrier_formats_prereg_v1 §2.3; same precedent as the doc_cfb
            # bypass): census view = narrative_text + blocks text of native pages
            # replaced by OCR. The security census covers the document's own text
            # evidence — the OCR layer is added on top; the replaced native layer is what the document
            # itself carries (TEXT_LAYER_CONFLICT discipline extended); the replaced pages' native
            # text is not inside narrative_text, so no double counting. metadata["security_carriers"]
            # is written only when non-zero; fact codes are appended into warnings.
            carrier_view_parts = [result.narrative_text]
            for replaced_page in metadata.get(
                "pdf_replaced_native_pages"
            ) or []:
                for block in replaced_page.get("blocks") or []:
                    if isinstance(block, dict) and block.get("text"):
                        carrier_view_parts.append(block["text"])
            carrier_census = detect_security_carriers(
                "\n".join(carrier_view_parts)
            )
            if carrier_census:
                metadata["security_carriers"] = carrier_census
                metadata.setdefault("warnings", []).extend(
                    build_carrier_facts(carrier_census)
                )
            return Document(text=result.narrative_text, metadata=metadata)
        except Exception as error:
            metadata.update(
                {
                    "parse_failed": True,
                    "parse_error": str(error),
                    "parse_duration_ms": round((time.perf_counter() - start_time) * 1000, 3),
                }
            )
            # B9: even an honest document-level failure keeps the byte-level
            # container facts so the failure stays attributable (a fake PDF
            # reports a broken magic, a truncated one reports a missing
            # %%EOF).  The scan does not need MuPDF to open the file, and it
            # is itself guarded so it can never mask the original failure.
            try:
                metadata["pdf_file_integrity"] = FileIntegrityExtractor().extract(
                    path
                ).metadata["pdf_file_integrity"]
            # The integrity extractor may raise arbitrary exceptions on damaged PDFs; omit the pdf_file_integrity key (honest absence).
            except Exception:
                pass
            return Document(text="", metadata=metadata)

    @staticmethod
    def _derive_unresolved_records(
        native_page_errors, ocr_page_routes
    ) -> list[dict[str, Any]]:
        """Derive object-level registration records from existing degradation facts of the native/OCR channels.

        - native channel: per-page failures from ``pdf_native_page_errors``
          (``page-{n}-native_text``, NATIVE_PAGE_ERROR);
        - OCR channel: per-page facts from ``ocr_page_routes`` where final_route ==
          native_fallback, fallback_reason mapped
          OCR_BACKEND_UNAVAILABLE / OCR_EMPTY_OUTPUT /
          CHANNEL_DEGRADED (``page-{n}-ocr``).
        PDF page text has no body placeholder line — placeholder_text is always empty
        (marker_contract.md §1.2 "no placeholder line means empty"); the detailed failure evidence
        travels with the record (stable external code, internal evidence).
        """
        records: list[dict[str, Any]] = []
        for page_error in native_page_errors or []:
            page_number = page_error.get("page_number")
            if not isinstance(page_number, int):
                continue
            records.append(
                {
                    "object_type": "page_text",
                    "object_id": f"page-{page_number}-native_text",
                    "resolution": "unresolved",
                    "reason_code": "NATIVE_PAGE_ERROR",
                    "source": f"page-{page_number}",
                    "placeholder_text": None,
                    "error": page_error.get("error"),
                }
            )
        for route in ocr_page_routes or []:
            if route.get("final_route") != "native_fallback":
                continue
            reason = route.get("fallback_reason")
            page_number = route.get("page_number")
            if reason is None or not isinstance(page_number, int):
                continue
            reason_code = (
                "OCR_BACKEND_UNAVAILABLE"
                if reason == "ocr_backend_unavailable"
                else "OCR_EMPTY_OUTPUT"
                if reason == "ocr_empty_output"
                else "CHANNEL_DEGRADED"
            )
            records.append(
                {
                    "object_type": "page_text",
                    "object_id": f"page-{page_number}-ocr",
                    "resolution": "unresolved",
                    "reason_code": reason_code,
                    "source": f"page-{page_number}",
                    "placeholder_text": None,
                    "fallback_reason": reason,
                }
            )
        return records

    @staticmethod
    def _derive_kie_unresolved(invoice_fields, filename) -> list[dict[str, Any]]:
        """Derive field-level unresolved registration records from the KIE per-field envelope (#127 v1).

        Scans only the _KIE_CORE_FIELDS frozen list (fields both families should have): fields with value
        None → NO_TEXT_EVIDENCE (no evidence in the text layer, unrecoverable when rendered in an image
        region; real OCR work is a separate project); note travels with the record as-is. Family-difference
        fields (Western drawer/remark) are not in the list and are not registered.
        source takes the file name (KIE input is whole-document narrative text, a document-level
        locator), object_id = ``invoice:{field}``.
        """
        records: list[dict[str, Any]] = []
        fields = (invoice_fields or {}).get("fields") or {}
        for field_name in _KIE_CORE_FIELDS:
            envelope = fields.get(field_name)
            if not isinstance(envelope, dict):
                continue
            if envelope.get("value") is not None:
                continue
            record = build_unresolved_record(
                object_type="invoice_field",
                object_id=f"invoice:{field_name}",
                resolution="unresolved",
                reason_code="NO_TEXT_EVIDENCE",
                source=filename,
                placeholder_text=None,
                note=envelope.get("note"),
            )
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _build_text_diagnostics(text: str) -> dict[str, Any]:
        return build_diagnostics(
            replacement_chars=count_replacement_chars(text),
            suspicious_chars=count_suspicious_chars(text),
            printable_ratio=printable_ratio(text),
            symbol_ratio=symbol_ratio(text),
            empty_line_ratio=empty_line_ratio(text),
            hidden_unicode=detect_hidden_unicode(text),
            contains_ansi_escape=contains_ansi_escape(text),
            emoji_ratio=emoji_ratio(text),
            blank_run_excess_ratio=blank_run_excess_ratio(text),
        )

    @staticmethod
    def _serialize_evidence(extraction) -> dict[str, Any]:
        """Persist immutable evidence in JSON-compatible document metadata."""
        return {
            "extraction_method": extraction.extraction_method,
            "metadata": dict(extraction.metadata),
            "pages": [
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
                }
                for page in extraction.pages
            ],
        }
