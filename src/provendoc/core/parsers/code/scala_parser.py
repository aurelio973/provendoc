"""Scala code parser with extends/with relation extraction."""

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.p4_backends import ScalaTreeSitterBackend


class ScalaCodeParser(BaseCodeParser):
    """Scala code parser."""

    language = "scala"
    file_type = "scala"
    backend_class = ScalaTreeSitterBackend
