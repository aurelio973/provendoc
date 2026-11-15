"""Go code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class GoCodeParser(BaseCodeParser):
    """Go code parser."""

    language = "go"
    file_type = "go"
