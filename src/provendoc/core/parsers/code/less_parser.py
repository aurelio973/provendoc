"""Less stylesheet parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class LessCodeParser(BaseCodeParser):
    """Less stylesheet parser."""

    language = "less"
    file_type = "less"
