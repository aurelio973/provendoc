"""Swift code parser with protocol-conformance extraction."""

from provendoc.core.parsers.code.base import BaseCodeParser
from provendoc.core.parsers.code.p4_backends import SwiftTreeSitterBackend


class SwiftCodeParser(BaseCodeParser):
    """Swift code parser."""

    language = "swift"
    file_type = "swift"
    backend_class = SwiftTreeSitterBackend
