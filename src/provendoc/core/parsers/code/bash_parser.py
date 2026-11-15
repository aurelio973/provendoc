"""Bash code parser with source and command-call extraction."""

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.p4_backends import BashTreeSitterBackend


class BashCodeParser(BaseCodeParser):
    """Bash code parser."""

    language = "bash"
    file_type = "bash"
    backend_class = BashTreeSitterBackend
