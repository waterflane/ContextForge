"""Conservative syntax references used to review selected declaration ranges."""

from __future__ import annotations

import ast
import builtins
import symtable
from dataclasses import dataclass

from tree_sitter import Node, Parser

from contextforge.intelligence.codemap import FileCodeMap, SymbolRecord
from contextforge.intelligence.polyglot import _language

_JS_BUILTINS = {
    "undefined",
    "NaN",
    "Infinity",
    "console",
    "Math",
    "JSON",
    "Object",
    "Array",
    "String",
    "Number",
    "Boolean",
    "Promise",
    "Set",
    "Map",
    "Date",
    "Error",
    "RegExp",
    "Symbol",
    "BigInt",
    "parseInt",
    "parseFloat",
    "isNaN",
}


@dataclass(frozen=True)
class SymbolDependencies:
    symbol_ids: tuple[str, ...] = ()
    unresolved_names: tuple[str, ...] = ()
    supported: bool = True


def symbol_dependencies(
    source: str,
    code_map: FileCodeMap,
    symbol: SymbolRecord,
) -> SymbolDependencies:
    """Find unique enclosing-scope declarations; ambiguous names stay unresolved."""
    if code_map.language == "Python":
        names = _python_names(source, symbol)
    elif code_map.language in {"JavaScript", "TypeScript"}:
        names = _js_names(source, code_map, symbol)
    else:
        return SymbolDependencies(supported=False)
    if names is None:
        return SymbolDependencies(supported=False)
    owners = {None, symbol.parent_symbol_id}
    by_id = {item.symbol_id: item for item in code_map.symbols}
    parent = by_id.get(symbol.parent_symbol_id or "")
    visited: set[str] = set()
    while parent is not None and parent.symbol_id not in visited:
        visited.add(parent.symbol_id)
        owners.add(parent.parent_symbol_id)
        parent = by_id.get(parent.parent_symbol_id or "")
    found: set[str] = set()
    unresolved: set[str] = set()
    ignored_unresolved = (
        set(dir(builtins))
        if code_map.language == "Python"
        else _JS_BUILTINS
        if code_map.language in {"JavaScript", "TypeScript"}
        else set()
    )
    for name in names:
        candidates = [
            item
            for item in code_map.symbols
            if item.name == name and item.parent_symbol_id in owners
        ]
        # The nearest lexical scope wins over a module-level declaration.
        local = [
            item
            for item in candidates
            if item.parent_symbol_id == symbol.parent_symbol_id
        ]
        if local:
            candidates = local
        if len(candidates) == 1:
            if candidates[0].symbol_id != symbol.symbol_id:
                found.add(candidates[0].symbol_id)
        elif not candidates and name in ignored_unresolved:
            continue
        else:
            unresolved.add(name)
    return SymbolDependencies(tuple(sorted(found)), tuple(sorted(unresolved)))


def _python_names(source: str, symbol: SymbolRecord) -> set[str] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    node: ast.AST | None = next(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and item.name == symbol.name
            and symbol.declaration_range.start_line
            <= item.lineno
            <= symbol.declaration_range.end_line
        ),
        None,
    )
    if node is None:
        node = next(
            (
                item
                for item in ast.walk(tree)
                if isinstance(item, (ast.Assign, ast.AnnAssign))
                and item.lineno == symbol.declaration_range.start_line
            ),
            None,
        )
        if node is None:
            return None
    # Compile the selected declaration in isolation: Python's scope analyzer
    # distinguishes nested locals, closures, globals and comprehensions. Names
    # outside this declaration become globals and are resolved against CodeMap.
    try:
        table = symtable.symtable(ast.unparse(node), "<declaration>", "exec")
    except SyntaxError:
        # For example, a nonlocal declaration needs its enclosing function.
        return None
    names: set[str] = set()
    pending = [table]
    while pending:
        scope = pending.pop()
        names.update(
            item.get_name()
            for item in scope.get_symbols()
            if item.is_global() and item.is_referenced()
        )
        pending.extend(scope.get_children())
    names.update(
        item.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id in {"self", "cls"}
    )
    return names


def _js_names(
    source: str, code_map: FileCodeMap, symbol: SymbolRecord
) -> set[str] | None:
    raw = source.encode("utf-8")
    tree = Parser(_language(code_map.language or "", code_map.path)).parse(raw)
    target: Node | None = None
    stack = [tree.root_node]
    start = (
        symbol.declaration_range.start_line - 1,
        symbol.declaration_range.start_column,
    )
    end = (symbol.declaration_range.end_line - 1, symbol.declaration_range.end_column)
    while stack:
        node = stack.pop()
        if tuple(node.start_point) == start and tuple(node.end_point) == end:
            target = node
            break
        stack.extend(reversed(node.named_children))
    if target is None or target.has_error:
        return None

    def text(node: Node) -> str:
        return raw[node.start_byte : node.end_byte].decode("utf-8")

    def binding_identifiers(node: Node) -> set[str]:
        if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
            return {text(node)}
        if node.type in {"type_annotation", "type_arguments", "type_parameters"}:
            return set()
        if node.type in {"required_parameter", "optional_parameter"}:
            pattern = node.child_by_field_name("pattern")
            return set() if pattern is None else binding_identifiers(pattern)
        if node.type in {"assignment_pattern", "object_assignment_pattern"}:
            left = node.child_by_field_name("left")
            if left is None:
                left = next(iter(node.named_children), None)
            return set() if left is None else binding_identifiers(left)
        if node.type in {"pair_pattern", "pair"}:
            value = node.child_by_field_name("value")
            return set() if value is None else binding_identifiers(value)
        result: set[str] = set()
        for child in node.named_children:
            result.update(binding_identifiers(child))
        return result

    scopes = {
        "statement_block",
        "function_declaration",
        "function_expression",
        "arrow_function",
        "method_definition",
        "catch_clause",
        "class_body",
        "for_statement",
        "for_in_statement",
    }
    functions = {
        "function_declaration",
        "function_expression",
        "arrow_function",
        "method_definition",
    }

    def bindings(node: Node) -> set[str]:
        result: set[str] = set()
        if node.type in {"required_parameter", "optional_parameter"}:
            pattern = node.child_by_field_name("pattern")
            if pattern is not None:
                result.update(binding_identifiers(pattern))
        if node.type == "formal_parameters":
            for child in node.named_children:
                result.update(binding_identifiers(child))
        if node.type in {"arrow_function", "catch_clause"}:
            parameter = node.child_by_field_name("parameter")
            if parameter is not None:
                result.update(binding_identifiers(parameter))
        if node.type in {
            "variable_declarator",
            "function_declaration",
            "class_declaration",
        }:
            name = node.child_by_field_name("name")
            if name is not None:
                result.update(binding_identifiers(name))
        return result

    def function_var_bindings(root: Node) -> set[str]:
        result: set[str] = set()
        pending = list(root.named_children)
        while pending:
            node = pending.pop()
            if node.type in functions:
                continue
            if node.type == "class_body":
                continue
            if node.type == "variable_declarator":
                declaration = node.parent
                if (
                    declaration is not None
                    and declaration.type == "variable_declaration"
                ):
                    name = node.child_by_field_name("name")
                    if name is not None:
                        result.update(binding_identifiers(name))
            pending.extend(node.named_children)
        return result

    def free_names(root: Node, outer: set[str]) -> set[str]:
        local: set[str] = set()
        if root.type in functions:
            local.update(function_var_bindings(root))
        pending = [root]
        while pending:
            node = pending.pop()
            if node != root and node.type in scopes:
                # A nested declaration binds its name, not its parameters/body.
                if node.type == "function_declaration":
                    local.update(bindings(node))
                continue
            local.update(bindings(node))
            pending.extend(node.named_children)
        bound = outer | local
        result: set[str] = set()
        pending = [root]
        while pending:
            node = pending.pop()
            if node.type in {"type_annotation", "type_arguments", "type_parameters"}:
                continue
            if node != root and node.type in scopes:
                result.update(free_names(node, bound))
                continue
            if node.type in {"identifier", "shorthand_property_identifier"}:
                name = text(node)
                if name not in bound:
                    result.add(name)
            if node.type == "member_expression":
                owner = node.child_by_field_name("object")
                prop = node.child_by_field_name("property")
                if owner is not None and owner.type == "this" and prop is not None:
                    result.add(text(prop))
            pending.extend(node.named_children)
        return result

    return free_names(target, {symbol.name})
