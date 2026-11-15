"""C++ code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class CppCodeParser(BaseCodeParser):
    """C++ code parser."""

    language = "cpp"
    file_type = "cpp"
