# ============================================================
# Code Domain
# Cross-language parse result contracts
# ============================================================
"""CodeStructure — the cross-language contract for code parse results.

Design principles (matching the project's evidence-first philosophy):

    - Syntax errors are searchable parse results, not parse failures.
      Tree-sitter ERROR / MISSING nodes fold into CodeDiagnostic with
      row/col and snippet — auditable and searchable.
    - Recovery over rejection: locally correct structure in damaged
      source (functions, classes, comments) is still extracted; the
      status degrades to "recovered" instead of dropping the file.
    - Comments are first-class citizens: all extracted as CommentUnit,
      and the text body never leaves document.text.
    - Source spans always use the parse-stage text
      (Document.original_text); cleaning / representation stages
      never write back into this structure, and line offsets are declared via span_basis.

Serialization conventions:
    to_metadata() emits a JSON-compatible dict mounted at
    document.metadata["code"]; large lists (comments/symbols/units)
    are truncated to *_export_limit, full counts stay in stats.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# Export truncation limits — metadata targets reporting/retrieval, not full dumps
_COMMENTS_EXPORT_LIMIT = 200
_SYMBOLS_EXPORT_LIMIT = 300
_UNITS_EXPORT_LIMIT = 300
_DIAGNOSTICS_EXPORT_LIMIT = 100
_SNIPPET_CHAR_LIMIT = 120


class ParseStatus(str, Enum):
    """Code parse status.

    ok        — full parse by the main backend, no ERROR nodes
    recovered — fault-tolerant parse succeeded; ERROR/MISSING nodes exist
                (syntax errors recovered, local structure still trusted)
    degraded  — main backend crashed, degraded to minimal info (diagnostics only)
    empty     — empty file
    """

    OK = "ok"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    EMPTY = "empty"


def _clip_snippet(source: str, start: int, end: int) -> str:
    """Clip a diagnostic snippet (single line, stripped, length-limited)."""
    snippet = source[start:end].strip()
    if len(snippet) > _SNIPPET_CHAR_LIMIT:
        snippet = snippet[:_SNIPPET_CHAR_LIMIT] + "…"
    return snippet.replace("\n", "\\n")


@dataclass
class SourceSpan:
    """Source range — 1-based lines (inclusive), 0-based byte offsets (inclusive).

    line/col come from tree-sitter Point; byte offsets point into the UTF-8
    encoded source text, aligned with Node.start_byte / end_byte.
    """

    line_start: int
    line_end: int
    col_start: int = 0
    col_end: int = 0
    byte_start: int = 0
    byte_end: int = 0

    def to_dict(self) -> Dict[str, int]:
        """Serialize to a dict (with UTF-8 byte ranges)."""
        return {
            "line_start": self.line_start,
            "line_end": self.line_end,
            "col_start": self.col_start,
            "col_end": self.col_end,
            # UTF-8 byte ranges are required by CodeRepresentation to slice
            # parse-stage source without relying on cleaning-stage line offsets.
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }


@dataclass
class CodeDiagnostic:
    """A searchable syntax-error / recovery event record."""

    code: str          # "syntax_error" | "missing_node" | "parse_failed" ...
    message: str
    severity: str = "error"   # error | warning
    span: Optional[SourceSpan] = None
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "snippet": self.snippet,
            **(self.span.to_dict() if self.span else {}),
        }


@dataclass
class CommentUnit:
    """Comment unit — kind separates line / block / doc comments / injected languages."""

    kind: str          # "line" | "block" | "doc" | "injected"
    text: str
    span: SourceSpan

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "kind": self.kind,
            "text": self.text,
            **self.span.to_dict(),
        }


@dataclass
class SymbolUnit:
    """Symbol unit — a named entity: function / class / method / interface / rule."""

    name: str
    kind: str          # "function" | "class" | "method" | "rule" | ...
    span: SourceSpan
    parent: Optional[str] = None   # owning class / namespace name (best effort)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "name": self.name,
            "kind": self.kind,
            "parent": self.parent,
            **self.span.to_dict(),
        }


@dataclass
class StructuralUnit:
    """Structural unit — top-level structural facts other than comments (imports, SFC blocks)."""

    kind: str          # "import" | "sfc_block" | ...
    name: str
    span: SourceSpan
    detail: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "kind": self.kind,
            "name": self.name,
            **self.span.to_dict(),
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class CodeStructure:
    """Cross-language code parse result (the pre-cleaning parse snapshot)."""

    language: str                    # canonical language name: "python", "javascript", ...
    backend: str                     # "tree_sitter" | "tree_sitter_vue" | "lexical"
    status: str                      # ParseStatus value
    diagnostics: List[CodeDiagnostic] = field(default_factory=list)
    comments: List[CommentUnit] = field(default_factory=list)
    symbols: List[SymbolUnit] = field(default_factory=list)
    structural_units: List[StructuralUnit] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------
    # serialization
    # ------------------------------------------

    def to_metadata(self) -> Dict[str, Any]:
        """Export a metadata dict (each list truncated to its export limit)."""
        return {
            "language": self.language,
            "backend": self.backend,
            "status": self.status,
            # Line-number basis: parse-stage text = Document.original_text.
            # Cleaning may change line numbers; consumers must use original_text.
            "span_basis": "parse_stage_text",
            "diagnostics": [
                d.to_dict() for d in self.diagnostics[:_DIAGNOSTICS_EXPORT_LIMIT]
            ],
            "comments": [
                c.to_dict() for c in self.comments[:_COMMENTS_EXPORT_LIMIT]
            ],
            "symbols": [
                s.to_dict() for s in self.symbols[:_SYMBOLS_EXPORT_LIMIT]
            ],
            "structural_units": [
                u.to_dict() for u in self._exported_structural_units()
            ],
            "stats": self._stats_with_counts(),
        }

    def _exported_structural_units(self) -> List["StructuralUnit"]:
        """Export ordering: non-call units (import / extends / sfc_block) first.

        Call units usually far outnumber other structural units; truncating in
        source order would push imports out of the export window on large files,
        breaking dependency-retention evaluation. Export key structures first, then calls (in source order) with the remaining budget.
        """
        primary = [u for u in self.structural_units if u.kind != "call"]
        calls = [u for u in self.structural_units if u.kind == "call"]
        return (primary + calls)[:_UNITS_EXPORT_LIMIT]

    def _stats_with_counts(self) -> Dict[str, Any]:
        stats = dict(self.stats)
        stats.setdefault("comment_count", len(self.comments))
        stats.setdefault("symbol_count", len(self.symbols))
        stats.setdefault("structural_unit_count", len(self.structural_units))
        stats.setdefault("diagnostic_count", len(self.diagnostics))
        # P1 dependency counts: full counts (never lost to export truncation).
        for kind in ("call", "reference", "extends", "implements", "mixin"):
            stats[f"{kind}_count"] = sum(
                1 for u in self.structural_units if u.kind == kind
            )
        return stats

    # ------------------------------------------
    # builders (used by the backends)
    # ------------------------------------------

    @staticmethod
    def degraded(language: str, backend: str, message: str) -> "CodeStructure":
        """Minimal degraded result when the main backend crashed — never raises."""
        return CodeStructure(
            language=language,
            backend=backend,
            status=ParseStatus.DEGRADED.value,
            diagnostics=[
                CodeDiagnostic(
                    code="parse_failed",
                    severity="error",
                    message=message,
                )
            ],
        )

    @staticmethod
    def empty(language: str, backend: str) -> "CodeStructure":
        """Build an EMPTY result for empty source."""
        return CodeStructure(
            language=language,
            backend=backend,
            status=ParseStatus.EMPTY.value,
        )
