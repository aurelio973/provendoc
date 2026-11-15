"""R code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class RCodeParser(BaseCodeParser):
    """R code parser."""

    language = "r"
    file_type = "r"
