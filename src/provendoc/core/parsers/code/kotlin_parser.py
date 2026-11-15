"""Kotlin code parser with interface and delegation-aware extraction."""

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.p4_backends import KotlinTreeSitterBackend


class KotlinCodeParser(BaseCodeParser):
    """Kotlin code parser."""

    language = "kotlin"
    file_type = "kotlin"
    backend_class = KotlinTreeSitterBackend
