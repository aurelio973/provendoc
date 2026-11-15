"""JavaScript code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class JavaScriptCodeParser(BaseCodeParser):
    """JavaScript code parser."""

    language = "javascript"
    file_type = "javascript"
