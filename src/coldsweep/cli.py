"""Command line. Every command is a thin shell over the core; no policy lives here."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from . import converge, mechanical, merge, mutation, store, verify
from . import spec as spec_mod
from .models import (
    Finding,
    FixOutcome,
    FixResult,
    Profile,
    RawFinding,
    Rule,
    ScanRound,
    Shard,
    ShardResult,
)
from .runner import AgentError, Runner
from .shard import ShardError, build_shards, resolve_editable, shard_warnings
from .store import Paths, StoreError

TEMPLATE_DIR = Path(__file__).parent / "templates"

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Automates the fresh-context re-verification cycle for open-ended agent tasks.")

# Flags that override a validated profile field carry that field's bound here too: an
# override arrives straight from argv and never passes through the pydantic model it replaces.
def _repo_opt() -> typer.models.OptionInfo:
    """Fresh OptionInfo per command -- typer mutates the instance it is handed."""
    return typer.Option("--repo", "-C", help="Target repository (default: nearest .coldsweep/ ancestor).")


def _task_opt() -> typer.models.OptionInfo:
    """Required on every command that touches state.

    There is no default task and no last-used pointer. A tool whose whole purpose is to stop
    stale bookkeeping from declaring victory must not guess which task it is acting on.
    """
    return typer.Option("--task", "-t", envvar="COLDSWEEP_TASK",
                        help="Task to act on. Required: coldsweep has no default task.")


def _paths(repo: Path | None, task: str) -> Paths:
    return Paths(store.find_repo(repo), task)


def _load(repo: Path | None, task: str) -> tuple[Paths, Profile, list[Finding]]:
    paths = _paths(repo, task)
    profile = store.load_profile(paths)
    _recover_interrupted_mutation(paths)
    return paths, profile, store.load_findings(paths)


def _recover_interrupted_mutation(paths: Paths) -> None:
    """Every command checks. A killed mutation run leaves a mutant in the working tree, and
    the next thing the user does must not be the one that quietly builds on it."""
    for restored in mutation.restore_interrupted(paths.repo, paths.mutation_lock):
        typer.secho(f"restored {restored}: a mutation run was interrupted and had left this file "
                    f"holding a mutant", fg=typer.colors.YELLOW, err=True)


def _blockers(paths: Paths, profile: Profile) -> list[str]:
    """Gate reasons that do not live in the finding set. Currently: an unfrozen or drifted spec."""
    return spec_mod.blockers(paths.repo, profile, paths.spec_lock)


def _evaluate(paths: Paths, profile: Profile, findings: list[Finding]) -> converge.ConvergenceReport:
    return converge.evaluate(findings, profile, store.completed_rounds(paths),
                             _blockers(paths, profile), store.incomplete_rounds(paths))


def _fail(message: str, code: int = 2) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


@app.command()
def init(
    task: Annotated[str, _task_opt()],
    template: Annotated[str, typer.Argument(
        help="Profile template: issues, docs, or a path to a YAML file.")] = "issues",
    repo: Annotated[Path | None, _repo_opt()] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite this task's profile.")] = False,
) -> None:
    """Create a task: scaffold .coldsweep/tasks/<task>/ from a profile template."""
    root = (repo or Path.cwd()).resolve()
    try:
        paths = Paths(root, task)
    except StoreError as exc:
        _fail(str(exc))
        return
    source = Path(template) if template.endswith((".yaml", ".yml")) else TEMPLATE_DIR / f"{template}.yaml"
    if not source.is_file():
        available = ", ".join(sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml")))
        _fail(f"no such profile template: {template} (available: {available})")
    if paths.profile.exists() and not force:
        _fail(f"task {task!r} already exists at {paths.root}; pass --force to overwrite its profile")

    paths.runs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, paths.profile)
    if not paths.findings.exists():
        paths.findings.write_text("", encoding="utf-8")
    (paths.container / ".gitignore").write_text(
        "tasks/*/index.sqlite\ntasks/*/mutants.sqlite\ntasks/*/mutants.lock\n", encoding="utf-8")
    store.load_profile(paths)

    created = store.load_profile(paths)
    typer.echo(f"created task {task!r} at {paths.root} from {source.name}")
    if created.budget_bounded:
        typer.secho("  every rule here is decided by an agent, so this task is budget-bounded: "
                    "nothing in it forces the gate to close.\n"
                    f"  run it to max_rounds={created.convergence.max_rounds} and read "
                    f"`coldsweep status --task {task}`. Converging is possible here, never "
                    f"promised.", fg=typer.colors.YELLOW)
    typer.echo("  committed: profile.yaml, findings.jsonl, runs/    gitignored: index.sqlite")
    typer.echo(f"  edit {paths.profile} to state the task, then: coldsweep run --task {task}")
    siblings = [t for t in store.list_tasks(paths.repo) if t != task]
    if siblings:
        typer.echo(f"  other tasks in this repo: {', '.join(siblings)}")


task_app = typer.Typer(no_args_is_help=True, help="Inspect the tasks defined in this repository.")
app.add_typer(task_app, name="task")


@task_app.command("list")
def task_list(
    repo: Annotated[Path | None, _repo_opt()] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output.")] = False,
) -> None:
    """List every task, with its round count and whether its gate is open."""
    root = store.find_repo(repo)
    names = store.list_tasks(root)
    rows = []
    for name in names:
        paths = Paths(root, name)
        try:
            profile = store.load_profile(paths)
            findings = store.load_findings(paths)
            # The same evaluation `coldsweep converged` runs, blockers and coverage included. A
            # listing that judged the gate by a cheaper rule would be the stale summary this
            # tool exists to delete.
            report = _evaluate(paths, profile, findings)
            rows.append({"task": name, "profile": profile.name, "rounds": len(report.completed_rounds),
                         "findings": len(findings), "converged": report.converged})
        except (StoreError, spec_mod.SpecError) as exc:
            rows.append({"task": name, "error": str(exc).splitlines()[0]})

    if as_json:
        typer.echo(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        typer.echo("no tasks; create one with `coldsweep init <template> --task <name>`")
        return
    typer.echo(f"{'TASK':<24} {'PROFILE':<12} {'ROUNDS':>6} {'FINDINGS':>9}  GATE")
    for row in rows:
        if "error" in row:
            typer.secho(f"{row['task']:<24} {row['error']}", fg=typer.colors.RED)
            continue
        gate = "open" if row["converged"] else "shut"
        colour = typer.colors.GREEN if row["converged"] else typer.colors.YELLOW
        typer.secho(f"{row['task']:<24} {row['profile']:<12} {row['rounds']:>6} {row['findings']:>9}  {gate}",
                    fg=colour)


@app.command()
def shard(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the shard list as JSON.")] = False,
) -> None:
    """Print the resolved shard list."""
    paths, profile, _ = _load(repo, task)
    shards = build_shards(paths.repo, profile)
    for warning in shard_warnings(profile):
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if as_json:
        typer.echo(json.dumps([s.model_dump() for s in shards], indent=2))
        return
    for s in shards:
        typer.echo(f"{s.id}  {' '.join(s.files)}")
    typer.echo(f"\n{len(shards)} shard(s), {sum(len(s.files) for s in shards)} file(s)", err=True)


@app.command()
def scan(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    round_no: Annotated[int | None, typer.Option(
        "--round", min=1, help="Round number (default: next).")] = None,
) -> None:
    """Run the mechanical checks and one full agent scan; write runs/<N>.json."""
    paths, profile, _ = _load(repo, task)
    n = round_no if round_no is not None else store.next_round(paths)
    shards = build_shards(paths.repo, profile)
    if not shards:
        _fail("scope resolved to zero files; check profile scope.include against `git ls-files`")
    for warning in shard_warnings(profile):
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)

    # Every deterministic subsystem appends to one list. Each is exhaustive over its own rules
    # and blind to the others', so a profile that runs two of them must carry both results.
    deterministic: list[RawFinding] = []
    try:
        if profile.spec is not None:
            drifted = _blockers(paths, profile)
            if drifted:
                for reason in drifted:
                    typer.secho(f"error: {reason}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2)
            traced, spec_report = spec_mod.run(paths.repo, profile, spec_mod.load_lock(paths.spec_lock))
            deterministic.extend(traced)
            typer.echo(f"round {n}: spec {spec_report.items} item(s) -- implemented "
                       f"{spec_report.implemented}, unimplemented {spec_report.unimplemented}, "
                       f"stale markers {spec_report.stale_markers}")
        if profile.mutation is not None:
            survivors, mutation_report = mutation.run(paths.repo, profile, paths.mutants,
                                                      paths.mutation_lock)
            deterministic.extend(survivors)
            for source in mutation_report.unexercised:
                typer.secho(f"round {n}: {source} is never imported by its paired tests",
                            fg=typer.colors.YELLOW, err=True)
            typer.echo(f"round {n}: mutation {mutation_report.mutants} mutant(s) over "
                       f"{mutation_report.shards} file(s) -- killed {mutation_report.killed}, "
                       f"survived {mutation_report.survived}, no tests {mutation_report.no_tests}, "
                       f"cached {mutation_report.cached}, {mutation_report.duration_s}s")
        mech = _deterministic_shards(paths, profile, shards, n, deterministic)
    except (mechanical.MechanicalError, mutation.MutationError, spec_mod.SpecError) as exc:
        _fail(str(exc))
        return
    mech_count = sum(len(r.findings) for r in mech)
    if mech_count:
        typer.echo(f"round {n}: deterministic checks produced {mech_count} finding(s)")

    runner = Runner(paths.repo, profile)
    typer.echo(f"round {n}: scanning {len(shards)} shard(s) with model "
               f"{runner.scan_phase(n).model} (parallelism {profile.agent.parallelism})")
    results: list[ShardResult] = asyncio.run(runner.scan(shards, n))

    scan_round = ScanRound(round=n, profile_version=profile.version, shards=[*results, *mech])
    path = store.save_round(paths, scan_round)

    agent_count = sum(len(r.findings) for r in results)
    typer.echo(f"round {n}: {agent_count} agent finding(s) across {len(results)} shard(s) -> {path}")
    if scan_round.failed_shards:
        typer.secho(f"round {n}: {len(scan_round.failed_shards)} shard(s) failed: "
                    f"{', '.join(scan_round.failed_shards)}", fg=typer.colors.RED, err=True)
        for r in results:
            if not r.ok:
                typer.secho(f"  {r.shard}: {r.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


def _deterministic_shards(paths: Paths, profile: Profile, shards: list[Shard], round_no: int,
                          raws: list[RawFinding] | None = None) -> list[ShardResult]:
    """Deterministic output rides in the same round record as agent output, tagged by source."""
    findings = mechanical.run_all(paths.repo, profile, shards, round_no)
    pending = [RawFinding(rule_id=f.rule_id, anchor=f.anchor, evidence=f.evidence,
                          description=f.description) for f in findings]
    pending.extend(raws or [])
    if not pending:
        return []
    files_of = {s.id: s.files for s in shards}
    file_to_shard = {f: s.id for s in shards for f in s.files}
    grouped: dict[str, list[RawFinding]] = {}
    for raw in pending:
        shard_id = file_to_shard.get(raw.anchor.split("::", 1)[0].strip(), "deterministic")
        grouped.setdefault(shard_id, []).append(raw)
    return [
        ShardResult(shard=shard_id, files=files_of.get(shard_id, []), ok=True, model="mechanical",
                    source="mechanical", findings=group)
        for shard_id, group in sorted(grouped.items())
    ]


spec_app = typer.Typer(no_args_is_help=True, help="Author and freeze the spec of a features task.")
app.add_typer(spec_app, name="spec")


@spec_app.command("freeze")
def spec_freeze(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
) -> None:
    """Record what every spec item says right now. Work is measured against this, not the file."""
    paths, profile, _ = _load(repo, task)
    if profile.spec is None:
        _fail(f"task {task!r} defines no `spec:` block; there is nothing to freeze")
        return
    try:
        items = spec_mod.load_spec(paths.repo, profile.spec)
        previous = spec_mod.load_lock(paths.spec_lock)
        drift = spec_mod.drift_of(previous, items)
        lock, _ = spec_mod.freeze(paths.repo, profile, max([*store.completed_rounds(paths), 0]) + 1)
    except spec_mod.SpecError as exc:
        _fail(str(exc))
        return

    store.atomic_write(paths.spec_lock, lock.model_dump_json(indent=2) + "\n")
    if previous is None:
        typer.secho(f"froze {len(lock.items)} spec item(s) from {profile.spec.path}", fg=typer.colors.GREEN)
    elif drift.clean:
        typer.echo(f"{profile.spec.path} was already frozen and has not drifted")
    else:
        typer.secho(f"re-froze {profile.spec.path}", fg=typer.colors.YELLOW)
        for reason in drift.reasons():
            typer.echo(f"  {reason}")
    typer.echo(f"  {paths.spec_lock}")


@spec_app.command("status")
def spec_status(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output.")] = False,
) -> None:
    """Every spec item, whether it is frozen, and what claims to implement it."""
    paths, profile, _ = _load(repo, task)
    if profile.spec is None:
        _fail(f"task {task!r} defines no `spec:` block")
        return
    try:
        items = spec_mod.load_spec(paths.repo, profile.spec)
        lock = spec_mod.load_lock(paths.spec_lock)
        markers = spec_mod.find_markers(paths.repo, profile)
    except spec_mod.SpecError as exc:
        _fail(str(exc))
        return
    drift = spec_mod.drift_of(lock, items)

    rows = [{"id": i.id, "title": i.title,
             "frozen": bool(lock and i.id in lock.items),
             "drifted": i.id in drift.changed,
             "implemented_at": sorted({a for _f, a in markers.get(i.id, [])})}
            for i in items]
    if as_json:
        typer.echo(json.dumps({"spec": profile.spec.path, "frozen_round": lock.frozen_round if lock else None,
                               "drift": drift.model_dump(), "items": rows}, indent=2, sort_keys=True))
        return

    typer.echo(f"{profile.spec.path}: {len(items)} item(s), "
               f"{'frozen at round ' + str(lock.frozen_round) if lock else 'NOT FROZEN'}")
    for row in rows:
        if not row["frozen"]:
            mark, colour = "new", typer.colors.YELLOW
        elif row["drifted"]:
            mark, colour = "drifted", typer.colors.YELLOW
        elif row["implemented_at"]:
            mark, colour = "traced", typer.colors.GREEN
        else:
            mark, colour = "unimplemented", typer.colors.RED
        typer.secho(f"  {row['id']:<8} {mark:<14} {row['title']}", fg=colour)
        for anchor in row["implemented_at"]:
            typer.echo(f"           -> {anchor}")
    for reason in drift.reasons():
        typer.secho(f"\n{reason}", fg=typer.colors.YELLOW)
    typer.secho("\nthe loop never validates the spec itself: an incomplete spec converges cleanly",
                fg=typer.colors.BRIGHT_BLACK)


@app.command()
def mutants(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output.")] = False,
) -> None:
    """Run the mutation subsystem on its own and report which symbols nothing pins."""
    paths, profile, _ = _load(repo, task)
    if profile.mutation is None:
        _fail(f"task {task!r} defines no `mutation:` block; nothing to run")
        return
    try:
        findings, report = mutation.run(paths.repo, profile, paths.mutants, paths.mutation_lock)
    except mutation.MutationError as exc:
        _fail(str(exc))
        return

    if as_json:
        typer.echo(json.dumps({"report": report.model_dump(),
                               "survivors": [f.model_dump() for f in findings]},
                              indent=2, sort_keys=True))
        return
    typer.echo(f"{report.mutants} mutant(s) over {report.shards} file(s) in {report.duration_s}s")
    typer.echo(f"  killed {report.killed}   survived {report.survived}   no tests {report.no_tests}"
               f"   errors {report.errors}")
    typer.echo(f"  cache hits {report.cached} mutant(s), {report.probes_cached} whole-suite run(s)"
               f"   skipped after first survivor {report.skipped}")
    for source in report.unexercised:
        typer.secho(f"  {source}: never imported by its paired tests, so no mutant of it could "
                    f"ever be killed", fg=typer.colors.YELLOW)
    if not findings:
        typer.secho("\nevery mutant was killed: the suite pins every mutable symbol in scope",
                    fg=typer.colors.GREEN)
        return
    typer.secho(f"\nnothing pins these {len(findings)} symbol(s)", fg=typer.colors.YELLOW)
    for f in findings:
        typer.echo(f"  {f.anchor}\n    {f.description}")


@app.command()
def ingest(
    run_file: Annotated[Path, typer.Argument(help="Path to runs/<N>.json.")],
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    force: Annotated[bool, typer.Option("--force", help="Ingest even though shards failed.")] = False,
    no_llm: Annotated[bool, typer.Option(
        "--no-llm", help="Never adjudicate; the 0.75-0.92 band becomes a new finding.")] = False,
) -> None:
    """Validate, merge and update findings.jsonl."""
    paths, profile, findings = _load(repo, task)
    scan_round = store.load_round(run_file if run_file.is_absolute() else Path.cwd() / run_file)
    if not scan_round.ok and not force:
        _fail(f"round {scan_round.round} has {len(scan_round.failed_shards)} failed shard(s); "
              f"coverage is incomplete. Re-run `coldsweep scan --round {scan_round.round}` or pass --force.")

    adjudicator = None if no_llm else Runner(paths.repo, profile).adjudicator()
    findings, record = merge.merge_round(findings, scan_round, profile, scan_round.round, adjudicator)
    store.save_findings(paths, findings)
    store.save_run_record(paths, record)
    store.rebuild_index(paths, profile, findings)

    typer.echo(
        f"round {record.round}: ingested {record.ingested} -> new {record.new}, exact {record.exact}, "
        f"fuzzy {record.fuzzy}, adjudicated {record.adjudicated}, reopened {record.reopened}, "
        f"stale-closed {record.stale_closed}"
    )
    if record.unclassified:
        typer.secho(f"round {record.round}: {record.unclassified} unclassified finding(s) "
                    f"-- run `coldsweep adjudicate`", fg=typer.colors.YELLOW)


def _unproven_sources(paths: Paths, profile: Profile,
                      outcomes: dict[str, FixResult | AgentError],
                      by_id: dict[str, Finding]) -> dict[str, str]:
    """Sources a fix claimed to resolve whose paired tests do not pass. Reports as it goes.

    A profile whose remedy is a test does not get to record `fixed` on the agent's word.
    """
    claimed = {by_id[o.id].file for r in outcomes.values() if not isinstance(r, AgentError)
               for o in r.results if o.outcome == "fixed" and o.id in by_id}
    rejected = mutation.reject_failing_fixes(paths.repo, profile, sorted(claimed))
    for source, detail in rejected.items():
        typer.secho(f"  {source}: paired tests fail after the fix; its findings stay open",
                    fg=typer.colors.RED, err=True)
        typer.secho(f"    {detail.strip().splitlines()[-1] if detail.strip() else '(no output)'}",
                    fg=typer.colors.RED, err=True)
    return rejected


def _record_outcomes(outcomes: dict[str, FixResult | AgentError],
                     groups: dict[str, list[Finding]], by_id: dict[str, Finding],
                     rejected: dict[str, str], round_no: int) -> dict[str, int]:
    """Apply what each fix agent reported to the finding set. Counts by resulting state."""
    counts = {"fixed": 0, "disputed": 0, "failed": 0, "unproven": 0}
    for file, result in outcomes.items():
        if isinstance(result, AgentError):
            typer.secho(f"  {file}: fix agent failed: {result}", fg=typer.colors.RED, err=True)
            counts["failed"] += 1
            continue
        for outcome in result.results:
            target = by_id.get(outcome.id)
            if target is None:
                typer.secho(f"  {file}: fix agent reported unknown finding id {outcome.id}",
                            fg=typer.colors.YELLOW, err=True)
                continue
            counts[_apply_outcome(target, outcome, rejected, round_no)] += 1
        for missing in {f.id for f in groups[file]} - {o.id for o in result.results}:
            typer.secho(f"  {file}: fix agent did not report on {missing}",
                        fg=typer.colors.YELLOW, err=True)
    return counts


def _apply_outcome(target: Finding, outcome: FixOutcome, rejected: dict[str, str],
                   round_no: int) -> str:
    """Move one finding to the state its fix earned. Returns which state that was."""
    if outcome.outcome != "fixed":
        target.status = "disputed"
        target.adjudicated = False
        target.log(round_no, "dispute", detail=outcome.detail)
        return "disputed"
    if target.file in rejected:
        # Stays open: the next round re-derives it, exactly as if nothing had happened.
        target.log(round_no, "fix-unproven", detail=f"paired tests fail: {rejected[target.file]}")
        return "unproven"
    target.status = "fixed"
    target.log(round_no, "fix", detail=outcome.detail)
    return "fixed"


@app.command()
def fix(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    rule: Annotated[str | None, typer.Option("--rule", "-r", help="Only work findings under this rule.")] = None,
    limit: Annotated[int | None, typer.Option(
        "--limit", min=1, help="Work at most N findings.")] = None,
) -> None:
    """Work open findings."""
    paths, profile, findings = _load(repo, task)
    n = max([*store.completed_rounds(paths), 0])
    todo = [f for f in findings
            if f.status == "open" and not converge.is_unclassified(f, profile)]
    if rule:
        todo = [f for f in todo if f.rule_id == rule]
    todo.sort(key=lambda f: (f.file, f.rule_id, f.id))
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        typer.echo("nothing open to fix")
        return

    groups: dict[str, list[Finding]] = {}
    for f in todo:
        groups.setdefault(f.anchor if profile.fix_scope == "task" else f.file, []).append(f)
    unit = "work item" if profile.fix_scope == "task" else "file"
    typer.echo(f"fixing {len(todo)} finding(s) across {len(groups)} {unit}(s) "
               f"with model {profile.models.fix}")

    by_id = {f.id: f for f in findings}
    editable = resolve_editable(paths.repo, profile) if profile.fix_scope == "task" else None
    outcomes = asyncio.run(Runner(paths.repo, profile).fix(groups, editable))

    rejected = _unproven_sources(paths, profile, outcomes, by_id)
    counts = _record_outcomes(outcomes, groups, by_id, rejected, n)

    store.save_findings(paths, findings)
    store.rebuild_index(paths, profile, findings)
    summary = (f"fixed {counts['fixed']}, disputed {counts['disputed']}, "
               f"failed files {counts['failed']}")
    if counts["unproven"]:
        summary += f", unproven {counts['unproven']} (paired tests fail; left open)"
    typer.echo(summary)
    if counts["failed"]:
        raise typer.Exit(1)


@app.command(name="verify")
def verify_cmd(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
) -> None:
    """Re-check fixed findings against evidence_sha."""
    paths, profile, findings = _load(repo, task)
    n = max([*store.completed_rounds(paths), 0])
    stats = verify.verify_findings(paths.repo, profile, findings, n)
    store.save_findings(paths, findings)
    store.rebuild_index(paths, profile, findings)
    typer.echo(f"verified {stats['verified']}, reopened {stats['reopened']}, "
               f"deferred to next round {stats['deferred']}")


@app.command()
def status(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable status.")] = False,
) -> None:
    """Counts by status and rule; lists unclassified and disputed."""
    paths, profile, findings = _load(repo, task)
    rounds = store.completed_rounds(paths)
    report = _evaluate(paths, profile, findings)
    counts = converge.status_counts(findings)

    if as_json:
        typer.echo(json.dumps({
            "profile": profile.name,
            "rounds": rounds,
            "by_status": dict(counts["status"]),
            "by_rule": dict(counts["rule"]),
            "by_source": dict(counts["source"]),
            "convergence": report.model_dump(),
        }, indent=2, sort_keys=True))
        return

    typer.echo(f"profile: {profile.name}   rounds completed: {len(rounds)}   findings: {len(findings)}")
    typer.echo("\nby status")
    for key in ("open", "fixed", "verified", "lapsed", "disputed", "wontfix"):
        typer.echo(f"  {key:<10} {counts['status'].get(key, 0)}")
    typer.echo("\nby rule")
    for rule_id, count in sorted(counts["rule"].items()):
        marker = "" if rule_id in profile.rule_ids else "  (off-taxonomy)"
        typer.echo(f"  {rule_id:<32} {count}{marker}")

    if report.unclassified_ids:
        typer.secho(f"\nunclassified ({len(report.unclassified_ids)})", fg=typer.colors.YELLOW)
        for f in sorted((f for f in findings if f.id in set(report.unclassified_ids)), key=lambda f: f.id):
            typer.echo(f"  {f.id:<40} {f.anchor}\n    {f.description}")
    if report.disputed_pending_ids:
        typer.secho(f"\ndisputed, not adjudicated ({len(report.disputed_pending_ids)})", fg=typer.colors.YELLOW)
        for f in sorted((f for f in findings if f.id in set(report.disputed_pending_ids)), key=lambda f: f.id):
            typer.echo(f"  {f.id:<40} {f.anchor}\n    {f.description}")

    counts_by_round = dict(report.new_per_round)
    typer.echo(f"\nnew findings per round: "
               f"{', '.join(f'{r}: +{counts_by_round.get(str(r), 0)}' for r in rounds) or '(none)'}")
    if report.converged:
        typer.secho(f"\nconverged: {report.k} consecutive quiet round(s), nothing open", fg=typer.colors.GREEN)
    else:
        typer.secho("\nnot converged", fg=typer.colors.YELLOW)
        for reason in report.reasons:
            typer.echo(f"  - {reason}")


@app.command()
def converged(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
) -> None:
    """Exit 0 if converged, 1 otherwise. Prints nothing -- this is the gate."""
    try:
        paths, profile, findings = _load(repo, task)
        report = _evaluate(paths, profile, findings)
    except (StoreError, ShardError, spec_mod.SpecError):
        raise typer.Exit(1) from None
    raise typer.Exit(0 if report.converged else 1)


@app.command()
def adjudicate(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    wontfix_unclassified: Annotated[bool, typer.Option("--wontfix-unclassified",
        help="Non-interactive: mark every unclassified finding wontfix.")] = False,
    accept_disputes: Annotated[bool, typer.Option("--accept-disputes",
        help="Non-interactive: accept every pending dispute as adjudicated.")] = False,
) -> None:
    """Interactive triage of disputed and unclassified findings."""
    paths, profile, findings = _load(repo, task)
    n = max([*store.completed_rounds(paths), 0])
    unclassified = converge.unclassified_pending(findings, profile)
    disputes = converge.disputed_pending(findings)
    if not unclassified and not disputes:
        typer.echo("nothing to adjudicate")
        return

    profile, touched = _triage_unclassified(paths, profile, unclassified, n, wontfix_unclassified)
    touched += _triage_disputes(disputes, n, accept_disputes)

    store.save_findings(paths, findings)
    store.rebuild_index(paths, profile, findings)
    typer.echo(f"\nadjudicated {touched} finding(s)")


def _triage_unclassified(paths: Paths, profile: Profile, unclassified: list[Finding], round_no: int,
                         wontfix_all: bool) -> tuple[Profile, int]:
    """Off-taxonomy findings: adopt the rule, retire the finding, or leave it in the bucket."""
    touched = 0
    for f in unclassified:
        typer.echo(f"\nunclassified  {f.id}\n  rule (off-taxonomy): {f.rule_id}\n  anchor: {f.anchor}\n"
                   f"  {f.description}")
        choice = "w" if wontfix_all else typer.prompt(
            "  [a]dd rule to taxonomy / [w]ontfix / [s]kip", default="s").strip().lower()[:1]
        if choice == "a":
            mode = typer.prompt("  mode [absence|presence]", default="absence").strip()
            description = typer.prompt("  rule description", default=f.description).strip()
            try:
                rule = Rule(id=f.rule_id, mode=mode, description=description)
                profile = store.append_rule(paths, profile, rule)
            except ValidationError:
                typer.secho(f"  mode must be 'absence' or 'presence', got {mode!r}; skipped",
                            fg=typer.colors.RED, err=True)
                continue
            except StoreError as exc:
                typer.secho(f"  {exc}", fg=typer.colors.RED, err=True)
                continue
            f.log(round_no, "classify", detail=f"rule {f.rule_id} added to taxonomy")
            touched += 1
        elif choice == "w":
            f.status = "wontfix"
            f.adjudicated = True
            f.log(round_no, "wontfix", detail="unclassified, rejected in triage")
            touched += 1
    return profile, touched


def _triage_disputes(disputes: list[Finding], round_no: int, accept_all: bool) -> int:
    """Fix-agent disputes: accept the objection, overrule it, or retire the finding."""
    touched = 0
    for f in disputes:
        typer.echo(f"\ndisputed  {f.id}\n  rule: {f.rule_id}\n  anchor: {f.anchor}\n  {f.description}")
        for event in f.history[-3:]:
            typer.echo(f"    round {event.round} {event.action}: {event.detail}")
        choice = "a" if accept_all else typer.prompt(
            "  [a]ccept dispute / [r]eopen / [w]ontfix / [s]kip", default="s").strip().lower()[:1]
        if choice == "a":
            f.adjudicated = True
            f.log(round_no, "adjudicate", detail="dispute accepted in triage")
            touched += 1
        elif choice == "r":
            f.status = "open"
            f.adjudicated = False
            # The trail keeps every reopen; the guard counts from here. Deleting the events
            # would reset the same counter and lose the record of what actually happened.
            f.reopen_baseline = f.reopens_logged
            f.log(round_no, "adjudicate",
                  detail="dispute rejected in triage; reopened and the oscillation count reset")
            touched += 1
        elif choice == "w":
            f.status = "wontfix"
            f.adjudicated = True
            f.log(round_no, "wontfix", detail="dispute upheld in triage")
            touched += 1
    return touched


@app.command()
def run(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    max_rounds: Annotated[int | None, typer.Option(
        "--max-rounds", min=1, help="Override the profile hard stop.")] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Never adjudicate during merge.")] = False,
    no_fix: Annotated[bool, typer.Option("--no-fix", help="Scan and ingest only; never edit the repository.")] = False,
) -> None:
    """Full loop until converged or max_rounds."""
    paths, profile, _ = _load(repo, task)
    ceiling = max_rounds if max_rounds is not None else profile.convergence.max_rounds

    while True:
        findings = store.load_findings(paths)
        rounds = store.completed_rounds(paths)
        report = _evaluate(paths, profile, findings)
        if report.converged:
            typer.secho(f"converged after {len(rounds)} round(s)", fg=typer.colors.GREEN)
            _summary(paths)
            return
        if report.needs_triage:
            pending = len(report.disputed_pending_ids) + len(report.unclassified_ids)
            typer.secho(f"stopping after {len(rounds)} round(s): {pending} finding(s) need triage, "
                        f"and no further round can clear them", fg=typer.colors.YELLOW, err=True)
            for reason in report.reasons:
                typer.secho(f"  - {reason}", fg=typer.colors.YELLOW, err=True)
            typer.secho(f"  run: coldsweep adjudicate --task {task}", fg=typer.colors.YELLOW, err=True)
            _summary(paths)
            raise typer.Exit(1)
        if len(rounds) >= ceiling:
            spent = profile.budget_bounded
            colour = typer.colors.YELLOW if spent else typer.colors.RED
            headline = (f"budget spent: {len(rounds)} round(s) of max_rounds={ceiling}. This profile "
                        f"has no deterministic decider, so nothing in it forced the gate to close"
                        if spent else
                        f"hard stop: {len(rounds)} round(s) reached max_rounds={ceiling} without converging")
            typer.secho(headline, fg=colour, err=True)
            for reason in report.reasons:
                typer.secho(f"  - {reason}", fg=colour, err=True)
            _summary(paths)
            raise typer.Exit(1)

        n = store.next_round(paths)
        typer.secho(f"\n=== round {n} ===", bold=True)
        try:
            scan(task=task, repo=repo, round_no=n)
        except typer.Exit as exc:
            if exc.exit_code != 0:
                typer.secho(f"round {n}: scan failed; stopping rather than ingesting partial coverage",
                            fg=typer.colors.RED, err=True)
                raise
        ingest(run_file=paths.run_file(n), task=task, repo=repo, force=False, no_llm=no_llm)
        if not no_fix:
            try:
                fix(task=task, repo=repo, rule=None, limit=None)
            except typer.Exit as exc:
                # A dead fix agent costs one round's work, never the budget. Whatever it failed
                # to resolve is still open, the next round re-derives it from scratch, and the
                # gate -- not the agent's exit code -- decides whether the task is done.
                if not exc.exit_code:
                    raise
                typer.secho(f"round {n}: some fix agents failed; continuing, their findings stay "
                            f"open for the next round", fg=typer.colors.YELLOW, err=True)
            verify_cmd(task=task, repo=repo)


def _summary(paths: Paths) -> None:
    findings = store.load_findings(paths)
    counts = converge.status_counts(findings)
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts["status"].items())))



def main() -> None:
    try:
        app()
    except (StoreError, ShardError, AgentError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
