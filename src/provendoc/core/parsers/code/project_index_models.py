# ============================================================
# Code Domain — Project Index Models
# ============================================================
"""Data models and snapshot serialization for the project index.

Holds every fact carrier of the read-only index: ``ProjectFile`` / ``FileEdge`` (P2
file layer), ``ProjectSymbol`` / ``SymbolEdge`` (P5 symbol layer), the ``ProjectIndex``
read-only snapshot with its query methods, plus the ``FileParseSnapshot`` /
``ProjectIndexSnapshot`` persistence structures needed by the P6 incremental cache.
Relation building lives in ``project_index_graph`` and ``project_indexer``.

Serialization rule: cache content is untrusted-boundary JSON; every field is
validated on restore, and invalid content fails the whole cache (``ValueError``) — no guessed repairs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from provendoc.core.parsers.code.contracts import (
    CodeDiagnostic,
    CodeStructure,
    CommentUnit,
    SourceSpan,
    StructuralUnit,
    SymbolUnit,
)

# The incremental cache stores per-file parse snapshots only; project edges are rebuilt from snapshots every time.
_INDEX_CACHE_SCHEMA_VERSION = 1
_DEFAULT_INDEX_CACHE_RELATIVE_PATH = ".cache/provendoc/project-index-v1.json"
_MAX_IMPACTED_FILES = 10_000

# Metadata export truncation limit (full counts stay in stats)
_MAX_EDGES_EXPORT = 2_000
_MAX_UNRESOLVED_EXPORT = 1_000
_MAX_SYMBOLS_EXPORT = 5_000
_MAX_SYMBOL_EDGES_EXPORT = 3_000
_MAX_UNRESOLVED_SYMBOLS_EXPORT = 2_000


@dataclass
class ProjectFile:
    """One source file in the index."""

    path: str
    language: str
    module: str
    symbols: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    status: str = "ok"
    content_sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        data = {
            "path": self.path,
            "language": self.language,
            "module": self.module,
            "symbols": self.symbols,
            "imports": self.imports,
            "status": self.status,
        }
        if self.content_sha256:
            data["content_sha256"] = self.content_sha256
        return data


@dataclass
class FileEdge:
    """File-level reference edge (resolved to a project file)."""

    source: str
    target: str
    raw_import: str
    line_start: int
    line_end: int
    kind: str = "import"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "source": self.source,
            "target": self.target,
            "raw_import": self.raw_import,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "kind": self.kind,
        }


@dataclass
class ProjectSymbol:
    """A named symbol defined by one AST node in the project.

    ``exported_as`` is the public name importers can use. Python top-level
    definitions are importable under their own names; JS/TS appear here only with an explicit export.
    """

    id: str
    file: str
    name: str
    kind: str
    span: SourceSpan
    parent: Optional[str] = None
    exported_as: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "id": self.id,
            "file": self.file,
            "name": self.name,
            "kind": self.kind,
            "parent": self.parent,
            "exported_as": sorted(self.exported_as),
            **self.span.to_dict(),
        }


@dataclass
class SymbolEdge:
    """A cross-file symbol relation confirmed by both an import binding and the target definition."""

    source_file: str
    target_file: str
    kind: str
    source_symbol: Optional[str]
    target_symbol: str
    target_name: str
    import_local: str
    imported_name: str
    raw_import: str
    span: SourceSpan
    resolution: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "source_file": self.source_file,
            "target_file": self.target_file,
            "kind": self.kind,
            "source_symbol": self.source_symbol,
            "target_symbol": self.target_symbol,
            "target_name": self.target_name,
            "import_local": self.import_local,
            "imported_name": self.imported_name,
            "raw_import": self.raw_import,
            "resolution": self.resolution,
            **self.span.to_dict(),
        }


@dataclass
class FileParseSnapshot:
    """A persistable single-file parse snapshot.

    Only the source file's language, content fingerprint and ``CodeStructure`` are cached;
    file/symbol edges depend on the batch's file set and are never cached, so additions, deletions and export changes leave no stale relations.
    """

    path: str
    language: str
    content_sha256: str
    structure: CodeStructure

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "path": self.path,
            "language": self.language,
            "content_sha256": self.content_sha256,
            "structure": _code_structure_to_dict(self.structure),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileParseSnapshot":
        """Restore a snapshot from a dict; raises ValueError on invalid identity or structure."""
        path = data.get("path")
        language = data.get("language")
        content_sha256 = data.get("content_sha256")
        structure = data.get("structure")
        if (
            not isinstance(path, str)
            or not isinstance(language, str)
            or not isinstance(content_sha256, str)
        ):
            raise ValueError("Invalid file parse snapshot identity")
        if not isinstance(structure, dict):
            raise ValueError("Invalid file parse snapshot structure")
        return cls(
            path=path,
            language=language,
            content_sha256=content_sha256,
            structure=_code_structure_from_dict(structure),
        )


@dataclass
class ProjectIndexSnapshot:
    """A JSON snapshot of the project incremental index.

    ``parser_fingerprint`` binds the cache to the current parser implementation; on
    mismatch the cache is still usable for change comparison but its parse structures are never reused.
    """

    root: str
    parser_fingerprint: str
    files: Dict[str, FileParseSnapshot] = field(default_factory=dict)
    schema_version: int = _INDEX_CACHE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict (files sorted by path)."""
        return {
            "schema_version": self.schema_version,
            "root": self.root,
            "parser_fingerprint": self.parser_fingerprint,
            "files": [self.files[path].to_dict() for path in sorted(self.files)],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectIndexSnapshot":
        """Restore the cache from a dict; raises ValueError on invalid schema or entries."""
        if data.get("schema_version") != _INDEX_CACHE_SCHEMA_VERSION:
            raise ValueError("Unsupported project index cache schema")
        root = data.get("root")
        parser_fingerprint = data.get("parser_fingerprint")
        items = data.get("files")
        if not isinstance(root, str) or not isinstance(parser_fingerprint, str):
            raise ValueError("Invalid project index cache identity")
        if not isinstance(items, list):
            raise ValueError("Invalid project index cache files")
        files: Dict[str, FileParseSnapshot] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid project index cache file entry")
            snapshot = FileParseSnapshot.from_dict(item)
            if not snapshot.path or snapshot.path in files:
                raise ValueError("Duplicate or empty project index cache path")
            files[snapshot.path] = snapshot
        return cls(root=root, parser_fingerprint=parser_fingerprint, files=files)

    @classmethod
    def load(cls, path: Path) -> Optional["ProjectIndexSnapshot"]:
        """Read an untrusted-boundary JSON cache; a corrupt cache counts as a miss."""
        try:
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return cls.from_dict(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def save(self, path: Path) -> bool:
        """Atomically write the cache; write failures never affect the finished index result."""
        cache_path = Path(path)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
            return True
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def matches(self, root: Path, parser_fingerprint: str) -> bool:
        """Whether the cache matches the given project root and parser fingerprint."""
        return (
            self.root == _root_identity(root)
            and self.parser_fingerprint == parser_fingerprint
        )


@dataclass
class ImpactAnalysis:
    """Change-impact scope derived from confirmed reverse edges."""

    changed_files: List[str]
    impacted_files: List[str]
    reasons: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    truncated: bool = False
    max_files: int = _MAX_IMPACTED_FILES

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "changed_files": self.changed_files,
            "impacted_files": self.impacted_files,
            "reasons": self.reasons,
            "truncated": self.truncated,
            "max_files": self.max_files,
        }


@dataclass
class IncrementalProjectIndexResult:
    """Graph, snapshots and change facts of one incremental indexing run."""

    index: "ProjectIndex"
    snapshot: ProjectIndexSnapshot
    cache_status: str
    cache_path: Optional[str]
    reused_files: List[str] = field(default_factory=list)
    reparsed_files: List[str] = field(default_factory=list)
    added_files: List[str] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    cache_written: bool = False
    impact: Optional[ImpactAnalysis] = None

    def to_metadata(self) -> Dict[str, Any]:
        """Export as a metadata dict (cache state, impact analysis, index stats)."""
        return {
            "cache": {
                "status": self.cache_status,
                "path": self.cache_path,
                "reused_files": self.reused_files,
                "reparsed_files": self.reparsed_files,
                "added_files": self.added_files,
                "changed_files": self.changed_files,
                "deleted_files": self.deleted_files,
                "written": self.cache_written,
            },
            "impact": self.impact.to_dict() if self.impact else None,
            "index": self.index.to_metadata(),
        }


@dataclass
class ProjectIndex:
    """Read-only project-index snapshot with P2 file edges and P5 symbol edges."""

    root: str
    files: List[ProjectFile] = field(default_factory=list)
    edges: List[FileEdge] = field(default_factory=list)
    unresolved: List[Dict[str, Any]] = field(default_factory=list)
    symbols: List[ProjectSymbol] = field(default_factory=list)
    symbol_edges: List[SymbolEdge] = field(default_factory=list)
    unresolved_symbols: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def file_paths(self) -> Set[str]:
        """The set of all indexed file paths."""
        return {file.path for file in self.files}

    def incoming(self, path: str) -> List[FileEdge]:
        """Reference edges pointing at this file."""
        return [edge for edge in self.edges if edge.target == path]

    def outgoing(self, path: str) -> List[FileEdge]:
        """Reference edges emitted by this file."""
        return [edge for edge in self.edges if edge.source == path]

    def symbol_incoming(self, path: str, name: Optional[str] = None) -> List[SymbolEdge]:
        """Project symbol edges referencing ``path`` (optionally restricted to ``name``)."""
        return [
            edge
            for edge in self.symbol_edges
            if edge.target_file == path and (name is None or edge.target_name == name)
        ]

    def symbol_outgoing(self, path: str) -> List[SymbolEdge]:
        """Resolved project symbol edges emitted by ``path``."""
        return [edge for edge in self.symbol_edges if edge.source_file == path]

    def callers(self, path: str, name: str) -> List[SymbolEdge]:
        """Resolved project-internal calls to the given function/method symbol."""
        return [
            edge
            for edge in self.symbol_incoming(path, name)
            if edge.kind == "call"
        ]

    def callees(self, path: str) -> List[SymbolEdge]:
        """Resolved in-file calls that target project-internal definitions."""
        return [edge for edge in self.symbol_outgoing(path) if edge.kind == "call"]

    def parents(self, path: str, name: str) -> List[SymbolEdge]:
        """extends/implements/mixin relations of a class/interface to project-internal parent types."""
        return [
            edge
            for edge in self.symbol_outgoing(path)
            if edge.kind in {"extends", "implements", "mixin"}
            and edge.source_symbol is not None
            and edge.source_symbol.startswith(f"{path}:{name}:")
        ]

    def children(self, path: str, name: str) -> List[SymbolEdge]:
        """Relations inheriting from / implementing the given project-internal type."""
        return [
            edge
            for edge in self.symbol_incoming(path, name)
            if edge.kind in {"extends", "implements", "mixin"}
        ]

    def impact(
        self,
        changed_paths: Iterable[str],
        previous: Optional["ProjectIndex"] = None,
        max_files: int = _MAX_IMPACTED_FILES,
    ) -> ImpactAnalysis:
        """Analyze the bounded reverse impact of a change on importers.

        ``previous`` may supply reverse edges for deleted files or old relations. Results
        propagate only along P2 file-import edges and resolved P5 ``SymbolEdge``s; unresolved
        or dynamic relations produce no speculative impact. Every affected file keeps a per-edge auditable reason.
        """
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        raw_paths = [changed_paths] if isinstance(changed_paths, str) else changed_paths
        changed = sorted(
            {
                _normalize_index_path(path, self.root)
                for path in raw_paths
                if isinstance(path, str) and path.strip()
            }
        )
        reasons: Dict[str, List[Dict[str, Any]]] = {
            path: [{"kind": "changed"}] for path in changed
        }
        impacted = list(changed[:max_files])
        truncated = len(changed) > max_files
        queued: Set[str] = set(impacted)
        queue: List[Tuple[str, int]] = [(path, 0) for path in impacted]

        indexes = [self]
        if previous is not None:
            indexes.append(previous)
        file_reverse: Dict[str, List[FileEdge]] = {}
        symbol_reverse: Dict[str, List[SymbolEdge]] = {}
        for candidate in indexes:
            for file_edge in candidate.edges:
                file_reverse.setdefault(file_edge.target, []).append(file_edge)
            for symbol_edge in candidate.symbol_edges:
                symbol_reverse.setdefault(symbol_edge.target_file, []).append(symbol_edge)

        def append_reason(path: str, reason: Dict[str, Any]) -> None:
            """Append one impact reason for a path (deduplicated)."""
            entries = reasons.setdefault(path, [])
            if reason not in entries:
                entries.append(reason)

        def add_dependent(path: str, reason: Dict[str, Any], depth: int) -> None:
            """Register an affected file and enqueue it; sets truncated when over the limit."""
            nonlocal truncated
            append_reason(path, reason)
            if path in queued:
                return
            if len(impacted) >= max_files:
                truncated = True
                return
            queued.add(path)
            impacted.append(path)
            queue.append((path, depth))

        cursor = 0
        while cursor < len(queue):
            target, depth = queue[cursor]
            cursor += 1
            for file_edge in file_reverse.get(target, []):
                add_dependent(
                    file_edge.source,
                    {
                        "kind": "file_import",
                        "target_file": file_edge.target,
                        "raw_import": file_edge.raw_import,
                        "line_start": file_edge.line_start,
                        "line_end": file_edge.line_end,
                        "relation_kind": file_edge.kind,
                        "depth": depth + 1,
                    },
                    depth + 1,
                )
            for symbol_edge in symbol_reverse.get(target, []):
                add_dependent(
                    symbol_edge.source_file,
                    {
                        "kind": "symbol_reference",
                        "target_file": symbol_edge.target_file,
                        "target_symbol": symbol_edge.target_symbol,
                        "target_name": symbol_edge.target_name,
                        "source_symbol": symbol_edge.source_symbol,
                        "relation_kind": symbol_edge.kind,
                        "resolution": symbol_edge.resolution,
                        "line_start": symbol_edge.span.line_start,
                        "line_end": symbol_edge.span.line_end,
                        "depth": depth + 1,
                    },
                    depth + 1,
                )

        return ImpactAnalysis(
            changed_files=changed,
            impacted_files=impacted,
            reasons={path: reasons[path] for path in impacted},
            truncated=truncated,
            max_files=max_files,
        )

    def analyze_impact(
        self,
        changed_paths: Iterable[str],
        previous: Optional["ProjectIndex"] = None,
        max_files: int = _MAX_IMPACTED_FILES,
    ) -> ImpactAnalysis:
        """Full-semantic alias of ``impact``, for intent-based lookup."""
        return self.impact(changed_paths, previous=previous, max_files=max_files)

    def to_metadata(self) -> Dict[str, Any]:
        """Export a JSON-compatible snapshot; lists are capped."""
        return {
            "root": self.root,
            "files": [file.to_dict() for file in self.files],
            "file_edges": [edge.to_dict() for edge in self.edges[:_MAX_EDGES_EXPORT]],
            "unresolved": self.unresolved[:_MAX_UNRESOLVED_EXPORT],
            "symbols": [symbol.to_dict() for symbol in self.symbols[:_MAX_SYMBOLS_EXPORT]],
            "symbol_edges": [
                edge.to_dict() for edge in self.symbol_edges[:_MAX_SYMBOL_EDGES_EXPORT]
            ],
            "unresolved_symbols": self.unresolved_symbols[
                :_MAX_UNRESOLVED_SYMBOLS_EXPORT
            ],
            "stats": self.stats,
        }


# ==================================
# P6 snapshot serialization helpers
# ==================================


def _content_sha256(raw: bytes) -> str:
    """Return a stable content fingerprint of the source file's raw bytes, independent of mtime or file size."""
    return hashlib.sha256(raw).hexdigest()


def _root_identity(root: Path) -> str:
    """Normalize the cache's project root to an absolute, resolved path."""
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(Path(root).absolute())


def _normalize_index_path(source_path: str, root: str) -> str:
    """Normalize a caller-supplied source path, relative or under the root."""
    path = Path(source_path)
    root_path = Path(root)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root_path.resolve()).as_posix()
        except (OSError, ValueError):
            return path.as_posix()
    return source_path.replace("\\", "/").lstrip("./")


def _source_span_from_dict(data: Dict[str, Any]) -> SourceSpan:
    fields = (
        "line_start",
        "line_end",
        "col_start",
        "col_end",
        "byte_start",
        "byte_end",
    )
    values: Dict[str, int] = {}
    for name in fields:
        value = data.get(name, 0)
        if not isinstance(value, int):
            raise ValueError(f"Invalid source span field: {name}")
        values[name] = value
    return SourceSpan(**values)


def _code_structure_to_dict(structure: CodeStructure) -> Dict[str, Any]:
    """Fully serialize a ``CodeStructure`` for trust-independent JSON caching."""
    return {
        "language": structure.language,
        "backend": structure.backend,
        "status": structure.status,
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": diagnostic.message,
                "severity": diagnostic.severity,
                "span": diagnostic.span.to_dict() if diagnostic.span else None,
                "snippet": diagnostic.snippet,
            }
            for diagnostic in structure.diagnostics
        ],
        "comments": [
            {
                "kind": comment.kind,
                "text": comment.text,
                "span": comment.span.to_dict(),
            }
            for comment in structure.comments
        ],
        "symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "span": symbol.span.to_dict(),
                "parent": symbol.parent,
            }
            for symbol in structure.symbols
        ],
        "structural_units": [
            {
                "kind": unit.kind,
                "name": unit.name,
                "span": unit.span.to_dict(),
                "detail": unit.detail,
            }
            for unit in structure.structural_units
        ],
        "stats": structure.stats,
    }


def _code_structure_from_dict(data: Dict[str, Any]) -> CodeStructure:
    """Validate and restore a cached ``CodeStructure``; invalid content fails the whole cache."""
    language = data.get("language")
    backend = data.get("backend")
    status = data.get("status")
    diagnostics_data = data.get("diagnostics", [])
    comments_data = data.get("comments", [])
    symbols_data = data.get("symbols", [])
    units_data = data.get("structural_units", [])
    stats = data.get("stats", {})
    if (
        not isinstance(language, str)
        or not isinstance(backend, str)
        or not isinstance(status, str)
    ):
        raise ValueError("Invalid cached code structure identity")
    if not all(
        isinstance(value, list)
        for value in (diagnostics_data, comments_data, symbols_data, units_data)
    ) or not isinstance(stats, dict):
        raise ValueError("Invalid cached code structure collections")

    diagnostics: List[CodeDiagnostic] = []
    for item in diagnostics_data:
        if not isinstance(item, dict):
            raise ValueError("Invalid cached diagnostic")
        code = item.get("code")
        message = item.get("message")
        severity = item.get("severity", "error")
        snippet = item.get("snippet", "")
        span_data = item.get("span")
        if (
            not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(severity, str)
            or not isinstance(snippet, str)
        ):
            raise ValueError("Invalid cached diagnostic fields")
        if span_data is not None and not isinstance(span_data, dict):
            raise ValueError("Invalid cached diagnostic span")
        diagnostics.append(
            CodeDiagnostic(
                code=code,
                message=message,
                severity=severity,
                span=_source_span_from_dict(span_data) if span_data else None,
                snippet=snippet,
            )
        )

    comments: List[CommentUnit] = []
    for item in comments_data:
        if not isinstance(item, dict):
            raise ValueError("Invalid cached comment")
        kind = item.get("kind")
        text = item.get("text")
        span_data = item.get("span")
        if not isinstance(kind, str) or not isinstance(text, str) or not isinstance(span_data, dict):
            raise ValueError("Invalid cached comment fields")
        comments.append(CommentUnit(kind=kind, text=text, span=_source_span_from_dict(span_data)))

    symbols: List[SymbolUnit] = []
    for item in symbols_data:
        if not isinstance(item, dict):
            raise ValueError("Invalid cached symbol")
        name = item.get("name")
        kind = item.get("kind")
        parent = item.get("parent")
        span_data = item.get("span")
        if (
            not isinstance(name, str)
            or not isinstance(kind, str)
            or (parent is not None and not isinstance(parent, str))
            or not isinstance(span_data, dict)
        ):
            raise ValueError("Invalid cached symbol fields")
        symbols.append(
            SymbolUnit(
                name=name,
                kind=kind,
                parent=parent,
                span=_source_span_from_dict(span_data),
            )
        )

    structural_units: List[StructuralUnit] = []
    for item in units_data:
        if not isinstance(item, dict):
            raise ValueError("Invalid cached structural unit")
        kind = item.get("kind")
        name = item.get("name")
        detail = item.get("detail")
        span_data = item.get("span")
        if (
            not isinstance(kind, str)
            or not isinstance(name, str)
            or (detail is not None and not isinstance(detail, dict))
            or not isinstance(span_data, dict)
        ):
            raise ValueError("Invalid cached structural unit fields")
        structural_units.append(
            StructuralUnit(
                kind=kind,
                name=name,
                detail=detail,
                span=_source_span_from_dict(span_data),
            )
        )

    return CodeStructure(
        language=language,
        backend=backend,
        status=status,
        diagnostics=diagnostics,
        comments=comments,
        symbols=symbols,
        structural_units=structural_units,
        stats=stats,
    )
