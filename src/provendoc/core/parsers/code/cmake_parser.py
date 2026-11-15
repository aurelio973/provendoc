"""CMake code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class CMakeCodeParser(BaseCodeParser):
    """CMake (.cmake) parser.

    The bare filename CMakeLists.txt carries a .txt suffix and must be routed by filename first;
    that is a different mechanism — honestly not covered this round (see the gap register).
    """

    language = "cmake"
    file_type = "cmake"
