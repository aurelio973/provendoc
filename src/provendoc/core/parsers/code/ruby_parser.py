"""Ruby code parser with require and mixin-aware extraction."""

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.p4_backends import RubyTreeSitterBackend


class RubyCodeParser(BaseCodeParser):
    """Ruby code parser."""

    language = "ruby"
    file_type = "ruby"
    backend_class = RubyTreeSitterBackend
