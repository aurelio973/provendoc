"""Perl code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class PerlCodeParser(BaseCodeParser):
    """Perl code parser."""

    language = "perl"
    file_type = "perl"
