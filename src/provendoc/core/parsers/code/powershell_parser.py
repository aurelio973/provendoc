"""PowerShell code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class PowerShellCodeParser(BaseCodeParser):
    """PowerShell code parser (.ps1/.psm1/.psd1)."""

    language = "powershell"
    file_type = "powershell"
