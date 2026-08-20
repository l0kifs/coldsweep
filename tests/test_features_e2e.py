"""The features task, driven through the real CLI: author, freeze, trace, converge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import git_init, read_jsonl

from coldsweep.store import Paths

pytestmark = pytest.mark.e2e

STUB = str(Path(__file__).parent / "stub_agent.py")
ROOT = Path(__file__).resolve().parent.parent
TASK = "ship-sessions"

SPEC = """# Session handling

### FR-1 Session expiry

A session expires 30 minutes after the last request.

### FR-2 Audit log

Every expiry is written to the audit log.
"""

IMPL = '''def expire(session):
    """Expire a session that has gone idle."""
    # spec: FR-1
    session.active = False
    return session
'''


def profile_data(**overrides) -> dict:
    data = {
        "version": 1, "name": "features",
        "scope": {"include": ["src/**/*.py", "SPEC.md"], "exclude": []},
        "files_per_shard": 1, "fix_scope": "task",
        "convergence": {"k": 2, "max_rounds": 8},
        "models": {"scan": "stub", "fix": "stub", "adjudicate": "stub"},
        "agent": {"command": [sys.executable, STUB], "append_flags": False,
                  "parallelism": 2, "retries": 1, "timeout_s": 120},
        "rules": [
            {"id": "unimplemented-spec-item", "mode": "presence",
             "description": "A frozen spec item nothing in scope claims."},
            {"id": "stale-spec-reference", "mode": "absence",
             "description": "A marker naming an item that no longer exists."},
        ],
        "mechanical": [],
        "spec": {"path": "SPEC.md", "unimplemented_rule_id": "unimplemented-spec-item",
                 "stale_reference_rule_id": "stale-spec-reference"},
    }
    data.update(overrides)
    return data


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "session.py").write_text(IMPL)
    (tmp_path / "SPEC.md").write_text(SPEC)
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


def init(project: Path, **kw) -> Paths:
    paths = Paths(project, TASK)
    paths.runs.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text(yaml.safe_dump(profile_data(**kw), sort_keys=False))
    return paths


def test_the_features_template_scaffolds_and_reports(project: Path):
    assert coldsweep(project, "init", "features").returncode == 0
    result = coldsweep(project, "spec", "status")
    assert result.returncode == 0 and "NOT FROZEN" in result.stdout
    assert "the loop never validates the spec itself" in result.stdout


def test_nothing_runs_against_an_unfrozen_spec(project: Path):
    init(project)
    assert coldsweep(project, "converged").returncode == 1
    scan = coldsweep(project, "scan")
    assert scan.returncode == 2 and "not frozen" in scan.stderr
    assert "shut" in coldsweep(project, "task", "list", task=None).stdout


def test_freezing_records_every_item(project: Path):
    paths = init(project)
    result = coldsweep(project, "spec", "freeze")
    assert result.returncode == 0 and "froze 2 spec item(s)" in result.stdout
    lock = json.loads(paths.spec_lock.read_text())
    assert sorted(lock["items"]) == ["FR-1", "FR-2"] and lock["spec"] == "SPEC.md"


def test_spec_status_separates_traced_from_unimplemented(project: Path):
    init(project)
    coldsweep(project, "spec", "freeze")
    rows = {r["id"]: r for r in json.loads(coldsweep(project, "spec", "status", "--json").stdout)["items"]}
    assert rows["FR-1"]["implemented_at"] == ["src/session.py::expire"]
    assert rows["FR-2"]["implemented_at"] == [] and rows["FR-2"]["frozen"] is True
    assert "traced" in coldsweep(project, "spec", "status").stdout


def test_an_unimplemented_item_becomes_a_finding_decided_by_code(project: Path):
    paths = init(project)
    coldsweep(project, "spec", "freeze")
    scan = coldsweep(project, "scan")
    assert scan.returncode == 0 and "unimplemented 1" in scan.stdout

    coldsweep(project, "ingest", str(paths.run_file(1)))
    findings = [f for f in read_jsonl(paths.findings) if f["rule_id"] == "unimplemented-spec-item"]
    assert [f["anchor"] for f in findings] == ["SPEC.md::FR-2"]
    assert findings[0]["source"] == "mechanical", "traceability is decided by code, not by a model"


def test_implementing_the_item_closes_it(project: Path):
    paths = init(project)
    coldsweep(project, "spec", "freeze")
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(1)))
    assert coldsweep(project, "converged").returncode == 1

    (project / "src" / "audit.py").write_text(
        '# spec: FR-2\ndef record(event):\n    """Write an expiry to the audit log."""\n    return event\n')
    for n in (2, 3, 4):
        coldsweep(project, "scan")
        coldsweep(project, "ingest", str(paths.run_file(n)))
    statuses = {f["rule_id"]: f["status"] for f in read_jsonl(paths.findings)}
    assert statuses["unimplemented-spec-item"] == "verified"
    assert coldsweep(project, "converged").returncode == 0


def test_editing_a_frozen_spec_reopens_the_gate(project: Path):
    paths = init(project)
    coldsweep(project, "spec", "freeze")
    (project / "src" / "audit.py").write_text("# spec: FR-2\ndef record(event):\n    return event\n")
    for n in (1, 2, 3):
        coldsweep(project, "scan")
        coldsweep(project, "ingest", str(paths.run_file(n)))
    assert coldsweep(project, "converged").returncode == 0

    (project / "SPEC.md").write_text(SPEC.replace("30 minutes", "15 minutes"))
    assert coldsweep(project, "converged").returncode == 1
    assert "reworded" in coldsweep(project, "status").stdout
    assert coldsweep(project, "scan").returncode == 2

    refreeze = coldsweep(project, "spec", "freeze")
    assert "re-froze" in refreeze.stdout and "reworded" in refreeze.stdout
    assert coldsweep(project, "converged").returncode == 0


def test_a_new_item_after_convergence_reopens_the_work(project: Path):
    paths = init(project)
    coldsweep(project, "spec", "freeze")
    (project / "src" / "audit.py").write_text("# spec: FR-2\ndef record(event):\n    return event\n")
    for n in (1, 2, 3):
        coldsweep(project, "scan")
        coldsweep(project, "ingest", str(paths.run_file(n)))
    assert coldsweep(project, "converged").returncode == 0

    (project / "SPEC.md").write_text(SPEC + "\n### FR-3 Rate limit\n\nExpiry is rate limited.\n")
    assert coldsweep(project, "converged").returncode == 1, "an unfrozen addition shuts the gate"
    coldsweep(project, "spec", "freeze")
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(4)))
    open_items = [f["anchor"] for f in read_jsonl(paths.findings) if f["status"] == "open"]
    assert "SPEC.md::FR-3" in open_items


def test_a_renamed_item_leaves_its_marker_behind(project: Path):
    paths = init(project)
    (project / "SPEC.md").write_text(SPEC.replace("### FR-1 Session expiry", "### FR-7 Session expiry"))
    coldsweep(project, "spec", "freeze")
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(1)))
    by_rule = {f["rule_id"]: f for f in read_jsonl(paths.findings)}
    assert by_rule["stale-spec-reference"]["anchor"] == "src/session.py::expire"
    assert by_rule["stale-spec-reference"]["evidence"] == "spec: FR-1"


def test_a_task_scoped_fix_may_edit_the_whole_scope(project: Path):
    paths = init(project)
    coldsweep(project, "spec", "freeze")
    coldsweep(project, "scan")
    coldsweep(project, "ingest", str(paths.run_file(1)))
    result = coldsweep(project, "fix", env={"STUB_MODE": "stubborn"})
    assert result.returncode == 0 and "work item(s)" in result.stdout


def test_the_spec_commands_refuse_a_task_without_a_spec_block(project: Path):
    init(project, spec=None)
    assert coldsweep(project, "spec", "freeze").returncode == 2
    assert "no `spec:` block" in coldsweep(project, "spec", "status").stderr
