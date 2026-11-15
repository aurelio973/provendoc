"""Dockerfile code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class DockerfileCodeParser(BaseCodeParser):
    """Dockerfile parser (.dockerfile suffix and bare filename Dockerfile)."""

    language = "dockerfile"
    file_type = "dockerfile"
