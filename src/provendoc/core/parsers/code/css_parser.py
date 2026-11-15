"""CSS code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class CssCodeParser(BaseCodeParser):
    """CSS code parser."""

    language = "css"
    file_type = "css"
