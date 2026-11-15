# ============================================================
# Code Domain — Project Index Import & Binding Resolution
# ============================================================
"""Language-level import resolution and local-binding inference for P2/P5 (pure functions).

Handles only raw text of AST units the generic backend already confirmed as
import/export: Python relative/absolute module paths, JS/TS relative specifiers
and explicit export clauses, and Go import-path tails. All resolution is
deterministic — anything that does not land on an in-project file becomes an
unresolved record; no guessing. Binding inference (``_python_bindings`` /
``_js_ts_bindings``) accepts only identifiers, ``as`` aliases and explicit module
paths; dynamic dispatch and star imports are given up as-is.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from provendoc.core.parsers.code.contracts import (
    SourceSpan,
    StructuralUnit,
    SymbolUnit,
)
from provendoc.core.parsers.code.project_index_models import (
    ProjectFile,
    ProjectSymbol,
)

# Only these single-file relations may become cross-file symbol edges.
_SYMBOL_RELATION_KINDS = {"call", "reference", "extends", "implements", "mixin"}
_SUPPORTED_SYMBOL_LANGUAGES = {"python", "javascript", "typescript"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_QUALIFIED_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$"
)


@dataclass(frozen=True)
class _ImportBinding:
    """Deterministic local bindings from import syntax (internal only)."""

    local_name: str
    target_file: str
    imported_name: str
    kind: str
    raw_import: str


def resolve_import(
    unit: StructuralUnit,
    language: str,
    from_dir: PurePosixPath,
    paths: Set[str],
    files_by_module: Dict[str, str],
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Resolve an import statement into ``(edges, unresolved_specs)``."""
    raw = unit.name.strip()
    if not raw:
        return [], []
    if language == "python":
        return _resolve_python(raw, from_dir, paths, files_by_module)
    if language in {"javascript", "typescript"}:
        return _resolve_js_ts(raw, from_dir, paths)
    if language == "go":
        return _resolve_go(raw, paths)
    return [], [raw]


def _resolve_python(
    raw: str,
    from_dir: PurePosixPath,
    paths: Set[str],
    files_by_module: Dict[str, str],
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Python: deterministic file resolution of relative/absolute import paths."""
    text = _strip_semicolon(raw).strip()
    if text.startswith("from "):
        module_part = text[5:].split(" import ", 1)[0].strip()
        target = _python_module_target(
            module_part,
            from_dir,
            paths,
            files_by_module,
        )
        if target:
            kind = "relative" if module_part.startswith(".") else "import"
            return [(kind, target)], []
        return [], [_python_import_label(module_part)]
    if text.startswith("import "):
        edges: List[Tuple[str, str]] = []
        leftovers: List[str] = []
        for name in _split_import_items(text[7:]):
            module_name = name.split(" as ", 1)[0].strip()
            target = _python_absolute_module_target(module_name, files_by_module)
            if target:
                edges.append(("import", target))
            elif module_name:
                leftovers.append(module_name)
        return edges, leftovers
    return [], []


def _resolve_js_ts(
    raw: str,
    from_dir: PurePosixPath,
    paths: Set[str],
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """JS/TS: relative paths, suffix and index completion."""
    spec = _first_quoted(raw)
    if not spec or not spec.startswith("."):
        return [], [spec or raw]
    target = _js_ts_target(spec, from_dir, paths)
    if target:
        return [("relative", target)], []
    return [], [spec]


def _resolve_go(raw: str, paths: Set[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Go: file edges built only from deterministic matches of import-path tails to project dirs."""
    edges: List[Tuple[str, str]] = []
    leftovers: List[str] = []
    for import_path in _all_quoted(raw):
        tail = import_path.rsplit("/", 1)[-1] if "/" in import_path else None
        if tail is None:
            leftovers.append(import_path)
            continue
        target = next(
            (
                path
                for path in sorted(paths)
                if PurePosixPath(path).parent.name == tail
            ),
            None,
        )
        if target:
            edges.append(("import", target))
        else:
            leftovers.append(import_path)
    return edges, leftovers


# ==================================
# P5 binding and export helpers
# ==================================


def _python_bindings(
    raw: str,
    from_dir: PurePosixPath,
    paths: Set[str],
    files_by_module: Dict[str, str],
    files_by_path: Dict[str, ProjectFile],
) -> List[_ImportBinding]:
    """Turn a Python AST import into explicit local bindings.

    Text splitting here happens only on import AST nodes the generic backend
    already confirmed, and accepts only Python identifiers, ``as`` aliases and
    explicit module paths.
    """
    text = _strip_semicolon(raw).strip()
    bindings: List[_ImportBinding] = []
    if text.startswith("from ") and " import " in text:
        module_part, names_part = text[5:].split(" import ", 1)
        module_part = module_part.strip()
        module_target = _python_module_target(
            module_part,
            from_dir,
            paths,
            files_by_module,
        )
        if not module_target:
            return []
        base_module = files_by_path[module_target].module
        for item in _split_import_items(names_part):
            imported, local = _import_name_and_alias(item)
            if imported == "*":
                continue
            if not imported or not local:
                continue
            # ``from pkg import child`` binds to a module only when ``pkg.child`` is an
            # exact project module; no longest-prefix fallback, otherwise
            # ``from pkg.api import helper`` would mistake helper for the api module.
            child_target = files_by_module.get(f"{base_module}.{imported}")
            if child_target:
                bindings.append(
                    _ImportBinding(local, child_target, "", "module", raw)
                )
            else:
                bindings.append(
                    _ImportBinding(local, module_target, imported, "named", raw)
                )
        return bindings
    if text.startswith("import "):
        for item in _split_import_items(text[7:]):
            module_name, local = _import_name_and_alias(item)
            if not module_name or not local:
                continue
            target = _python_absolute_module_target(module_name, files_by_module)
            if target:
                bindings.append(_ImportBinding(local, target, "", "module", raw))
        return bindings
    return bindings


def _js_ts_bindings(
    raw: str,
    from_dir: PurePosixPath,
    paths: Set[str],
) -> List[_ImportBinding]:
    """Parse named/default/namespace bindings in a JS/TS AST-backed import declaration."""
    spec = _first_quoted(raw)
    if not spec or not spec.startswith("."):
        return []
    target = _js_ts_target(spec, from_dir, paths)
    if not target:
        return []
    match = re.match(r"^\s*import\s+(.+?)\s+from\s+[\"']", raw, re.DOTALL)
    if not match:
        return []  # side-effect import has no local binding
    clause = match.group(1).strip()
    bindings: List[_ImportBinding] = []
    if clause.startswith("{") and clause.endswith("}"):
        return _js_ts_named_bindings(clause[1:-1], target, raw)
    namespace = re.fullmatch(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", clause)
    if namespace:
        return [_ImportBinding(namespace.group(1), target, "", "namespace", raw)]

    # Default import optionally followed by a named or namespace clause.
    default_clause, separator, remainder = clause.partition(",")
    default_name = default_clause.strip()
    if _IDENTIFIER_RE.fullmatch(default_name):
        bindings.append(_ImportBinding(default_name, target, "default", "default", raw))
    if not separator:
        return bindings
    remainder = remainder.strip()
    if remainder.startswith("{") and remainder.endswith("}"):
        bindings.extend(_js_ts_named_bindings(remainder[1:-1], target, raw))
    else:
        namespace = re.fullmatch(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", remainder)
        if namespace:
            bindings.append(_ImportBinding(namespace.group(1), target, "", "namespace", raw))
    return bindings


def _js_ts_named_bindings(body: str, target: str, raw: str) -> List[_ImportBinding]:
    bindings: List[_ImportBinding] = []
    for item in _split_import_items(body):
        imported, local = _import_name_and_alias(item)
        if imported and local:
            bindings.append(_ImportBinding(local, target, imported, "named", raw))
    return bindings


def _binding_for_target(
    target: str,
    bindings: Dict[str, _ImportBinding],
) -> Tuple[Optional[_ImportBinding], str, str, str]:
    """Return the import binding and public symbol name for a target; on miss, an audit reason."""
    if _IDENTIFIER_RE.fullmatch(target):
        binding = bindings.get(target)
        if binding is None:
            return None, "", "", "no_project_import_binding"
        if binding.kind == "module":
            return None, "", "", "module_used_without_explicit_member"
        return binding, binding.imported_name, f"{binding.kind}_import", ""

    if not _QUALIFIED_IDENTIFIER_RE.fullmatch(target):
        return None, "", "", "dynamic_or_non_identifier_target"
    head, *members = target.split(".")
    binding = bindings.get(head)
    if binding is None:
        return None, "", "", "dynamic_target_without_namespace_binding"
    if binding.kind not in {"module", "namespace"}:
        return None, "", "", "dynamic_member_on_non_namespace_import"
    if len(members) != 1:
        return None, "", "", "dynamic_member_chain"
    return binding, members[0], f"{binding.kind}_member", ""


def _js_ts_export_pairs(raw: str) -> List[Tuple[str, str]]:
    """Extract ``(local, public)`` pairs from an AST-backed JS/TS export statement."""
    text = _strip_semicolon(raw).strip()
    pairs: List[Tuple[str, str]] = []
    default = re.match(
        r"^export\s+default\s+(?:async\s+)?(?:function|class)\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\b",
        text,
    )
    if default:
        return [(default.group(1), "default")]
    direct = re.match(
        r"^export\s+(?:declare\s+)?(?:async\s+)?"
        r"(?:function|class|interface|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b",
        text,
    )
    if direct:
        return [(direct.group(1), direct.group(1))]
    clause = re.match(r"^export\s*\{(.*)\}\s*$", text, re.DOTALL)
    if not clause:
        return pairs
    for item in _split_import_items(clause.group(1)):
        local, public = _import_name_and_alias(item)
        if local and public:
            pairs.append((local, public))
    return pairs


# ==================================
# P2 resolution and general helpers
# ==================================


def _python_module_target(
    module_part: str,
    from_dir: PurePosixPath,
    paths: Set[str],
    files_by_module: Dict[str, str],
) -> Optional[str]:
    if not module_part.startswith("."):
        return _python_absolute_module_target(module_part, files_by_module)
    level = len(module_part) - len(module_part.lstrip("."))
    tail = module_part[level:]
    base = from_dir
    for _ in range(level - 1):
        base = base.parent
    base_dir = "" if str(base) == "." else str(base)
    if tail:
        stem = tail.replace(".", "/")
        candidates: tuple[str, ...] = (
            _join_posix(base_dir, f"{stem}.py"),
            _join_posix(base_dir, f"{stem}/__init__.py"),
        )
    else:
        candidates = (_join_posix(base_dir, "__init__.py"),)
    return next((candidate for candidate in candidates if candidate in paths), None)


def _python_absolute_module_target(
    module_name: str,
    files_by_module: Dict[str, str],
) -> Optional[str]:
    if not module_name:
        return None
    parts = module_name.split(".")
    for count in range(len(parts), 0, -1):
        target = files_by_module.get(".".join(parts[:count]))
        if target:
            return target
    return None


def _python_import_label(module_part: str) -> str:
    return module_part or "."


def _js_ts_target(
    spec: str,
    from_dir: PurePosixPath,
    paths: Set[str],
) -> Optional[str]:
    joined = posixpath.normpath(str(from_dir / spec))
    candidates = [
        joined,
        f"{joined}.ts", f"{joined}.tsx", f"{joined}.js", f"{joined}.jsx",
        f"{joined}.mjs", f"{joined}.cjs",
        f"{joined}/index.ts", f"{joined}/index.tsx",
        f"{joined}/index.js", f"{joined}/index.jsx",
    ]
    return next((candidate for candidate in candidates if candidate in paths), None)


def _split_import_items(text: str) -> List[str]:
    """Split a comma-separated AST import/export identifier list with no nested syntax."""
    return [item.strip() for item in text.strip().strip("()").split(",") if item.strip()]


def _import_name_and_alias(item: str) -> Tuple[str, str]:
    item = item.strip()
    if " as " in item:
        name, alias = item.split(" as ", 1)
        name, alias = name.strip(), alias.strip()
    else:
        name = alias = item
    if not _IDENTIFIER_RE.fullmatch(name.replace(".", "_")):
        return "", ""
    if not _IDENTIFIER_RE.fullmatch(alias):
        return "", ""
    return name, alias


def _symbol_id(path: str, symbol: SymbolUnit) -> str:
    return f"{path}:{symbol.name}:{symbol.span.byte_start}"


def _enclosing_symbol_id(span: SourceSpan, symbols: List[ProjectSymbol]) -> Optional[str]:
    containers = [
        symbol
        for symbol in symbols
        if symbol.span.byte_start <= span.byte_start and span.byte_end <= symbol.span.byte_end
    ]
    if not containers:
        return None
    return min(
        containers,
        key=lambda symbol: symbol.span.byte_end - symbol.span.byte_start,
    ).id


def _module_name(rel_path: str, language: str) -> str:
    """Derive the module identifier from a file's relative path (Python uses dotted paths)."""
    parts = PurePosixPath(rel_path).with_suffix("").parts
    if language == "python":
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    return rel_path


def _join_posix(base: str, tail: str) -> str:
    return posixpath.normpath(f"{base}/{tail}")


def _strip_semicolon(text: str) -> str:
    return text.rstrip(";").rstrip()


def _first_quoted(text: str) -> Optional[str]:
    """Extract the content of the first single/double-quoted string."""
    for quote in ("'", '"'):
        start = text.find(quote)
        if start < 0:
            continue
        end = text.find(quote, start + 1)
        if end > start:
            return text[start + 1:end]
    return None


def _all_quoted(text: str) -> List[str]:
    """Extract all double-quoted string contents (Go imports, including block imports)."""
    specs: List[str] = []
    rest = text
    while True:
        start = rest.find('"')
        if start < 0:
            return specs
        end = rest.find('"', start + 1)
        if end <= start:
            return specs
        specs.append(rest[start + 1:end])
        rest = rest[end + 1:]
