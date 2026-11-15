"""React TSX parser; uses the dedicated TSX grammar."""

from provendoc.core.parsers.code.base import BaseCodeParser


class ReactTsxParser(BaseCodeParser):
    """React TSX parser."""

    language = "tsx"
    file_type = "react_tsx"
