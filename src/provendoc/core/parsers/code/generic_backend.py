# ============================================================
# Code Domain
# Tree-sitter tolerant backend
# ============================================================
"""Tree-sitter unified fault-tolerant backend.

This module does no code repair. It turns Tree-sitter's error recovery into
``CodeDiagnostic`` facts: even when a function's tail is incomplete, the remaining parsed
functions, classes, imports and comments are preserved. This way the retrieval system can answer "where the syntax errors are" while still
using the undamaged local structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from provendoc.core.parsers.code.contracts import (
    CodeDiagnostic,
    CodeStructure,
    CommentUnit,
    ParseStatus,
    SourceSpan,
    StructuralUnit,
    SymbolUnit,
)

# Different grammars name nodes of the same abstract structure differently. Conservative mapping only:
# unknown nodes are not force-classified; source text and diagnostics are still kept.
_SYMBOL_NODE_KINDS = {
    "class_definition": "class",  # python / scala
    "class_declaration": "class",
    "class_specifier": "class",
    "interface_declaration": "interface",
    "interface_definition": "interface",
    "protocol_declaration": "interface",  # swift
    "trait_definition": "trait",  # scala
    "object_definition": "object",  # scala
    "enum_declaration": "enum",
    "enum_specifier": "enum",
    "function_definition": "function",  # python / scala
    "function_declaration": "function",
    "function_item": "function",
    "function_signature_item": "function",
    "function_expression": "function",
    "protocol_function_declaration": "method",  # swift
    "method": "method",  # ruby
    "method_definition": "method",
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "trait_item": "trait",
    "struct_item": "struct",
    "enum_item": "enum",  # rust enum definitions
    "type_alias_declaration": "type",
    "type_spec": "type",  # go type declarations (kind refined per body)
    "struct_specifier": "struct",  # c / c++ (definitions only, see guard)
    # css / scss rule sets (selectors carry the rule identity)
    "rule_set": "rule",
}

# Nodes needing definition/specific-type determination: conditional rules beyond the static kind map.
def _symbol_kind_for(node: Any) -> Optional[str]:
    """Resolve symbol kind, guarding node types that also appear in
    non-definition positions in their grammar.

    - ``struct_specifier`` (C/C++): also used for *type usages*
      (``struct point p;``). Only a definition carries a
      ``field_declaration_list`` body.
    - ``type_spec`` (Go): the declared kind depends on the body —
      ``interface_type`` / ``struct_type`` refine the kind so
      interfaces and structs are first-class container symbols.
    """
    kind = _SYMBOL_NODE_KINDS.get(node.type)
    if kind is None:
        return None
    if node.type == "struct_specifier":
        if not any(
            child.type == "field_declaration_list" for child in node.children
        ):
            return None
    elif node.type == "type_spec":
        for child in node.children:
            if child.type == "interface_type":
                return "interface"
            if child.type == "struct_type":
                return "struct"
    return kind

_IMPORT_NODE_TYPES = {
    "import_statement",
    "import_declaration",
    "import_from_statement",
    "import_header",  # kotlin
    "preproc_include",
    "using_directive",
    "using_declaration",
    "use_declaration",
    "require_call",
}

# JavaScript / TypeScript export declarations are source-level facts.  Preserve
# the AST-backed statement so P5 can resolve only explicitly exported symbols.
_EXPORT_NODE_TYPES = {"export_statement"}

_IDENTIFIER_NODE_TYPES = {
    "identifier",
    "type_identifier",
    "property_identifier",
    "field_identifier",
    "scoped_identifier",
    "namespace_identifier",
    "constant",  # ruby class/module constants
    "simple_identifier",  # swift / kotlin
    "word",  # bash function definitions
}

# P1 dependency extraction --------------------------------------------------------
# call nodes → field candidates carrying the callee target (grammar naming differs; try each).
# The target may be a bare identifier (locally resolvable) or a dotted chain (resolved at runtime; marked dynamic).
_CALL_NODE_FIELDS = {
    "call": ("function", "method"),        # python (function) / ruby (method)
    "call_expression": ("function",),      # javascript / typescript / php
    "method_invocation": ("name",),        # java
    "invocation_expression": ("function",),  # c#
}

# instantiation nodes → type field, emitted as a "reference" unit (static type-reference fact).
_INSTANTIATION_NODE_FIELDS = {
    "new_expression": ("constructor",),             # javascript / typescript
    "object_creation_expression": ("type",),        # java
}

# inheritance / implementation: declaration nodes → list of (field, kind).
_HERITAGE_FIELDS = {
    "class_definition": (("superclasses", "extends"),),          # python: argument_list
    "class_declaration": (
        # Java: the superclass field text contains the "extends" keyword,
        # the interfaces field is a super_interfaces node (containing "implements").
        # TypeScript's class_heritage has no field; handled by standalone clause nodes.
        ("superclass", "extends"),
        ("interfaces", "implements"),
        ("bases", "extends"),
    ),
    "abstract_class_declaration": (
        ("superclass", "extends"),
        ("interfaces", "implements"),
    ),
    "class_specifier": (),  # c/c++: base_class_clause is a standalone child without a field,
                              # handled by _HERITAGE_CLAUSE_KINDS.
    "struct_item": (("base_type_clause", "extends"),),           # rust
    "trait_item": (("bounds", "extends"),),                      # rust trait bound
}

# Standalone base-clause nodes (appearing as direct children of declaration nodes): node type → kind.
_HERITAGE_CLAUSE_KINDS = {
    "extends_clause": "extends",          # typescript
    "implements_clause": "implements",    # typescript
    "base_class_clause": "extends",       # c / c++ (text carries a leading ':')
    "base_clause": "extends",             # php
    "class_interface_clause": "implements",  # php
    "base_list": "extends",               # c# (text carries a leading ':')
}

# Keywords and access modifiers inside base clauses do not join the target name
# (Java superclass field text is "extends Base", C++ is "public Base").
_HERITAGE_NOISE_TOKENS = {
    "public", "private", "protected", "internal", "virtual",
    "final", "abstract", "sealed", "override", "transient",
    "extends", "implements",
}

# Ruby mixin keywords: module inclusion appearing as method calls.
_MIXIN_CALL_NAMES = {"include", "extend", "prepend"}

# Defensive cap on dependency units per file: even abnormally huge generated files still finish parsing.
_MAX_DEPENDENCY_UNITS = 5_000
# Character cap for a single dependency target: guards against garbage text inside ERROR nodes.
_MAX_TARGET_CHARS = 200

_MAX_VISITED_NODES = 200_000


@dataclass(frozen=True)
class BackendResult:
    """Expose the root tree's internal results for composite backends like Vue."""

    structure: CodeStructure
    root_node: Any


def _source_slice(source: bytes, start: int, end: int) -> str:
    return source[start:end].decode("utf-8", errors="replace").strip()


def _span(node: Any) -> SourceSpan:
    """Map a Tree-sitter node position to the public source span."""
    return SourceSpan(
        line_start=node.start_point.row + 1,
        line_end=max(node.start_point.row + 1, node.end_point.row + 1),
        col_start=node.start_point.column,
        col_end=node.end_point.column,
        byte_start=node.start_byte,
        byte_end=node.end_byte,
    )


def _node_name(node: Any, source: bytes) -> str:
    """Best-effort extraction of the definition name; never fabricate symbols from error-node content."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        name = _source_slice(source, name_node.start_byte, name_node.end_byte)
        if name:
            return name

    # Some C/C++/CSS grammars have no field for the name declaration; take only the first direct identifier child.
    for child in node.children:
        if child.type in _IDENTIFIER_NODE_TYPES:
            name = _source_slice(source, child.start_byte, child.end_byte)
            if name:
                return name

    return "<anonymous>"


def _parent_symbol_name(node: Any, source: bytes) -> Optional[str]:
    parent = node.parent
    while parent is not None:
        if parent.type in _SYMBOL_NODE_KINDS:
            name = _node_name(parent, source)
            return name if name != "<anonymous>" else None
        parent = parent.parent
    return None


def _comment_kind(node_type: str, text: str) -> str:
    if "doc" in node_type or text.startswith(("///", "/**", "##")):
        return "doc"
    if text.lstrip().startswith(("//", "#", "--")):
        return "line"
    return "block"


def _diagnostic_for(node: Any, source: bytes) -> Optional[CodeDiagnostic]:
    is_missing = bool(getattr(node, "is_missing", False))
    is_error = bool(getattr(node, "is_error", False)) or node.type == "ERROR"
    if not is_missing and not is_error:
        return None

    span = _span(node)
    snippet = _source_slice(source, node.start_byte, node.end_byte)
    if is_missing:
        expected = node.type if node.type != "MISSING" else "syntax token"
        return CodeDiagnostic(
            code="missing_node",
            message=f"Parser recovered from a missing {expected}.",
            severity="error",
            span=span,
            snippet=snippet,
        )
    return CodeDiagnostic(
        code="syntax_error",
        message="Parser recovered from invalid or incomplete syntax.",
        severity="error",
        span=span,
        snippet=snippet,
    )


def _append_unique(items: list[Any], item: Any, seen: set[tuple[Any, ...]], key: tuple[Any, ...]) -> None:
    if key not in seen:
        seen.add(key)
        items.append(item)


def _first_field(node: Any, field_names: tuple[str, ...]) -> Optional[Any]:
    """Return the first existing field child in candidate order."""
    for field_name in field_names:
        child = node.child_by_field_name(field_name)
        if child is not None:
            return child
    return None


def _is_plain_identifier(target: str) -> bool:
    """A bare identifier may resolve in the local symbol table; dotted chains are left to runtime (dynamic)."""
    return target.isidentifier()


def _split_heritage_targets(text: str) -> list[str]:
    """Split base-clause text into individual target names, stripping keywords/modifiers and garbage."""
    text = text.strip()
    # Python superclasses is an argument_list; the text comes with parentheses.
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    # C++ base_class_clause text carries a leading ':'.
    while text.startswith(":"):
        text = text[1:].strip()
    targets: list[str] = []
    for part in text.split(","):
        tokens = [
            token for token in part.split() if token not in _HERITAGE_NOISE_TOKENS
        ]
        candidate = " ".join(tokens).strip()
        # Target names must not contain these characters — a hit means garbage text from ERROR recovery.
        if not candidate or any(ch in candidate for ch in "()={};"):
            continue
        if len(candidate) > _MAX_TARGET_CHARS:
            continue
        targets.append(candidate)
    return targets


def _dependency_units(node: Any, node_type: str, source: bytes) -> list[StructuralUnit]:
    """Best-effort extraction of dependency units from a single AST node.

    Records only facts visible on the syntax tree:
      - call       callee target (identifier locally resolvable; dotted chains marked dynamic)
      - reference  type reference inside new / instantiation expressions
      - extends / implements  bases / interfaces from declaration nodes and standalone clauses
      - mixin      Ruby include / extend / prepend module inclusion
    No fabricated resolution: any missing field means skip; targets are never guessed.
    """
    units: list[StructuralUnit] = []

    # --- calls ---
    call_fields = _CALL_NODE_FIELDS.get(node_type)
    if call_fields is not None:
        callee = None
        matched_field = ""
        for field_name in call_fields:
            child = node.child_by_field_name(field_name)
            if child is not None:
                callee = child
                matched_field = field_name
                break
        if callee is not None:
            target = _source_slice(source, callee.start_byte, callee.end_byte)
            if target and len(target) <= _MAX_TARGET_CHARS:
                # Ruby mixin: include/extend/prepend hit via the method field.
                if (
                    node_type == "call"
                    and matched_field == "method"
                    and target in _MIXIN_CALL_NAMES
                ):
                    arguments = node.child_by_field_name("arguments")
                    mixin_target = ""
                    if arguments is not None and arguments.children:
                        first = arguments.children[0]
                        mixin_target = _source_slice(
                            source, first.start_byte, first.end_byte
                        )
                    if mixin_target and len(mixin_target) <= _MAX_TARGET_CHARS:
                        units.append(
                            StructuralUnit(
                                kind="mixin",
                                name=mixin_target,
                                span=_span(node),
                                detail={
                                    "target": mixin_target,
                                    "via": target,
                                    "dynamic": not _is_plain_identifier(mixin_target),
                                },
                            )
                        )
                else:
                    # Java method_invocation: name is only the method name;
                    # the receiver (object field) is separate evidence, recorded too.
                    receiver = ""
                    if node_type == "method_invocation":
                        receiver_node = node.child_by_field_name("object")
                        if receiver_node is not None:
                            receiver = _source_slice(
                                source, receiver_node.start_byte, receiver_node.end_byte
                            )
                    detail = {
                        "target": target,
                        # Dynamic dispatch on a named receiver cannot be reliably resolved
                        # to a local method in a single-file AST, even if its bare method name coincidentally collides.
                        "dynamic": not _is_plain_identifier(target) or bool(receiver),
                    }
                    if receiver:
                        detail["receiver"] = receiver
                    units.append(
                        StructuralUnit(
                            kind="call",
                            name=target,
                            span=_span(node),
                            detail=detail,
                        )
                    )

    # --- instantiation references ---
    instantiation_fields = _INSTANTIATION_NODE_FIELDS.get(node_type)
    if instantiation_fields is not None:
        type_node = _first_field(node, instantiation_fields)
        if type_node is not None:
            target = _source_slice(source, type_node.start_byte, type_node.end_byte)
            if target and len(target) <= _MAX_TARGET_CHARS:
                units.append(
                    StructuralUnit(
                        kind="reference",
                        name=target,
                        span=_span(node),
                        detail={
                            "target": target,
                            "subtype": "instantiation",
                            "dynamic": not _is_plain_identifier(target),
                        },
                    )
                )

    # --- inheritance fields on declaration nodes (Java superclass/interfaces etc.) ---
    heritage_entries = _HERITAGE_FIELDS.get(node_type)
    if heritage_entries:
        for field_name, kind in heritage_entries:
            bases = list(node.children_by_field_name(field_name))
            if not bases:
                single = node.child_by_field_name(field_name)
                if single is not None:
                    bases = [single]
            for base in bases:
                text = _source_slice(source, base.start_byte, base.end_byte)
                for target in _split_heritage_targets(text):
                    units.append(
                        StructuralUnit(
                            kind=kind,
                            name=target,
                            span=_span(base),
                            detail={"target": target},
                        )
                    )

    # --- standalone base clauses (TS extends_clause / PHP base_clause etc.) ---
    # The clause's whole text is the target list (contains the extends/implements keywords, stripped
    # by _split_heritage_targets), independent of grammar field-naming differences.
    clause_kind = _HERITAGE_CLAUSE_KINDS.get(node_type)
    if clause_kind:
        text = _source_slice(source, node.start_byte, node.end_byte)
        for target in _split_heritage_targets(text):
            units.append(
                StructuralUnit(
                    kind=clause_kind,
                    name=target,
                    span=_span(node),
                    detail={"target": target},
                )
            )

    return units


def _classify_dependency_scope(structure: CodeStructure) -> None:
    """Mark static dependencies unresolvable by the current file's symbol table as file-external references.

    This is a document-level fact, not cross-project resolution: ``external`` only means the target is not in
    the current ``CodeStructure.symbols`` — it may be a built-in symbol, an imported symbol, or a symbol from
    another file in the project. P2 project indexing can resolve the actual origin on this basis.

    This function allows a safe re-run after embedded content (HTML/Vue etc.) is merged into the host
    structure, so each call first clears the scope flags from the previous run against the smaller local symbol table.
    """
    local_names = {symbol.name for symbol in structure.symbols if symbol.name}
    dependency_kinds = {"call", "reference", "extends", "implements", "mixin"}
    for unit in structure.structural_units:
        if unit.kind not in dependency_kinds:
            continue
        detail = dict(unit.detail or {})
        # Embedded expressions resolve first within their own local scope; after merging into the host they
        # need re-evaluation with the full symbol table; clearing stale flags keeps the re-run idempotent without leftover fake externals.
        detail.pop("external", None)
        detail.pop("external_scope", None)
        target = str(detail.get("target") or unit.name or "")
        if (
            target
            and not detail.get("dynamic")
            and target not in local_names
        ):
            detail["external"] = True
            detail["external_scope"] = "file"
        unit.detail = detail or None


class TreeSitterCodeBackend:
    """Fault-tolerant structure extractor for a single-language grammar."""

    backend_name = "tree_sitter"

    def parse(self, source_text: str, language: str) -> BackendResult:
        """Parse source and return the structure result; degrades when the backend is missing or raises, never throws."""
        if not source_text:
            return BackendResult(
                structure=CodeStructure.empty(language, self.backend_name),
                root_node=None,
            )

        try:
            from tree_sitter_language_pack import get_parser
        except ImportError as exc:
            return BackendResult(
                structure=CodeStructure.degraded(
                    language,
                    self.backend_name,
                    "tree-sitter-language-pack is unavailable: " + str(exc),
                ),
                root_node=None,
            )

        source = source_text.encode("utf-8")
        try:
            parser = get_parser(language)
            tree = parser.parse(source)
        except Exception as exc:  # grammar download/load errors must not reject ingestion
            return BackendResult(
                structure=CodeStructure.degraded(
                    language,
                    self.backend_name,
                    "Tree-sitter parse backend failed: " + str(exc),
                ),
                root_node=None,
            )

        root = tree.root_node
        structure = CodeStructure(
            language=language,
            backend=self.backend_name,
            status=ParseStatus.OK.value,
            stats={
                "root_node_type": root.type,
                "source_bytes": len(source),
                "source_lines": source_text.count("\n") + 1,
                "column_encoding": "utf8_bytes",
            },
        )
        self._collect(root, source, structure)
        if structure.diagnostics:
            structure.status = ParseStatus.RECOVERED.value
        return BackendResult(structure=structure, root_node=root)

    def _collect(self, root: Any, source: bytes, structure: CodeStructure) -> None:
        """One DFS pass collecting errors, comments, symbols and import structure simultaneously."""
        stack = [root]
        visited = 0
        diagnostic_keys: set[tuple[Any, ...]] = set()
        comment_keys: set[tuple[Any, ...]] = set()
        symbol_keys: set[tuple[Any, ...]] = set()
        structural_keys: set[tuple[Any, ...]] = set()

        while stack:
            node = stack.pop()
            visited += 1
            if visited > _MAX_VISITED_NODES:
                structure.diagnostics.append(
                    CodeDiagnostic(
                        code="node_limit_reached",
                        severity="warning",
                        message=(
                            "Parser traversal stopped after "
                            f"{_MAX_VISITED_NODES} nodes; remaining structure was not indexed."
                        ),
                    )
                )
                break

            diagnostic = _diagnostic_for(node, source)
            if diagnostic:
                key: tuple[Any, ...] = (
                    diagnostic.code,
                    diagnostic.span.byte_start if diagnostic.span else -1,
                    diagnostic.span.byte_end if diagnostic.span else -1,
                )
                _append_unique(structure.diagnostics, diagnostic, diagnostic_keys, key)

            node_type = node.type
            if "comment" in node_type:
                text = _source_slice(source, node.start_byte, node.end_byte)
                if text:
                    comment = CommentUnit(
                        kind=_comment_kind(node_type, text),
                        text=text,
                        span=_span(node),
                    )
                    key = (comment.span.byte_start, comment.span.byte_end)
                    _append_unique(structure.comments, comment, comment_keys, key)

            symbol_kind = _symbol_kind_for(node)
            if symbol_kind and not bool(getattr(node, "is_error", False)):
                symbol = SymbolUnit(
                    name=_node_name(node, source),
                    kind=symbol_kind,
                    span=_span(node),
                    parent=_parent_symbol_name(node, source),
                )
                key = (symbol.kind, symbol.name, symbol.span.byte_start, symbol.span.byte_end)
                _append_unique(structure.symbols, symbol, symbol_keys, key)

            if node_type in _IMPORT_NODE_TYPES:
                snippet = _source_slice(source, node.start_byte, node.end_byte)
                if snippet:
                    unit = StructuralUnit(
                        kind="import",
                        name=snippet,
                        span=_span(node),
                    )
                    key = (unit.kind, unit.span.byte_start, unit.span.byte_end)
                    _append_unique(structure.structural_units, unit, structural_keys, key)

            if node_type in _EXPORT_NODE_TYPES:
                snippet = _source_slice(source, node.start_byte, node.end_byte)
                if snippet:
                    unit = StructuralUnit(
                        kind="export",
                        name=snippet,
                        span=_span(node),
                    )
                    key = (unit.kind, unit.span.byte_start, unit.span.byte_end)
                    _append_unique(structure.structural_units, unit, structural_keys, key)

            # P1: call / reference / extends / implements / mixin dependency facts.
            # Dependency collection stops after the defensive cap, but the remaining structure (imports etc.) is extracted as usual.
            if len(structure.structural_units) < _MAX_DEPENDENCY_UNITS:
                for unit in _dependency_units(node, node_type, source):
                    key = (
                        unit.kind,
                        unit.name,
                        unit.span.byte_start,
                        unit.span.byte_end,
                    )
                    _append_unique(structure.structural_units, unit, structural_keys, key)

            # reversed push preserves source order in the extracted collections.
            stack.extend(reversed(node.children))

        _classify_dependency_scope(structure)
        structure.stats["visited_node_count"] = visited
