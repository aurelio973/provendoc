"""PHP code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class PhpCodeParser(BaseCodeParser):
    """PHP code parser."""

    language = "php"
    file_type = "php"
