"""Verified declaration extraction for common languages using Tree-sitter."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from tree_sitter import Language, Node, Parser

from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import (
    FileCodeMap,
    ParserDiagnostic,
    SourceRange,
    SymbolKind,
    SymbolRecord,
    stable_fact_id,
)
from contextforge.intelligence.models import AnalyzerIdentity
from contextforge.intelligence.python import DEFAULT_CODEMAP_SOURCE_LIMIT
from contextforge.repositories import ProjectFile, ProjectSnapshot

POLYGLOT_ANALYZER = AnalyzerIdentity(
    analyzer_id="tree-sitter-polyglot",
    analyzer_version="5",
    analysis_prompt_version="none",
    response_schema_version=1,
)

SUPPORTED_POLYGLOT_LANGUAGES = (
    "C",
    "C#",
    "C++",
    "Go",
    "Java",
    "JavaScript",
    "PHP",
    "Ruby",
    "Rust",
    "TypeScript",
)

_GRAMMARS: dict[str, tuple[str, str]] = {
    "C": ("tree_sitter_c", "language"),
    "C#": ("tree_sitter_c_sharp", "language"),
    "C++": ("tree_sitter_cpp", "language"),
    "Go": ("tree_sitter_go", "language"),
    "Java": ("tree_sitter_java", "language"),
    "JavaScript": ("tree_sitter_javascript", "language"),
    "PHP": ("tree_sitter_php", "language_php"),
    "Ruby": ("tree_sitter_ruby", "language"),
    "Rust": ("tree_sitter_rust", "language"),
    "TypeScript": ("tree_sitter_typescript", "language_typescript"),
}

_KINDS: dict[str, dict[str, SymbolKind]] = {
    "JavaScript": {
        "class_declaration": SymbolKind.CLASS,
        "function_declaration": SymbolKind.FUNCTION,
        "generator_function_declaration": SymbolKind.FUNCTION,
        "method_definition": SymbolKind.METHOD,
    },
    "TypeScript": {
        "abstract_class_declaration": SymbolKind.CLASS,
        "class_declaration": SymbolKind.CLASS,
        "enum_declaration": SymbolKind.ENUM,
        "function_declaration": SymbolKind.FUNCTION,
        "generator_function_declaration": SymbolKind.FUNCTION,
        "interface_declaration": SymbolKind.INTERFACE,
        "internal_module": SymbolKind.NAMESPACE,
        "function_signature": SymbolKind.FUNCTION,
        "method_definition": SymbolKind.METHOD,
        "method_signature": SymbolKind.METHOD,
        "type_alias_declaration": SymbolKind.TYPE_ALIAS,
    },
    "Java": {
        "class_declaration": SymbolKind.CLASS,
        "constructor_declaration": SymbolKind.CONSTRUCTOR,
        "enum_declaration": SymbolKind.ENUM,
        "interface_declaration": SymbolKind.INTERFACE,
        "method_declaration": SymbolKind.METHOD,
        "record_declaration": SymbolKind.STRUCT,
    },
    "C#": {
        "class_declaration": SymbolKind.CLASS,
        "constructor_declaration": SymbolKind.CONSTRUCTOR,
        "enum_declaration": SymbolKind.ENUM,
        "interface_declaration": SymbolKind.INTERFACE,
        "local_function_statement": SymbolKind.FUNCTION,
        "method_declaration": SymbolKind.METHOD,
        "namespace_declaration": SymbolKind.NAMESPACE,
        "file_scoped_namespace_declaration": SymbolKind.NAMESPACE,
        "record_declaration": SymbolKind.STRUCT,
        "struct_declaration": SymbolKind.STRUCT,
    },
    "Go": {
        "function_declaration": SymbolKind.FUNCTION,
        "method_declaration": SymbolKind.METHOD,
        "type_spec": SymbolKind.TYPE_ALIAS,
    },
    "Rust": {
        "enum_item": SymbolKind.ENUM,
        "function_item": SymbolKind.FUNCTION,
        "function_signature_item": SymbolKind.METHOD,
        "mod_item": SymbolKind.NAMESPACE,
        "struct_item": SymbolKind.STRUCT,
        "trait_item": SymbolKind.TRAIT,
        "type_item": SymbolKind.TYPE_ALIAS,
    },
    "C": {
        "enum_specifier": SymbolKind.ENUM,
        "function_definition": SymbolKind.FUNCTION,
        "struct_specifier": SymbolKind.STRUCT,
        "type_definition": SymbolKind.TYPE_ALIAS,
    },
    "C++": {
        "class_specifier": SymbolKind.CLASS,
        "enum_specifier": SymbolKind.ENUM,
        "function_definition": SymbolKind.FUNCTION,
        "namespace_definition": SymbolKind.NAMESPACE,
        "struct_specifier": SymbolKind.STRUCT,
        "type_definition": SymbolKind.TYPE_ALIAS,
    },
    "PHP": {
        "class_declaration": SymbolKind.CLASS,
        "enum_declaration": SymbolKind.ENUM,
        "function_definition": SymbolKind.FUNCTION,
        "interface_declaration": SymbolKind.INTERFACE,
        "method_declaration": SymbolKind.METHOD,
        "trait_declaration": SymbolKind.TRAIT,
    },
    "Ruby": {
        "class": SymbolKind.CLASS,
        "method": SymbolKind.METHOD,
        "module": SymbolKind.NAMESPACE,
        "singleton_method": SymbolKind.METHOD,
    },
}


@dataclass(slots=True)
class _Draft:
    node: Node
    name: str
    kind: SymbolKind
    parent_index: int | None
    symbol_id: str = ""
    qualified_name: str = ""
    callable_node: Node | None = None


def extract_polyglot_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Parse one supported source file and retain only syntax-backed declarations."""

    selected = read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(
            max_files=1,
            max_source_bytes=max_source_bytes,
            max_content_bytes=max_source_bytes,
        ),
    )
    source = selected.blocks[0].text
    source_bytes = source.encode("utf-8")
    language_name = project_file.language or ""
    parser = Parser(_language(language_name, project_file.path))
    tree = parser.parse(source_bytes)
    diagnostics = _diagnostics(tree.root_node)
    drafts: list[_Draft] = []
    omitted: list[ParserDiagnostic] = []

    def visit(node: Node, parent_index: int | None) -> None:
        next_parent = parent_index
        declaration_node: Node | None = None
        prototype_name: Node | None = None
        verified = not node.has_error
        ancestor = node.parent
        while verified and ancestor is not None:
            verified = not ancestor.is_error and not ancestor.is_missing
            ancestor = ancestor.parent
        kind = _KINDS.get(language_name, {}).get(node.type)
        if language_name in {"C", "C++"} and node.type == "function_declarator":
            prototype_name, declaration_node = _c_prototype(node)
            if prototype_name is not None and declaration_node is not None:
                kind = SymbolKind.FUNCTION
        if node.type in {"class_specifier", "struct_specifier", "enum_specifier"} and (
            node.child_by_field_name("body") is None
            and (
                node.parent is None
                or node.parent.type != "declaration"
                or node.parent.child_by_field_name("declarator") is not None
            )
        ):
            kind = None  # A type reference is not another declaration.
        if language_name == "Go" and node.type == "type_spec":
            target_type = node.child_by_field_name("type")
            if target_type is not None:
                kind = {
                    "struct_type": SymbolKind.STRUCT,
                    "interface_type": SymbolKind.INTERFACE,
                }.get(target_type.type, kind)
        binding = _binding(node, language_name, source_bytes)
        if binding is not None and verified:
            binding_name, kind, callable_node = binding
            next_parent = len(drafts)
            drafts.append(
                _Draft(
                    node,
                    _text(source_bytes, binding_name)[:500],
                    kind,
                    parent_index,
                    callable_node=callable_node,
                )
            )
            # The named binding owns its callable; do not create a second symbol
            # for a named function-expression initializer.
            children = (
                callable_node.named_children
                if callable_node is not None
                else node.named_children
            )
            for child in children:
                visit(child, next_parent)
            return
        if kind is not None and verified:
            name_node = (
                prototype_name
                or node.child_by_field_name("name")
                or _find_name_node(node)
            )
            if name_node is not None:
                name = _text(source_bytes, name_node).strip()
                if name:
                    if kind == SymbolKind.FUNCTION and _is_async(node, source_bytes):
                        kind = SymbolKind.ASYNC_FUNCTION
                    next_parent = len(drafts)
                    drafts.append(
                        _Draft(
                            declaration_node or node,
                            name[:500],
                            kind,
                            parent_index,
                            callable_node=(
                                node if declaration_node is not None else None
                            ),
                        )
                    )
            elif len(omitted) < 20:
                omitted.append(
                    ParserDiagnostic(
                        code="unsupported_declaration",
                        severity="warning",
                        message=f"No verified name extracted for {node.type}",
                        range=_range(node),
                    )
                )
        for child in node.named_children:
            visit(child, next_parent)
            if child.type == "file_scoped_namespace_declaration":
                next_parent = next(
                    (i for i, draft in enumerate(drafts) if draft.node == child),
                    next_parent,
                )

    visit(tree.root_node, None)
    for draft in drafts:
        owner_name = _method_owner(draft.node, language_name, source_bytes)
        if owner_name is not None:
            owners = [
                index
                for index, item in enumerate(drafts)
                if item.name == owner_name
                and item.kind
                in {
                    SymbolKind.CLASS,
                    SymbolKind.STRUCT,
                    SymbolKind.TYPE_ALIAS,
                    SymbolKind.ENUM,
                }
            ]
            if len(owners) == 1:
                draft.parent_index = owners[0]
                draft.kind = SymbolKind.METHOD

    # Receiver/impl ownership can refer to a type declared later in the file.
    def qualified_name(index: int) -> str:
        draft = drafts[index]
        if draft.parent_index is None:
            return draft.name
        return qualified_name(draft.parent_index) + "." + draft.name

    for index, draft in enumerate(drafts):
        draft.qualified_name = qualified_name(index)
    for draft in drafts:
        parent = drafts[draft.parent_index] if draft.parent_index is not None else None
        if (
            draft.kind == SymbolKind.METHOD
            and (
                (
                    language_name in {"JavaScript", "TypeScript"}
                    and draft.name == "constructor"
                )
                or (language_name == "PHP" and draft.name == "__construct")
                or (language_name == "Ruby" and draft.name == "initialize")
            )
        ) or (
            language_name == "C++"
            and draft.kind == SymbolKind.FUNCTION
            and parent is not None
            and parent.kind in {SymbolKind.CLASS, SymbolKind.STRUCT}
            and draft.name == parent.name
        ):
            draft.kind = SymbolKind.CONSTRUCTOR
        elif (
            language_name in {"C++", "Rust"}
            and parent is not None
            and parent.kind in {SymbolKind.CLASS, SymbolKind.STRUCT, SymbolKind.TRAIT}
            and draft.kind in {SymbolKind.FUNCTION, SymbolKind.ASYNC_FUNCTION}
        ):
            draft.kind = SymbolKind.METHOD
        source_range = _range(draft.node)
        draft.symbol_id = stable_fact_id(
            "symbol",
            project_file.path,
            draft.qualified_name,
            draft.kind.value,
            source_range.start_line,
            source_range.start_column,
        )

    symbols: list[SymbolRecord] = []
    for index, draft in enumerate(drafts):
        callable_node = draft.callable_node or draft.node
        body = callable_node.child_by_field_name("body")
        contained = tuple(
            child.symbol_id
            for child in drafts
            if child.parent_index == index
            and child.kind in {SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
            # An anonymous object returned inside a callable has lexical
            # ancestry, but does not turn that callable into a method owner.
            and draft.kind
            not in {
                SymbolKind.FUNCTION,
                SymbolKind.ASYNC_FUNCTION,
                SymbolKind.METHOD,
                SymbolKind.CONSTRUCTOR,
            }
        )
        symbols.append(
            SymbolRecord(
                symbol_id=draft.symbol_id,
                name=draft.name,
                qualified_name=draft.qualified_name,
                kind=draft.kind,
                is_async=(
                    draft.kind in {SymbolKind.ASYNC_FUNCTION, SymbolKind.METHOD}
                    and _is_async(callable_node, source_bytes)
                ),
                signature=_signature(source_bytes, draft.node, body),
                declaration_range=_range(draft.node),
                body_range=None if body is None else _range(body),
                parent_symbol_id=(
                    None
                    if draft.parent_index is None
                    else drafts[draft.parent_index].symbol_id
                ),
                contained_methods=tuple(sorted(contained)),
                visibility=_visibility(draft.node, source_bytes),
            )
        )
    return FileCodeMap(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=POLYGLOT_ANALYZER,
        parse_status="partial" if diagnostics or omitted else "parsed",
        line_count=selected.source_line_count,
        module_has_executable_code=_module_has_executable_code(
            tree.root_node, language_name
        ),
        symbols=tuple(symbols),
        diagnostics=tuple(
            sorted(
                (*diagnostics, *omitted),
                key=lambda item: (
                    item.range.start_line if item.range else 0,
                    item.range.start_column if item.range else 0,
                    item.code,
                ),
            )
        ),
    )


def _module_has_executable_code(root: Node, language: str) -> bool:
    if language not in {"JavaScript", "TypeScript"}:
        return False
    allowed = {"comment", "empty_statement", "export_statement", "import_statement"}
    return any(child.type not in allowed for child in root.named_children)


def _binding(
    node: Node,
    language: str,
    source: bytes,
) -> tuple[Node, SymbolKind, Node | None] | None:
    if language in {"JavaScript", "TypeScript"} and node.type in {
        "variable_declarator",
        "public_field_definition",
        "field_definition",
    }:
        name = node.child_by_field_name("name")
        if name is None or name.type not in {"identifier", "property_identifier"}:
            return None
        value = node.child_by_field_name("value")
        while value is not None and value.type == "parenthesized_expression":
            value = next(iter(value.named_children), None)
        if value is not None and value.type in {
            "arrow_function",
            "function_expression",
            "generator_function",
        }:
            kind = (
                SymbolKind.ASYNC_FUNCTION
                if _is_async(value, source)
                else SymbolKind.FUNCTION
            )
            if node.type != "variable_declarator":
                kind = SymbolKind.METHOD
            return name, kind, value
        declaration = node.parent
        constant = declaration is not None and _text(
            source, declaration
        ).lstrip().startswith("const ")
        return name, SymbolKind.CONSTANT if constant else SymbolKind.VARIABLE, None
    if language == "Rust" and node.type in {"const_item", "static_item"}:
        name = node.child_by_field_name("name")
        if name is not None:
            return (
                name,
                SymbolKind.CONSTANT
                if node.type == "const_item"
                else SymbolKind.VARIABLE,
                None,
            )
    if language in {"Java", "C#"} and node.type == "variable_declarator":
        name = node.child_by_field_name("name")
        if name is not None:
            declaration = node.parent
            if declaration is not None and declaration.type == "variable_declaration":
                declaration = declaration.parent
            header = (
                b""
                if declaration is None
                else source[declaration.start_byte : node.start_byte]
            )
            constant = (
                b"final" in header.split()
                if language == "Java"
                else b"const" in header.split()
            )
            return name, SymbolKind.CONSTANT if constant else SymbolKind.VARIABLE, None
    if language == "Go" and node.type in {"var_spec", "const_spec"}:
        name = node.child_by_field_name("name")
        if name is not None:
            return (
                name,
                SymbolKind.CONSTANT
                if node.type == "const_spec"
                else SymbolKind.VARIABLE,
                None,
            )
    if language in {"C", "C++"}:
        name = (
            node.child_by_field_name("declarator")
            if node.type == "init_declarator"
            else (
                node
                if node.type in {"identifier", "field_identifier"}
                and node.parent is not None
                and node.parent.type in {"declaration", "field_declaration"}
                else None
            )
        )
        if name is not None and name.type in {"identifier", "field_identifier"}:
            declaration = node.parent
            header = (
                b""
                if declaration is None
                else source[declaration.start_byte : node.start_byte]
            )
            return (
                name,
                SymbolKind.CONSTANT
                if b"const" in header.split()
                else SymbolKind.VARIABLE,
                None,
            )
    if language == "PHP" and node.type in {"const_element", "property_element"}:
        name = node.child_by_field_name("name") or next(iter(node.named_children), None)
        if name is not None:
            return (
                name,
                SymbolKind.CONSTANT
                if node.type == "const_element"
                else SymbolKind.VARIABLE,
                None,
            )
    if language == "Ruby" and node.type == "assignment":
        name = node.child_by_field_name("left")
        if name is not None and name.type in {"constant", "identifier"}:
            return (
                name,
                SymbolKind.CONSTANT if name.type == "constant" else SymbolKind.VARIABLE,
                None,
            )
    return None


def _method_owner(node: Node, language: str, source: bytes) -> str | None:
    if language == "Go" and node.type == "method_declaration":
        receiver = node.child_by_field_name("receiver")
        if receiver is not None:
            stack = [receiver]
            while stack:
                current = stack.pop()
                if current.type == "type_identifier":
                    return _text(source, current)
                stack.extend(reversed(current.named_children))
    if language == "Rust" and node.type == "function_item":
        parent = node.parent
        if (
            parent is not None
            and parent.parent is not None
            and parent.parent.type == "impl_item"
        ):
            owner = parent.parent.child_by_field_name("type")
            while owner is not None and owner.type == "generic_type":
                owner = owner.child_by_field_name("type")
            if owner is not None and owner.type == "type_identifier":
                return _text(source, owner)
    return None


def _language(language_name: str, path: str) -> Language:
    module_name, function_name = _GRAMMARS[language_name]
    if language_name == "TypeScript" and path.casefold().endswith(".tsx"):
        function_name = "language_tsx"
    capsule = getattr(import_module(module_name), function_name)()
    return Language(capsule)


def _find_name_node(node: Node) -> Node | None:
    preferred = {
        "constant",
        "constant_path",
        "field_identifier",
        "identifier",
        "namespace_identifier",
        "operator_name",
        "destructor_name",
        "property_identifier",
        "scoped_identifier",
        "qualified_identifier",
        "type_identifier",
    }
    current = node.child_by_field_name("declarator")
    while current is not None:
        if current.type in preferred:
            return current
        current = current.child_by_field_name("declarator")
    return None


def _c_prototype(node: Node) -> tuple[Node | None, Node | None]:
    """Return a declaration-owned function name without promoting function pointers."""

    name = node.child_by_field_name("declarator")
    if name is None or name.type not in {
        "identifier",
        "field_identifier",
        "qualified_identifier",
    }:
        return None, None
    ancestor = node.parent
    wrappers = {
        "array_declarator",
        "attributed_declarator",
        "function_declarator",
        "parenthesized_declarator",
        "pointer_declarator",
        "reference_declarator",
    }
    while ancestor is not None and ancestor.type in wrappers:
        ancestor = ancestor.parent
    if ancestor is None or ancestor.type not in {"declaration", "field_declaration"}:
        return None, None
    return name, ancestor


def _range(node: Node) -> SourceRange:
    return SourceRange(
        start_line=node.start_point.row + 1,
        start_column=node.start_point.column,
        end_line=node.end_point.row + 1,
        end_column=node.end_point.column,
    )


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="strict")


def _signature(source: bytes, node: Node, body: Node | None) -> str:
    end = body.start_byte if body is not None else node.end_byte
    value = source[node.start_byte : end].decode("utf-8", errors="strict").strip()
    return " ".join(value.split())[:500] or node.type


def _is_async(node: Node, source: bytes) -> bool:
    return "async" in _modifier_words(node, source)


def _visibility(
    node: Node, source: bytes
) -> Literal["public", "private", "explicit_export", "unknown"]:
    words = _modifier_words(node, source)
    if "private" in words or "protected" in words:
        return "private"
    if "export" in words or "pub" in words:
        return "explicit_export"
    if "public" in words:
        return "public"
    return "unknown"


def _modifier_words(node: Node, source: bytes) -> set[str]:
    words: set[str] = set()
    allowed = {"async", "export", "private", "protected", "pub", "public"}
    containers = {
        "accessibility_modifier",
        "modifier",
        "modifiers",
        "visibility_modifier",
    }
    if node.parent is not None and node.parent.type in {
        "export_statement",
        "export_declaration",
    }:
        words.add("export")
    for child in node.children:
        value = _text(source, child).strip()
        if value in allowed:
            words.add(value)
        if child.type not in containers:
            continue
        pending = [child]
        while pending:
            modifier = pending.pop()
            value = _text(source, modifier).strip()
            if value in allowed:
                words.add(value)
            pending.extend(modifier.children)
    return words


def _diagnostics(root: Node) -> tuple[ParserDiagnostic, ...]:
    result: list[ParserDiagnostic] = []
    stack = [root]
    while stack and len(result) < 20:
        node = stack.pop()
        if node.is_error or node.is_missing:
            result.append(
                ParserDiagnostic(
                    code="tree_sitter_parse_error",
                    message=f"Tree-sitter reported {node.type!r} syntax",
                    severity="error",
                    range=_range(node),
                )
            )
        stack.extend(reversed(node.named_children))
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.range.start_line if item.range else 0,
                item.range.start_column if item.range else 0,
            ),
        )
    )


__all__ = [
    "POLYGLOT_ANALYZER",
    "SUPPORTED_POLYGLOT_LANGUAGES",
    "extract_polyglot_code_map",
]
