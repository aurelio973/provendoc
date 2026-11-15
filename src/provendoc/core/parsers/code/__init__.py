"""Code domain parsers — one public parser per source language/framework.

Each parser preserves source text and comments, uses Tree-sitter recovery for
invalid syntax, and emits a CodeStructure payload in document.metadata["code"].
"""

from provendoc.core.parsers.code.bash_parser import BashCodeParser
from provendoc.core.parsers.code.c_parser import CCodeParser
from provendoc.core.parsers.code.cmake_parser import CMakeCodeParser
from provendoc.core.parsers.code.cpp_parser import CppCodeParser
from provendoc.core.parsers.code.csharp_parser import CSharpCodeParser
from provendoc.core.parsers.code.css_parser import CssCodeParser
from provendoc.core.parsers.code.dart_parser import DartCodeParser
from provendoc.core.parsers.code.dockerfile_parser import DockerfileCodeParser
from provendoc.core.parsers.code.go_parser import GoCodeParser
from provendoc.core.parsers.code.hcl_parser import HclCodeParser
from provendoc.core.parsers.code.html_parser import HtmlCodeParser
from provendoc.core.parsers.code.java_parser import JavaCodeParser
from provendoc.core.parsers.code.javascript_parser import JavaScriptCodeParser
from provendoc.core.parsers.code.kotlin_parser import KotlinCodeParser
from provendoc.core.parsers.code.less_parser import LessCodeParser
from provendoc.core.parsers.code.lua_parser import LuaCodeParser
from provendoc.core.parsers.code.makefile_parser import MakefileCodeParser
from provendoc.core.parsers.code.perl_parser import PerlCodeParser
from provendoc.core.parsers.code.php_parser import PhpCodeParser
from provendoc.core.parsers.code.powershell_parser import PowerShellCodeParser
from provendoc.core.parsers.code.proto_parser import ProtoCodeParser
from provendoc.core.parsers.code.python_parser import PythonCodeParser
from provendoc.core.parsers.code.r_parser import RCodeParser
from provendoc.core.parsers.code.react_jsx_parser import ReactJsxParser
from provendoc.core.parsers.code.react_tsx_parser import ReactTsxParser
from provendoc.core.parsers.code.ruby_parser import RubyCodeParser
from provendoc.core.parsers.code.rust_parser import RustCodeParser
from provendoc.core.parsers.code.scala_parser import ScalaCodeParser
from provendoc.core.parsers.code.scss_parser import ScssCodeParser
from provendoc.core.parsers.code.sql_parser import SqlCodeParser
from provendoc.core.parsers.code.swift_parser import SwiftCodeParser
from provendoc.core.parsers.code.typescript_parser import TypeScriptCodeParser
from provendoc.core.parsers.code.vue_parser import VueCodeParser
from provendoc.core.parsers.code.zig_parser import ZigCodeParser

__all__ = [
    "BashCodeParser",
    "CCodeParser",
    "CMakeCodeParser",
    "CppCodeParser",
    "CSharpCodeParser",
    "CssCodeParser",
    "DartCodeParser",
    "DockerfileCodeParser",
    "GoCodeParser",
    "HclCodeParser",
    "HtmlCodeParser",
    "JavaCodeParser",
    "JavaScriptCodeParser",
    "KotlinCodeParser",
    "LessCodeParser",
    "LuaCodeParser",
    "MakefileCodeParser",
    "PerlCodeParser",
    "PhpCodeParser",
    "PowerShellCodeParser",
    "ProtoCodeParser",
    "PythonCodeParser",
    "RCodeParser",
    "ReactJsxParser",
    "ReactTsxParser",
    "RubyCodeParser",
    "RustCodeParser",
    "ScalaCodeParser",
    "ScssCodeParser",
    "SqlCodeParser",
    "SwiftCodeParser",
    "TypeScriptCodeParser",
    "VueCodeParser",
    "ZigCodeParser",
]
