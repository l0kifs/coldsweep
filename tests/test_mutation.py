"""The mutation subsystem: deterministic operators, an honest runtime, and a cache that
invalidates on exactly what could change a verdict."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git_init

from coldsweep.models import MutationConfig, Profile, Scope
from coldsweep.mutation import (
    BACKUP_SUFFIX,
    MutationCache,
    MutationError,
    MutationRunner,
    apply_mutant,
    build_mutation_shards,
    generate_mutants,
    paired_tests,
    restore_interrupted,
    run,
)

SOURCE = '''"""Module docstring, not behaviour."""


def parse_port(raw):
    """Return the port, or None when it is out of range."""
    port = int(raw)
    if 1 <= port and port <= 65535:
        return port
    return None


class Gate:
    def allows(self, value):
        return not value


TOTAL = 3 + 4
'''


def config(**kw) -> MutationConfig:
    return MutationConfig(rule_id="untested-behaviour", **kw)


def mutants(source: str = SOURCE, **kw):
    return generate_mutants("src/port.py", source.encode(), config(**kw))


def test_every_operator_produces_the_expected_mutation():
    by_op: dict[str, set[str]] = {}
    for m in mutants(operators=["comparison", "arithmetic", "boolean", "constant", "return", "unary"]):
        by_op.setdefault(m.operator, set()).add(m.display)
    assert "'<=' -> '>'" in by_op["comparison"]
    assert "'and' -> 'or'" in by_op["boolean"]
    assert "'+' -> '-'" in by_op["arithmetic"]
    assert "'port' -> 'None'" in by_op["return"]
    assert "'65535' -> '65536'" in by_op["constant"]
    assert by_op["unary"] == {"'not ' -> ''"}


def test_anchors_are_symbol_paths_and_never_line_numbers():
    anchors = {m.anchor for m in mutants()}
    assert "src/port.py::parse_port" in anchors
    assert "src/port.py::Gate::allows" in anchors
    assert "src/port.py" in anchors, "module-level code anchors to the file"
    assert all(not m.anchor.rstrip(":").split("::")[-1].isdigit() for m in mutants())


def test_docstrings_are_documentation_not_behaviour():
    assert all("docstring" not in m.original for m in mutants())
    assert all("out of range" not in m.original for m in mutants())


def test_identity_is_stable_across_unrelated_edits():
    before = {m.id: m.display for m in mutants()}
    edited = SOURCE.replace("TOTAL = 3 + 4", "OTHER = 9\nTOTAL = 3 + 4")
    after = {m.id: m.display for m in mutants(edited)}
    assert set(before) <= set(after), "shifting code down the file must not re-identify mutants"


def test_repeated_operators_at_one_anchor_stay_distinct():
    comparisons = [m for m in mutants() if m.operator == "comparison"]
    assert len(comparisons) == 2 and len({m.id for m in comparisons}) == 2


def test_generation_is_deterministic():
    assert [m.model_dump() for m in mutants()] == [m.model_dump() for m in mutants()]


def test_a_mutant_splices_only_its_own_span():
    mutant = next(m for m in mutants() if m.operator == "return")
    result = apply_mutant(SOURCE.encode(), mutant).decode()
    assert "return None\n    return None" in result
    assert result.count("def parse_port") == 1


def test_the_anchor_budget_is_respected():
    assert len(mutants(max_mutants_per_anchor=2)) <= 2 * 3


def test_unparseable_sources_are_reported_not_skipped():
    with pytest.raises(MutationError, match="cannot parse"):
        generate_mutants("src/bad.py", b"def (:\n", config())


# --- runtime ---------------------------------------------------------------

PORT_SRC = '''def parse_port(raw):
    port = int(raw)
    if port < 1:
        return None
    return port
'''

# Pins the boundary as well as the happy path. Without the port=1 case the `1 -> 2` mutant
# survives -- which is precisely the gap a coverage number would have hidden.
STRONG_TEST = '''from src.port import parse_port


def test_rejects_below_range():
    assert parse_port("0") is None


def test_accepts_the_lowest_valid_port():
    assert parse_port("1") == 1


def test_accepts_in_range():
    assert parse_port("8080") == 8080
'''

VACUOUS_TEST = '''from src.port import parse_port


def test_it_runs():
    parse_port("8080")
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "port.py").write_text(PORT_SRC)
    (tmp_path / "tests").mkdir()
    git_init(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def profile(**kw) -> Profile:
    kw.setdefault("test_command", f"{sys.executable} -m pytest -q -x {{tests}}")
    return Profile(
        name="tests", scope=Scope(include=["src/**/*.py"], exclude=["**/__init__.py"]),
        rules=[], mutation=config(**kw),
    )


def test_a_real_suite_kills_every_mutant(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    findings, report = run(project, profile(stop_at_first_survivor=False), project / "cache.sqlite")
    assert report.survived == 0 and report.no_tests == 0 and report.killed == report.mutants
    assert findings == []


def test_a_vacuous_test_does_not_save_it(project: Path):
    """The whole point: this file has a test, and full line coverage, and pins nothing."""
    (project / "tests" / "test_port.py").write_text(VACUOUS_TEST)
    findings, report = run(project, profile(), project / "cache.sqlite")
    assert report.survived > 0
    assert [f.anchor for f in findings] == ["src/port.py::parse_port"]
    assert findings[0].rule_id == "untested-behaviour" and findings[0].evidence is None


def test_no_test_file_at_all_is_reported_without_running_anything(project: Path):
    findings, report = run(project, profile(), project / "cache.sqlite")
    assert report.no_tests > 0 and report.killed == 0
    assert [f.anchor for f in findings] == ["src/port.py::parse_port"]


def test_one_finding_per_symbol_however_many_mutants_demonstrate_it(project: Path):
    (project / "tests" / "test_port.py").write_text(VACUOUS_TEST)
    findings, _ = run(project, profile(stop_at_first_survivor=False), project / "cache.sqlite")
    assert len(findings) == 1
    assert "and" in findings[0].description or "->" in findings[0].description


def test_the_working_tree_is_left_exactly_as_found(project: Path):
    (project / "tests" / "test_port.py").write_text(VACUOUS_TEST)
    before = (project / "src" / "port.py").read_text()
    run(project, profile(), project / "cache.sqlite")
    assert (project / "src" / "port.py").read_text() == before
    assert list(project.rglob(f"*{BACKUP_SUFFIX}")) == []


def test_a_crash_mid_mutation_is_repaired_on_the_next_run(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    source = project / "src" / "port.py"
    original = source.read_text()
    source.with_name(source.name + BACKUP_SUFFIX).write_text(original)
    source.write_text("raise SystemExit('left mutated by a killed process')\n")

    cache = MutationCache(project / "cache.sqlite")
    restored = MutationRunner(project, profile().mutation, cache).restore_orphans()
    cache.close()
    assert restored == ["src/port.py"]
    assert source.read_text() == original


def test_a_red_suite_is_refused_rather_than_measured(project: Path):
    (project / "tests" / "test_port.py").write_text(
        "def test_broken():\n    assert False, 'the suite is red before any mutation'\n")
    with pytest.raises(MutationError, match="baseline test run failed"):
        run(project, profile(), project / "cache.sqlite")


# --- cache -----------------------------------------------------------------

def test_the_second_run_is_all_cache_hits(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    cache_path = project / "cache.sqlite"
    _, first = run(project, profile(), cache_path)
    _, second = run(project, profile(), cache_path)
    assert first.cached == 0
    assert second.cached == second.mutants and second.mutants > 0


def test_editing_the_source_invalidates_its_results(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    cache_path = project / "cache.sqlite"
    run(project, profile(), cache_path)
    (project / "src" / "port.py").write_text(PORT_SRC.replace("port < 1", "port <= 0"))
    _, again = run(project, profile(), cache_path)
    assert again.cached == 0


def test_editing_the_tests_invalidates_the_results_they_judged(project: Path):
    (project / "tests" / "test_port.py").write_text(VACUOUS_TEST)
    cache_path = project / "cache.sqlite"
    _, before = run(project, profile(), cache_path)
    assert before.survived > 0
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    _, after = run(project, profile(), cache_path)
    assert after.cached == 0 and after.survived == 0


def test_changing_the_test_command_invalidates_the_results(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    cache_path = project / "cache.sqlite"
    run(project, profile(), cache_path)
    _, after = run(project, profile(test_command=f"{sys.executable} -m pytest -q {{tests}}"), cache_path)
    assert after.cached == 0


def test_the_cache_is_rebuildable_by_deleting_it(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    cache_path = project / "cache.sqlite"
    _, first = run(project, profile(), cache_path)
    cache_path.unlink()
    _, rebuilt = run(project, profile(), cache_path)
    assert rebuilt.killed == first.killed and rebuilt.cached == 0


# --- shard strategy --------------------------------------------------------

def test_a_shard_pairs_one_source_file_with_its_tests(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    shards = build_mutation_shards(project, profile())
    assert [s.source for s in shards] == ["src/port.py"]
    assert shards[0].tests == ["tests/test_port.py"]
    assert shards[0].mutants


def test_the_pairing_convention_is_configurable(project: Path):
    (project / "tests" / "port_test.py").write_text(STRONG_TEST)
    assert paired_tests(project, "src/port.py", config().test_patterns) == []
    assert paired_tests(project, "src/port.py", ["tests/{stem}_test.py"]) == ["tests/port_test.py"]


def test_files_with_nothing_to_mutate_get_no_shard(project: Path):
    (project / "src" / "empty.py").write_text("from os import path  # nothing mutable\n")
    assert "src/empty.py" not in [s.source for s in build_mutation_shards(project, profile())]


def test_a_profile_without_a_mutation_block_runs_nothing(project: Path):
    plain = Profile(scope=Scope(include=["src/**/*.py"]))
    assert build_mutation_shards(project, plain) == []
    findings, report = run(project, plain, project / "cache.sqlite")
    assert findings == [] and report.mutants == 0
    assert not (project / "cache.sqlite").exists(), "no mutation block, no cache file"


def test_a_same_length_mutation_leaves_no_stale_bytecode(project: Path):
    """CPython validates a .pyc by mtime and size, so a same-length mutant can outlive its restore."""
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_port.py"],
                   cwd=project, capture_output=True, check=True)          # warm the cache
    run(project, profile(stop_at_first_survivor=False), project / "cache.sqlite")

    after = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_port.py"],
                           cwd=project, capture_output=True, text=True, check=False)
    assert after.returncode == 0, f"the suite now sees a mutant, not the original:\n{after.stdout}"


# --- harness sentinel -------------------------------------------------------

# A suite that runs green without ever importing the module under mutation. In the wild this
# is usually an installed copy shadowing the working tree; the sentinel does not care which,
# only that the tests demonstrably never reach the code.
BLIND_TEST = "def test_unrelated():\n    assert 1 + 1 == 2\n"


def test_a_suite_that_never_imports_the_code_is_refused_not_measured(project: Path):
    """Every mutant surviving means the harness is broken, not that nothing is tested."""
    (project / "tests" / "test_port.py").write_text(BLIND_TEST)
    with pytest.raises(MutationError, match="does not exercise the code under mutation"):
        run(project, profile(), project / "cache.sqlite")


def test_the_refusal_names_the_command_and_the_files(project: Path):
    (project / "tests" / "test_port.py").write_text(BLIND_TEST)
    with pytest.raises(MutationError) as exc:
        run(project, profile(), project / "cache.sqlite")
    assert "src/port.py" in str(exc.value) and "pytest" in str(exc.value)


def test_one_unexercised_file_among_several_is_a_finding_not_a_refusal(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    (project / "src" / "orphan.py").write_text("def unused():\n    return 1\n")
    (project / "tests" / "test_orphan.py").write_text(BLIND_TEST)

    findings, report = run(project, profile(), project / "cache.sqlite")
    assert report.unexercised == ["src/orphan.py"]
    orphan = [f for f in findings if f.anchor == "src/orphan.py"]
    assert len(orphan) == 1 and "never import this module" in orphan[0].description
    assert report.killed > 0, "the file that is exercised was still measured normally"


def test_a_working_harness_passes_the_sentinel(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    _, report = run(project, profile(), project / "cache.sqlite")
    assert report.unexercised == [] and report.killed == report.mutants


def test_the_sentinel_always_restores_the_source(project: Path):
    (project / "tests" / "test_port.py").write_text(BLIND_TEST)
    before = (project / "src" / "port.py").read_text()
    with pytest.raises(MutationError):
        run(project, profile(), project / "cache.sqlite")
    assert (project / "src" / "port.py").read_text() == before
    assert list(project.rglob(f"*{BACKUP_SUFFIX}")) == []


def test_files_without_paired_tests_never_reach_the_sentinel(project: Path):
    findings, report = run(project, profile(), project / "cache.sqlite")
    assert report.unexercised == [] and report.no_tests > 0
    assert [f.anchor for f in findings] == ["src/port.py::parse_port"]


# --- interrupted runs -------------------------------------------------------

def leave_a_mutant(project: Path, lock: Path) -> str:
    """Exactly what a killed process leaves behind: a backup, a mutated file, and a lock."""
    source = project / "src" / "port.py"
    original = source.read_text()
    source.with_name(source.name + BACKUP_SUFFIX).write_text(original)
    source.write_text("raise SystemExit('left behind by a killed run')\n")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("src/port.py")
    return original


def test_an_interrupted_run_is_recovered_from_the_lock_alone(project: Path):
    lock = project / ".coldsweep" / "tasks" / "t" / "mutants.lock"
    original = leave_a_mutant(project, lock)
    assert restore_interrupted(project, lock) == ["src/port.py"]
    assert (project / "src" / "port.py").read_text() == original
    assert not lock.exists()
    assert list(project.rglob(f"*{BACKUP_SUFFIX}")) == []


def test_recovery_is_a_no_op_when_nothing_was_interrupted(project: Path):
    lock = project / ".coldsweep" / "tasks" / "t" / "mutants.lock"
    assert restore_interrupted(project, lock) == []
    assert restore_interrupted(project, project / "nowhere.lock") == []


def test_a_stale_lock_without_a_backup_is_cleared_not_obeyed(project: Path):
    lock = project / ".coldsweep" / "tasks" / "t" / "mutants.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("src/port.py")
    before = (project / "src" / "port.py").read_text()
    assert restore_interrupted(project, lock) == []
    assert (project / "src" / "port.py").read_text() == before and not lock.exists()


def test_the_lock_names_the_file_while_a_mutant_is_being_judged(project: Path):
    """The lock has to exist during the test run, or a kill mid-run leaves no trace."""
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    lock = project / "mutants.lock"
    cache = MutationCache(project / "cache.sqlite")
    runner = MutationRunner(project, profile().mutation, cache, lock)
    seen: list[str] = []
    original = (project / "src" / "port.py").read_bytes()
    with runner._swapped(project / "src" / "port.py", original, b"x = 1\n"):
        seen.append(lock.read_text())
    cache.close()
    assert seen == ["src/port.py"]
    assert not lock.exists(), "and it is gone the moment the file is put back"


def test_a_completed_run_leaves_no_lock(project: Path):
    (project / "tests" / "test_port.py").write_text(STRONG_TEST)
    lock = project / "mutants.lock"
    run(project, profile(), project / "cache.sqlite", lock)
    assert not lock.exists()
