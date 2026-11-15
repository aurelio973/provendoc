"""SQL code parser with DDL and CTE-aware Tree-sitter extraction.

The generic code backend retains SQL comments and recovery diagnostics, while this
backend adds syntax-grounded schema/query definitions that are meaningful SQL
retrieval boundaries.  Procedure extraction has a deliberately narrow fallback
for grammars that currently recover ``CREATE PROCEDURE`` as an ERROR node.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Optional

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.contracts import CodeStructure, StructuralUnit, SymbolUnit
from provendoc.core.parsers.code.generic_backend import (
    TreeSitterCodeBackend,
    _classify_dependency_scope,
    _source_slice,
    _span,
)


class SqlTreeSitterBackend(TreeSitterCodeBackend):
    """Add SQL DDL and common-table-expression facts to generic parse output."""

    backend_name = "tree_sitter_sql"

    _DDL_NODE_KINDS = {
        "create_table": "table",
        "create_view": "view",
        "create_function": "function",
    }
    _PROCEDURE_PATTERN = re.compile(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+"
        r"(?P<name>(?:\"(?:[^\"]|\"\")*\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:\"(?:[^\"]|\"\")*\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*))*)",
        re.IGNORECASE,
    )

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        super()._collect(root, source, structure)

        symbol_keys = {
            (symbol.kind, symbol.name, symbol.span.byte_start, symbol.span.byte_end)
            for symbol in structure.symbols
        }
        unit_keys = {
            (unit.kind, unit.name, unit.span.byte_start, unit.span.byte_end)
            for unit in structure.structural_units
        }

        for node in self._walk(root):
            object_kind = self._DDL_NODE_KINDS.get(node.type)
            if object_kind:
                name = self._definition_name(node, source, "object_reference")
                if name:
                    self._append_definition(
                        structure,
                        symbol_keys,
                        unit_keys,
                        name=name,
                        object_kind=object_kind,
                        node=node,
                    )
            elif node.type == "cte":
                name = self._definition_name(node, source, "identifier")
                if name:
                    self._append_definition(
                        structure,
                        symbol_keys,
                        unit_keys,
                        name=name,
                        object_kind="cte",
                        node=node,
                    )
            elif node.type == "ERROR":
                # Some supported SQL dialects expose CREATE PROCEDURE as ERROR
                # rather than a dedicated grammar node.  Restrict the fallback
                # to that recovered node, never scan arbitrary source text.
                procedure = self._procedure_name_from_error(node, source)
                if procedure:
                    self._append_definition(
                        structure,
                        symbol_keys,
                        unit_keys,
                        name=procedure,
                        object_kind="procedure",
                        node=node,
                        extraction="tree_sitter_error_fallback",
                    )

        _classify_dependency_scope(structure)
        for object_kind in ("table", "view", "function", "procedure", "cte"):
            structure.stats[f"sql_{object_kind}_count"] = sum(
                1 for symbol in structure.symbols if symbol.kind == object_kind
            )

    @staticmethod
    def _walk(root: Any) -> Iterator[Any]:
        stack = [root]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    @staticmethod
    def _definition_name(node: Any, source: bytes, expected_type: str) -> str:
        for child in node.children:
            if child.type == expected_type:
                return _source_slice(source, child.start_byte, child.end_byte)
        return ""

    def _procedure_name_from_error(self, node: Any, source: bytes) -> Optional[str]:
        fragment = _source_slice(source, node.start_byte, node.end_byte)
        match = self._PROCEDURE_PATTERN.search(fragment)
        return match.group("name").strip() if match else None

    @staticmethod
    def _append_definition(
        structure: CodeStructure,
        symbol_keys: set[tuple[str, str, int, int]],
        unit_keys: set[tuple[str, str, int, int]],
        *,
        name: str,
        object_kind: str,
        node: Any,
        extraction: str = "tree_sitter",
    ) -> None:
        span = _span(node)
        symbol_key = (object_kind, name, span.byte_start, span.byte_end)
        if symbol_key not in symbol_keys:
            symbol_keys.add(symbol_key)
            structure.symbols.append(SymbolUnit(name=name, kind=object_kind, span=span))

        unit_key = ("sql_definition", name, span.byte_start, span.byte_end)
        if unit_key not in unit_keys:
            unit_keys.add(unit_key)
            structure.structural_units.append(
                StructuralUnit(
                    kind="sql_definition",
                    name=name,
                    span=span,
                    detail={"object_kind": object_kind, "extraction": extraction},
                )
            )


class SqlCodeParser(BaseCodeParser):
    """SQL parser with schema-definition and CTE extraction."""

    language = "sql"
    file_type = "sql"
    backend_class = SqlTreeSitterBackend
