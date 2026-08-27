"""Persistence. ``findings.jsonl`` is the source of truth; SQLite is a rebuildable index."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Finding, Profile, Rule, RunRecord, ScanRound, SpendRecord

COLDSWEEP_DIR = ".coldsweep"
TASKS_DIR = "tasks"
TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RULES_BLOCK = re.compile(r"^rules:[ \t]*$", re.MULTILINE)


class StoreError(RuntimeError):
    pass


def validate_task(name: str) -> str:
    """Task names address a directory, so they are checked, never merely trusted."""
    if not TASK_NAME.match(name or ""):
        raise StoreError(
            f"invalid task name {name!r}: use lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit, at most 64 characters"
        )
    return name


class Paths:
    """Every path coldsweep touches inside a target repo, for one named task.

    There is no default task and no fallback: state cannot be reached without naming the task
    it belongs to, so no command can act on a task the caller did not choose.
    """

    def __init__(self, repo: Path, task: str) -> None:
        self.repo = repo.resolve()
        self.task = validate_task(task)
        self.container = self.repo / COLDSWEEP_DIR
        self.root = self.container / TASKS_DIR / self.task

    @property
    def profile(self) -> Path:
        return self.root / "profile.yaml"

    @property
    def findings(self) -> Path:
        return self.root / "findings.jsonl"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def spec_lock(self) -> Path:
        """The freeze record. Committed: it is a decision, not a derived artefact."""
        return self.root / "spec.lock"

    @property
    def spend(self) -> Path:
        """What this task's agent calls cost. Committed: an event log, not a derived index.

        Nothing can rebuild it -- the subprocesses are gone and their envelopes with them -- so
        it is source of truth in the same sense findings.jsonl is.
        """
        return self.root / "spend.jsonl"

    @property
    def mutants(self) -> Path:
        """Mutation cache: derived and rebuildable, but expensive, so it is kept out of git."""
        return self.root / "mutants.sqlite"

    @property
    def mutation_lock(self) -> Path:
        """Names the file a mutation run is currently holding swapped out.

        Present only while a mutant is being judged. Left behind, it is the single cheap signal
        that a run died with the working tree modified.
        """
        return self.root / "mutants.lock"

    def run_file(self, round_no: int) -> Path:
        return self.runs / f"{round_no}.json"

    def ingest_file(self, round_no: int) -> Path:
        return self.runs / f"{round_no}.ingest.json"


def find_repo(start: Path | None = None) -> Path:
    """Walk up for a directory containing .coldsweep/, else fall back to the starting directory."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / COLDSWEEP_DIR / TASKS_DIR).is_dir():
            return candidate
    return here


def list_tasks(repo: Path) -> list[str]:
    """Every task that has a profile. Order is stable so listings and errors are comparable."""
    root = repo.resolve() / COLDSWEEP_DIR / TASKS_DIR
    if not root.is_dir():
        return []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise StoreError(f"cannot list {root}: {exc}") from exc
    return sorted(entry.name for entry in entries if (entry / "profile.yaml").is_file())


def atomic_write(path: Path, data: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    except OSError as exc:
        raise StoreError(f"cannot write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_profile(paths: Paths) -> Profile:
    if not paths.profile.is_file():
        known = list_tasks(paths.repo)
        hint = f"existing tasks: {', '.join(known)}" if known else "no tasks exist yet"
        raise StoreError(
            f"task {paths.task!r} has no profile at {paths.profile}\n"
            f"  {hint}\n"
            f"  create it with: coldsweep init <template> --task {paths.task}"
        )
    try:
        text = paths.profile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {paths.profile}: {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise StoreError(f"invalid profile {paths.profile}:\n{exc}") from exc
    try:
        return Profile.model_validate(raw)
    except ValidationError as exc:
        raise StoreError(f"invalid profile {paths.profile}:\n{exc}") from exc


def save_profile(paths: Paths, profile: Profile) -> None:
    """Serialise the whole model over the file.

    Every comment in the file is lost. profile.yaml is the committed statement of the task and
    is written by hand, so nothing on the normal path may call this -- see ``append_rule`` for
    editing the taxonomy.
    """
    data = profile.model_dump(mode="json", exclude_defaults=False)
    atomic_write(paths.profile, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _render_rule(rule: Rule) -> list[str]:
    """One rule as YAML sequence-item lines, indented to match the templates."""
    body = yaml.safe_dump(rule.model_dump(exclude_defaults=True), sort_keys=False,
                          allow_unicode=True, width=100).splitlines()
    return ["  - " + body[0]] + ["    " + line for line in body[1:]]


def append_rule(paths: Paths, profile: Profile, rule: Rule) -> Profile:
    """Add one rule to the taxonomy, leaving the rest of the file byte for byte as it was.

    Re-serialising the model would be shorter and would delete every comment in a file whose
    whole job is to explain the task to the next reader. So the rule is spliced into the
    existing text instead, and the result is re-parsed and compared against what was intended
    before anything is written. A splice that would change anything beyond the taxonomy is an
    error naming the block to paste, never a silent rewrite.
    """
    if not paths.profile.is_file():
        raise StoreError(f"task {paths.task!r} has no profile at {paths.profile}")
    try:
        text = paths.profile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {paths.profile}: {exc}") from exc
    block = _render_rule(rule)
    manual = "\n".join(block)
    match = _RULES_BLOCK.search(text)
    if match is None:
        raise StoreError(f"{paths.profile} has no block-style `rules:` section to extend; "
                         f"add the rule by hand:\n{manual}")

    lines = text.splitlines()
    start = text[:match.start()].count("\n")
    last = start
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not lines[i][0].isspace() and not stripped.startswith("#"):
            break                       # the next top-level key ends the block
        if stripped and not stripped.startswith("#"):
            last = i                    # an indented, non-comment line is part of the block
    candidate = "\n".join([*lines[:last + 1], *block, *lines[last + 1:]])
    if text.endswith("\n"):
        candidate += "\n"

    try:
        parsed = Profile.model_validate(yaml.safe_load(candidate) or {})
    except (yaml.YAMLError, ValidationError) as exc:
        raise StoreError(f"adding {rule.id!r} to {paths.profile} would not have produced a valid "
                         f"profile; add it by hand:\n{manual}\n{exc}") from exc
    if (parsed.rules != [*profile.rules, rule]
            or parsed.model_dump(exclude={"rules"}) != profile.model_dump(exclude={"rules"})):
        raise StoreError(f"adding {rule.id!r} to {paths.profile} would have changed more than the "
                         f"taxonomy; add it by hand:\n{manual}")
    atomic_write(paths.profile, candidate)
    return parsed


def load_findings(paths: Paths) -> list[Finding]:
    if not paths.findings.is_file():
        return []
    try:
        text = paths.findings.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {paths.findings}: {exc}") from exc
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Finding.model_validate_json(line))
        except ValidationError as exc:
            raise StoreError(f"{paths.findings}:{lineno}: corrupt finding record:\n{exc}") from exc
    return out


def save_findings(paths: Paths, findings: Iterable[Finding]) -> None:
    """One JSON object per line, sorted by id, so diffs and blame stay readable."""
    ordered = sorted(findings, key=lambda f: f.id)
    body = "".join(f.model_dump_json(exclude_none=False) + "\n" for f in ordered)
    atomic_write(paths.findings, body)


def save_round(paths: Paths, scan: ScanRound) -> Path:
    path = paths.run_file(scan.round)
    atomic_write(path, json.dumps(scan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return path


def load_round(path: Path) -> ScanRound:
    if not path.is_file():
        raise StoreError(f"no scan output at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc
    try:
        return ScanRound.model_validate_json(text)
    except ValidationError as exc:
        raise StoreError(f"{path}: invalid scan output:\n{exc}") from exc


def save_run_record(paths: Paths, record: RunRecord) -> Path:
    path = paths.ingest_file(record.round)
    atomic_write(path, json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return path


def append_spend(paths: Paths, record: SpendRecord) -> None:
    """Append one agent call to the ledger. Append-only, one line, never rewritten."""
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        with paths.spend.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
    except OSError as exc:
        raise StoreError(f"cannot write {paths.spend}: {exc}") from exc


def load_spend(paths: Paths) -> list[SpendRecord]:
    """Every recorded agent call. A malformed line is a hard error, like every other store."""
    if not paths.spend.is_file():
        return []
    try:
        text = paths.spend.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {paths.spend}: {exc}") from exc
    out: list[SpendRecord] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(SpendRecord.model_validate_json(line))
        except ValidationError as exc:
            raise StoreError(f"{paths.spend}:{lineno}: {exc}") from exc
    return out


def completed_rounds(paths: Paths) -> list[int]:
    """Rounds that finished ingest. A scanned-but-not-ingested round does not count."""
    if not paths.runs.is_dir():
        return []
    try:
        entries = list(paths.runs.glob("*.ingest.json"))
    except OSError as exc:
        raise StoreError(f"cannot list {paths.runs}: {exc}") from exc
    rounds: list[int] = []
    for entry in entries:
        stem = entry.name.removesuffix(".ingest.json")
        if stem.isdigit():
            rounds.append(int(stem))
    return sorted(rounds)


def incomplete_rounds(paths: Paths) -> list[int]:
    """Completed rounds whose ingest did not cover every shard.

    A round ingested with ``--force`` is still a round, but half a scan producing no new
    findings is not evidence that there is nothing new to find, so it must not count toward the
    quiet window. An ingest record that cannot be read is treated as incomplete for the same
    reason: it cannot show coverage, and the gate errs shut.
    """
    out: list[int] = []
    for round_no in completed_rounds(paths):
        try:
            record = RunRecord.model_validate_json(
                paths.ingest_file(round_no).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            print(f"coldsweep: {paths.ingest_file(round_no)}: unreadable ingest record, "
                  f"treating round {round_no} as incomplete: {exc}", file=sys.stderr)
            out.append(round_no)
            continue
        if record.failed_shards:
            out.append(round_no)
    return out


def next_round(paths: Paths) -> int:
    done = completed_rounds(paths)
    scanned = []
    if paths.runs.is_dir():
        try:
            scanned = [int(p.stem) for p in paths.runs.glob("*.json") if p.stem.isdigit()]
        except OSError as exc:
            raise StoreError(f"cannot list {paths.runs}: {exc}") from exc
    return max([*done, *scanned, 0]) + 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    anchor TEXT NOT NULL,
    file TEXT NOT NULL,
    evidence_sha TEXT,
    description TEXT,
    shard TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    unclassified INTEGER NOT NULL,
    adjudicated INTEGER NOT NULL,
    first_seen_round INTEGER NOT NULL,
    last_seen_round INTEGER NOT NULL,
    reopen_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_rule ON findings(rule_id);
CREATE INDEX IF NOT EXISTS findings_status ON findings(status);
CREATE TABLE IF NOT EXISTS events (
    finding_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    round INTEGER NOT NULL,
    action TEXT NOT NULL,
    method TEXT,
    score REAL,
    detail TEXT,
    PRIMARY KEY (finding_id, seq)
);
"""


def rebuild_index(paths: Paths, profile: Profile, findings: Iterable[Finding] | None = None) -> Path:
    """Derived index only -- dropped and rebuilt from findings.jsonl on every ingest."""
    items = list(findings) if findings is not None else load_findings(paths)
    taxonomy = profile.rule_ids
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.index.unlink(missing_ok=True)
    except OSError as exc:
        raise StoreError(f"cannot rebuild {paths.index}: {exc}") from exc
    try:
        con = sqlite3.connect(paths.index)
    except sqlite3.Error as exc:
        raise StoreError(f"cannot open {paths.index}: {exc}") from exc
    try:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.id, f.rule_id, f.anchor, f.file, f.evidence_sha, f.description, f.shard,
                    f.status, f.source, int(f.rule_id not in taxonomy), int(f.adjudicated),
                    f.first_seen_round, f.last_seen_round, f.reopen_count,
                )
                for f in items
            ],
        )
        con.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            [
                (f.id, i, e.round, e.action, e.method, e.score, e.detail)
                for f in items
                for i, e in enumerate(f.history)
            ],
        )
        con.commit()
    except sqlite3.Error as exc:
        raise StoreError(f"cannot rebuild {paths.index}: {exc}") from exc
    finally:
        con.close()
    return paths.index
