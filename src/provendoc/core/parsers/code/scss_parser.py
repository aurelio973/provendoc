"""SCSS code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class ScssCodeParser(BaseCodeParser):
    """SCSS code parser."""

    language = "scss"
    file_type = "scss"
