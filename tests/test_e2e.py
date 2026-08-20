"""The whole loop, driven through the real CLI against a scratch repository.

Uses the deterministic stub agent, so this exercises every module end to end -- scan, merge,
fix, verify, convergence and the gate -- with zero LLM calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import git_init, read_jsonl

from coldsweep import store
from coldsweep.store import Paths

pytestmark = pytest.mark.e2e

STUB = str(Path(__file__).parent / "stub_agent.py")
ROOT = Path(__file__).resolve().parent.parent
TASK = "e2e"

DIRTY = '''import json


def load(path):
    try:
        return json.loads(open(path).read())
    except OSError:
        pass
'''

CLEAN = '''def slug(value):
    """Return a lowercase URL slug for value."""
    return value.lower()
'''

RULES = [
    {"id": "swallowed-exception", "mode": "absence",
     "description": "An except block that discards the error."},
    {"id": "undocumented-public-symbol", "mode": "presence",
     "description": "A public symbol has no docstring saying what it does."},
]


def profile_data(rules=None, **overrides) -> dict:
    data = {
        "version": 1,
        "name": "e2e",
        "scope": {"include": ["src/**/*.py"], "exclude": []},
        "files_per_shard": 1,
        "convergence": {"k": 2, "max_rounds": 8},
        "models": {"scan": "stub", "fix": "stub", "adjudicate": "stub"},
        "agent": {"command": [sys.executable, STUB], "append_flags": False,
                  "parallelism": 2, "retries": 1, "timeout_s": 120},
        "rules": rules if rules is not None else RULES,
        "mechanical": [],
    }
    data.update(overrides)
    return data


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "loader.py").write_text(DIRTY)
    (tmp_path / "src" / "util.py").write_text(CLEAN)
    git_init(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def coldsweep(project: Path, *args: str, task: str | None = TASK,
          env: dict | None = None) -> subprocess.CompletedProcess:
    scoped = ["--task", task] if task else []
    return subprocess.run(
        [sys.executable, "-m", "coldsweep", *args, *scoped, "-C", str(project)],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "STUB_STATE": str(project / ".stub-state"), **(env or {})},
    )


def init(project: Path, task: str = TASK, **kw) -> Paths:
    paths = Paths(project, task)
    paths.runs.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text(yaml.safe_dump(profile_data(**kw), sort_keys=False))
    return paths


def test_init_scaffolds_a_working_task(project: Path):
    result = coldsweep(project, "init", "issues")
    assert result.returncode == 0, result.stderr
    paths = Paths(project, TASK)
    assert paths.root == project / ".coldsweep" / "tasks" / TASK
    assert paths.profile.is_file() and paths.findings.is_file() and paths.runs.is_dir()
    assert (paths.container / ".gitignore").read_text().split() == [
        "tasks/*/index.sqlite", "tasks/*/mutants.sqlite", "tasks/*/mutants.lock"]
    assert coldsweep(project, "shard").returncode == 0


def test_init_refuses_to_clobber_an_existing_task(project: Path):
    coldsweep(project, "init", "issues")
    assert coldsweep(project, "init", "docs").returncode == 2
    assert coldsweep(project, "init", "docs", "--force").returncode == 0


def test_shard_lists_one_file_per_shard(project: Path):
    init(project)
    result = coldsweep(project, "shard", "--json")
    shards = json.loads(result.stdout)
    assert [s["files"] for s in shards] == [["src/loader.py"], ["src/util.py"]]


def test_the_gate_is_shut_before_any_round_runs(project: Path):
    init(project)
    result = coldsweep(project, "converged")
    assert result.returncode == 1
    assert result.stdout == "" and result.stderr == "", "the gate prints nothing"


def test_the_full_loop_converges_and_opens_the_gate(project: Path):
    paths = init(project)
    result = coldsweep(project, "run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "converged after 3 round(s)" in result.stdout

    assert coldsweep(project, "converged").returncode == 0

    findings = read_jsonl(paths.findings)
    assert {f["rule_id"] for f in findings} == {"swallowed-exception", "undocumented-public-symbol"}
    assert {f["rule_id"]: f["status"] for f in findings} == {
        "swallowed-exception": "verified", "undocumented-public-symbol": "lapsed"}
    assert all(f["first_seen_round"] == 1 for f in findings)

    fixed = (project / "src" / "loader.py").read_text()
    assert "pass" not in fixed and "raise" in fixed
    assert '"""' in fixed


def test_the_loop_leaves_a_complete_audit_trail(project: Path):
    paths = init(project)
    coldsweep(project, "run")
    assert sorted(p.name for p in paths.runs.glob("*.json")) == [
        "1.ingest.json", "1.json", "2.ingest.json", "2.json", "3.ingest.json", "3.json"]
    assert json.loads(paths.ingest_file(1).read_text())["new"] == 2
    assert json.loads(paths.ingest_file(2).read_text())["new"] == 0
    for finding in read_jsonl(paths.findings):
        actions = [e["action"] for e in finding["history"]]
        assert actions[0] == "created" and "fix" in actions
        assert actions[-1] in ("verify", "close")


def test_a_second_run_over_a_converged_repo_does_nothing(project: Path):
    init(project)
    coldsweep(project, "run")
    before = (Paths(project, TASK).findings.read_text(), (project / "src" / "loader.py").read_text())
    result = coldsweep(project, "run")
    assert result.returncode == 0 and "converged after 3 round(s)" in result.stdout
    assert (Paths(project, TASK).findings.read_text(), (project / "src" / "loader.py").read_text()) == before


def test_a_regression_is_reopened_by_the_next_round(project: Path):
    """Convergence describes the finding set, not the working tree: a fresh round is what re-derives."""
    paths = init(project)
    coldsweep(project, "run")
    (project / "src" / "loader.py").write_text(DIRTY)
    assert coldsweep(project, "converged").returncode == 0, "the gate stays open until a round actually runs"

    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(4)))
    findings = read_jsonl(paths.findings)
    assert {f["status"] for f in findings} == {"open"}
    assert all(any(e["action"] == "reopen" for e in f["history"]) for f in findings)
    assert all(f["first_seen_round"] == 1 for f in findings), "a reopened finding is not a new one"
    assert coldsweep(project, "converged").returncode == 1

    assert coldsweep(project, "run").returncode == 0
    assert {f["status"] for f in read_jsonl(paths.findings)} == {"verified", "lapsed"}
    assert "pass" not in (project / "src" / "loader.py").read_text()


def test_a_failed_shard_fails_the_round_and_blocks_ingest(project: Path):
    paths = init(project)
    scan = coldsweep(project, "scan", env={"STUB_MODE": "garbage"})
    assert scan.returncode == 1 and "shard(s) failed" in scan.stderr

    ingest = coldsweep(project, "ingest", str(paths.run_file(1)))
    assert ingest.returncode == 2 and "coverage is incomplete" in ingest.stderr
    assert paths.findings.read_text() == "" if paths.findings.exists() else True
    assert coldsweep(project, "converged").returncode == 1


def test_a_transient_shard_failure_is_absorbed_by_the_retry(project: Path):
    init(project)
    result = coldsweep(project, "run", env={"STUB_MODE": "flaky"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_off_taxonomy_findings_are_quarantined_and_block_the_gate(project: Path):
    paths = init(project, rules=[RULES[0]])
    coldsweep(project, "scan", env={"STUB_MODE": "rogue"})
    coldsweep(project, "ingest", str(paths.run_file(1)))

    status = json.loads(coldsweep(project, "status", "--json").stdout)
    assert status["by_rule"]["undocumented-public-symbol"] == 1
    assert len(status["convergence"]["unclassified_ids"]) == 1
    assert coldsweep(project, "converged").returncode == 1

    human = coldsweep(project, "status").stdout
    assert "(off-taxonomy)" in human and "unclassified (1)" in human

    assert coldsweep(project, "adjudicate", "--wontfix-unclassified").returncode == 0
    status = json.loads(coldsweep(project, "status", "--json").stdout)
    assert status["convergence"]["unclassified_ids"] == []


def test_the_round_ceiling_exits_non_zero_and_reports_state(project: Path):
    init(project, convergence={"k": 2, "max_rounds": 2}, rules=[RULES[0]])
    result = coldsweep(project, "run", env={"STUB_MODE": "rogue"})
    assert result.returncode == 1
    assert "max_rounds=2" in result.stderr
    assert "unclassified finding(s) in the bucket" in result.stderr


def test_scan_never_sees_prior_state(project: Path):
    paths = init(project)
    coldsweep(project, "run")
    for round_file in sorted(paths.runs.glob("[0-9].json")):
        payload = round_file.read_text()
        assert "first_seen_round" not in payload
        assert "findings.jsonl" not in payload
        for shard in json.loads(payload)["shards"]:
            for finding in shard["findings"]:
                assert set(finding) == {"rule_id", "anchor", "evidence", "description"}


def test_the_index_is_rebuildable_from_the_jsonl(project: Path):
    paths = init(project)
    coldsweep(project, "run")
    assert paths.index.is_file()
    paths.index.unlink()
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(4)))
    assert paths.index.is_file()


def test_fix_can_be_scoped_to_a_single_rule(project: Path):
    paths = init(project)
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(1)))
    coldsweep(project, "fix", "--rule", "swallowed-exception")
    by_rule = {f["rule_id"]: f["status"] for f in read_jsonl(paths.findings)}
    assert by_rule["swallowed-exception"] == "fixed"
    assert by_rule["undocumented-public-symbol"] == "open"


def test_run_without_fix_never_edits_the_repository(project: Path):
    init(project)
    before = (project / "src" / "loader.py").read_text()
    result = coldsweep(project, "run", "--no-fix", "--max-rounds", "2")
    assert result.returncode == 1
    assert (project / "src" / "loader.py").read_text() == before


def test_a_pending_dispute_stops_the_loop_instead_of_burning_the_round_budget(project: Path):
    """Scanning cannot clear a dispute, so the loop must not keep paying for rounds that try."""
    paths = init(project, rules=[RULES[0]], convergence={"k": 2, "max_rounds": 8})
    result = coldsweep(project, "run", env={"STUB_MODE": "stubborn"})

    assert result.returncode == 1
    assert "need triage" in result.stderr
    assert f"coldsweep adjudicate --task {TASK}" in result.stderr
    assert len(store.completed_rounds(paths)) == 3, "stops as soon as the quiet window closes"
    assert "=== round 4 ===" not in result.stdout

    assert coldsweep(project, "adjudicate", "--accept-disputes").returncode == 0
    assert coldsweep(project, "converged").returncode == 0


def test_the_ceiling_still_fires_when_the_quiet_window_never_closes(project: Path):
    """A round budget too small to establish k quiet rounds is a budget problem, not triage."""
    init(project, rules=[RULES[0]], convergence={"k": 2, "max_rounds": 1})
    result = coldsweep(project, "run")
    assert result.returncode == 1
    assert "max_rounds=1" in result.stderr and "need at least k=2" in result.stderr
    assert "need triage" not in result.stderr


@pytest.mark.parametrize(("flag", "value"), [
    ("--max-rounds", "0"), ("--max-rounds", "-1"),
    ("--round", "0"), ("--round", "-3"),
    ("--limit", "0"), ("--limit", "-1"),
])
def test_override_flags_carry_the_bound_of_the_field_they_replace(project: Path, flag: str, value: str):
    """An override reaches the code straight from argv, never through the model it replaces."""
    init(project)
    command = {"--max-rounds": "run", "--round": "scan", "--limit": "fix"}[flag]
    result = coldsweep(project, command, flag, value)
    assert result.returncode == 2, f"{flag} {value} was accepted"
    assert "Invalid value" in result.stderr


def test_a_negative_round_never_reaches_the_run_directory(project: Path):
    paths = init(project)
    coldsweep(project, "scan", "--round", "-3")
    assert list(paths.runs.glob("*.json")) == [], "a bad round must not corrupt the round bookkeeping"


def test_valid_overrides_still_work(project: Path):
    init(project)
    assert coldsweep(project, "run", "--max-rounds", "1", "--no-fix").returncode == 1
    assert coldsweep(project, "fix", "--limit", "1").returncode == 0


def test_a_profile_with_no_deterministic_decider_says_it_will_not_converge(project: Path):
    result = coldsweep(project, "init", "issues")
    assert result.returncode == 0
    assert "budget-bounded" in result.stdout and "will not converge" in result.stdout


def test_a_profile_with_a_deterministic_decider_makes_no_such_claim(project: Path):
    result = coldsweep(project, "init", "features")
    assert "budget-bounded" not in result.stdout


def test_spending_the_budget_is_reported_as_spent_not_as_failure(project: Path):
    """A profile that was never going to converge must not report its ending as a hard stop."""
    init(project, rules=[RULES[0]], convergence={"k": 2, "max_rounds": 1})
    result = coldsweep(project, "run", "--no-fix")
    assert result.returncode == 1, "the gate is still shut, and still says so"
    assert "budget spent" in result.stderr and "never going to converge" in result.stderr
    assert "hard stop" not in result.stderr


def test_a_convergent_profile_still_reports_a_hard_stop(project: Path):
    rules = [{**RULES[0], "decided_by": "code"}]
    init(project, rules=rules, convergence={"k": 2, "max_rounds": 1})
    result = coldsweep(project, "run", "--no-fix")
    assert "hard stop" in result.stderr and "budget spent" not in result.stderr


def test_any_command_recovers_a_tree_a_killed_mutation_run_left_mutated(project: Path):
    paths = init(project)
    source = project / "src" / "loader.py"
    original = source.read_text()
    source.with_name(source.name + ".coldsweep-mutant-backup").write_text(original)
    source.write_text("raise SystemExit('left behind')\n")
    paths.mutation_lock.write_text("src/loader.py")

    result = coldsweep(project, "status")
    assert result.returncode == 0
    assert "a mutation run was interrupted" in result.stderr
    assert source.read_text() == original and not paths.mutation_lock.exists()
