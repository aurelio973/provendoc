"""React JSX parser; uses JavaScript grammar with JSX support."""

from provendoc.core.parsers.code.base import BaseCodeParser


class ReactJsxParser(BaseCodeParser):
    """React JSX parser (JavaScript grammar + JSX support)."""

    language = "javascript"
    file_type = "react_jsx"
