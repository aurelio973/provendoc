"""Rust code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class RustCodeParser(BaseCodeParser):
    """Rust code parser."""

    language = "rust"
    file_type = "rust"
