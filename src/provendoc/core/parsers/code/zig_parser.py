"""Zig code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class ZigCodeParser(BaseCodeParser):
    """Zig code parser."""

    language = "zig"
    file_type = "zig"
