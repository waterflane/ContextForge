"""Verified declaration extraction for common languages using Tree-sitter."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from tree_sitter import Language, Node, Parser

from contextforge.context import ReaderLimits, read_selected_text_file
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
    analyzer_version="1",
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

    def visit(node: Node, parent_index: int | None) -> None:
        next_parent = parent_index
        kind = _KINDS.get(language_name, {}).get(node.type)
        if kind is not None and not node.has_error:
            name_node = node.child_by_field_name("name") or _find_name_node(node)
            if name_node is not None:
                name = _text(source_bytes, name_node).strip()
                if name:
                    if kind == SymbolKind.FUNCTION and _is_async(node, source_bytes):
                        kind = SymbolKind.ASYNC_FUNCTION
                    next_parent = len(drafts)
                    drafts.append(_Draft(node, name[:500], kind, parent_index))
        for child in node.named_children:
            visit(child, next_parent)

    visit(tree.root_node, None)
    for draft in drafts:
        parent = drafts[draft.parent_index] if draft.parent_index is not None else None
        draft.qualified_name = (
            f"{parent.qualified_name}.{draft.name}" if parent else draft.name
        )
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
        body = draft.node.child_by_field_name("body")
        contained = tuple(
            child.symbol_id
            for child in drafts
            if child.parent_index == index
            and child.kind in {SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
        )
        symbols.append(
            SymbolRecord(
                symbol_id=draft.symbol_id,
                name=draft.name,
                qualified_name=draft.qualified_name,
                kind=draft.kind,
                is_async=draft.kind == SymbolKind.ASYNC_FUNCTION,
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
        parse_status="partial" if diagnostics else "parsed",
        line_count=selected.source_line_count,
        symbols=tuple(symbols),
        diagnostics=diagnostics,
    )


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
        "property_identifier",
        "scoped_identifier",
        "type_identifier",
    }
    stack = list(reversed(node.named_children))
    while stack:
        current = stack.pop()
        if current.type in preferred:
            return current
        stack.extend(reversed(current.named_children))
    return None


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
    value = source[node.start_byte:end].decode("utf-8", errors="strict").strip()
    return " ".join(value.split())[:500] or node.type


def _is_async(node: Node, source: bytes) -> bool:
    prefix = source[node.start_byte : min(node.end_byte, node.start_byte + 80)]
    return b"async" in prefix.split()


def _visibility(
    node: Node, source: bytes
) -> Literal["public", "private", "explicit_export", "unknown"]:
    prefix = source[node.start_byte : min(node.end_byte, node.start_byte + 160)]
    words = set(prefix.decode("utf-8", errors="ignore").replace("(", " ").split())
    if "private" in words or "protected" in words:
        return "private"
    if "public" in words or "export" in words or "pub" in words:
        return "explicit_export"
    return "unknown"


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
