"""Makefile code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class MakefileCodeParser(BaseCodeParser):
    """Makefile parser (.mk/.mak suffixes and bare filenames Makefile/GNUmakefile)."""

    language = "make"
    file_type = "makefile"
