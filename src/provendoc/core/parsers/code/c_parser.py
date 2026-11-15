"""C code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class CCodeParser(BaseCodeParser):
    """C code parser."""

    language = "c"
    file_type = "c"
