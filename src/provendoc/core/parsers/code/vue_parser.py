# ============================================================
# Code Domain
# Vue Single File Component parser
# ============================================================
"""Vue SFC parser.

The Vue grammar provides top-level ``template`` / ``script`` / ``style`` tolerant structure,
but script/style content is merely ``raw_text`` in some grammar versions. This parser
first keeps the SFC blocks, then delegates embedded content to the JavaScript, TypeScript,
CSS or SCSS grammar for a second-pass parse based on the ``lang`` attribute. Vue functions,
imports, rules, comments and syntax diagnostics thus follow the unified CodeStructure contract, with all spans aligned to the host .vue file.

Template expressions and custom languages (pug/less etc.) do not get a fabricated AST; the
corresponding SFC blocks are kept with a skipped reason, pending dedicated parser support.
"""

from __future__ import annotations

from typing import Any

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.contracts import CodeStructure, StructuralUnit
from provendoc.core.parsers.code.embedded import (
    element_block_info,
    parse_embedded_block,
    parse_template_expressions,
)
from provendoc.core.parsers.code.generic_backend import (
    TreeSitterCodeBackend,
    _classify_dependency_scope,
    _span,
)


class VueCodeParser(BaseCodeParser):
    """Vue Single File Component code parser."""

    language = "vue"
    file_type = "vue"

    def __init__(self, encoding: str = "utf-8", tolerant: bool = True):
        """Initialize and assemble the Vue SFC backend."""
        super().__init__(encoding=encoding, tolerant=tolerant)
        self._backend = _VueSfcBackend()


class _VueSfcBackend(TreeSitterCodeBackend):
    backend_name = "tree_sitter_vue"

    def parse(self, source_text: str, language: str):
        """Parse SFC source and second-pass parse supported script/style block content."""
        result = super().parse(source_text, language)
        if result.root_node is not None:
            self._collect_sfc_blocks(
                result.root_node,
                source_text.encode("utf-8"),
                result.structure,
            )
        return result

    @staticmethod
    def _collect_sfc_blocks(
        root: Any,
        source: bytes,
        structure: CodeStructure,
    ) -> None:
        """Collect SFC blocks and second-pass parse supported script/style content."""
        stack = [root]
        seen: set[tuple[int, int]] = set()
        while stack:
            node = stack.pop()
            if node.type in {"script_element", "style_element", "template_element"}:
                kind = node.type.removesuffix("_element")
                span = _span(node)
                key = (span.byte_start, span.byte_end)
                if key not in seen:
                    seen.add(key)
                    detail = None
                    if kind == "script":
                        language, detail = element_block_info(node, source, is_script=True)
                    elif kind == "style":
                        language, detail = element_block_info(node, source, is_script=False)
                    else:
                        language = None
                        detail = {"language": "html", "parsed": "sfc_structure_only"}

                    structure.structural_units.append(
                        StructuralUnit(
                            kind="sfc_block",
                            name=kind,
                            span=span,
                            detail=detail,
                        )
                    )
                    if language is not None:
                        parse_embedded_block(
                            structure,
                            node,
                            source,
                            language,
                            context=f"<vue:{kind}>",
                        )
            stack.extend(reversed(node.children))
        parse_template_expressions(structure, root, source, host_language="vue")
        # Template expressions and script content are both merged; recompute the dependency
        # scope with the full host symbol table so <script setup> local methods are not mis-flagged as external.
        _classify_dependency_scope(structure)
        structure.stats["sfc_block_count"] = sum(
            1 for unit in structure.structural_units if unit.kind == "sfc_block"
        )
