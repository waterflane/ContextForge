"""Verified declaration extraction for common languages using Tree-sitter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from tree_sitter import Language, Node, Parser

from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import (
    CallReference,
    FileCodeMap,
    ImportRecord,
    ParserDiagnostic,
    ReferenceOccurrence,
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
    analyzer_version="6",
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
    imports = _extract_imports(source, project_file.path, language_name)
    symbols = list(
        _attach_occurrences(tree.root_node, tuple(symbols), imports, source_bytes)
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
        imports=imports,
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


_IMPORT_ANCESTORS = {
    "import_declaration",
    "import_spec",
    "import_statement",
    "include_directive",
    "namespace_use_clause",
    "namespace_use_declaration",
    "preproc_include",
    "require_relative",
    "use_declaration",
    "using_directive",
}
_CALL_NODES = {
    "call",
    "call_expression",
    "function_call_expression",
    "invocation_expression",
    "member_call_expression",
    "method_invocation",
    "scoped_call_expression",
}
_IDENTIFIER_NODES = {
    "constant",
    "field_identifier",
    "identifier",
    "name",
    "namespace_identifier",
    "property_identifier",
    "scoped_identifier",
    "type_identifier",
}


def _extract_imports(source: str, path: str, language: str) -> tuple[ImportRecord, ...]:
    values: list[ImportRecord] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for module, imported, alias, observed, start, end in _import_specs(
            line, language
        ):
            source_range = SourceRange(
                start_line=line_number,
                start_column=start,
                end_line=line_number,
                end_column=end,
            )
            values.append(
                ImportRecord(
                    import_id=stable_fact_id(
                        "import",
                        path,
                        module,
                        imported,
                        alias,
                        line_number,
                        start,
                    ),
                    module=module,
                    imported_name=imported,
                    alias=alias,
                    observed_text=observed[:1_000],
                    source_range=source_range,
                )
            )
    unique = {item.import_id: item for item in values}
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.source_range.start_line,
                item.source_range.start_column,
                item.module or "",
                item.imported_name or "",
                item.alias or "",
            ),
        )
    )


def _import_specs(
    line: str, language: str
) -> tuple[tuple[str, str | None, str | None, str, int, int], ...]:
    stripped = line.strip()
    results: list[tuple[str, str | None, str | None, str, int, int]] = []

    def add(
        module: str,
        imported: str | None = None,
        alias: str | None = None,
        observed: str | None = None,
    ) -> None:
        clean = module.strip().strip("\"'")
        if not clean:
            return
        start = max(line.find(module), 0)
        results.append((clean, imported, alias, observed or stripped, start, len(line)))

    if language in {"JavaScript", "TypeScript"}:
        from_match = re.search(
            r"\b(?:import|export)\s+(.+?)\s+from\s+['\"]([^'\"]+)['\"]",
            line,
        )
        if from_match:
            bindings, module = from_match.groups()
            named = re.search(r"\{([^}]*)\}", bindings)
            if named:
                for item in named.group(1).split(","):
                    parts = re.split(r"\s+as\s+", item.strip())
                    if parts and parts[0]:
                        add(
                            module,
                            parts[0],
                            parts[1] if len(parts) == 2 else None,
                        )
            else:
                star = re.search(r"\*\s+as\s+(\w+)", bindings)
                add(module, alias=star.group(1) if star else None)
        require_matches = tuple(
            re.finditer(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]", line)
        )
        for require_match in require_matches:
            add(require_match.group(1))
        if from_match is None and not require_matches:
            side_effect = re.search(r"\bimport\s*['\"]([^'\"]+)['\"]", line)
            if side_effect:
                add(side_effect.group(1))
    elif language == "Go":
        match = re.search(r"(?:^|\s)([A-Za-z_]\w*\s+)?['\"]([^'\"]+)['\"]", line)
        if match and (stripped.startswith("import") or stripped.startswith(('"', "'"))):
            add(match.group(2), alias=(match.group(1) or "").strip() or None)
    elif language == "Rust":
        match = re.search(r"\b(?:use|mod)\s+([A-Za-z_][\w:]*)", line)
        if match:
            parts = match.group(1).split("::")
            add(
                "::".join(parts[:-1]) or parts[0], parts[-1] if len(parts) > 1 else None
            )
    elif language in {"Java", "C#"}:
        keyword = "import" if language == "Java" else "using"
        match = re.search(rf"\b{keyword}\s+(?:static\s+)?([A-Za-z_][\w.]*)", line)
        if match:
            parts = match.group(1).split(".")
            add(".".join(parts[:-1]) or parts[0], parts[-1] if len(parts) > 1 else None)
    elif language in {"C", "C++"}:
        match = re.search(r"#\s*include\s*([<\"])([^>\"]+)[>\"]", line)
        if match:
            add(match.group(2))
    elif language == "PHP":
        match = re.search(r"\buse\s+([A-Za-z_\\][\w\\]*)", line)
        if match:
            parts = match.group(1).split("\\")
            add(
                "\\".join(parts[:-1]) or parts[0], parts[-1] if len(parts) > 1 else None
            )
        for match in re.finditer(
            r"\b(?:require|require_once|include|include_once)\s*\(?\s*['\"]([^'\"]+)",
            line,
        ):
            add(match.group(1))
    elif language == "Ruby":
        match = re.search(r"\brequire(_relative)?\s*\(?\s*['\"]([^'\"]+)", line)
        if match:
            module = ("./" if match.group(1) else "") + match.group(2)
            add(module)
    return tuple(results)


def _attach_occurrences(
    root: Node,
    symbols: tuple[SymbolRecord, ...],
    imports: tuple[ImportRecord, ...],
    source: bytes,
) -> tuple[SymbolRecord, ...]:
    del imports
    calls: dict[str, list[CallReference]] = {item.symbol_id: [] for item in symbols}
    references: dict[str, list[ReferenceOccurrence]] = {
        item.symbol_id: [] for item in symbols
    }
    declaration_ranges = {
        (
            item.declaration_range.start_line,
            item.declaration_range.start_column,
        )
        for item in symbols
    }
    call_target_ranges: list[SourceRange] = []

    def owner(node: Node) -> SymbolRecord | None:
        region = _range(node)
        candidates = [
            item
            for item in symbols
            if _contains_range(item.body_range or item.declaration_range, region)
        ]
        return min(
            candidates,
            key=lambda item: (
                (item.body_range or item.declaration_range).end_line
                - (item.body_range or item.declaration_range).start_line,
                item.qualified_name,
            ),
            default=None,
        )

    def in_import(node: Node) -> bool:
        current: Node | None = node
        while current is not None:
            if current.type in _IMPORT_ANCESTORS:
                return True
            current = current.parent
        return False

    def visit_calls(node: Node) -> None:
        if node.type in _CALL_NODES and not node.has_error:
            target = (
                node.child_by_field_name("function")
                or node.child_by_field_name("name")
                or node.child_by_field_name("method")
                or next(iter(node.named_children), None)
            )
            selected_owner = owner(node)
            if target is not None and selected_owner is not None:
                observed = _text(source, target).strip()
                if observed and len(observed) <= 500:
                    region = _range(target)
                    call_target_ranges.append(region)
                    calls[selected_owner.symbol_id].append(
                        CallReference(
                            observed_name=observed,
                            source_range=region,
                            detection_method="polyglot_ast_call",
                        )
                    )
        for child in node.named_children:
            visit_calls(child)

    def visit_references(node: Node) -> None:
        if (
            node.type in _IDENTIFIER_NODES
            and not node.has_error
            and not in_import(node)
        ):
            selected_owner = owner(node)
            region = _range(node)
            if (
                selected_owner is not None
                and not any(
                    target.start_line <= region.start_line
                    and region.end_line <= target.end_line
                    and (
                        target.start_line != region.start_line
                        or target.start_column <= region.start_column
                    )
                    and (
                        target.end_line != region.end_line
                        or region.end_column <= target.end_column
                    )
                    for target in call_target_ranges
                )
                and (region.start_line, region.start_column) not in declaration_ranges
                and not _is_declaration_name(node)
            ):
                observed = _text(source, node).strip()
                if observed and len(observed) <= 500:
                    references[selected_owner.symbol_id].append(
                        ReferenceOccurrence(
                            observed_name=observed,
                            source_range=region,
                            detection_method="polyglot_ast_reference",
                        )
                    )
        for child in node.named_children:
            visit_references(child)

    visit_calls(root)
    visit_references(root)
    return tuple(
        item.model_copy(
            update={
                "direct_calls": tuple(
                    sorted(
                        {
                            (
                                call.source_range.start_line,
                                call.source_range.start_column,
                                call.observed_name,
                            ): call
                            for call in calls[item.symbol_id]
                        }.values(),
                        key=lambda call: (
                            call.source_range.start_line,
                            call.source_range.start_column,
                            call.observed_name,
                        ),
                    )
                ),
                "direct_references": tuple(
                    sorted(
                        {
                            (
                                reference.source_range.start_line,
                                reference.source_range.start_column,
                                reference.observed_name,
                            ): reference
                            for reference in references[item.symbol_id]
                        }.values(),
                        key=lambda reference: (
                            reference.source_range.start_line,
                            reference.source_range.start_column,
                            reference.observed_name,
                        ),
                    )
                ),
            }
        )
        for item in symbols
    )


def _is_declaration_name(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    return parent.child_by_field_name("name") == node and parent.type not in {
        "attribute",
        "field_expression",
        "member_access_expression",
        "member_expression",
        "qualified_name",
        "scoped_identifier",
    }


def _contains_range(container: SourceRange, nested: SourceRange) -> bool:
    start = (container.start_line, container.start_column)
    end = (container.end_line, container.end_column)
    nested_start = (nested.start_line, nested.start_column)
    nested_end = (nested.end_line, nested.end_column)
    return start <= nested_start and nested_end <= end


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
