# ============================================================
# Code Domain — Project Index Graph Building
# ============================================================
"""Deterministic construction of P2 file edges and the P5 symbol graph (pure functions, rebuilt from single-file structure snapshots).

``build_file_edges`` hands each import unit to the language resolver (see
``project_index_resolution.resolve_import``); a hit on an in-project file
becomes an edge, otherwise the raw text stays as an unresolved record.

``build_symbol_graph`` builds the export/definition index first, then resolves
single-file relations targeted by explicit import bindings: a ``SymbolEdge`` is
created only when import binding, export/definition and call facts correspond
unambiguously; dynamic dispatch, star imports, out-of-package targets and
ambiguous definitions stay as unresolved records with reasons — no guessing.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

from provendoc.core.parsers.code.contracts import (
    CodeStructure,
    StructuralUnit,
)
from provendoc.core.parsers.code.project_index_models import (
    FileEdge,
    ProjectFile,
    ProjectIndex,
    ProjectSymbol,
    SymbolEdge,
)
from provendoc.core.parsers.code.project_index_resolution import (
    _IDENTIFIER_RE,
    _SUPPORTED_SYMBOL_LANGUAGES,
    _SYMBOL_RELATION_KINDS,
    _binding_for_target,
    _enclosing_symbol_id,
    _ImportBinding,
    _js_ts_bindings,
    _js_ts_export_pairs,
    _python_bindings,
    _symbol_id,
    resolve_import,
)


def build_file_edges(
    index: ProjectIndex,
    structures: Dict[str, CodeStructure],
) -> None:
    """Build file-level reference edges from each file's CodeStructure import units (written in place into index)."""
    files_by_module = {file.module: file.path for file in index.files}
    paths = index.file_paths()
    for file in index.files:
        structure = structures.get(file.path)
        if structure is None:
            continue
        seen_targets: Set[str] = set()
        for unit in structure.structural_units:
            if unit.kind != "import":
                continue
            edges, leftovers = resolve_import(
                unit,
                file.language,
                PurePosixPath(file.path).parent,
                paths,
                files_by_module,
            )
            for kind, target in edges:
                if target == file.path or target in seen_targets:
                    continue
                seen_targets.add(target)
                index.edges.append(
                    FileEdge(
                        source=file.path,
                        target=target,
                        raw_import=unit.name,
                        line_start=unit.span.line_start,
                        line_end=unit.span.line_end,
                        kind=kind,
                    )
                )
            for leftover in leftovers:
                index.unresolved.append(
                    {
                        "source": file.path,
                        "raw_import": leftover,
                        "language": file.language,
                    }
                )


def build_symbol_graph(
    index: ProjectIndex,
    structures: Dict[str, CodeStructure],
) -> None:
    """Build the export/definition index, then resolve single-file relations targeted by explicit import bindings."""
    paths = index.file_paths()
    files_by_path = {file.path: file for file in index.files}
    files_by_module = {file.module: file.path for file in index.files}
    symbols_by_file = _collect_project_symbols(index, structures)
    export_index = _collect_exports(index, structures, symbols_by_file)
    bindings = _collect_import_bindings(
        index,
        structures,
        paths,
        files_by_module,
        files_by_path,
    )
    for file in index.files:
        if file.language not in _SUPPORTED_SYMBOL_LANGUAGES:
            continue
        structure = structures.get(file.path)
        if structure is None:
            continue
        file_bindings = bindings.get(file.path, {})
        for unit in structure.structural_units:
            if unit.kind not in _SYMBOL_RELATION_KINDS:
                continue
            _resolve_symbol_unit(
                index,
                file,
                unit,
                file_bindings,
                export_index,
                symbols_by_file,
            )


def _collect_project_symbols(
    index: ProjectIndex,
    structures: Dict[str, CodeStructure],
) -> Dict[str, List[ProjectSymbol]]:
    """Convert each single-file AST symbol into a project definition with a globally stable id."""
    symbols_by_file: Dict[str, List[ProjectSymbol]] = {}
    for file in index.files:
        structure = structures.get(file.path)
        if structure is None:
            continue
        file_symbols: List[ProjectSymbol] = []
        for symbol in structure.symbols:
            if not symbol.name or symbol.name == "<anonymous>":
                continue
            project_symbol = ProjectSymbol(
                id=_symbol_id(file.path, symbol),
                file=file.path,
                name=symbol.name,
                kind=symbol.kind,
                span=symbol.span,
                parent=symbol.parent,
            )
            file_symbols.append(project_symbol)
            index.symbols.append(project_symbol)
        symbols_by_file[file.path] = file_symbols
    return symbols_by_file


def _collect_exports(
    index: ProjectIndex,
    structures: Dict[str, CodeStructure],
    symbols_by_file: Dict[str, List[ProjectSymbol]],
) -> Dict[Tuple[str, str], List[ProjectSymbol]]:
    """Build the ``(target_file, public_name) → definitions`` index.

    JS/TS uses Tree-sitter-extracted export_statement units; Python top-level
    definitions are referenceable when explicitly imported, without trying to
    interpret ``__all__``.
    """
    exports: Dict[Tuple[str, str], List[ProjectSymbol]] = {}
    for file in index.files:
        symbols = symbols_by_file.get(file.path, [])
        top_level = [symbol for symbol in symbols if symbol.parent is None]
        pairs: List[Tuple[str, str]] = []
        if file.language == "python":
            pairs = [(symbol.name, symbol.name) for symbol in top_level]
        elif file.language in {"javascript", "typescript"}:
            structure = structures.get(file.path)
            if structure is not None:
                for unit in structure.structural_units:
                    if unit.kind == "export":
                        pairs.extend(_js_ts_export_pairs(unit.name))
        for local_name, public_name in pairs:
            matches = [symbol for symbol in top_level if symbol.name == local_name]
            for symbol in matches:
                if public_name not in symbol.exported_as:
                    symbol.exported_as.append(public_name)
                exports.setdefault((file.path, public_name), []).append(symbol)
    return exports


def _collect_import_bindings(
    index: ProjectIndex,
    structures: Dict[str, CodeStructure],
    paths: Set[str],
    files_by_module: Dict[str, str],
    files_by_path: Dict[str, ProjectFile],
) -> Dict[str, Dict[str, _ImportBinding]]:
    """Build alias/namespace bindings from AST import units; imports are never guessed from text."""
    result: Dict[str, Dict[str, _ImportBinding]] = {}
    for file in index.files:
        if file.language not in _SUPPORTED_SYMBOL_LANGUAGES:
            continue
        structure = structures.get(file.path)
        if structure is None:
            continue
        file_bindings: Dict[str, _ImportBinding] = {}
        for unit in structure.structural_units:
            if unit.kind != "import":
                continue
            if file.language == "python":
                parsed = _python_bindings(
                    unit.name,
                    PurePosixPath(file.path).parent,
                    paths,
                    files_by_module,
                    files_by_path,
                )
            else:
                parsed = _js_ts_bindings(
                    unit.name,
                    PurePosixPath(file.path).parent,
                    paths,
                )
            for binding in parsed:
                # A same-name rebinding shadows the old one; the last source import is the auditable fact.
                file_bindings[binding.local_name] = binding
        result[file.path] = file_bindings
    return result


def _resolve_symbol_unit(
    index: ProjectIndex,
    file: ProjectFile,
    unit: StructuralUnit,
    bindings: Dict[str, _ImportBinding],
    export_index: Dict[Tuple[str, str], List[ProjectSymbol]],
    symbols_by_file: Dict[str, List[ProjectSymbol]],
) -> None:
    detail = dict(unit.detail or {})
    target = str(detail.get("target") or unit.name or "").strip()
    if not target:
        return
    # A bare identifier already confirmed as a local definition by the
    # single-file symbol table must not be overridden by a same-name import
    # binding — this guard keeps local calls from being wired to remote code.
    if _IDENTIFIER_RE.fullmatch(target) and not detail.get("external"):
        return
    binding, imported_name, resolution, reason = _binding_for_target(target, bindings)
    if binding is None:
        if detail.get("external") or detail.get("dynamic"):
            _append_unresolved_symbol(index, file.path, unit, target, reason)
        return

    definitions = export_index.get((binding.target_file, imported_name), [])
    if len(definitions) != 1:
        reason = "target_symbol_not_found" if not definitions else "ambiguous_target_symbol"
        _append_unresolved_symbol(
            index,
            file.path,
            unit,
            target,
            reason,
            binding=binding,
            imported_name=imported_name,
        )
        return

    target_symbol = definitions[0]
    source_symbol = _enclosing_symbol_id(
        unit.span,
        symbols_by_file.get(file.path, []),
    )
    # The span always points at the actual call/instantiation/inheritance AST
    # node, never rewritten to the enclosing function or class range; the latter
    # is already expressed by source_symbol.
    edge = SymbolEdge(
        source_file=file.path,
        target_file=binding.target_file,
        kind=unit.kind,
        source_symbol=source_symbol,
        target_symbol=target_symbol.id,
        target_name=target_symbol.name,
        import_local=binding.local_name,
        imported_name=imported_name,
        raw_import=binding.raw_import,
        span=unit.span,
        resolution=resolution,
    )
    key = (
        edge.source_file,
        edge.target_symbol,
        edge.kind,
        edge.span.byte_start,
        edge.span.byte_end,
    )
    if not any(
        (
            current.source_file,
            current.target_symbol,
            current.kind,
            current.span.byte_start,
            current.span.byte_end,
        )
        == key
        for current in index.symbol_edges
    ):
        index.symbol_edges.append(edge)


def _append_unresolved_symbol(
    index: ProjectIndex,
    source: str,
    unit: StructuralUnit,
    target: str,
    reason: str,
    binding: Optional[_ImportBinding] = None,
    imported_name: str = "",
) -> None:
    record: Dict[str, Any] = {
        "source_file": source,
        "kind": unit.kind,
        "target": target,
        "reason": reason,
        "line_start": unit.span.line_start,
        "line_end": unit.span.line_end,
    }
    if binding is not None:
        record.update(
            {
                "target_file": binding.target_file,
                "import_local": binding.local_name,
                "imported_name": imported_name,
                "raw_import": binding.raw_import,
            }
        )
    key = (
        record["source_file"],
        record["kind"],
        record["target"],
        record["reason"],
        record["line_start"],
        record["line_end"],
    )
    if not any(
        (
            current["source_file"],
            current["kind"],
            current["target"],
            current["reason"],
            current["line_start"],
            current["line_end"],
        )
        == key
        for current in index.unresolved_symbols
    ):
        index.unresolved_symbols.append(record)


def finalize_stats(index: ProjectIndex, discovered: int, max_files: int) -> None:
    """Aggregate index statistics (file/edge/symbol counts, language distribution) into index.stats."""
    languages: Dict[str, int] = {}
    for file in index.files:
        languages[file.language] = languages.get(file.language, 0) + 1
    exported_symbol_count = sum(bool(symbol.exported_as) for symbol in index.symbols)
    index.stats = {
        "file_count": len(index.files),
        "discovered_count": discovered,
        "edge_count": len(index.edges),
        "unresolved_count": len(index.unresolved),
        "symbol_count": len(index.symbols),
        "exported_symbol_count": exported_symbol_count,
        "symbol_edge_count": len(index.symbol_edges),
        "unresolved_symbol_count": len(index.unresolved_symbols),
        "languages": languages,
        "max_files": max_files,
        "truncated": discovered >= max_files,
        "symbol_resolution_languages": sorted(_SUPPORTED_SYMBOL_LANGUAGES),
    }
