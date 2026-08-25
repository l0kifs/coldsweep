"""Syntax resolution, keyed by file extension.

Two consumers need the same thing from a source file: `verify` needs the text of the symbol an
anchor names, and the mutation subsystem needs the byte spans worth mutating. Both used to get
it from ``ast``, which meant both silently degraded to whole-file behaviour outside Python --
`verify` to a repo-wide idiom search that reopens fixed findings, and mutation to not running
at all.

Python keeps the stdlib ``ast`` path unchanged: it is the reference implementation and the one
with measurements behind it. Every other language goes through tree-sitter, and a language
whose grammar is not installed resolves to ``None`` rather than to a guess -- the same "cannot
tell" answer the callers already handle, reported by name so it is visible rather than silent.

Adding a language is a table entry plus a test with a real sample. Do not add one from the
grammar's documentation alone: node type names differ between grammars in ways that only
parsing a file reveals (C# exposes a unary operator as an anonymous token, TypeScript exposes
it as an ``operator`` field), and a wrong entry produces confidently empty results.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from functools import cache


class GrammarError(RuntimeError):
    """Raised only when a grammar is present but unusable. A missing grammar is not an error."""


@dataclass(frozen=True)
class Language:  # pylint: disable=too-many-instance-attributes
    """One language's parse configuration.

    ``symbol_nodes`` contribute a segment to an anchor path; every other node is descended
    through without contributing one. That is deliberate for wrappers a human would not write
    into an anchor -- a C# namespace, a Rust ``mod``, a TypeScript ``export`` -- because the
    anchor has to match what a scan agent writes, and an agent writes ``Repo::Count``, never
    ``App.Core::Repo::Count``.
    """

    name: str
    module: str
    factory: str
    symbol_nodes: frozenset[str]
    # Binary operator tokens live in an ``operator`` field in every grammar checked so far.
    binary_nodes: frozenset[str] = frozenset()
    # Unary negation: some grammars field it, some leave it an anonymous first child.
    unary_nodes: frozenset[str] = frozenset()
    bool_nodes: frozenset[str] = frozenset()
    int_nodes: frozenset[str] = frozenset()
    string_nodes: frozenset[str] = frozenset()
    true_token: str = "true"
    false_token: str = "false"
    # Node types that carry a bare name, for declarations with no ``name`` field at all. A Rust
    # `impl` block is the case: it names the type it implements and nothing else.
    name_node_types: frozenset[str] = frozenset()
    # A field whose type name becomes a leading segment. Go needs it: `func (r *Repo) Count()`
    # and `func (s *Store) Count()` both have the name `Count`, so without the receiver they
    # derive the same anchor, the same id, and one of the two findings is silently lost.
    qualifier_field: str | None = None
    # Subtrees stepped over when resolving a declaration's own name: a body, whose first type
    # mention is something the declaration uses rather than what it is called, and a generic
    # parameter list, which precedes the real name in `impl<T> Box<T>`.
    body_nodes: frozenset[str] = frozenset()
    # Content that makes the file fail to load, for the "do the tests touch this at all" probe.
    sentinel: bytes = b"coldsweep harness sentinel\n"
    extensions: tuple[str, ...] = ()


PYTHON = Language(
    name="python",
    module="",
    factory="",
    symbol_nodes=frozenset(),
    sentinel=b'raise ImportError("coldsweep harness sentinel")\n',
    extensions=(".py",),
)

C_SHARP = Language(
    name="c_sharp",
    module="tree_sitter_c_sharp",
    factory="language",
    symbol_nodes=frozenset({
        "class_declaration", "struct_declaration", "interface_declaration", "record_declaration",
        "record_struct_declaration", "enum_declaration", "method_declaration",
        "constructor_declaration", "destructor_declaration", "property_declaration",
        "local_function_statement", "operator_declaration", "indexer_declaration",
    }),
    binary_nodes=frozenset({"binary_expression"}),
    unary_nodes=frozenset({"prefix_unary_expression"}),
    bool_nodes=frozenset({"boolean_literal"}),
    int_nodes=frozenset({"integer_literal"}),
    string_nodes=frozenset({"string_literal", "verbatim_string_literal",
                            "interpolated_string_expression", "raw_string_literal"}),
    sentinel=b"#error coldsweep harness sentinel\n",
    extensions=(".cs",),
)

_TS_SYMBOLS = frozenset({
    "class_declaration", "abstract_class_declaration", "interface_declaration",
    "enum_declaration", "function_declaration", "generator_function_declaration",
    "method_definition", "abstract_method_signature", "method_signature",
})

TYPESCRIPT = Language(
    name="typescript",
    module="tree_sitter_typescript",
    factory="language_typescript",
    symbol_nodes=_TS_SYMBOLS,
    binary_nodes=frozenset({"binary_expression"}),
    unary_nodes=frozenset({"unary_expression"}),
    bool_nodes=frozenset({"true", "false"}),
    int_nodes=frozenset({"number"}),
    string_nodes=frozenset({"string", "template_string"}),
    sentinel=b"export const coldsweepHarnessSentinel: number = ;\n",
    extensions=(".ts", ".mts", ".cts", ".js", ".mjs", ".cjs"),
)

TSX = Language(
    name="tsx",
    module="tree_sitter_typescript",
    factory="language_tsx",
    symbol_nodes=_TS_SYMBOLS,
    binary_nodes=TYPESCRIPT.binary_nodes,
    unary_nodes=TYPESCRIPT.unary_nodes,
    bool_nodes=TYPESCRIPT.bool_nodes,
    int_nodes=TYPESCRIPT.int_nodes,
    string_nodes=TYPESCRIPT.string_nodes,
    sentinel=TYPESCRIPT.sentinel,
    extensions=(".tsx", ".jsx"),
)

GO = Language(
    name="go",
    module="tree_sitter_go",
    factory="language",
    # `type_spec`, not `type_declaration`: the declaration is the `type (...)` group, and only
    # the spec inside it carries a name. A grouped declaration holds several.
    symbol_nodes=frozenset({"function_declaration", "method_declaration", "type_spec"}),
    binary_nodes=frozenset({"binary_expression"}),
    unary_nodes=frozenset({"unary_expression"}),
    bool_nodes=frozenset({"true", "false"}),
    int_nodes=frozenset({"int_literal"}),
    string_nodes=frozenset({"interpreted_string_literal", "raw_string_literal"}),
    name_node_types=frozenset({"type_identifier"}),
    qualifier_field="receiver",
    body_nodes=frozenset({"block"}),
    sentinel=b"!!! coldsweep harness sentinel !!!\n",
    extensions=(".go",),
)

RUST = Language(
    name="rust",
    module="tree_sitter_rust",
    factory="language",
    # `impl_item` is a symbol node despite having no name of its own: it supplies the type
    # segment that keeps `Repo::count` and `Store::count` apart. `mod_item` is deliberately
    # absent -- a module is a wrapper an agent does not write into an anchor.
    symbol_nodes=frozenset({
        "function_item", "struct_item", "enum_item", "trait_item", "union_item", "impl_item",
    }),
    binary_nodes=frozenset({"binary_expression"}),
    unary_nodes=frozenset({"unary_expression"}),
    bool_nodes=frozenset({"boolean_literal"}),
    int_nodes=frozenset({"integer_literal"}),
    string_nodes=frozenset({"string_literal", "raw_string_literal"}),
    name_node_types=frozenset({"type_identifier"}),
    body_nodes=frozenset({"declaration_list", "block", "type_parameters"}),
    # A real macro for exactly this, rather than a syntax error that only happens to fail.
    sentinel=b'compile_error!("coldsweep harness sentinel");\n',
    extensions=(".rs",),
)

JAVA = Language(
    name="java",
    module="tree_sitter_java",
    factory="language",
    symbol_nodes=frozenset({
        "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
        "annotation_type_declaration", "method_declaration", "constructor_declaration",
    }),
    binary_nodes=frozenset({"binary_expression"}),
    unary_nodes=frozenset({"unary_expression"}),
    bool_nodes=frozenset({"true", "false"}),
    int_nodes=frozenset({"decimal_integer_literal", "hex_integer_literal"}),
    string_nodes=frozenset({"string_literal"}),
    body_nodes=frozenset({"class_body", "block", "interface_body", "enum_body"}),
    sentinel=b"!!! coldsweep harness sentinel !!!\n",
    extensions=(".java",),
)

LANGUAGES: tuple[Language, ...] = (PYTHON, C_SHARP, TYPESCRIPT, TSX, GO, RUST, JAVA)

BY_EXTENSION: dict[str, Language] = {ext: lang for lang in LANGUAGES for ext in lang.extensions}


def language_for(file: str) -> Language | None:
    """The language configured for a path's extension, or ``None`` when none is."""
    dot = file.rfind(".")
    return BY_EXTENSION.get(file[dot:].lower()) if dot >= 0 else None


@cache
def _parser(language: Language):
    """The parser for one language, or ``None`` when its grammar is not installed.

    Cached because building a ``Language`` from a grammar pointer is not free and every file in
    a scope repeats it. A missing grammar caches as ``None`` too: it will not appear mid-run.
    """
    try:
        import tree_sitter  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    try:
        grammar = importlib.import_module(language.module)
    except ImportError:
        return None
    try:
        return tree_sitter.Parser(tree_sitter.Language(getattr(grammar, language.factory)()))
    except (AttributeError, TypeError, ValueError) as exc:
        # The grammar is installed but does not expose what the table claims. That is a broken
        # table entry or an incompatible grammar version, not a missing optional dependency, and
        # silently treating it as absent would hide a resolver that should be working.
        raise GrammarError(
            f"{language.module}.{language.factory}() is not a usable tree-sitter language: {exc}"
        ) from exc


def resolves(file: str) -> bool:
    """Whether this path's symbols can be located at all. Drives the reason a verify defers."""
    language = language_for(file)
    if language is None:
        return False
    return language is PYTHON or _parser(language) is not None


def support() -> list[tuple[str, str, bool]]:
    """``(language, extensions, available)`` for every configured language, for reporting."""
    return [(lang.name, " ".join(lang.extensions),
             lang is PYTHON or _parser(lang) is not None) for lang in LANGUAGES]


# --- symbol ranges ---------------------------------------------------------

def _python_ranges(source: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    ranges: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                path = [*stack, child.name]
                ranges.append((child.lineno, child.end_lineno or child.lineno, "::".join(path)))
                walk(child, path)
            else:
                walk(child, stack)

    walk(tree, [])
    return ranges


def _first_named(node, types: frozenset[str], skip: frozenset[str], source: bytes) -> str | None:
    """The first descendant of one of ``types``, in document order, skipping opaque subtrees.

    ``skip`` subtrees are stepped over rather than aborting the search. Two things have to be
    stepped over: a body, whose first type mention is something the declaration *uses* rather
    than what it is called, and a generic parameter list, which in `impl<T> Box<T>` precedes the
    real name and would otherwise resolve the whole block to ``T``.
    """
    for child in node.children:
        if child.type in skip:
            continue
        if child.type in types:
            return source[child.start_byte:child.end_byte].decode("utf-8", "replace").strip() or None
        found = _first_named(child, types, skip, source)
        if found is not None:
            return found
    return None


def _node_name(node, source: bytes, language: Language) -> str | None:
    """The anchor segment a declaration contributes, or ``None`` when it contributes none.

    A qualified name (``App.Core``) is taken whole rather than split: it only ever appears on
    wrapper nodes that contribute no anchor segment, and splitting it would invent segments an
    agent never writes.

    Two shapes need more than a ``name`` field. A Rust `impl` block has no name at all -- it
    names the type it implements -- and a Go method's name is only unique within its receiver.
    Both resolve to a segment here rather than being dropped, because dropping either collides
    two different symbols onto one anchor, and an anchor collision is an *identity* collision:
    the second finding derives the id of the first and merge silently absorbs it.
    """
    named = node.child_by_field_name("name")
    text = (source[named.start_byte:named.end_byte].decode("utf-8", "replace").strip()
            if named is not None else None)
    if not text:
        return _first_named(node, language.name_node_types, language.body_nodes, source)
    if language.qualifier_field:
        receiver = node.child_by_field_name(language.qualifier_field)
        owner = (_first_named(receiver, language.name_node_types, language.body_nodes, source)
                 if receiver is not None else None)
        if owner:
            return f"{owner}::{text}"
    return text


def _tree_ranges(source: str, language: Language) -> list[tuple[int, int, str]]:
    parser = _parser(language)
    if parser is None:
        return []
    body = source.encode("utf-8")
    root = parser.parse(body).root_node
    ranges: list[tuple[int, int, str]] = []

    def walk(node, stack: list[str]) -> None:
        for child in node.children:
            path = stack
            if child.type in language.symbol_nodes:
                name = _node_name(child, body, language)
                if name is not None:
                    path = [*stack, name]
                    ranges.append((child.start_point[0] + 1, child.end_point[0] + 1, "::".join(path)))
            walk(child, path)

    walk(root, [])
    return ranges


def symbol_ranges(source: str, file: str = "") -> list[tuple[int, int, str]]:
    """Line ranges of every named symbol, outermost first, for anchoring.

    Empty for a file whose extension names no configured language, whose grammar is not
    installed, or which no longer parses. All three mean "cannot tell", which is what every
    caller already does with an anchor it fails to locate.

    An unrecognised extension resolves to nothing rather than to Python. A marker in prose is
    not code, and parsing it as Python would anchor a finding to a symbol the file does not
    have. Only a caller that passes no ``file`` at all is taken to be holding Python source.
    """
    language = language_for(file) if file else PYTHON
    if language is None:
        return []
    if language is PYTHON:
        return _python_ranges(source)
    return _tree_ranges(source, language)


def symbol_text(source: str, anchor: str, file: str = "") -> str | None:
    """The source of the symbol an anchor names, or ``None`` when it cannot be located.

    ``None`` is the answer for a module-level anchor, a renamed or deleted symbol, a file whose
    grammar is missing, and a file that no longer parses. Every caller treats it as "cannot
    tell" rather than as an empty symbol, because those cases are indistinguishable here and
    none of them is evidence.

    One path can name several ranges -- a Rust ``struct Repo`` and its ``impl Repo``, a C#
    partial class, a merged TypeScript declaration -- and all of them are returned, joined. The
    caller searches this text for an offending snippet and treats its absence as proof of a fix,
    so returning only the first range would report the snippet gone whenever it lives in one of
    the others. That is a silent loss of a real work item; a snippet found in a sibling range
    costs one more round instead.
    """
    path = anchor.split("::", 1)[1] if "::" in anchor else ""
    if not path:
        return None
    lines = source.splitlines()
    bodies = ["\n".join(lines[start - 1:end])
              for start, end, symbol in symbol_ranges(source, file or anchor.split("::", 1)[0])
              if symbol == path]
    return "\n".join(bodies) if bodies else None


def anchor_for(file: str, source: str, lineno: int) -> str:
    """The innermost symbol containing a line, as a stable anchor. Never a line number."""
    best_start, best_path = -1, ""
    for start, end, path in symbol_ranges(source, file):
        if best_start < start <= lineno <= end:
            best_start, best_path = start, path
    return f"{file}::{best_path}" if best_path else file


# --- mutation sites --------------------------------------------------------

# Mutations that hold a type constant on both sides. Everything here rewrites an operator or a
# literal into another of the same type, so a statically typed language still compiles.
COMPARISON = {
    "==": "!=", "!=": "==", "===": "!==", "!==": "===",
    "<=": ">", ">=": "<", "<": ">=", ">": "<=",
}
ARITHMETIC = {"+": "-", "-": "+", "*": "/", "/": "*", "%": "*"}
BOOLEAN = {"&&": "||", "||": "&&", "and": "or", "or": "and", "&": "|", "|": "&"}


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _is_stringish(node, language: Language) -> bool:
    return node is not None and node.type in language.string_nodes


def mutation_sites(source: bytes, file: str, operators: set[str]) -> list[tuple[str, str, str, int, int]]:
    """``(anchor_path, operator, replacement, start, end)`` for every mutation worth making.

    ``anchor_path`` is the symbol path *within* the file; the caller prefixes the file. Byte
    offsets, so the caller splices without re-parsing.

    Only type-preserving mutations are produced. A mutation that changes an expression's type --
    the classic "return null" -- does not compile in a statically typed language, and a mutant
    that fails to build exits non-zero exactly like a test failure, so it would be recorded as
    *killed* and the symbol would be reported as tested when nothing tested it. Under-reporting
    a finding is the failure this tool exists to prevent, so the operator is not offered rather
    than offered with a caveat.
    """
    language = language_for(file)
    if language is None or language is PYTHON:
        return []
    parser = _parser(language)
    if parser is None:
        return []
    root = parser.parse(source).root_node
    found: list[tuple[str, str, str, int, int]] = []

    def record(operator: str, replacement: str, start: int, end: int, stack: list[str]) -> None:
        found.append(("::".join(stack), operator, replacement, start, end))

    def binary(node, stack: list[str]) -> None:
        token = node.child_by_field_name("operator")
        if token is None:
            return
        text = _text(token, source)
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if text in COMPARISON and "comparison" in operators:
            record("comparison", COMPARISON[text], token.start_byte, token.end_byte, stack)
        elif text in BOOLEAN and "boolean" in operators:
            record("boolean", BOOLEAN[text], token.start_byte, token.end_byte, stack)
        elif text in ARITHMETIC and "arithmetic" in operators:
            # `+` over strings is concatenation, and `-` over them does not typecheck. Skipping
            # the pair is cheaper than being wrong about it, and costs one operator on one node.
            if text in ("+", "-") and (_is_stringish(left, language) or _is_stringish(right, language)):
                return
            record("arithmetic", ARITHMETIC[text], token.start_byte, token.end_byte, stack)

    def unary(node, stack: list[str]) -> None:
        """Drop a boolean negation. Bool in, bool out, so the expression still typechecks."""
        token = node.child_by_field_name("operator")
        if token is None:
            token = node.children[0] if node.children else None
        if token is None or _text(token, source) != "!":
            return
        record("unary", "", token.start_byte, token.end_byte, stack)

    def literal(node, stack: list[str]) -> None:
        text = _text(node, source)
        if node.type in language.bool_nodes or text in (language.true_token, language.false_token):
            flipped = language.false_token if text == language.true_token else language.true_token
            if text in (language.true_token, language.false_token):
                record("constant", flipped, node.start_byte, node.end_byte, stack)
            return
        if node.type in language.int_nodes:
            try:
                value = int(text, 0)
            except ValueError:
                return  # a float, or a suffixed literal; not worth guessing a same-type successor
            record("constant", str(value + 1), node.start_byte, node.end_byte, stack)

    def walk(node, stack: list[str]) -> None:
        for child in node.children:
            path = stack
            if child.type in language.symbol_nodes:
                name = _node_name(child, source, language)
                if name is not None:
                    path = [*stack, name]
            if child.type in language.binary_nodes:
                binary(child, path)
            elif child.type in language.unary_nodes and "unary" in operators:
                unary(child, path)
            elif "constant" in operators:
                literal(child, path)
            walk(child, path)

    walk(root, [])
    return found
