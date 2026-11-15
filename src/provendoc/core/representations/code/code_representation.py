# ============================================================
# Code Representation
# ============================================================
"""Code-domain AST structural representation.

The parser stage already provides auditable spans for
functions, classes, methods, interfaces, rules and component
blocks. This representation layer turns them into structured
retrieval units instead of re-parsing or hard-splitting by
token. Source slices are always based on
``Document.original_text`` and UTF-8 byte offsets, so they are
unaffected by CodeCleaner's blank-line normalization, and
every unit traces back to the parse-stage text.
"""

from __future__ import annotations

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SemanticPrimitives,
    SourceContext,
    UnitType,
)
from provendoc.base.representation import BaseRepresentation
from provendoc.core.representations.code.code_chunker import (
    materialize_text,
    spec_metadata,
    structural_chunk_specs,
)


class CodeRepresentation(BaseRepresentation):
    """Map parser structure facts into complete, locatable code retrieval units."""

    def build(self, document):
        """Build parser AST structure facts into code retrieval units.

        Slices are based on UTF-8 byte offsets into
        document.original_text (span_basis="parse_stage_text"),
        unaffected by CodeCleaner whitespace normalization; a
        degraded parse (status != "ok") lowers confidence to
        0.7.

        Args:
            document: Document (metadata["code"] carries parse
                facts such as symbols/comments/diagnostics).

        Returns:
            RepresentationResult: representation_type="code".
        """
        code_metadata = document.metadata.get("code", {})
        language = code_metadata.get("language", "unknown")
        status = code_metadata.get("status", "degraded")
        parse_stage_text = document.original_text if document.original_text is not None else document.text

        if not parse_stage_text:
            return RepresentationResult(
                representation_type="code",
                content=[],
                metadata={
                    "chunk_count": 0,
                    "language": language,
                    "parse_status": status,
                    "chunking": "structural_ast",
                    "span_basis": "parse_stage_text",
                },
            )

        source = parse_stage_text.encode("utf-8")
        specs = structural_chunk_specs(parse_stage_text, code_metadata)
        symbols = code_metadata.get("symbols", [])
        comments = code_metadata.get("comments", [])
        diagnostics = code_metadata.get("diagnostics", [])
        sha256 = document.metadata.get("sha256", "")
        confidence = 1.0 if status == "ok" else 0.7
        units = []

        for index, spec in enumerate(specs):
            text = materialize_text(parse_stage_text, spec)
            # structural_chunk_specs guarantees all materialized blocks are nonempty.
            if not text:
                continue
            metadata = spec_metadata(
                spec,
                source,
                symbols,
                comments,
                diagnostics,
            )
            metadata.update(
                {
                    "chunk_index": index,
                    "language": language,
                    "parse_status": status,
                    "source_stage": "original_text",
                }
            )
            units.append(
                RepresentationUnit(
                    text=text,
                    unit_type=UnitType.CODE,
                    sequence_index=index,
                    component_types=[spec.source_kind],
                    semantic_primitives=SemanticPrimitives(
                        identity=(
                            f"code:{sha256}:{spec.start}:{spec.end}:{index}"
                        ),
                        locality_scope="structural_block",
                        semantic_role="code_structure",
                        integrity_guarantees=[
                            "source_preservation",
                            "comment_preservation",
                            "structural_block_complete",
                            "span_retrievability",
                        ],
                        confidence=confidence,
                    ),
                    source_context=SourceContext(
                        file_name=document.metadata.get("file_name"),
                        file_type=document.metadata.get("file_type"),
                        line_start=metadata["line_start"],
                        line_end=metadata["line_end"],
                    ),
                    metadata=metadata,
                )
            )

        chunk_kinds = {}
        for unit in units:
            kind = unit.metadata["chunk_kind"]
            chunk_kinds[kind] = chunk_kinds.get(kind, 0) + 1

        return RepresentationResult(
            representation_type="code",
            content=units,
            metadata={
                "chunk_count": len(units),
                "language": language,
                "parse_status": status,
                "chunking": "structural_ast",
                "span_basis": "parse_stage_text",
                "source_stage": "original_text",
                "chunk_kinds": chunk_kinds,
                "note": (
                    "Code is chunked by maximal non-overlapping AST / component "
                    "boundaries; non-structural top-level source is retained as "
                    "module_fragment units."
                ),
            },
        )
