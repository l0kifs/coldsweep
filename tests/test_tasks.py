"""Task scoping. coldsweep has no default task, and no task can see another's state.

The two regressions pinned here are the defects that motivated the model: a second task
reporting convergence without running a round, and a retired rule leaving stale findings
outside the gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import read_jsonl
from test_e2e import DIRTY, RULES, coldsweep, init, profile_data, project  # noqa: F401

from coldsweep.store import list_tasks, load_findings

pytestmark = pytest.mark.e2e

STATE_COMMANDS = ["shard", "scan", "fix", "verify", "status", "converged", "adjudicate", "run"]


@pytest.mark.parametrize("command", STATE_COMMANDS)
def test_no_command_acts_on_a_task_it_was_not_given(project: Path, command: str):  # noqa: F811
    init(project)
    result = coldsweep(project, command, task=None)
    assert result.returncode != 0
    assert "--task" in result.stderr or "--task" in result.stdout


def test_the_task_may_come_from_the_environment(project: Path):  # noqa: F811
    init(project, task="from-env")
    result = coldsweep(project, "shard", task=None, env={"COLDSWEEP_TASK": "from-env"})
    assert result.returncode == 0, result.stderr
    assert "src/loader.py" in result.stdout


def test_init_requires_a_task_name(project: Path):  # noqa: F811
    assert coldsweep(project, "init", "issues", task=None).returncode != 0


@pytest.mark.parametrize("name", ["../escape", "with/slash", "UPPER", "", "-leading"])
def test_task_names_that_address_the_filesystem_are_rejected(project: Path, name: str):  # noqa: F811
    result = coldsweep(project, "init", "issues", task=name)
    assert result.returncode != 0
    assert not (project / ".coldsweep" / "tasks" / name).exists()


def test_tasks_do_not_share_findings_or_rounds(project: Path):  # noqa: F811
    absence = init(project, task="absence-only", rules=[RULES[0]])
    presence = init(project, task="presence-only", rules=[RULES[1]])

    assert coldsweep(project, "run", task="absence-only").returncode == 0
    assert read_jsonl(absence.findings)
    assert load_findings(presence) == [], "the other task saw none of it"
    assert list(presence.runs.glob("*.json")) == []
    assert sorted(list_tasks(project)) == ["absence-only", "presence-only"]

    absence_rules = {f["rule_id"] for f in read_jsonl(absence.findings)}
    assert absence_rules == {"swallowed-exception"}


def test_a_second_task_runs_rounds_instead_of_inheriting_convergence(project: Path):  # noqa: F811
    """The regression: a fresh task must never open its gate on the previous task's rounds."""
    init(project, task="first", rules=[RULES[0]])
    assert coldsweep(project, "run", task="first").returncode == 0
    assert coldsweep(project, "converged", task="first").returncode == 0

    second = init(project, task="second", rules=[RULES[1]])
    assert coldsweep(project, "converged", task="second").returncode == 1, "a task with no rounds is not converged"

    result = coldsweep(project, "run", task="second")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== round 1 ===" in result.stdout, "the second task starts its own round numbering"
    findings = read_jsonl(second.findings)
    assert {f["rule_id"] for f in findings} == {"undocumented-public-symbol"}
    assert '"""' in (project / "src" / "loader.py").read_text()


def test_retiring_a_rule_puts_its_findings_back_in_the_bucket(project: Path):  # noqa: F811
    """The regression: taxonomy membership is derived, so it cannot go stale in the gate."""
    paths = init(project)
    assert coldsweep(project, "run").returncode == 0
    assert coldsweep(project, "converged").returncode == 0

    profile = yaml.safe_load(paths.profile.read_text())
    profile["rules"] = [RULES[0]]
    paths.profile.write_text(yaml.safe_dump(profile, sort_keys=False))

    assert coldsweep(project, "converged").returncode == 1, "the retired rule's findings are off-taxonomy now"
    report = json.loads(coldsweep(project, "status", "--json").stdout)
    assert len(report["convergence"]["unclassified_ids"]) == 1
    assert report["convergence"]["open_ids"] == [], "off-taxonomy findings are bucketed, not counted as open"

    assert coldsweep(project, "adjudicate", "--wontfix-unclassified").returncode == 0
    assert coldsweep(project, "converged").returncode == 0


def test_task_list_reports_each_gate(project: Path):  # noqa: F811
    init(project, task="done", rules=[RULES[0]])
    init(project, task="pending", rules=[RULES[1]])
    coldsweep(project, "run", task="done")

    rows = {r["task"]: r for r in json.loads(coldsweep(project, "task", "list", "--json", task=None).stdout)}
    assert rows["done"]["converged"] is True and rows["done"]["rounds"] == 3
    assert rows["pending"]["converged"] is False and rows["pending"]["rounds"] == 0

    human = coldsweep(project, "task", "list", task=None).stdout
    assert "done" in human and "open" in human and "shut" in human


def test_an_unknown_task_names_the_ones_that_exist(project: Path):  # noqa: F811
    init(project, task="real")
    result = coldsweep(project, "status", task="imaginary")
    assert result.returncode == 2
    assert "existing tasks: real" in result.stderr
    assert "coldsweep init <template> --task imaginary" in result.stderr


def test_a_repository_with_no_tasks_says_so(project: Path):  # noqa: F811
    assert "no tasks" in coldsweep(project, "task", "list", task=None).stdout
