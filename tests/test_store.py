"""findings.jsonl is the source of truth; SQLite is derived and rebuildable at any time."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import TASK, read_jsonl

from coldsweep import store as store_module
from coldsweep.models import Profile, RawFinding, Rule, RunRecord, ScanRound, ShardResult
from coldsweep.store import (
    Paths,
    StoreError,
    atomic_write,
    completed_rounds,
    find_repo,
    list_tasks,
    load_findings,
    load_profile,
    load_round,
    next_round,
    rebuild_index,
    reserve_task,
    save_findings,
    save_profile,
    save_round,
    save_run_record,
    validate_task,
)

PROFILE = Profile(rules=[])


def sample(n: int):
    return [RawFinding(rule_id="r", anchor=f"src/a.py::f{i}", description=f"d{i}").to_finding("s", 1)
            for i in range(n)]


def test_findings_are_one_object_per_line_sorted_by_id(paths: Paths):
    save_findings(paths, list(reversed(sample(5))))
    lines = paths.findings.read_text().splitlines()
    assert len(lines) == 5
    ids = [row["id"] for row in read_jsonl(paths.findings)]
    assert ids == sorted(ids)


def test_a_round_trip_preserves_every_field(paths: Paths):
    findings = sample(3)
    findings[0].status = "disputed"
    findings[0].adjudicated = True
    findings[0].log(2, "dispute", method="fuzzy", score=0.8, detail="why")
    save_findings(paths, findings)
    restored = {f.id: f for f in load_findings(paths)}
    assert restored[findings[0].id].model_dump() == findings[0].model_dump()


def test_a_corrupt_line_is_reported_with_its_number(paths: Paths):
    save_findings(paths, sample(2))
    paths.findings.write_text(paths.findings.read_text() + '{"id": "broken"}\n')
    with pytest.raises(StoreError, match=":3:"):
        load_findings(paths)


def test_a_missing_findings_file_is_an_empty_set(paths: Paths):
    assert load_findings(paths) == []


def test_only_ingested_rounds_count_as_completed(paths: Paths):
    paths.run_file(1).write_text("{}")
    assert completed_rounds(paths) == []
    save_run_record(paths, RunRecord(round=1))
    assert completed_rounds(paths) == [1]
    assert next_round(paths) == 2


def test_a_scanned_but_unignested_round_is_not_reused(paths: Paths):
    save_run_record(paths, RunRecord(round=1))
    paths.run_file(2).write_text("{}")
    assert next_round(paths) == 3, "round 2 was scanned; the next scan must not overwrite it"


def test_the_index_is_dropped_and_rebuilt_from_the_jsonl(paths: Paths):
    findings = sample(3)
    findings[0].log(1, "created", method="new")
    save_findings(paths, findings)
    rebuild_index(paths, PROFILE, findings)
    rebuild_index(paths, PROFILE)
    con = sqlite3.connect(paths.index)
    assert con.execute("SELECT count(*) FROM findings").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    con.close()


def test_deleting_the_index_loses_nothing(paths: Paths):
    save_findings(paths, sample(2))
    rebuild_index(paths, PROFILE)
    paths.index.unlink()
    assert len(load_findings(paths)) == 2
    rebuild_index(paths, PROFILE)
    assert paths.index.is_file()


def test_a_missing_profile_names_the_fix(paths: Paths):
    with pytest.raises(StoreError, match="coldsweep init"):
        load_profile(paths)


def test_the_repository_root_is_found_from_a_subdirectory(repo: Path):
    paths = Paths(repo, TASK)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text("version: 1\n")
    assert find_repo(repo / "src" / "nested") == repo


def test_writes_are_atomic(paths: Paths):
    save_findings(paths, sample(2))
    assert not list(paths.root.glob(".tmp-*")), "no temp files survive a successful write"


def test_a_profile_round_trips_through_yaml(paths: Paths):
    profile = Profile(name="round-trip", files_per_shard=3,
                      rules=[Rule(id="r", mode="absence", description="Une règle — accentuée")])
    save_profile(paths, profile)
    assert load_profile(paths) == profile


def test_a_saved_profile_stays_readable_rather_than_minimal(paths: Paths):
    """Defaults are written out and key order is preserved: this file is edited by hand."""
    save_profile(paths, Profile(name="readable"))
    text = paths.profile.read_text()
    assert "convergence" in text and "files_per_shard" in text, "defaults are not elided"
    assert text.index("version") < text.index("scope") < text.index("rules"), "declared order kept"


def test_non_ascii_survives_a_profile_write(paths: Paths):
    save_profile(paths, Profile(rules=[Rule(id="r", mode="absence", description="ошибка")]))
    assert "ошибка" in paths.profile.read_text()
    assert load_profile(paths).rules[0].description == "ошибка"


def test_an_empty_profile_file_loads_the_defaults(paths: Paths):
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text("")
    assert load_profile(paths) == Profile()


def test_a_finding_with_no_evidence_still_writes_its_null_fields(paths: Paths):
    """The jsonl is read by humans in diffs; an absent key and a null key are not the same."""
    save_findings(paths, sample(1))
    row = read_jsonl(paths.findings)[0]
    assert "evidence_sha" in row and row["evidence_sha"] is None
    assert "evidence" in row and row["evidence"] is None


def test_a_round_record_is_written_sorted_indented_and_newline_terminated(paths: Paths):
    scan = ScanRound(round=4, shards=[ShardResult(shard="s", files=["a.py"])])
    path = save_round(paths, scan)
    assert path == paths.run_file(4) and path.is_file()
    text = path.read_text()
    assert text.endswith("\n") and "\n  " in text, "indented and newline terminated"
    assert text.index('"profile_version"') < text.index('"round"'), "keys sorted"
    assert load_round(path) == scan


def test_a_run_record_is_written_the_same_way(paths: Paths):
    path = save_run_record(paths, RunRecord(round=2, new=1))
    assert path == paths.ingest_file(2)
    text = path.read_text()
    assert text.endswith("\n") and json.loads(text)["new"] == 1


def test_a_task_with_no_runs_directory_starts_at_round_one(repo: Path):
    fresh = Paths(repo, "brand-new")
    assert completed_rounds(fresh) == []
    assert next_round(fresh) == 1


def test_the_index_records_taxonomy_membership_as_it_is_now(paths: Paths):
    findings = sample(2)
    findings[0].rule_id = "in-taxonomy"
    save_findings(paths, findings)
    rebuild_index(paths, Profile(rules=[Rule(id="in-taxonomy", mode="absence")]), findings)
    con = sqlite3.connect(paths.index)
    rows = dict(con.execute("SELECT rule_id, unclassified FROM findings").fetchall())
    con.close()
    assert rows["in-taxonomy"] == 0 and rows["r"] == 1


def test_rebuilding_an_index_that_does_not_exist_yet_is_fine(paths: Paths):
    save_findings(paths, sample(1))
    assert not paths.index.exists()
    assert rebuild_index(paths, PROFILE) == paths.index and paths.index.is_file()


def test_every_task_path_hangs_off_the_task_directory(repo: Path):
    """These are the whole on-disk contract; nothing else pins their names or their layout."""
    paths = Paths(repo, "shape")
    root = repo / ".coldsweep" / "tasks" / "shape"
    assert paths.container == repo / ".coldsweep"
    assert paths.root == root
    assert paths.profile == root / "profile.yaml"
    assert paths.findings == root / "findings.jsonl"
    assert paths.runs == root / "runs"
    assert paths.index == root / "index.sqlite"
    assert paths.mutants == root / "mutants.sqlite"
    assert paths.mutation_lock == root / "mutants.lock"
    assert paths.spec_lock == root / "spec.lock"
    assert paths.run_file(3) == root / "runs" / "3.json"
    assert paths.ingest_file(3) == root / "runs" / "3.ingest.json"


@pytest.mark.parametrize("name", ["", "../escape", "with/slash", "UPPER", "-leading", "x" * 65])
def test_a_task_name_that_could_address_another_directory_is_refused(name: str):
    with pytest.raises(StoreError, match="invalid task name"):
        validate_task(name)


@pytest.mark.parametrize("name", ["a", "harden-io", "ship_v2", "task.1", "9lives"])
def test_ordinary_task_names_are_accepted(name: str):
    assert validate_task(name) == name


def test_a_directory_outside_any_project_resolves_to_itself(tmp_path: Path):
    assert find_repo(tmp_path) == tmp_path.resolve()


def test_listing_tasks_where_none_exist_is_empty_not_an_error(repo: Path):
    assert list_tasks(repo) == []
    Paths(repo, "second").runs.mkdir(parents=True)
    assert list_tasks(repo) == [], "a directory without a profile is not a task"


def test_tasks_are_listed_in_a_stable_order(repo: Path):
    for name in ("zulu", "alpha", "mike"):
        paths = Paths(repo, name)
        paths.root.mkdir(parents=True)
        paths.profile.write_text("version: 1\n")
    assert list_tasks(repo) == ["alpha", "mike", "zulu"]


def test_an_atomic_write_creates_the_directories_it_needs(repo: Path):
    target = repo / "deep" / "nested" / "file.txt"
    atomic_write(target, "content")
    assert target.read_text() == "content"
    assert not list((repo / "deep" / "nested").glob(".tmp-*"))


def test_an_atomic_write_replaces_an_existing_file(repo: Path):
    target = repo / "file.txt"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text() == "second"


def test_the_index_is_built_even_when_the_task_directory_is_missing(repo: Path):
    fresh = Paths(repo, "never-used")
    assert not fresh.root.exists()
    assert rebuild_index(fresh, PROFILE) == fresh.index and fresh.index.is_file()


def test_records_are_indented_by_exactly_two_spaces(paths: Paths):
    save_round(paths, ScanRound(round=1, shards=[ShardResult(shard="s", files=["a.py"])]))
    save_run_record(paths, RunRecord(round=1, new=1))
    for path in (paths.run_file(1), paths.ingest_file(1)):
        second = path.read_text().splitlines()[1]
        assert second.startswith("  ") and not second.startswith("   ")


def test_a_run_record_is_written_with_sorted_keys(paths: Paths):
    save_run_record(paths, RunRecord(round=1, new=1))
    text = paths.ingest_file(1).read_text()
    assert text.index('"adjudicated"') < text.index('"round"') < text.index('"unclassified"')


def test_every_reserved_task_gets_its_own_directory(repo: Path):
    claimed = [reserve_task(repo, "harden-io") for _ in range(50)]
    assert len({p.task for p in claimed}) == 50
    assert all(p.task.startswith("harden-io-") and p.root.is_dir() for p in claimed)
    assert sorted(entry.name for entry in (repo / ".coldsweep" / "tasks").iterdir()) == \
        sorted(p.task for p in claimed)


def test_a_repeated_suffix_loses_the_directory_rather_than_sharing_it(repo: Path, monkeypatch):
    """The guarantee is the exclusive mkdir, not the odds. Force the collision and check."""
    tokens = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _n: next(tokens))
    first = reserve_task(repo, "t")
    second = reserve_task(repo, "t")
    assert (first.task, second.task) == ("t-aaaaaaaa", "t-bbbbbbbb")


def test_a_task_name_too_long_to_carry_a_suffix_is_refused(repo: Path):
    reserve_task(repo, "a" * 55)
    with pytest.raises(StoreError, match="at most 55 characters"):
        reserve_task(repo, "a" * 56)


def test_reserving_gives_up_rather_than_looping_forever(repo: Path, monkeypatch):
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _n: "cccccccc")
    reserve_task(repo, "t")
    with pytest.raises(StoreError, match="could not claim"):
        reserve_task(repo, "t")
