"""Language-specific evidence extractors for P4 code-domain parsers.

These backends build on the generic tolerant traversal.  They only add facts
whose AST node type and local syntax make the mapping unambiguous; runtime and
cross-file resolution remains intentionally out of scope.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from provendoc.core.parsers.code.contracts import (
    CodeStructure,
    StructuralUnit,
    SymbolUnit,
)
from provendoc.core.parsers.code.generic_backend import (
    TreeSitterCodeBackend,
    _classify_dependency_scope,
    _is_plain_identifier,
    _source_slice,
    _span,
)


class _P4LanguageBackend(TreeSitterCodeBackend):
    """Small shared helpers for grammar-specific P4 facts."""

    @staticmethod
    def _walk(root: Any) -> Iterator[Any]:
        stack = [root]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    @staticmethod
    def _add_unit(
        structure: CodeStructure,
        seen: set[tuple[str, str, int, int]],
        *,
        kind: str,
        name: str,
        node: Any,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        span = _span(node)
        key = (kind, name, span.byte_start, span.byte_end)
        if name and key not in seen:
            seen.add(key)
            structure.structural_units.append(
                StructuralUnit(kind=kind, name=name, span=span, detail=detail)
            )

    @staticmethod
    def _unit_keys(structure: CodeStructure) -> set[tuple[str, str, int, int]]:
        return {
            (unit.kind, unit.name, unit.span.byte_start, unit.span.byte_end)
            for unit in structure.structural_units
        }

    @staticmethod
    def _first_descendant(node: Any, node_types: set[str]) -> Optional[Any]:
        stack = list(reversed(node.children))
        while stack:
            current = stack.pop()
            if current.type in node_types:
                return current
            stack.extend(reversed(current.children))
        return None

    @classmethod
    def _first_descendant_text(
        cls,
        node: Any,
        source: bytes,
        node_types: set[str],
    ) -> str:
        target = cls._first_descendant(node, node_types)
        return _source_slice(source, target.start_byte, target.end_byte) if target else ""


class RubyTreeSitterBackend(_P4LanguageBackend):
    """Extract Ruby require/import, superclass and module-mixin facts."""

    _MIXIN_NAMES = {"include", "extend", "prepend"}
    _REQUIRE_NAMES = {"require", "require_relative"}

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)
        seen = self._unit_keys(structure)
        class_nodes = [node for node in self._walk(root) if node.type == "class"]
        class_symbols = []
        for node in class_nodes:
            name = self._first_descendant_text(node, source, {"constant"})
            if not name:
                continue
            span = _span(node)
            class_symbols.append((name, span))
            if not any(
                symbol.kind == "class"
                and symbol.name == name
                and symbol.span.byte_start == span.byte_start
                and symbol.span.byte_end == span.byte_end
                for symbol in structure.symbols
            ):
                structure.symbols.append(SymbolUnit(name=name, kind="class", span=span))
        for symbol in structure.symbols:
            if symbol.kind != "method":
                continue
            for class_name, class_span in class_symbols:
                if (
                    class_span.byte_start <= symbol.span.byte_start
                    and symbol.span.byte_end <= class_span.byte_end
                ):
                    symbol.parent = class_name
                    break

        for node in self._walk(root):
            if node.type == "class":
                superclass = next(
                    (child for child in node.children if child.type == "superclass"),
                    None,
                )
                if superclass is not None:
                    target = _source_slice(
                        source, superclass.start_byte, superclass.end_byte
                    ).lstrip("<").strip()
                    self._add_unit(
                        structure,
                        seen,
                        kind="extends",
                        name=target,
                        node=superclass,
                        detail={"target": target},
                    )
            elif node.type == "call":
                call_name = self._call_name(node, source)
                argument_list = next(
                    (child for child in node.children if child.type == "argument_list"),
                    None,
                )
                arguments = (
                    _source_slice(source, argument_list.start_byte, argument_list.end_byte)
                    if argument_list is not None
                    else ""
                )
                if call_name in self._REQUIRE_NAMES:
                    snippet = _source_slice(source, node.start_byte, node.end_byte)
                    self._add_unit(
                        structure,
                        seen,
                        kind="import",
                        name=snippet,
                        node=node,
                        detail={"target": arguments, "via": call_name},
                    )
                elif call_name in self._MIXIN_NAMES:
                    for target in self._mixin_targets(arguments):
                        self._add_unit(
                            structure,
                            seen,
                            kind="mixin",
                            name=target,
                            node=node,
                            detail={
                                "target": target,
                                "via": call_name,
                                "dynamic": not _is_plain_identifier(target),
                            },
                        )

        _classify_dependency_scope(structure)

    @staticmethod
    def _call_name(node: Any, source: bytes) -> str:
        for child in node.children:
            if child.type == "identifier":
                return _source_slice(source, child.start_byte, child.end_byte)
        return ""

    @staticmethod
    def _mixin_targets(arguments: str) -> list[str]:
        return [
            part.strip()
            for part in arguments.split(",")
            if part.strip() and len(part.strip()) <= 200
        ]


class SwiftTreeSitterBackend(_P4LanguageBackend):
    """Map Swift protocol declarations and class inheritance/conformance."""

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)
        seen = self._unit_keys(structure)
        protocol_names = {
            symbol.name for symbol in structure.symbols if symbol.kind == "interface"
        }

        for node in self._walk(root):
            if node.type not in {"class_declaration", "protocol_declaration"}:
                continue
            specifications = [
                child for child in node.children if child.type == "inheritance_specifier"
            ]
            for index, specification in enumerate(specifications):
                target = self._first_descendant_text(
                    specification, source, {"type_identifier", "simple_identifier"}
                )
                if not target:
                    continue
                kind = (
                    "implements"
                    if target in protocol_names
                    else ("extends" if index == 0 else "implements")
                )
                self._add_unit(
                    structure,
                    seen,
                    kind=kind,
                    name=target,
                    node=specification,
                    detail={"target": target, "language": "swift"},
                )

        _classify_dependency_scope(structure)


class KotlinTreeSitterBackend(_P4LanguageBackend):
    """Differentiate Kotlin interfaces and map delegation specifiers."""

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)
        class_nodes = [node for node in self._walk(root) if node.type == "class_declaration"]
        interface_spans = {
            (node.start_byte, node.end_byte)
            for node in class_nodes
            if any(child.type == "interface" for child in node.children)
        }
        for symbol in structure.symbols:
            if (
                symbol.kind == "class"
                and (symbol.span.byte_start, symbol.span.byte_end) in interface_spans
            ):
                symbol.kind = "interface"

        seen = self._unit_keys(structure)
        interface_names = {
            symbol.name for symbol in structure.symbols if symbol.kind == "interface"
        }
        for node in class_nodes:
            specifications = [
                child for child in node.children if child.type == "delegation_specifier"
            ]
            for index, specification in enumerate(specifications):
                target = self._delegation_target(specification, source)
                if not target:
                    continue
                kind = (
                    "implements"
                    if target in interface_names or index > 0
                    else "extends"
                )
                self._add_unit(
                    structure,
                    seen,
                    kind=kind,
                    name=target,
                    node=specification,
                    detail={"target": target, "language": "kotlin"},
                )

        _classify_dependency_scope(structure)

    def _delegation_target(self, specification: Any, source: bytes) -> str:
        return self._first_descendant_text(
            specification, source, {"type_identifier", "simple_identifier"}
        )


class ScalaTreeSitterBackend(_P4LanguageBackend):
    """Split Scala ``extends Base with Trait`` into extends and mixin facts."""

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)
        clauses = [node for node in self._walk(root) if node.type == "extends_clause"]
        clause_spans = {(node.start_byte, node.end_byte) for node in clauses}
        # The generic comma-oriented splitter intentionally cannot infer Scala's
        # ``with`` semantics; replace its coarse clause record with exact types.
        structure.structural_units = [
            unit
            for unit in structure.structural_units
            if not (
                unit.kind == "extends"
                and (unit.span.byte_start, unit.span.byte_end) in clause_spans
            )
        ]
        seen = self._unit_keys(structure)
        for clause in clauses:
            targets = [
                _source_slice(source, child.start_byte, child.end_byte)
                for child in clause.children
                if child.type in {"type_identifier", "identifier"}
            ]
            for index, target in enumerate(targets):
                kind = "extends" if index == 0 else "mixin"
                detail = {"target": target}
                if kind == "mixin":
                    detail["via"] = "with"
                self._add_unit(
                    structure,
                    seen,
                    kind=kind,
                    name=target,
                    node=clause,
                    detail=detail,
                )

        _classify_dependency_scope(structure)


class BashTreeSitterBackend(_P4LanguageBackend):
    """Record Bash source imports and command invocations without resolution."""

    _SOURCE_COMMANDS = {"source", "."}

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)
        seen = self._unit_keys(structure)
        for node in self._walk(root):
            if node.type != "command":
                continue
            command_name = next(
                (child for child in node.children if child.type == "command_name"),
                None,
            )
            if command_name is None:
                continue
            target = _source_slice(source, command_name.start_byte, command_name.end_byte)
            if not target:
                continue
            arguments = [
                _source_slice(source, child.start_byte, child.end_byte)
                for child in node.children
                if child is not command_name and child.is_named
            ]
            if target in self._SOURCE_COMMANDS and arguments:
                imported = arguments[0]
                self._add_unit(
                    structure,
                    seen,
                    kind="import",
                    name=_source_slice(source, node.start_byte, node.end_byte),
                    node=node,
                    detail={"target": imported, "via": target},
                )
            elif target not in self._SOURCE_COMMANDS:
                self._add_unit(
                    structure,
                    seen,
                    kind="call",
                    name=target,
                    node=node,
                    detail={"target": target, "dynamic": not _is_plain_identifier(target)},
                )

        _classify_dependency_scope(structure)
