# ============================================================
# Code Domain
# HTML parser — code-domain entry point with embedded script/style
# ============================================================
"""HTML code parser.

HTML is part of frontend code (same domain as JS/TS/CSS/React/Vue). This parser
uses the tree-sitter html grammar for tolerant structural parsing, keeps HTML comments,
and delegates ``<script>`` / ``<style>`` blocks to their language grammars for a
second-pass parse (see embedded.py), so functions, rules, comments and diagnostics
all flow into the unified ``metadata["code"]`` contract.

Non-executable blocks (``type="text/template"`` etc.) are recorded as structural units but skip parsing,
creating no pseudo symbols or noisy diagnostics.
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


class HtmlCodeParser(BaseCodeParser):
    """HTML code-domain parser — .html / .htm."""

    language = "html"
    file_type = "html"

    def __init__(self, encoding: str = "utf-8", tolerant: bool = True):
        """Initialize and assemble the HTML embedded backend."""
        super().__init__(encoding=encoding, tolerant=tolerant)
        self._backend = _HtmlEmbeddedBackend()


class _HtmlEmbeddedBackend(TreeSitterCodeBackend):
    backend_name = "tree_sitter_html"

    def parse(self, source_text: str, language: str):
        """Parse HTML source and second-pass parse script/style blocks with their languages."""
        result = super().parse(source_text, language)
        if result.root_node is not None:
            self._collect_embedded_blocks(
                result.root_node,
                source_text.encode("utf-8"),
                result.structure,
            )
        return result

    def _collect_embedded_blocks(
        self,
        root: Any,
        source: bytes,
        structure: CodeStructure,
    ) -> None:
        """Record script/style blocks and second-pass parse their content with the right language."""
        stack = [root]
        seen: set[tuple[int, int]] = set()
        while stack:
            node = stack.pop()
            if node.type in ("script_element", "style_element"):
                is_script = node.type == "script_element"
                language, detail = element_block_info(node, source, is_script)
                span = _span(node)
                key = (span.byte_start, span.byte_end)
                if key not in seen:
                    seen.add(key)
                    structure.structural_units.append(
                        StructuralUnit(
                            kind="html_block",
                            name="script" if is_script else "style",
                            span=span,
                            detail=detail,
                        )
                    )
                    if language is not None:
                        tag = "script" if is_script else "style"
                        attrs = (
                            f' lang="{detail["language"]}"' if is_script else ""
                        )
                        parse_embedded_block(
                            structure,
                            node,
                            source,
                            language,
                            context=f"<{tag}{attrs}>" if attrs else f"<{tag}>",
                        )
            stack.extend(reversed(node.children))
        parse_template_expressions(structure, root, source, host_language="html")
        # script/style/inline-handler content is all merged; re-judge local vs out-of-file
        # static dependency scope with the full host symbol table.
        _classify_dependency_scope(structure)
        structure.stats["html_block_count"] = sum(
            1 for unit in structure.structural_units if unit.kind == "html_block"
        )
