from provendoc.base.exporter import BaseExporter
from provendoc.core.exporters.atomic_write import atomic_write_json
from provendoc.core.foundation.serialization import safe_serialize


class RawDocumentExporter(BaseExporter):
    """L1 raw-layer exporter — the parser's original snapshot (pre-
    cleaning, immutable).

    Contract (docs/*/internal/artifact_contract.md §2.2):
        - The core text must be Document.original_text (the
          pre-cleaning snapshot); never document.text (cleaning
          overwrites it).
        - Encoding emits only conclusion fields
          (encoding_used / encoding_fallback); the decode attempt
          chain belongs to the debug layer.
        - DQR (DocumentQualityReport) evaluates original_text and
          is written alongside the raw layer.
    """

    def export(
            self,
            data,
            path,
            doc_quality=None,
    ):
        """Export the L1 raw-layer JSON (original_text snapshot +
        conclusion-level encoding/diagnostics).

        Args:
            data: Document (core text taken from original_text;
                legacy objects without that field fall back to
                text).
            path: output file path (atomic write).
            doc_quality: optional DQR (DocumentQualityReport)
                evaluating original_text, written alongside the
                raw layer.
        """
        metadata = data.metadata

        # raw contract: original_text first; fall back to text only
        # when the object lacks that field (legacy construction).
        original_text = getattr(data, "original_text", None)
        if original_text is None:
            original_text = data.text

        # File source text (decoded verbatim, without BOM stripping /
        # newline normalization / invisible-char sanitization) —
        # the "editor view". Only text-like formats carry a value;
        # None means the format provides no source text (e.g. the
        # semantically rebuilt output of pdf/docx/excel).
        source_text = getattr(data, "source_text", None)

        projected = {
            "layer": "raw",
            "file_name": metadata.get("file_name"),
            "file_type": metadata.get("file_type"),
            "mime_type": metadata.get("mime_type"),
            "parser": metadata.get("parser"),
            "encoding": {
                # conclusion-level fields; the attempt chain
                # (decode_trace) belongs to the debug layer
                "encoding_used": metadata.get("encoding_used"),
                "encoding_fallback": bool(
                    metadata.get("encoding_fallback")
                ),
            },
            "sha256": metadata.get("sha256"),
            "text": original_text,
            "source_text": source_text,
            "text_statistics": {
                "char_count": len(original_text),
                "line_count": len(original_text.splitlines()),
            },
            "diagnostics": metadata.get(
                "diagnostics",
                {}
            ),
            "semantic_signals": metadata.get(
                "semantic_signals",
                {}
            ),
            # security carrier census: structured family counts;
            # fact-code reporting lives in warnings (same source).
            "security_carriers": metadata.get(
                "security_carriers",
                {}
            ),
            "warnings": metadata.get(
                "warnings",
                []
            ),
        }

        # DQR: source-document quality assessment (evaluation
        # target = original_text)
        if doc_quality is not None:
            projected["document_quality"] = doc_quality

        # markdown headings
        if "headings" in metadata:
            projected["headings"] = metadata["headings"]

        serialized = safe_serialize(projected)

        # atomic write: temp + os.replace — an interrupted process
        # never leaves a partial raw JSON (same implementation as
        # the markdown/json exporters, single source of truth).
        atomic_write_json(path, serialized)
