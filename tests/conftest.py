from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coldsweep.models import Profile, RawFinding, ScanRound, ShardResult
from coldsweep.store import Paths

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(yaml.safe_load((FIXTURES / "profile_mixed.yaml").read_text()))


def load_round(name: str) -> ScanRound:
    return ScanRound.model_validate_json((FIXTURES / name).read_text())


def make_round(round_no: int, *shards: tuple[str, list[str], list[dict]], ok: bool = True,
               source: str = "agent") -> ScanRound:
    """Build a round record from inline finding dicts, for cases a fixture file would obscure."""
    return ScanRound(
        round=round_no,
        shards=[
            ShardResult(shard=sid, files=files, ok=ok, source=source,  # type: ignore[arg-type]
                        findings=[RawFinding.model_validate(f) for f in findings])
            for sid, files, findings in shards
        ],
    )


def git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch git repo with in-scope, out-of-scope and gitignored files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    (tmp_path / "src" / "b.py").write_text("B = 2\n")
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "src" / "nested" / "c.py").write_text("C = 3\n")
    (tmp_path / "src" / "migrations").mkdir()
    (tmp_path / "src" / "migrations" / "0001.py").write_text("M = 0\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n")
    (tmp_path / "notes.txt").write_text("out of scope\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.py").write_text("GEN = 1\n")
    (tmp_path / ".gitignore").write_text("build/\n")
    git_init(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


TASK = "unit"


@pytest.fixture
def paths(repo: Path) -> Paths:
    p = Paths(repo, TASK)
    p.runs.mkdir(parents=True, exist_ok=True)
    return p


def write_profile(paths: Paths, data: dict) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.profile.write_text(yaml.safe_dump(data, sort_keys=False))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
