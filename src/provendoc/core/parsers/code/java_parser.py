"""Java code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class JavaCodeParser(BaseCodeParser):
    """Java code parser."""

    language = "java"
    file_type = "java"
