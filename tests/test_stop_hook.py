"""The stop hook must translate the gate, never re-implement it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import git_init

from coldsweep.hooks.stop import ALLOW_STOP, BLOCK_STOP
from coldsweep.store import Paths

ROOT = Path(__file__).resolve().parent.parent


TASK = "hooked"


def run_hook(payload: dict, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "coldsweep.hooks.stop", *args], input=json.dumps(payload),
                          cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def gated(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    git_init(tmp_path)
    paths = Paths(tmp_path, TASK)
    paths.runs.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text(yaml.safe_dump({
        "version": 1, "scope": {"include": ["src/**/*.py"]},
        "convergence": {"k": 2, "max_rounds": 8},
        "rules": [{"id": "swallowed-exception", "mode": "absence", "description": "d"}],
    }))
    return tmp_path


def test_an_unconverged_task_blocks_the_stop(gated: Path):
    result = run_hook({}, gated, "--task", TASK)
    assert result.returncode == BLOCK_STOP
    assert f"coldsweep status --task {TASK}" in result.stderr


def test_the_hook_refuses_to_guess_which_task_it_gates(gated: Path):
    result = run_hook({}, gated)
    assert result.returncode == BLOCK_STOP
    assert "no task named" in result.stderr


def test_an_already_firing_hook_never_jams_the_gate_shut(gated: Path):
    assert run_hook({"stop_hook_active": True}, gated, "--task", TASK).returncode == ALLOW_STOP


def test_a_converged_task_allows_the_stop(gated: Path):
    paths = Paths(gated, TASK)
    for n in (1, 2):
        paths.ingest_file(n).write_text(json.dumps({"round": n}))
    assert run_hook({}, gated, "--task", TASK).returncode == ALLOW_STOP


def test_an_unknown_task_blocks_rather_than_silently_allowing(tmp_path: Path):
    assert run_hook({}, tmp_path, "--task", "nope").returncode == BLOCK_STOP


@pytest.mark.parametrize("payload", ["[1, 2]", '"a string"', "42", "null", "not json at all"])
def test_a_payload_that_is_not_an_object_allows_the_stop(gated: Path, payload: str):
    """The loop-breaker lives in the payload; unable to read it, the hook must not risk a jam."""
    result = subprocess.run([sys.executable, "-m", "coldsweep.hooks.stop", "--task", TASK],
                            input=payload, cwd=gated, capture_output=True, text=True, check=False)
    assert result.returncode == ALLOW_STOP
    assert "not a JSON object" in result.stderr


def test_a_well_formed_payload_still_reaches_the_gate(gated: Path):
    assert run_hook({"cwd": str(gated)}, gated, "--task", TASK).returncode == BLOCK_STOP
