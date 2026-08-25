"""Symbol resolution and mutation sites, per language.

Every language here is checked against a real sample rather than against the grammar's
documentation. Node type names differ between grammars in ways only parsing reveals, and a
wrong table entry fails by returning nothing, which reads exactly like a clean file.
"""

from __future__ import annotations

import pytest

from coldsweep import syntax

CSHARP = """namespace App.Core {
  public class Repo {
    public int Count(string s) {
      if (s.Length == 0 || s == null) { return 1 + 2; }
      return s.Length * 3;
    }
    public bool Ok() { return !Flag; }
  }
  public interface IThing { void Go(); }
}
"""

TYPESCRIPT = """export class Repo {
  count(s: string): number {
    if (s.length === 0 && s != null) { return 1 + 2; }
    return s.length * 3;
  }
}
export function top(a: number) { return !a; }
"""

OPERATORS = {"comparison", "arithmetic", "boolean", "constant", "unary"}


def paths_of(source: str, file: str) -> list[str]:
    return [path for _, _, path in syntax.symbol_ranges(source, file)]


# --- symbol resolution -----------------------------------------------------

def test_python_still_resolves_through_ast():
    """The reference implementation is unchanged; every measurement was taken against it."""
    source = "class A:\n    def m(self):\n        pass\n"
    assert paths_of(source, "m.py") == ["A", "A::m"]
    assert syntax.symbol_text(source, "m.py::A::m") == "    def m(self):\n        pass"


def test_csharp_resolves_classes_and_methods():
    assert paths_of(CSHARP, "Repo.cs") == ["Repo", "Repo::Count", "Repo::Ok", "IThing", "IThing::Go"]


def test_a_namespace_contributes_no_anchor_segment():
    """An agent writes `Repo::Count`, never `App.Core::Repo::Count`, so neither may the resolver.

    An anchor that disagrees with what the scan agent produces re-derives a different id, which
    surfaces every fixed finding as a brand new one for as long as the run lasts.
    """
    assert all(not path.startswith("App") for path in paths_of(CSHARP, "Repo.cs"))


def test_typescript_resolves_through_the_export_wrapper():
    assert paths_of(TYPESCRIPT, "repo.ts") == ["Repo", "Repo::count", "top"]


def test_symbol_text_returns_the_symbol_not_the_file():
    body = syntax.symbol_text(CSHARP, "Repo.cs::Repo::Ok")
    assert body is not None
    assert "Ok()" in body and "Count" not in body


def test_symbol_text_takes_its_language_from_the_anchor():
    """`verify` passes only source and anchor; the extension in the anchor has to be enough."""
    assert syntax.symbol_text(TYPESCRIPT, "repo.ts::top") is not None


def test_anchor_for_finds_the_innermost_symbol():
    assert syntax.anchor_for("Repo.cs", CSHARP, 4) == "Repo.cs::Repo::Count"


@pytest.mark.parametrize("file", ["notes.md", "script.rb", "Makefile"])
def test_an_unconfigured_extension_resolves_to_nothing(file: str):
    """Prose is not code. Parsing it anyway anchors findings to symbols the file has no idea about."""
    assert syntax.symbol_ranges("def looks_like_code():\n    pass\n", file) == []
    assert not syntax.resolves(file)


def test_a_file_that_no_longer_parses_is_cannot_tell_not_empty():
    assert syntax.symbol_ranges("public class {{{", "Broken.cs") == []
    assert syntax.symbol_text("public class {{{", "Broken.cs::Repo") is None


def test_support_reports_every_configured_language():
    names = {name for name, _, _ in syntax.support()}
    assert {"python", "c_sharp", "typescript"} <= names
    assert dict((name, ok) for name, _, ok in syntax.support())["python"] is True


# --- mutation sites --------------------------------------------------------

def sites(source: str, file: str) -> list[tuple[str, str, str]]:
    return [(anchor, op, repl)
            for anchor, op, repl, _, _ in syntax.mutation_sites(source.encode(), file, OPERATORS)]


def test_csharp_operators_are_mutated_within_their_anchor():
    found = sites(CSHARP, "Repo.cs")
    assert ("Repo::Count", "comparison", "!=") in found
    assert ("Repo::Count", "boolean", "&&") in found
    assert ("Repo::Count", "arithmetic", "-") in found
    assert ("Repo::Ok", "unary", "") in found


def test_typescript_strict_equality_maps_to_its_own_negation():
    """`===` must become `!==`, not `!=`: the loose form is a different operator, not a mutation."""
    assert ("Repo::count", "comparison", "!==") in sites(TYPESCRIPT, "repo.ts")


def test_no_mutation_changes_an_expressions_type():
    """A `return null` mutant does not compile, and a build failure exits like a test failure.

    It would then be recorded as *killed*, and the symbol reported as tested when nothing tested
    it -- the tool under-reporting a finding, which is the failure it exists to prevent.
    """
    for source, file in ((CSHARP, "Repo.cs"), (TYPESCRIPT, "repo.ts")):
        assert not [s for s in sites(source, file) if s[1] == "return"]
        assert not [s for s in sites(source, file) if s[2] in ("null", "None", "nil")]


def test_string_concatenation_is_not_mutated_into_subtraction():
    source = 'class A { public string J() { return "a" + "b"; } public int N() { return 1 + 2; } }'
    found = sites(source, "A.cs")
    assert ("A::N", "arithmetic", "-") in found
    assert not [s for s in found if s[0] == "A::J" and s[1] == "arithmetic"]


def test_a_boolean_literal_flips_to_the_other_boolean():
    found = sites("class A { public bool M() { return true; } }", "A.cs")
    assert ("A::M", "constant", "false") in found


def test_operators_not_configured_produce_no_sites():
    assert syntax.mutation_sites(CSHARP.encode(), "Repo.cs", {"comparison"}) != []
    assert syntax.mutation_sites(CSHARP.encode(), "Repo.cs", set()) == []


def test_python_yields_no_tree_sitter_sites():
    """Python mutants come from `_Collector`; producing them twice would double every mutant."""
    assert syntax.mutation_sites(b"x = 1 + 2\n", "m.py", OPERATORS) == []


def test_sites_are_byte_spans_over_the_original_source():
    body = CSHARP.encode()
    for _, _, replacement, start, end in syntax.mutation_sites(body, "Repo.cs", OPERATORS):
        assert body[start:end].decode() != replacement


# --- Go, Rust, Java --------------------------------------------------------

GO = """package app

type Repo struct { n int }

func (r *Repo) Count(s string) int {
	if len(s) == 0 || s == "" { return 1 + 2 }
	return len(s) * 3
}

func (s *Store) Count(x string) int { return 4 }

func Top(a bool) bool { return !a }
"""

RUST = """pub struct Repo { n: i32 }

impl Repo {
    pub fn count(&self, s: &str) -> usize {
        if s.len() == 0 && true { return 1 + 2; }
        s.len() * 3
    }
}

impl<T> Box<T> { fn get(&self) -> bool { !flag } }

pub fn top(a: bool) -> bool { !a }
"""

JAVA = """package app;

public class Repo {
  public int count(String s) {
    if (s.length() == 0 && s != null) { return 1 + 2; }
    return s.length() * 3;
  }
  public boolean ok() { return !flag; }
  interface IThing { void go(); }
}
"""


def test_go_resolves_functions_methods_and_types():
    assert paths_of(GO, "repo.go") == ["Repo", "Repo::Count", "Store::Count", "Top"]


def test_a_go_method_is_qualified_by_its_receiver():
    """`func (r *Repo) Count` and `func (s *Store) Count` are two symbols with one name.

    Anchoring both as `Count` collides them, and an anchor collision is an identity collision:
    the second finding derives the id of the first and merge absorbs it without a trace.
    """
    paths = paths_of(GO, "repo.go")
    assert "Repo::Count" in paths and "Store::Count" in paths
    assert len(paths) == len(set(paths))


def test_a_go_receiver_survives_a_pointer_and_a_type_parameter():
    assert paths_of("package a\nfunc (r *Repo[T]) Go() {}\n", "a.go") == ["Repo::Go"]


def test_rust_takes_its_segment_from_the_impl_type():
    """An `impl` block has no name of its own; without its type every method is bare."""
    assert paths_of(RUST, "repo.rs") == ["Repo", "Repo", "Repo::count", "Box", "Box::get", "top"]


def test_a_generic_impl_resolves_to_the_type_not_its_parameter():
    """`impl<T> Box<T>` -- the parameter list precedes the type, so document order alone is wrong."""
    assert paths_of("impl<T: Clone> Wrap<T> { fn w(&self) {} }\n", "a.rs") == ["Wrap", "Wrap::w"]


def test_a_rust_module_contributes_no_anchor_segment():
    assert paths_of("mod inner { pub fn f() {} }\n", "a.rs") == ["f"]


def test_java_resolves_nested_declarations():
    assert paths_of(JAVA, "Repo.java") == [
        "Repo", "Repo::count", "Repo::ok", "Repo::IThing", "Repo::IThing::go"]


def test_symbol_text_joins_every_range_one_path_names():
    """A Rust `struct` and its `impl` share a path, and the caller reads absence as proof of a fix.

    Returning only the first range reports the snippet gone whenever it lives in the other one --
    a real work item closed on nothing.
    """
    body = syntax.symbol_text(RUST, "repo.rs::Repo")
    assert body is not None
    assert "struct Repo" in body and "fn count" in body


@pytest.mark.parametrize("source,file,anchor", [
    (GO, "repo.go", "Repo::Count"), (RUST, "repo.rs", "Repo::count"),
    (JAVA, "Repo.java", "Repo::count")])
def test_operators_are_mutated_within_their_anchor(source: str, file: str, anchor: str):
    found = sites(source, file)
    assert (anchor, "comparison", "!=") in found
    assert (anchor, "arithmetic", "-") in found
    assert [s for s in found if s[0] == anchor and s[1] == "boolean"]


@pytest.mark.parametrize("source,file", [(GO, "repo.go"), (RUST, "repo.rs"), (JAVA, "Repo.java")])
def test_no_mutation_changes_a_type_in_any_typed_language(source: str, file: str):
    assert not [s for s in sites(source, file) if s[1] == "return"]
    assert not [s for s in sites(source, file) if s[2] in ("null", "None", "nil")]


def test_unary_negation_is_dropped_where_the_grammar_fields_it_and_where_it_does_not():
    """Go and Java field the operator; Rust and C# leave it an anonymous first child."""
    assert ("Top", "unary", "") in sites(GO, "repo.go")
    assert ("Repo::ok", "unary", "") in sites(JAVA, "Repo.java")
    assert ("Box::get", "unary", "") in sites(RUST, "repo.rs")


def test_each_languages_integer_and_boolean_literals_are_recognised():
    assert ("Repo::Count", "constant", "1") in sites(GO, "repo.go")
    assert ("Repo::count", "constant", "false") in sites(RUST, "repo.rs")
    assert ("Repo::count", "constant", "1") in sites(JAVA, "Repo.java")


def test_go_string_concatenation_is_not_mutated_into_subtraction():
    source = 'package a\nfunc J() string { return "a" + "b" }\nfunc N() int { return 1 + 2 }\n'
    found = sites(source, "a.go")
    assert ("N", "arithmetic", "-") in found
    assert not [s for s in found if s[0] == "J" and s[1] == "arithmetic"]
