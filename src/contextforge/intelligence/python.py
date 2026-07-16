"""Deterministic Python CodeMap extraction using only the standard library."""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Protocol, cast

from contextforge.context import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import (
    CallReference,
    DecoratorRecord,
    ExportRecord,
    FileCodeMap,
    ImportRecord,
    ParameterRecord,
    ParserDiagnostic,
    RelationshipRecord,
    RelationshipTarget,
    SourceRange,
    SymbolKind,
    SymbolRecord,
    stable_fact_id,
)
from contextforge.intelligence.models import AnalyzerIdentity
from contextforge.repositories import ProjectFile, ProjectSnapshot

PYTHON_ANALYZER = AnalyzerIdentity(
    analyzer_id="python-ast",
    analyzer_version="1",
    analysis_prompt_version="none",
    response_schema_version=1,
)
DEFAULT_CODEMAP_SOURCE_LIMIT = 1_000_000

_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_Callable = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(slots=True)
class _SymbolDraft:
    node: ast.stmt
    name: str
    qualified_name: str
    kind: SymbolKind
    parent_index: int | None
    symbol_id: str = ""


class _Positioned(Protocol):
    lineno: int
    col_offset: int
    end_lineno: int | None
    end_col_offset: int | None


class _DirectFactVisitor(ast.NodeVisitor):
    """Collect syntax executed directly in one lexical body, not nested bodies."""

    def __init__(self, source: str) -> None:
        self._source = source
        self.calls: list[CallReference] = []
        self.raises: list[tuple[SourceRange, str]] = []
        self.configuration_keys: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name is not None:
            self.calls.append(
                CallReference(
                    observed_name=name,
                    source_range=_node_range(node.func),
                )
            )
        self._record_call_configuration(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            exception = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = _dotted_name(exception)
            if name is not None:
                self.raises.append((_node_range(exception), name))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _dotted_name(node.value) == "os.environ":
            key = _literal_string(node.slice)
            if key is not None:
                self.configuration_keys.add(key)
        self.generic_visit(node)

    def _record_call_configuration(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name not in {"os.getenv", "os.environ.get"} or not node.args:
            return
        key = _literal_string(node.args[0])
        if key is not None:
            self.configuration_keys.add(key)


def extract_python_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Safely verify and parse one snapshot-owned Python file."""

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
    try:
        module = ast.parse(source, filename=project_file.path, type_comments=True)
    except SyntaxError as exc:
        return FileCodeMap(
            path=project_file.path,
            source_sha256=project_file.sha256,
            source_size_bytes=project_file.size_bytes,
            language=project_file.language,
            analyzer=PYTHON_ANALYZER,
            parse_status="parse_error",
            line_count=selected.source_line_count,
            diagnostics=(_syntax_diagnostic(exc),),
        )

    drafts = _collect_symbol_drafts(module, project_file.path)
    _assign_symbol_ids(drafts, project_file.path)
    symbols = _build_symbols(drafts, source, project_file.path)
    symbols = _resolve_local_calls(symbols, project_file.path)
    imports = _extract_imports(module, source, project_file.path)
    exports = _extract_exports(module, symbols, source, project_file.path)
    relationships = _local_relationships(project_file.path, symbols, imports, exports)
    constants = tuple(
        sorted(
            symbol.name
            for symbol in symbols
            if symbol.parent_symbol_id is None
            and symbol.kind == SymbolKind.VARIABLE
            and symbol.name.isupper()
        )
    )
    return FileCodeMap(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=PYTHON_ANALYZER,
        parse_status="parsed",
        line_count=selected.source_line_count,
        module_docstring=ast.get_docstring(module, clean=False),
        imports=imports,
        exports=exports,
        top_level_constants=constants,
        symbols=symbols,
        relationships=relationships,
    )


def _collect_symbol_drafts(module: ast.Module, path: str) -> list[_SymbolDraft]:
    drafts: list[_SymbolDraft] = []
    module_name = _module_name(path)

    def scan_statements(
        statements: list[ast.stmt],
        *,
        parent_index: int | None,
        parent_qualified: str,
        parent_is_class: bool,
    ) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{parent_qualified}.{statement.name}"
                if parent_is_class:
                    kind = SymbolKind.METHOD
                elif isinstance(statement, ast.AsyncFunctionDef):
                    kind = SymbolKind.ASYNC_FUNCTION
                else:
                    kind = SymbolKind.FUNCTION
                index = len(drafts)
                drafts.append(
                    _SymbolDraft(
                        node=statement,
                        name=statement.name,
                        qualified_name=qualified,
                        kind=kind,
                        parent_index=parent_index,
                    )
                )
                scan_statements(
                    statement.body,
                    parent_index=index,
                    parent_qualified=qualified,
                    parent_is_class=False,
                )
            elif isinstance(statement, ast.ClassDef):
                qualified = f"{parent_qualified}.{statement.name}"
                index = len(drafts)
                drafts.append(
                    _SymbolDraft(
                        node=statement,
                        name=statement.name,
                        qualified_name=qualified,
                        kind=SymbolKind.CLASS,
                        parent_index=parent_index,
                    )
                )
                scan_statements(
                    statement.body,
                    parent_index=index,
                    parent_qualified=qualified,
                    parent_is_class=True,
                )
            elif parent_index is None and isinstance(
                statement, (ast.Assign, ast.AnnAssign, ast.TypeAlias)
            ):
                for name in _assignment_names(statement):
                    kind = (
                        SymbolKind.TYPE_ALIAS
                        if isinstance(statement, ast.TypeAlias)
                        else SymbolKind.VARIABLE
                    )
                    drafts.append(
                        _SymbolDraft(
                            node=statement,
                            name=name,
                            qualified_name=f"{parent_qualified}.{name}",
                            kind=kind,
                            parent_index=None,
                        )
                    )
            else:
                for child_statements in _nested_statement_lists(statement):
                    scan_statements(
                        child_statements,
                        parent_index=parent_index,
                        parent_qualified=parent_qualified,
                        parent_is_class=parent_is_class,
                    )

    scan_statements(
        module.body,
        parent_index=None,
        parent_qualified=module_name,
        parent_is_class=False,
    )
    return sorted(
        drafts,
        key=lambda item: (
            item.node.lineno,
            item.node.col_offset,
            item.qualified_name,
            item.kind.value,
        ),
    )


def _assign_symbol_ids(drafts: list[_SymbolDraft], path: str) -> None:
    ordinals: Counter[tuple[str, str]] = Counter()
    for draft in drafts:
        key = (draft.kind.value, draft.qualified_name)
        ordinals[key] += 1
        draft.symbol_id = stable_fact_id(
            "symbol",
            "Python",
            path,
            draft.kind.value,
            draft.qualified_name,
            ordinals[key],
        )


def _build_symbols(
    drafts: list[_SymbolDraft], source: str, path: str
) -> tuple[SymbolRecord, ...]:
    result: list[SymbolRecord] = []
    for index, draft in enumerate(drafts):
        node = draft.node
        parent_id = (
            drafts[draft.parent_index].symbol_id
            if draft.parent_index is not None
            else None
        )
        direct = _direct_facts(node, source)
        decorators = (
            tuple(
                DecoratorRecord(
                    expression=_source_segment(source, decorator),
                    source_range=_node_range(decorator),
                )
                for decorator in node.decorator_list
            )
            if isinstance(node, _Definition)
            else ()
        )
        contained_methods = tuple(
            sorted(
                child.symbol_id
                for child in drafts
                if child.parent_index == index and child.kind == SymbolKind.METHOD
            )
        )
        callable_node = node if isinstance(node, _Callable) else None
        class_node = node if isinstance(node, ast.ClassDef) else None
        result.append(
            SymbolRecord(
                symbol_id=draft.symbol_id,
                name=draft.name,
                qualified_name=draft.qualified_name,
                kind=draft.kind,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                signature=(
                    _declaration_signature(source, node)
                    if isinstance(node, _Definition)
                    else _source_segment(source, node)
                ),
                declaration_range=_node_range(node),
                body_range=_body_range(node),
                parent_symbol_id=parent_id,
                docstring=(
                    ast.get_docstring(node, clean=False)
                    if isinstance(node, _Definition)
                    else None
                ),
                decorators=decorators,
                parameters=(
                    _parameters(callable_node, source)
                    if callable_node is not None
                    else ()
                ),
                return_annotation=(
                    _optional_segment(source, callable_node.returns)
                    if callable_node is not None
                    else None
                ),
                base_classes=(
                    tuple(_source_segment(source, base) for base in class_node.bases)
                    if class_node is not None
                    else ()
                ),
                contained_methods=contained_methods,
                direct_calls=tuple(direct.calls),
                raised_exceptions=tuple(name for _, name in direct.raises),
                configuration_keys=tuple(sorted(direct.configuration_keys)),
                visibility="private" if draft.name.startswith("_") else "public",
            )
        )
    return tuple(result)


def _direct_facts(node: ast.AST, source: str) -> _DirectFactVisitor:
    visitor = _DirectFactVisitor(source)
    body = getattr(node, "body", ())
    if isinstance(body, list):
        for statement in body:
            visitor.visit(statement)
    visitor.calls.sort(
        key=lambda call: (
            call.source_range.start_line,
            call.source_range.start_column,
            call.observed_name,
        )
    )
    visitor.raises.sort(key=lambda item: (*_range_tuple(item[0]), item[1]))
    return visitor


def _resolve_local_calls(
    symbols: tuple[SymbolRecord, ...], path: str
) -> tuple[SymbolRecord, ...]:
    by_name: dict[str, list[SymbolRecord]] = {}
    for symbol in symbols:
        by_name.setdefault(symbol.name, []).append(symbol)
    result: list[SymbolRecord] = []
    for symbol in symbols:
        calls: list[CallReference] = []
        for call in symbol.direct_calls:
            candidates = (
                by_name.get(call.observed_name, [])
                if "." not in call.observed_name
                else []
            )
            contained = [
                item for item in candidates if item.parent_symbol_id == symbol.symbol_id
            ]
            siblings = (
                [
                    item
                    for item in candidates
                    if item.parent_symbol_id == symbol.parent_symbol_id
                ]
                if symbol.kind != SymbolKind.METHOD
                else []
            )
            module_level = [
                item for item in candidates if item.parent_symbol_id is None
            ]
            if len(contained) == 1:
                selected = contained
            elif contained:
                selected = []
            elif len(siblings) == 1:
                selected = siblings
            elif siblings:
                selected = []
            else:
                selected = module_level
            if len(selected) == 1:
                target = selected[0]
                calls.append(
                    call.model_copy(
                        update={
                            "resolution": "internal",
                            "target_symbol_id": target.symbol_id,
                            "target_file_path": path,
                            "detection_method": "python_lexical_name",
                        }
                    )
                )
            else:
                calls.append(call)
        result.append(symbol.model_copy(update={"direct_calls": tuple(calls)}))
    return tuple(result)


def _extract_imports(
    module: ast.Module, source: str, path: str
) -> tuple[ImportRecord, ...]:
    records: list[ImportRecord] = []
    nodes = sorted(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in nodes:
        observed = _source_segment(source, node)
        for ordinal, alias in enumerate(node.names, start=1):
            module_name = alias.name if isinstance(node, ast.Import) else node.module
            imported_name = None if isinstance(node, ast.Import) else alias.name
            level = 0 if isinstance(node, ast.Import) else node.level
            records.append(
                ImportRecord(
                    import_id=stable_fact_id(
                        "import",
                        path,
                        node.lineno,
                        node.col_offset,
                        ordinal,
                        module_name,
                        imported_name,
                        alias.asname,
                    ),
                    module=module_name,
                    imported_name=imported_name,
                    alias=alias.asname,
                    level=level,
                    observed_text=observed,
                    source_range=_node_range(node),
                )
            )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                *_range_tuple(item.source_range),
                item.module or "",
                item.imported_name or "",
                item.alias or "",
            ),
        )
    )


def _extract_exports(
    module: ast.Module,
    symbols: tuple[SymbolRecord, ...],
    source: str,
    path: str,
) -> tuple[ExportRecord, ...]:
    records: list[ExportRecord] = []
    symbols_by_name: dict[str, list[SymbolRecord]] = {}
    for symbol in symbols:
        if symbol.parent_symbol_id is None:
            symbols_by_name.setdefault(symbol.name, []).append(symbol)
            if not symbol.name.startswith("_"):
                records.append(
                    ExportRecord(
                        export_id=stable_fact_id(
                            "export",
                            path,
                            "conventional",
                            symbol.name,
                            symbol.symbol_id,
                        ),
                        name=symbol.name,
                        kind="conventional",
                        source_range=symbol.declaration_range,
                        target_symbol_id=symbol.symbol_id,
                    )
                )
    for statement in module.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value: ast.expr | None
        if isinstance(statement, ast.Assign):
            target = statement.targets[0]
            value = statement.value
        else:
            target = statement.target
            value = statement.value
        if not isinstance(target, ast.Name) or target.id != "__all__":
            continue
        names = _static_string_collection(value)
        if names is None:
            continue
        for ordinal, name in enumerate(names, start=1):
            candidates = symbols_by_name.get(name, [])
            records.append(
                ExportRecord(
                    export_id=stable_fact_id(
                        "export", path, "explicit", statement.lineno, ordinal, name
                    ),
                    name=name,
                    kind="explicit",
                    source_range=_node_range(statement),
                    target_symbol_id=(
                        candidates[0].symbol_id if len(candidates) == 1 else None
                    ),
                )
            )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                *_range_tuple(item.source_range),
                item.kind,
                item.name,
                item.export_id,
            ),
        )
    )


def _local_relationships(
    path: str,
    symbols: tuple[SymbolRecord, ...],
    imports: tuple[ImportRecord, ...],
    exports: tuple[ExportRecord, ...],
) -> tuple[RelationshipRecord, ...]:
    relationships: list[RelationshipRecord] = []
    for item in imports:
        module_name = "." * item.level + (item.module or "")
        target_name = item.imported_name or item.module
        relationships.append(
            _relationship(
                kind="import",
                path=path,
                source_symbol_id=_containing_symbol_id(symbols, item.source_range),
                source_range=item.source_range,
                observed_text=item.observed_text,
                target=RelationshipTarget(
                    resolution="unresolved",
                    module_name=module_name,
                    observed_name=target_name,
                ),
                method="python_ast_import",
            )
        )
    for symbol in symbols:
        if symbol.parent_symbol_id is not None:
            relationships.append(
                _relationship(
                    kind="contains",
                    path=path,
                    source_symbol_id=symbol.parent_symbol_id,
                    source_range=symbol.declaration_range,
                    observed_text=symbol.name,
                    target=RelationshipTarget(
                        resolution="internal",
                        file_path=path,
                        symbol_id=symbol.symbol_id,
                    ),
                    method="python_lexical_parent",
                )
            )
        for call in symbol.direct_calls:
            relationships.append(
                _relationship(
                    kind="call",
                    path=path,
                    source_symbol_id=symbol.symbol_id,
                    source_range=call.source_range,
                    observed_text=call.observed_name,
                    target=RelationshipTarget(
                        resolution=call.resolution,
                        file_path=call.target_file_path,
                        symbol_id=call.target_symbol_id,
                        observed_name=call.observed_name,
                    ),
                    method=call.detection_method,
                )
            )
    for export in exports:
        relationships.append(
            _relationship(
                kind="export",
                path=path,
                source_symbol_id=None,
                source_range=export.source_range,
                observed_text=export.name,
                target=RelationshipTarget(
                    resolution="internal"
                    if export.target_symbol_id is not None
                    else "unresolved",
                    file_path=path if export.target_symbol_id is not None else None,
                    symbol_id=export.target_symbol_id,
                    observed_name=export.name,
                ),
                method=f"python_{export.kind}_export",
            )
        )
    return tuple(
        sorted(
            relationships,
            key=lambda item: (
                *_range_tuple(item.source_range),
                item.kind,
                item.relationship_id,
            ),
        )
    )


def _relationship(
    *,
    kind: Literal[
        "import", "contains", "call", "export", "tests", "tested_by", "test_reference"
    ],
    path: str,
    source_symbol_id: str | None,
    source_range: SourceRange,
    observed_text: str,
    target: RelationshipTarget,
    method: str,
) -> RelationshipRecord:
    relationship_id = stable_fact_id(
        "relationship",
        kind,
        path,
        source_symbol_id,
        _range_tuple(source_range),
        target.model_dump(mode="json"),
        method,
    )
    return RelationshipRecord(
        relationship_id=relationship_id,
        kind=kind,
        source_file_path=path,
        source_symbol_id=source_symbol_id,
        source_range=source_range,
        observed_text=observed_text,
        target=target,
        detection_method=method,
    )


def _parameters(node: _Callable, source: str) -> tuple[ParameterRecord, ...]:
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(arguments.defaults)
    ) + list(arguments.defaults)
    records: list[ParameterRecord] = []
    for index, (argument, default) in enumerate(zip(positional, defaults, strict=True)):
        records.append(
            ParameterRecord(
                name=argument.arg,
                kind="positional_only"
                if index < len(arguments.posonlyargs)
                else "positional_or_keyword",
                annotation=_optional_segment(source, argument.annotation),
                default=_optional_segment(source, default),
            )
        )
    if arguments.vararg is not None:
        records.append(
            ParameterRecord(
                name=arguments.vararg.arg,
                kind="var_positional",
                annotation=_optional_segment(source, arguments.vararg.annotation),
            )
        )
    for argument, default in zip(
        arguments.kwonlyargs, arguments.kw_defaults, strict=True
    ):
        records.append(
            ParameterRecord(
                name=argument.arg,
                kind="keyword_only",
                annotation=_optional_segment(source, argument.annotation),
                default=_optional_segment(source, default),
            )
        )
    if arguments.kwarg is not None:
        records.append(
            ParameterRecord(
                name=arguments.kwarg.arg,
                kind="var_keyword",
                annotation=_optional_segment(source, arguments.kwarg.annotation),
            )
        )
    return tuple(records)


def _declaration_signature(source: str, node: _Definition) -> str:
    tokens = tuple(tokenize.generate_tokens(io.StringIO(source).readline))
    expected = (
        "async"
        if isinstance(node, ast.AsyncFunctionDef)
        else ("class" if isinstance(node, ast.ClassDef) else "def")
    )
    start_index = next(
        index
        for index, token in enumerate(tokens)
        if token.start[0] == node.lineno and token.string == expected
    )
    depth = 0
    for token in tokens[start_index:]:
        if token.type == tokenize.OP and token.string in "([{":
            depth += 1
        elif token.type == tokenize.OP and token.string in ")]}":
            depth -= 1
        elif token.type == tokenize.OP and token.string == ":" and depth == 0:
            return _slice_source(source, tokens[start_index].start, token.end)
    raise ValueError("parsed declaration has no header terminator")


def _body_range(node: ast.AST) -> SourceRange | None:
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body:
        return None
    return SourceRange(
        start_line=body[0].lineno,
        start_column=body[0].col_offset,
        end_line=body[-1].end_lineno or body[-1].lineno,
        end_column=body[-1].end_col_offset or body[-1].col_offset,
    )


def _node_range(node: _Positioned) -> SourceRange:
    lineno = node.lineno
    column = node.col_offset
    end_lineno = node.end_lineno
    end_column = node.end_col_offset
    return SourceRange(
        start_line=lineno,
        start_column=column,
        end_line=end_lineno or lineno,
        end_column=end_column if end_column is not None else column,
    )


def _syntax_diagnostic(error: SyntaxError) -> ParserDiagnostic:
    line = max(error.lineno or 1, 1)
    column = max((error.offset or 1) - 1, 0)
    end_line = max(error.end_lineno or line, line)
    end_column = max((error.end_offset or error.offset or 1) - 1, 0)
    if (end_line, end_column) < (line, column):
        end_line, end_column = line, column
    return ParserDiagnostic(
        code="python_syntax_error",
        message=error.msg,
        severity="error",
        range=SourceRange(
            start_line=line,
            start_column=column,
            end_line=end_line,
            end_column=end_column,
        ),
    )


def _source_segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return segment if segment is not None else ""


def _optional_segment(source: str, node: ast.AST | None) -> str | None:
    return None if node is None else _source_segment(source, node)


def _slice_source(source: str, start: tuple[int, int], end: tuple[int, int]) -> str:
    lines = source.splitlines(keepends=True)
    if start[0] == end[0]:
        return lines[start[0] - 1][start[1] : end[1]]
    pieces = [lines[start[0] - 1][start[1] :]]
    pieces.extend(lines[start[0] : end[0] - 1])
    pieces.append(lines[end[0] - 1][: end[1]])
    return "".join(pieces)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _literal_string(node: ast.AST) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _static_string_collection(node: ast.AST | None) -> tuple[str, ...] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_collection(node.left)
        right = _static_string_collection(node.right)
        return None if left is None or right is None else (*left, *right)
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    values = tuple(_literal_string(item) for item in node.elts)
    return (
        None
        if any(value is None for value in values)
        else cast(tuple[str, ...], values)
    )


def _assignment_names(
    node: ast.Assign | ast.AnnAssign | ast.TypeAlias,
) -> tuple[str, ...]:
    targets: list[ast.expr]
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    else:
        targets = [node.name if isinstance(node, ast.TypeAlias) else node.target]
    names: list[str] = []
    for target in targets:
        names.extend(_target_names(target))
    return tuple(names)


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _target_names(item)]
    return []


def _nested_statement_lists(node: ast.AST) -> tuple[list[ast.stmt], ...]:
    values: list[list[ast.stmt]] = []
    for _name, value in ast.iter_fields(node):
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, ast.stmt) for item in value)
        ):
            values.append(cast(list[ast.stmt], value))
        elif isinstance(value, ast.AST) and not isinstance(
            value, (ast.expr, ast.pattern)
        ):
            values.extend(_nested_statement_lists(value))
    return tuple(values)


def _containing_symbol_id(
    symbols: tuple[SymbolRecord, ...], source_range: SourceRange
) -> str | None:
    containing = [
        symbol
        for symbol in symbols
        if _range_contains(symbol.declaration_range, source_range)
    ]
    if not containing:
        return None
    return max(
        containing,
        key=lambda item: (
            item.declaration_range.start_line,
            item.declaration_range.start_column,
        ),
    ).symbol_id


def _range_contains(outer: SourceRange, inner: SourceRange) -> bool:
    return (outer.start_line, outer.start_column) <= (
        inner.start_line,
        inner.start_column,
    ) and (inner.end_line, inner.end_column) <= (outer.end_line, outer.end_column)


def _range_tuple(value: SourceRange) -> tuple[int, int, int, int]:
    return (value.start_line, value.start_column, value.end_line, value.end_column)


def _module_name(path: str) -> str:
    pure = ".".join(PurePosixPath(path).with_suffix("").parts)
    if pure.endswith(".__init__"):
        pure = pure[: -len(".__init__")]
    return pure or "__init__"


__all__ = ["DEFAULT_CODEMAP_SOURCE_LIMIT", "PYTHON_ANALYZER", "extract_python_code_map"]
