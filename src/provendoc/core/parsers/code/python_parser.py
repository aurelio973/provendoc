"""Python code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class PythonCodeParser(BaseCodeParser):
    """Python code parser."""

    language = "python"
    file_type = "python"
