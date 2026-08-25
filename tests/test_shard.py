"""Scope comes from git, so ignored files stay out and rounds stay comparable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import owned_rules

from coldsweep.models import MutationConfig, Profile, Scope, SpecConfig
from coldsweep.shard import ShardError, build_shards, matches_any, resolve_scope, shard_id, shard_warnings


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("src/**/*.py", "src/a.py", True),
    ("src/**/*.py", "src/nested/deep/a.py", True),
    ("src/**/*.py", "other/a.py", False),
    ("src/*.py", "src/nested/a.py", False),
    ("**/migrations/**", "src/migrations/0001.py", True),
    ("**/migrations/**", "migrations/0001.py", True),
    ("**/migrations/**", "src/migration/0001.py", False),
    ("*.md", "README.md", True),
    ("*.md", "docs/guide.md", False),
    ("docs/**", "docs/a/b.md", True),
    ("a?c.py", "abc.py", True),
    ("a[bc]d.py", "abd.py", True),
    ("a[!bc]d.py", "abd.py", False),
])
def test_glob_semantics(pattern, path, expected):
    assert matches_any(path, [pattern]) is expected


def test_scope_excludes_gitignored_and_out_of_scope_files(repo: Path):
    resolved = resolve_scope(repo, Scope(include=["src/**/*.py"], exclude=["**/migrations/**"]))
    assert resolved == ["src/a.py", "src/b.py", "src/nested/c.py"]
    assert "build/gen.py" not in resolved, "gitignored files never enter scope"
    assert "notes.txt" not in resolved


def test_untracked_but_unignored_files_are_in_scope(repo: Path):
    (repo / "src" / "fresh.py").write_text("F = 1\n")
    assert "src/fresh.py" in resolve_scope(repo, Scope(include=["src/**/*.py"]))


def test_removed_files_leave_scope(repo: Path):
    subprocess.run(["git", "rm", "-q", "src/b.py"], cwd=repo, check=True)
    assert "src/b.py" not in resolve_scope(repo, Scope(include=["src/**/*.py"]))


def test_one_file_per_shard_by_default(repo: Path):
    shards = build_shards(repo, Profile(scope=Scope(include=["src/**/*.py"], exclude=["**/migrations/**"])))
    assert [s.files for s in shards] == [["src/a.py"], ["src/b.py"], ["src/nested/c.py"]]


def test_shards_are_deterministic_and_content_independent(repo: Path):
    profile = Profile(scope=Scope(include=["src/**/*.py"], exclude=["**/migrations/**"]), files_per_shard=2)
    before = build_shards(repo, profile)
    (repo / "src" / "a.py").write_text("A = 999  # edited\n")
    after = build_shards(repo, profile)
    assert [s.model_dump() for s in before] == [s.model_dump() for s in after]
    assert [len(s.files) for s in before] == [2, 1]


def test_shard_ids_are_derived_from_the_file_set(repo: Path):
    assert shard_id(["b.py", "a.py"]) == shard_id(["a.py", "b.py"])
    assert shard_id(["a.py"]) != shard_id(["b.py"])


def test_oversized_shards_are_warned_about():
    assert shard_warnings(Profile(files_per_shard=5)) == []
    assert shard_warnings(Profile(files_per_shard=6))


def test_a_non_repository_is_an_error(tmp_path: Path):
    with pytest.raises(ShardError):
        resolve_scope(tmp_path, Scope(include=["**/*.py"]))


def test_a_profile_that_judges_tests_is_shown_the_tests(repo: Path):
    """`vacuous-test` asks about test files, so the shard must contain them."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    scope = Scope(include=["src/**/*.py"], exclude=["**/migrations/**"])
    plain = Profile(scope=scope)
    paired = Profile(scope=scope, mutation=MutationConfig(rule_id="untested-behaviour"),
                     rules=owned_rules("untested-behaviour"))

    assert build_shards(repo, plain)[0].files == ["src/a.py"]
    assert build_shards(repo, paired)[0].files == ["src/a.py", "tests/test_a.py"]
    assert build_shards(repo, paired)[1].files == ["src/b.py"], "no pair, no addition"


def test_pairing_never_duplicates_a_file_already_in_the_shard(repo: Path):
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    profile = Profile(scope=Scope(include=["src/**/*.py", "tests/**/*.py"]),
                      files_per_shard=10,
                      mutation=MutationConfig(rule_id="r"), rules=owned_rules("r"))
    files = build_shards(repo, profile)[0].files
    assert len(files) == len(set(files))


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("**", "any/deep/path.py", True),
    ("**", "top.py", True),
    ("a/**/b", "a/x/b", True),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/c", False),
])
def test_double_star_spans_directories_wherever_it_appears(pattern, path, expected):
    assert matches_any(path, [pattern]) is expected


def test_shard_ids_separate_file_lists_that_would_otherwise_run_together():
    """Paths are joined with a separator before hashing, so ['ab','c'] and ['a','bc'] differ."""
    assert shard_id(["ab", "c"]) != shard_id(["a", "bc"])


def test_a_shard_id_is_a_fixed_width_handle():
    digest = shard_id(["a.py"])
    assert digest.startswith("s-") and len(digest) == len("s-") + 8


def test_the_spec_document_is_dropped_from_the_shard_list(repo: Path):
    (repo / "SPEC.md").write_text("### FR-1 Thing\n\nBody.\n")
    scope = Scope(include=["src/**/*.py", "SPEC.md"], exclude=["**/migrations/**"])
    with_spec = Profile(scope=scope, spec=SpecConfig(path="SPEC.md", unimplemented_rule_id="u"),
                       rules=owned_rules("u"))
    files = [f for s in build_shards(repo, with_spec) for f in s.files]
    assert "SPEC.md" not in files and "src/a.py" in files
    assert "SPEC.md" in [f for s in build_shards(repo, Profile(scope=scope)) for f in s.files]


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("a/**b", "a/xyz", False),
    ("a/**b", "a/xyzb", True),
    ("a**b", "axc", False),
    ("a**b", "axb", True),
])
def test_what_follows_a_double_star_is_still_part_of_the_pattern(pattern, path, expected):
    assert matches_any(path, [pattern]) is expected
