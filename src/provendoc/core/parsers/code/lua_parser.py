"""Lua code parser."""

from provendoc.core.parsers.code.base import BaseCodeParser


class LuaCodeParser(BaseCodeParser):
    """Lua code parser."""

    language = "lua"
    file_type = "lua"
