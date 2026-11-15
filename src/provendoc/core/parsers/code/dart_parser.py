"""Dart code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class DartCodeParser(BaseCodeParser):
    """Dart code parser."""

    language = "dart"
    file_type = "dart"
