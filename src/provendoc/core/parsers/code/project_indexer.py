# ============================================================
# Code Domain
# Project-level read-only index and evidence-driven symbol graph
# ============================================================
"""Project-level read-only index — entry orchestration, traversal, tolerant parsing and incremental reuse.

Two layers: P2 resolves parser-extracted imports into in-project file edges;
P5 builds ``SymbolEdge``s only when import bindings, exports/definitions and
call facts correspond unambiguously. Dynamic dispatch, star imports, out-of-package
targets and ambiguous definitions stay as unresolved records with reasons — no
guessing. Relation building lives in ``project_index_graph``, language-level
resolution in ``project_index_resolution``, models in ``project_index_models``.

P5 symbol-level resolution currently covers Python and JavaScript/TypeScript: Python
top-level definitions are referenced by explicit ``import`` / ``from ... import ...``;
JS/TS resolves only AST-confirmed explicit ``export`` declarations. Other languages
keep their P2 file edges and are never dressed up as resolved symbol relations.

P6 adds optional ``index_incremental()`` while keeping ``index()`` purely read-only:
it reuses unchanged files' AST snapshots via source SHA-256 fingerprints but rebuilds
the project graph every run, so stale cross-file relations are never reused.
``ProjectIndex.impact()`` outputs the affected scope from reverse file edges and confirmed symbol edges, without guessing at dynamic dispatch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from provendoc.core.parsers.code.contracts import CodeStructure
from provendoc.core.parsers.code.generic_backend import TreeSitterCodeBackend
from provendoc.core.parsers.code.language_map import language_for_suffix
from provendoc.core.parsers.code.project_index_graph import (
    build_file_edges,
    build_symbol_graph,
    finalize_stats,
)
from provendoc.core.parsers.code.project_index_models import (
    _DEFAULT_INDEX_CACHE_RELATIVE_PATH,
    _INDEX_CACHE_SCHEMA_VERSION,
    _MAX_IMPACTED_FILES,
    FileParseSnapshot,
    IncrementalProjectIndexResult,
    ProjectFile,
    ProjectIndex,
    ProjectIndexSnapshot,
    _content_sha256,
    _normalize_index_path,
    _root_identity,
)
from provendoc.core.parsers.code.project_index_resolution import _module_name
from provendoc.core.text import safe_decode

# Traversal defense limits
_MAX_FILES = 10_000
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
    ".cache", ".provendoc", "dist", "build", "target", ".idea", "vendor",
}


class ProjectIndexer:
    """Scan a project directory and build the read-only file graph and cross-file symbol graph.

    ``index`` never rewrites source, documents or chunks. It is re-runnable on top of
    existing per-file ``CodeStructure``s; every relation carries the raw import and
    source location for retrieval, reporting and change-impact analysis.
    """

    def __init__(self, max_files: int = _MAX_FILES):
        """Initialize with a file-count limit and assemble the tree-sitter backends."""
        self.max_files = max_files
        self._backend = TreeSitterCodeBackend()

    # ==================================
    # public API
    # ==================================

    def index(self, root: Path) -> ProjectIndex:
        """Build the full read-only project graph; the index cache is neither read nor written."""
        root = self._validate_root(root)
        sources = self._collect_sources(root)
        structures = {
            rel_path: self._parse_file(root / rel_path, language)
            for rel_path, language in sources.items()
        }
        return self._assemble_index(root, sources, structures)

    def index_incremental(
        self,
        root: Path,
        previous: Optional[ProjectIndexSnapshot] = None,
        cache_path: Optional[Path] = None,
        persist: bool = True,
        impact_max_files: int = _MAX_IMPACTED_FILES,
    ) -> IncrementalProjectIndexResult:
        """Reuse per-file parse snapshots by source content hash and rebuild the project relation graph.

        ``previous`` may pass in a caller-held snapshot; otherwise ``cache_path`` or the
        in-project default cache is read. Cache hit or not, file and symbol edges are
        re-aggregated. ``persist=False`` runs tests / memory-only and writes no cache file.
        """
        root = self._validate_root(root)
        if impact_max_files <= 0:
            raise ValueError("impact_max_files must be positive")
        if previous is not None and not isinstance(previous, ProjectIndexSnapshot):
            raise TypeError("previous must be a ProjectIndexSnapshot or None")

        parser_fingerprint = self._parser_fingerprint()
        resolved_cache_path = self._cache_path(root, cache_path)
        loaded_from_disk = False
        if previous is None:
            previous = ProjectIndexSnapshot.load(resolved_cache_path)
            loaded_from_disk = previous is not None

        compatible = (
            previous is not None and previous.matches(root, parser_fingerprint)
        )
        comparable = previous is not None and previous.root == _root_identity(root)
        if previous is None:
            cache_status = "miss"
        elif compatible:
            cache_status = "hit"
        elif comparable:
            cache_status = "stale_parser"
        else:
            cache_status = "incompatible"
        supplied_previous = previous is not None and not loaded_from_disk

        sources = self._collect_sources(root)
        structures: Dict[str, CodeStructure] = {}
        fingerprints: Dict[str, str] = {}
        snapshots: Dict[str, FileParseSnapshot] = {}
        reused_files: List[str] = []
        reparsed_files: List[str] = []
        added_files: List[str] = []
        changed_files: List[str] = []

        for rel_path, language in sources.items():
            raw: Optional[bytes]
            try:
                raw = (root / rel_path).read_bytes()
            except OSError:
                raw = None
            content_sha256 = _content_sha256(raw) if raw is not None else ""
            old = previous.files.get(rel_path) if comparable and previous else None
            unchanged = (
                old is not None
                and bool(content_sha256)
                and old.language == language
                and old.content_sha256 == content_sha256
            )
            if compatible and old is not None and unchanged:
                structure = old.structure
                reused_files.append(rel_path)
            else:
                structure = self._parse_bytes(raw, root / rel_path, language)
                reparsed_files.append(rel_path)
                if old is None:
                    added_files.append(rel_path)
                elif not unchanged:
                    changed_files.append(rel_path)
            structures[rel_path] = structure
            fingerprints[rel_path] = content_sha256
            snapshots[rel_path] = FileParseSnapshot(
                path=rel_path,
                language=language,
                content_sha256=content_sha256,
                structure=structure,
            )

        prior_paths = set(previous.files) if comparable and previous else set()
        deleted_files = sorted(prior_paths - set(sources))
        index = self._assemble_index(root, sources, structures, fingerprints)
        snapshot = ProjectIndexSnapshot(
            root=_root_identity(root),
            parser_fingerprint=parser_fingerprint,
            files=snapshots,
        )

        previous_index: Optional[ProjectIndex] = None
        if comparable and previous is not None:
            previous_sources = {
                path: item.language for path, item in previous.files.items()
            }
            previous_structures = {
                path: item.structure for path, item in previous.files.items()
            }
            previous_fingerprints = {
                path: item.content_sha256 for path, item in previous.files.items()
            }
            previous_index = self._assemble_index(
                root,
                previous_sources,
                previous_structures,
                previous_fingerprints,
            )
        impact = index.impact(
            [*added_files, *changed_files, *deleted_files],
            previous=previous_index,
            max_files=impact_max_files,
        )
        cache_written = snapshot.save(resolved_cache_path) if persist else False
        return IncrementalProjectIndexResult(
            index=index,
            snapshot=snapshot,
            cache_status="provided" if supplied_previous else cache_status,
            cache_path=str(resolved_cache_path) if persist else None,
            reused_files=sorted(reused_files),
            reparsed_files=sorted(reparsed_files),
            added_files=sorted(added_files),
            changed_files=sorted(changed_files),
            deleted_files=deleted_files,
            cache_written=cache_written,
            impact=impact,
        )

    @staticmethod
    def annotate_chunks(
        index: ProjectIndex,
        source_path: str,
        chunks: Iterable[Any],
    ) -> int:
        """Attach read-only project evidence to chunks intersecting import/symbol spans.

        ``project_references`` extends the P2 file-level import evidence;
        ``project_symbol_references`` is written only when P5 resolved a real definition.
        The call never changes chunk text, count or order; re-running is idempotent.
        """
        normalized = _normalize_index_path(source_path, index.root)
        file_edges = index.outgoing(normalized)
        symbol_edges = index.symbol_outgoing(normalized)
        if not file_edges and not symbol_edges:
            return 0

        annotated = 0
        for chunk in chunks:
            metadata = getattr(chunk, "metadata", None)
            if not isinstance(metadata, dict):
                continue
            line_start = metadata.get("line_start")
            line_end = metadata.get("line_end")
            if not isinstance(line_start, int) or not isinstance(line_end, int):
                continue

            file_evidence = [
                {
                    "target_file": edge.target,
                    "kind": edge.kind,
                    "raw_import": edge.raw_import,
                    "line_start": edge.line_start,
                    "line_end": edge.line_end,
                }
                for edge in file_edges
                if _lines_overlap(line_start, line_end, edge.line_start, edge.line_end)
            ]
            symbol_evidence = [
                {
                    "target_file": edge.target_file,
                    "target_symbol": edge.target_symbol,
                    "target_name": edge.target_name,
                    "kind": edge.kind,
                    "resolution": edge.resolution,
                    "line_start": edge.span.line_start,
                    "line_end": edge.span.line_end,
                }
                for edge in symbol_edges
                if _lines_overlap(
                    line_start,
                    line_end,
                    edge.span.line_start,
                    edge.span.line_end,
                )
            ]
            if file_evidence:
                metadata["project_references"] = file_evidence
            else:
                metadata.pop("project_references", None)
            if symbol_evidence:
                metadata["project_symbol_references"] = symbol_evidence
            else:
                metadata.pop("project_symbol_references", None)
            # Keep the P2 return contract: count only chunks with import file edges. Symbol
            # relations may land in later call chunks without changing existing count semantics.
            if file_evidence:
                annotated += 1
        return annotated

    # ==================================
    # traversal and tolerant parsing
    # ==================================

    def _collect_sources(self, root: Path) -> Dict[str, str]:
        """Collect indexable source files: rel_path → language."""
        sources: Dict[str, str] = {}
        stack: List[Path] = [root]
        while stack and len(sources) < self.max_files:
            current = stack.pop()
            if current.is_dir():
                if current != root and current.name in _SKIP_DIRS:
                    continue
                stack.extend(sorted(current.iterdir(), reverse=True))
                continue
            language = language_for_suffix(current.suffix.lower())
            if language is None:
                continue
            try:
                sources[current.relative_to(root).as_posix()] = language
            except ValueError:
                continue
        return sources

    @staticmethod
    def _validate_root(root: Path) -> Path:
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"Project root is not a directory: {root}")
        return root

    def _cache_path(self, root: Path, cache_path: Optional[Path]) -> Path:
        path = Path(cache_path) if cache_path is not None else Path(_DEFAULT_INDEX_CACHE_RELATIVE_PATH)
        return path if path.is_absolute() else root / path

    def _parser_fingerprint(self) -> str:
        """Fingerprint the parser implementation so stale structural snapshots are never reused.

        The fingerprint covers only what affects ``CodeStructure`` snapshot content (backend,
        contracts, language map); file/symbol edges are always rebuilt from snapshots, so
        parser changes leave no stale relations.
        """
        digest = hashlib.sha256()
        digest.update(f"project-index-cache:{_INDEX_CACHE_SCHEMA_VERSION}".encode("utf-8"))
        digest.update(self._backend.backend_name.encode("utf-8"))
        for source_path in (
            Path(__file__),
            Path(__file__).with_name("generic_backend.py"),
            Path(__file__).with_name("contracts.py"),
            Path(__file__).with_name("language_map.py"),
        ):
            try:
                digest.update(source_path.read_bytes())
            except OSError:
                digest.update(str(source_path).encode("utf-8"))
        return digest.hexdigest()

    def _parse_file(self, path: Path, language: str) -> CodeStructure:
        """Tolerantly parse one file; a parse failure never blocks the whole project index."""
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        return self._parse_bytes(raw, path, language)

    def _parse_bytes(
        self,
        raw: Optional[bytes],
        path: Path,
        language: str,
    ) -> CodeStructure:
        """Tolerantly parse already-read raw bytes, shared by the full and incremental entries."""
        backend = self._backend.backend_name
        if raw is None:
            return CodeStructure.degraded(language, backend, f"unreadable: {path}")
        text = safe_decode(raw).text
        if not text:
            return CodeStructure.empty(language, backend)
        try:
            return self._backend.parse(text, language).structure
        except Exception as exc:  # backend crashes must degrade, never block the whole project index.
            return CodeStructure.degraded(language, backend, f"parse_failed: {exc}")

    def _assemble_index(
        self,
        root: Path,
        sources: Dict[str, str],
        structures: Dict[str, CodeStructure],
        content_sha256: Optional[Dict[str, str]] = None,
    ) -> ProjectIndex:
        """Rebuild all project-level relations from per-file structure snapshots; old edges are never reused."""
        index = ProjectIndex(root=str(root))
        for rel_path, language in sources.items():
            structure = structures[rel_path]
            index.files.append(
                ProjectFile(
                    path=rel_path,
                    language=language,
                    module=_module_name(rel_path, language),
                    symbols=[symbol.name for symbol in structure.symbols],
                    imports=[
                        unit.name
                        for unit in structure.structural_units
                        if unit.kind == "import"
                    ],
                    status=structure.status,
                    content_sha256=(content_sha256 or {}).get(rel_path, ""),
                )
            )
        build_file_edges(index, structures)
        build_symbol_graph(index, structures)
        finalize_stats(index, len(sources), self.max_files)
        return index


def _lines_overlap(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    return first_start <= second_end and second_start <= first_end
