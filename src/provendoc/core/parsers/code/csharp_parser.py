"""C# code parser (Tree-sitter with syntax recovery)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class CSharpCodeParser(BaseCodeParser):
    """C# code parser."""

    language = "csharp"
    file_type = "csharp"
