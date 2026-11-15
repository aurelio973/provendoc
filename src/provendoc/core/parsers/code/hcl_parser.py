"""HCL / Terraform parser (structural, via tree-sitter hcl grammar)."""

from provendoc.core.parsers.code.base import BaseCodeParser


class HclCodeParser(BaseCodeParser):
    """HCL / Terraform parser."""

    language = "hcl"
    file_type = "hcl"
