"""TypeScript code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class TypeScriptCodeParser(BaseCodeParser):
    """TypeScript code parser."""

    language = "typescript"
    file_type = "typescript"
