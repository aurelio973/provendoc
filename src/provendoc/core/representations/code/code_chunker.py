# ============================================================
# Code Representation
# AST-span structural chunker
# ============================================================
"""Build retrievable code structure units from parser-exported source spans.

This module does not re-parse source code and does no token-size
splitting. It consumes the Tree-sitter facts in
``document.metadata["code"]``: functions, classes, methods,
interfaces, style rules, and HTML/Vue container blocks. Every slice
comes from ``Document.original_text`` and is located by UTF-8 byte
range, so it is unaffected by CodeCleaner blank-line compression.

Completeness policy:
    - Select the largest non-overlapping structural boundaries.
      Classes include their methods; SFC/HTML blocks include the
      embedded JavaScript/TypeScript/CSS/SCSS symbols, so no duplicate
      retrieval units are produced.
    - Any non-blank module-level source not covered by a structural
      boundary (imports, file comments, top-level statements, guards)
      is emitted as a ``module_fragment`` and never lost.
    - Only whitespace-only ranges may be skipped; every non-blank
      piece of source belongs to exactly one unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

_SYMBOL_KINDS = {
    "class",
    "constructor",
    "enum",
    "function",
    "interface",
    "method",
    "rule",
    "struct",
    "trait",
    "type",
    # SQL schema/query definitions and Scala singleton modules.
    "table",
    "view",
    "procedure",
    "cte",
    "object",
}
_CONTAINER_KINDS = {"sfc_block", "html_block"}


@dataclass(frozen=True)
class CodeChunkSpec:
    """A source structure range not yet materialized into a RepresentationUnit."""

    start: int
    end: int
    name: str
    kind: str
    source_kind: str
    parent: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


def _as_span(item: Dict[str, Any]) -> Optional[tuple[int, int]]:
    """Read the metadata-exported byte range; incomplete spans do not participate in structural chunking."""
    try:
        start = int(item["byte_start"])
        end = int(item["byte_end"])
    except (KeyError, TypeError, ValueError):
        return None
    return (start, end) if 0 <= start < end else None


def _line_number(source: bytes, byte_offset: int) -> int:
    return source[:byte_offset].count(b"\n") + 1


def _is_annotation_or_comment(line: bytes) -> bool:
    """Decide whether the line should stay in the same retrieval unit as the definition block that follows."""
    stripped = line.strip()
    return stripped.startswith(
        (
            b"@",
            b"#",
            b"//",
            b"/*",
            b"*",
            b"*/",
            b"<!--",
            b"-->",
            b"template",
        )
    )


def _include_leading_annotations(source: bytes, start: int) -> int:
    """Fold decorators / doc comments adjacent to the definition into it, without crossing blank lines."""
    line_start = source.rfind(b"\n", 0, start) + 1
    cursor = line_start
    while cursor > 0:
        previous_end = cursor - 1
        previous_start = source.rfind(b"\n", 0, previous_end) + 1
        line = source[previous_start:cursor]
        if not _is_annotation_or_comment(line):
            break
        cursor = previous_start
    return cursor


def _candidate_specs(code: Dict[str, Any], source_size: int) -> List[CodeChunkSpec]:
    """Extract candidate ranges usable as top-level boundaries from the unified code metadata."""
    candidates: List[CodeChunkSpec] = []

    for unit in code.get("structural_units", []):
        if unit.get("kind") not in _CONTAINER_KINDS:
            continue
        span = _as_span(unit)
        if span is None:
            continue
        start, end = span
        if end > source_size:
            continue
        detail = unit.get("detail")
        candidates.append(
            CodeChunkSpec(
                start=start,
                end=end,
                name=unit.get("name") or "component_block",
                kind="component_block",
                source_kind=unit["kind"],
                detail=dict(detail) if isinstance(detail, dict) else None,
            )
        )

    for symbol in code.get("symbols", []):
        symbol_kind = symbol.get("kind")
        if symbol_kind not in _SYMBOL_KINDS:
            continue
        span = _as_span(symbol)
        if span is None:
            continue
        start, end = span
        if end > source_size:
            continue
        candidates.append(
            CodeChunkSpec(
                start=start,
                end=end,
                name=symbol.get("name") or "<anonymous>",
                kind="symbol",
                source_kind=symbol_kind,
                parent=symbol.get("parent"),
            )
        )

    return candidates


def _maximal_non_overlapping(candidates: Iterable[CodeChunkSpec]) -> List[CodeChunkSpec]:
    """Select the largest non-overlapping structural ranges, preferring container blocks to preserve component boundaries."""
    unique: Dict[tuple[int, int], CodeChunkSpec] = {}
    for candidate in candidates:
        key = (candidate.start, candidate.end)
        previous = unique.get(key)
        # For equal ranges a container wins over a symbol; otherwise
        # the first-in-source stable result is kept.
        if previous is None or (
            candidate.kind == "component_block" and previous.kind != "component_block"
        ):
            unique[key] = candidate

    def rank(item: CodeChunkSpec) -> tuple[int, int, int, str]:
        """Sort key: larger ranges first, containers before symbols at the same size, then by start and name."""
        return (
            -(item.end - item.start),
            0 if item.kind == "component_block" else 1,
            item.start,
            item.name,
        )

    selected: List[CodeChunkSpec] = []
    for candidate in sorted(unique.values(), key=rank):
        if any(
            candidate.start < existing.end and existing.start < candidate.end
            for existing in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item.start, item.end, item.name))


def _expand_definition_prefixes(
    selected: List[CodeChunkSpec],
    source: bytes,
) -> List[CodeChunkSpec]:
    """Extend runs of decorators/doc comments into the symbol chunk prefix."""
    expanded: List[CodeChunkSpec] = []
    previous_end = 0
    for spec in selected:
        start = spec.start
        if spec.kind == "symbol":
            candidate_start = _include_leading_annotations(source, start)
            # If the candidate prefix would overlap the previous structural
            # block, skip the expansion rather than create an overlap.
            if candidate_start >= previous_end:
                start = candidate_start
        expanded.append(
            CodeChunkSpec(
                start=start,
                end=spec.end,
                name=spec.name,
                kind=spec.kind,
                source_kind=spec.source_kind,
                parent=spec.parent,
                detail=spec.detail,
            )
        )
        previous_end = spec.end
    return expanded


def structural_chunk_specs(source_text: str, code: Dict[str, Any]) -> List[CodeChunkSpec]:
    """Return structural blocks and module residual ranges in source order."""
    source = source_text.encode("utf-8")
    if not source:
        return []

    selected = _maximal_non_overlapping(_candidate_specs(code, len(source)))
    selected = _expand_definition_prefixes(selected, source)

    specs: List[CodeChunkSpec] = []
    cursor = 0
    fragment_index = 0
    for structural in selected:
        if structural.start > cursor:
            residual = source[cursor:structural.start]
            if residual.strip():
                fragment_index += 1
                specs.append(
                    CodeChunkSpec(
                        start=cursor,
                        end=structural.start,
                        name=f"<module_fragment_{fragment_index}>",
                        kind="module_fragment",
                        source_kind="module_fragment",
                    )
                )
        specs.append(structural)
        cursor = structural.end

    if cursor < len(source) and source[cursor:].strip():
        fragment_index += 1
        specs.append(
            CodeChunkSpec(
                start=cursor,
                end=len(source),
                name=f"<module_fragment_{fragment_index}>",
                kind="module_fragment",
                source_kind="module_fragment",
            )
        )

    # A non-blank file with no symbols / empty structure is still
    # retrievable as one whole-module unit.
    if not specs and source.strip():
        specs.append(
            CodeChunkSpec(
                start=0,
                end=len(source),
                name="<module>",
                kind="module_fragment",
                source_kind="module_fragment",
            )
        )
    return specs


def materialize_text(source_text: str, spec: CodeChunkSpec) -> str:
    """Slice by UTF-8 byte span and decode, preserving the parse-stage original source."""
    return source_text.encode("utf-8")[spec.start:spec.end].decode(
        "utf-8", errors="replace"
    )


def spec_metadata(
    spec: CodeChunkSpec,
    source: bytes,
    symbols: List[Dict[str, Any]],
    comments: List[Dict[str, Any]],
    diagnostics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build unit metadata, with counts of parse facts whose spans fall inside this chunk."""

    def in_range(item: Dict[str, Any]) -> bool:
        """Check whether the item's span lies fully inside the chunk range."""
        span = _as_span(item)
        return span is not None and spec.start <= span[0] and span[1] <= spec.end

    return {
        "chunking": "structural_ast",
        "chunk_kind": spec.kind,
        "source_kind": spec.source_kind,
        "name": spec.name,
        "parent": spec.parent,
        "span_basis": "parse_stage_text",
        "byte_start": spec.start,
        "byte_end": spec.end,
        "line_start": _line_number(source, spec.start),
        "line_end": _line_number(source, max(spec.start, spec.end - 1)),
        "symbol_count": sum(1 for symbol in symbols if in_range(symbol)),
        "comment_count": sum(1 for comment in comments if in_range(comment)),
        "diagnostic_count": sum(1 for diagnostic in diagnostics if in_range(diagnostic)),
        **({"detail": spec.detail} if spec.detail else {}),
    }
