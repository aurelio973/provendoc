"""Protocol Buffers code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class ProtoCodeParser(BaseCodeParser):
    """Protocol Buffers (.proto) parser."""

    language = "proto"
    file_type = "proto"
