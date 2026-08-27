"""Command line. Every command is a thin shell over the core; no policy lives here."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from . import converge, mechanical, merge, mutation, store, syntax, verify
from . import spec as spec_mod
from .models import (
    Finding,
    FixOutcome,
    FixResult,
    Profile,
    RawFinding,
    Rule,
    RunRecord,
    ScanRound,
    Shard,
    ShardResult,
    SpendRecord,
    evidence_sha,
)
from .runner import AgentError, Runner, fix_lanes
from .shard import (
    ShardError,
    build_shards,
    editable_slice,
    resolve_editable,
    shard_warnings,
)
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
    try:
        restored = mutation.restore_interrupted(paths.repo, paths.mutation_lock)
    except (OSError, mutation.MutationError) as exc:
        _fail(f"could not recover an interrupted mutation run: {exc}")
        return
    for r in restored:
        typer.secho(f"restored {r}: a mutation run was interrupted and had left this file "
                    f"holding a mutant", fg=typer.colors.YELLOW, err=True)


def _blockers(paths: Paths, profile: Profile) -> list[str]:
    """Gate reasons that do not live in the finding set. Currently: an unfrozen or drifted spec."""
    return spec_mod.blockers(paths.repo, profile, paths.spec_lock)


def _evaluate(paths: Paths, profile: Profile, findings: list[Finding]) -> converge.ConvergenceReport:
    return converge.evaluate(findings, profile, store.completed_rounds(paths),
                             _blockers(paths, profile), store.incomplete_rounds(paths))


def _runner(paths: Paths, profile: Profile, round_no: int) -> Runner:
    """Every Runner the CLI builds bills to the task's ledger. There is no unbilled path."""
    return Runner(paths.repo, profile, ledger=lambda r: store.append_spend(paths, r),
                  round_no=round_no)


def _fail(message: str, code: int = 2) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _save_findings(paths: Paths, profile: Profile, findings: list[Finding],
                   record: RunRecord | None = None) -> None:
    """Findings, an optional run record, and the index rebuilt from them: one write unit.

    Everything in ``store`` reports failure as ``StoreError``; caught here so the message names
    the task state that could not be written, rather than surfacing from ``main`` with no idea
    which write failed.
    """
    try:
        store.save_findings(paths, findings)
        if record is not None:
            store.save_run_record(paths, record)
        store.rebuild_index(paths, profile, findings)
    except StoreError as exc:
        _fail(f"could not save findings: {exc}")


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

    try:
        paths.runs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, paths.profile)
        for committed in (paths.findings, paths.spend):
            if not committed.exists():
                committed.write_text("", encoding="utf-8")
        (paths.container / ".gitignore").write_text(
            "tasks/*/index.sqlite\ntasks/*/mutants.sqlite\ntasks/*/mutants.lock\n", encoding="utf-8")
    except OSError as exc:
        _fail(f"could not scaffold task {task!r}: {exc}")
        return
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
def languages(
    as_json: Annotated[bool, typer.Option("--json", help="Emit the support table as JSON.")] = False,
) -> None:
    """Which languages resolve to symbols here, and which need a grammar installed.

    Worth running before trusting a non-Python task. A language without its grammar is not an
    error anywhere -- `verify` defers instead of deciding, and mutation skips the file -- so the
    only symptom is work quietly not happening. This is where that becomes visible.
    """
    rows = syntax.support()
    if as_json:
        typer.echo(json.dumps([{"language": n, "extensions": e.split(), "available": ok}
                               for n, e, ok in rows], indent=2))
        return
    for name, extensions, available in rows:
        mark = "yes" if available else "no"
        colour = typer.colors.GREEN if available else typer.colors.YELLOW
        typer.secho(f"{mark:4} {name:12} {extensions}", fg=colour)
    missing = [name for name, _, ok in rows if not ok]
    if missing:
        typer.echo("\ninstall the missing grammars with:  uv add coldsweep[languages]", err=True)


def _run_subsystems(paths: Paths, profile: Profile, n: int) -> list[RawFinding]:
    """Every deterministic subsystem this profile runs, into one list.

    Each is exhaustive over its own rules and blind to the others', so a profile that runs two
    of them must carry both results.
    """
    out: list[RawFinding] = []
    if profile.spec is not None:
        drifted = _blockers(paths, profile)
        if drifted:
            for reason in drifted:
                typer.secho(f"error: {reason}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        traced, spec_report = spec_mod.run(paths.repo, profile, spec_mod.load_lock(paths.spec_lock))
        out.extend(traced)
        typer.echo(f"round {n}: spec {spec_report.items} item(s) -- implemented "
                   f"{spec_report.implemented}, unimplemented {spec_report.unimplemented}, "
                   f"stale markers {spec_report.stale_markers}")
    if profile.mutation is not None:
        survivors, report = mutation.run(paths.repo, profile, paths.mutants, paths.mutation_lock)
        out.extend(survivors)
        for source in report.unexercised:
            typer.secho(f"round {n}: {source} is never imported by its paired tests",
                        fg=typer.colors.YELLOW, err=True)
        typer.echo(f"round {n}: mutation {report.mutants} mutant(s) over {report.shards} file(s) "
                   f"-- killed {report.killed}, survived {report.survived}, "
                   f"no tests {report.no_tests}, not built {report.not_built}, "
                   f"cached {report.cached}, {report.duration_s}s")
    return out


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

    try:
        mech = _deterministic_shards(paths, profile, shards, n,
                                     _run_subsystems(paths, profile, n))
    except (mechanical.MechanicalError, mutation.MutationError, spec_mod.SpecError) as exc:
        _fail(str(exc))
        return
    mech_count = sum(len(r.findings) for r in mech)
    if mech_count:
        typer.echo(f"round {n}: deterministic checks produced {mech_count} finding(s)")

    runner = _runner(paths, profile, n)
    typer.echo(f"round {n}: scanning {len(shards)} shard(s) with model "
               f"{runner.scan_phase(n).model} (parallelism {profile.agent.parallelism})")
    results: list[ShardResult] = asyncio.run(runner.scan(shards, n))

    scan_round = ScanRound(round=n, profile_version=profile.version, shards=[*results, *mech])
    try:
        path = store.save_round(paths, scan_round)
    except StoreError as exc:
        _fail(f"could not save round {n}: {exc}")
        return

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
    decides = bool(profile.mechanical or profile.mutation or profile.spec)
    if not pending:
        # A clean pass has to leave a trace. Returning nothing makes "the deciders ran and found
        # nothing" identical on the record to "the deciders never ran", and merge closes findings
        # on the difference: for a rule a subsystem owns, absence from a *complete* pass is proof,
        # and absence from no pass at all is nothing.
        return [ShardResult(shard="deterministic", files=[], ok=True, model="mechanical",
                            source="mechanical", findings=[])] if decides else []
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

    try:
        store.atomic_write(paths.spec_lock, lock.model_dump_json(indent=2) + "\n")
    except StoreError as exc:
        _fail(f"could not write {paths.spec_lock}: {exc}")
        return
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
               f"   not built {report.not_built}   errors {report.errors}")
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
        "--no-llm", help="Never adjudicate; the 0.75-0.92 band and same-anchor pairs "
                          "become new findings.")] = False,
) -> None:
    """Validate, merge and update findings.jsonl."""
    paths, profile, findings = _load(repo, task)
    scan_round = store.load_round(run_file if run_file.is_absolute() else Path.cwd() / run_file)
    if not scan_round.ok and not force:
        _fail(f"round {scan_round.round} has {len(scan_round.failed_shards)} failed shard(s); "
              f"coverage is incomplete. Re-run `coldsweep scan --round {scan_round.round}` or pass --force.")

    adjudicator = None if no_llm else _runner(paths, profile, scan_round.round).adjudicator()
    findings, record = merge.merge_round(findings, scan_round, profile, scan_round.round, adjudicator)
    _save_findings(paths, profile, findings, record)

    adjudication = f"adjudicated {record.adjudicated}"
    if record.adjudicator_calls:
        # A ruling of "different" merges nothing, so the merge count alone reads as though no
        # adjudication happened -- while every call was still an agent subprocess.
        adjudication += f" of {record.adjudicator_calls} call(s)"
    typer.echo(
        f"round {record.round}: ingested {record.ingested} -> new {record.new}, exact {record.exact}, "
        f"fuzzy {record.fuzzy}, {adjudication}, reopened {record.reopened}, "
        f"stale-closed {record.stale_closed}"
    )
    if record.unclassified:
        typer.secho(f"round {record.round}: {record.unclassified} unclassified finding(s) "
                    f"-- run `coldsweep adjudicate`", fg=typer.colors.YELLOW)


def _snapshot_symbols(paths: Paths, todo: list[Finding]) -> None:
    """Record each anchored symbol as it stands *before* the agents run.

    Verification compares against this to tell an additive remedy -- handling wrapped around
    the cited line, which leaves the line in place -- from an agent that changed nothing. Taken
    here because this is the last moment the pre-fix state exists.
    """
    sources: dict[str, str | None] = {}
    for finding in todo:
        if finding.file not in sources:
            try:
                sources[finding.file] = (paths.repo / finding.file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                typer.secho(f"  {finding.file}: could not read for pre-fix snapshot ({exc}); "
                            f"an additive fix to it will not be distinguished from no change",
                            fg=typer.colors.YELLOW, err=True)
                sources[finding.file] = None
        source = sources[finding.file]
        body = spec_mod.symbol_text(source, finding.anchor) if source is not None else None
        finding.pre_fix_sha = evidence_sha(body) if body is not None else None


def _editable_slices(paths: Paths, profile: Profile,
                     groups: dict[str, list[Finding]]) -> dict[str, list[str]]:
    """Which files each fix group may write, and therefore which groups can run together."""
    editable = resolve_editable(paths.repo, profile)
    slices = {key: editable_slice(profile, key, editable) for key in groups}
    lanes = fix_lanes(groups, slices)
    if len(lanes) < len(groups):
        serialised = sum(len(lane) - 1 for lane in lanes if len(lane) > 1)
        typer.echo(f"  {serialised} work item(s) share a file with another and run in sequence "
                   f"({len(lanes)} lane(s))")
    return slices


def _unproven_sources(paths: Paths, profile: Profile,
                      outcomes: dict[str, FixResult | AgentError],
                      by_id: dict[str, Finding]) -> dict[str, str]:
    """Sources a fix claimed to resolve whose paired tests do not pass. Reports as it goes.

    A profile whose remedy is a test does not get to record `fixed` on the agent's word.
    """
    claimed = {by_id[o.id].file for r in outcomes.values() if not isinstance(r, AgentError)
               for o in r.results if o.outcome == "fixed" and o.id in by_id}
    try:
        rejected = mutation.reject_failing_fixes(paths.repo, profile, sorted(claimed))
    except mutation.MutationError as exc:
        _fail(str(exc))
        return {}
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
        if rule not in profile.rule_ids:
            _fail(f"no such rule {rule!r} in profile {profile.name!r}; known rules: "
                  f"{', '.join(sorted(profile.rule_ids))}")
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
    _snapshot_symbols(paths, todo)
    slices = _editable_slices(paths, profile, groups) if profile.fix_scope == "task" else None
    outcomes = asyncio.run(_runner(paths, profile, n).fix(groups, slices))

    rejected = _unproven_sources(paths, profile, outcomes, by_id)
    counts = _record_outcomes(outcomes, groups, by_id, rejected, n)

    _save_findings(paths, profile, findings)
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
    stats = verify.verify_findings(paths.repo, profile, findings, n,
                                   paths.mutants, paths.mutation_lock)
    _save_findings(paths, profile, findings)
    typer.echo(f"verified {stats['verified']}, reopened {stats['reopened']}, "
               f"deferred to next round {stats['deferred']}")


def _report_spend(records: list[SpendRecord], total: converge.Spend) -> None:
    """What the task has cost so far, and how much of that is actually known."""
    if not total.calls:
        return
    typer.echo(f"\nspend over {total.calls} agent call(s)")
    if total.unmeasured == total.calls:
        # Printing $0.00 here would answer a question nobody can answer from this data.
        typer.secho("  unmeasured  no call reported usage; this agent command emits no envelope",
                    fg=typer.colors.YELLOW)
        return
    typer.echo(f"  cost       ${total.cost_usd:,.2f}")
    typer.echo(f"  tokens     {total.tokens:,}  "
               f"(in {total.input_tokens:,}  out {total.output_tokens:,}  "
               f"cache write {total.cache_creation_tokens:,}  read {total.cache_read_tokens:,})")
    by_phase = converge.spend_by(records, "phase")
    typer.echo("  by phase   " + "  ".join(
        f"{phase} ${s.cost_usd:,.2f} ({s.calls})" for phase, s in by_phase.items()))
    by_round = converge.spend_by(records, "round")
    typer.echo("  by round   " + "  ".join(
        f"{rnd}: ${s.cost_usd:,.2f}" for rnd, s in sorted(by_round.items(), key=lambda kv: int(kv[0]))))
    if total.failed:
        typer.secho(f"  failed     {total.failed} call(s) were paid for and returned nothing usable",
                    fg=typer.colors.YELLOW)
    if not total.complete:
        typer.secho(f"  unmeasured {total.unmeasured} call(s) returned no usage; "
                    f"the totals above are a lower bound", fg=typer.colors.YELLOW)


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

    spend = store.load_spend(paths)
    total = converge.tally(spend)

    if as_json:
        typer.echo(json.dumps({
            "profile": profile.name,
            "rounds": rounds,
            "by_status": dict(counts["status"]),
            "by_rule": dict(counts["rule"]),
            "by_source": dict(counts["source"]),
            "convergence": report.model_dump(),
            "spend": {
                "total": asdict(total),
                "by_phase": {k: asdict(v) for k, v in converge.spend_by(spend, "phase").items()},
                "by_round": {k: asdict(v) for k, v in converge.spend_by(spend, "round").items()},
            },
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

    _report_spend(spend, total)

    counts_by_round = dict(report.new_per_round)
    typer.echo(f"\nnew findings per round: "
               f"{', '.join(f'{r}: +{counts_by_round.get(str(r), 0)}' for r in rounds) or '(none)'}")
    if report.converged:
        typer.secho(f"\nconverged: {report.k} consecutive quiet round(s), nothing open", fg=typer.colors.GREEN)
    else:
        typer.secho("\nnot converged", fg=typer.colors.YELLOW)
        for reason in report.reasons:
            typer.echo(f"  - {reason}")
    _report_halves(report)


def _report_halves(report: converge.ConvergenceReport) -> None:
    """The two halves of the taxonomy, separately.

    A profile whose rules are all agent-decided has nothing to split, so it prints nothing: the
    global verdict already says everything there is to say. The split earns its line only where
    a deterministic decider sits next to a plateauing one and the single verdict would let the
    plateau hide an answer that is already settled.
    """
    if report.decidable.empty or report.budgeted.empty:
        return
    typer.echo("")
    for half in (report.decidable, report.budgeted):
        verdict = "converged" if half.converged else "not converged"
        colour = typer.colors.GREEN if half.converged else typer.colors.YELLOW
        typer.secho(f"  {half.label:<10} {verdict:<14} "
                    f"{len(half.rule_ids)} rule(s), {half.quiet_rounds} quiet round(s)", fg=colour)
        for reason in half.reasons:
            typer.echo(f"    - {reason}")


@app.command()
def converged(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    half: Annotated[str | None, typer.Option(
        "--half", help="Gate on one half of the taxonomy: 'decidable' or 'budgeted'.")] = None,
) -> None:
    """Exit 0 if converged, 1 otherwise. Prints nothing on those outcomes -- this is the gate.

    ``--half decidable`` gates on the rules a subsystem decides. Those are exhaustive over their
    scope and return the same set every pass, so their window closing is an answer rather than a
    budget running out. On a profile that mixes them with agent-decided rules the global gate
    can never open -- measured twice on this repository, the agent half was still producing new
    findings in the last round of every run -- and a caller that wants the decidable answer would
    otherwise have to parse `status`.

    A genuine failure to evaluate (corrupt findings, bad scope, ...) is not the same as an
    unconverged task: it is reported on stderr and exits 2, so a caller can tell "not done yet"
    from "could not tell". Naming a half that no rule falls into is the same kind of failure: it
    exits 2 rather than reporting a vacuous 0.
    """
    if half is not None and half not in ("decidable", "budgeted"):
        _fail(f"unknown half {half!r}: expected 'decidable' or 'budgeted'")
        return
    try:
        paths, profile, findings = _load(repo, task)
        report = _evaluate(paths, profile, findings)
    except (StoreError, ShardError, spec_mod.SpecError) as exc:
        _fail(f"could not evaluate convergence: {exc}")
        return
    if half is None:
        raise typer.Exit(0 if report.converged else 1)
    chosen: converge.HalfReport = (report.decidable if half == "decidable"
                                   else report.budgeted)
    if chosen.empty:
        _fail(f"no rule in this profile is in the {half} half; there is nothing to gate on")
        return
    raise typer.Exit(0 if chosen.converged else 1)


@app.command()
def adjudicate(
    task: Annotated[str, _task_opt()],
    repo: Annotated[Path | None, _repo_opt()] = None,
    wontfix_unclassified: Annotated[bool, typer.Option("--wontfix-unclassified",
        help="Non-interactive: mark every unclassified finding wontfix.")] = False,
    accept_disputes: Annotated[bool, typer.Option("--accept-disputes",
        help="Non-interactive: accept every pending dispute as adjudicated.")] = False,
) -> None:
    """Triage disputed and unclassified findings, asking only what a person has to decide.

    A dispute under a `decided_by: code` rule is settled against the deciding subsystem first.
    The half it can close it closes; the half it cannot -- a symbol still reported, where the
    disagreement is about how much more to spend rather than about a fact -- reaches the prompt
    with the re-derivation attached.
    """
    paths, profile, findings = _load(repo, task)
    n = max([*store.completed_rounds(paths), 0])
    unclassified = converge.unclassified_pending(findings, profile)
    if converge.disputed_pending(findings):
        settled = verify.settle_disputes(paths.repo, profile, findings, n,
                                         paths.mutants, paths.mutation_lock)
        if any(settled.values()):
            typer.echo(f"re-derived: {settled['verified']} settled as done, "
                       f"{settled['confirmed']} confirmed still open, "
                       f"{settled['undecidable']} could not be re-derived")
        if settled["verified"]:
            _save_findings(paths, profile, findings)
    disputes = converge.disputed_pending(findings)
    if not unclassified and not disputes:
        typer.echo("nothing left to adjudicate")
        return

    profile, touched = _triage_unclassified(paths, profile, unclassified, n, wontfix_unclassified)
    touched += _triage_disputes(disputes, n, accept_disputes)

    _save_findings(paths, profile, findings)
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
            except (StoreError, OSError) as exc:
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
        pending = len(report.disputed_pending_ids)
        bound = profile.convergence.max_disputes
        if bound is not None and pending >= bound:
            typer.secho(f"stopping after {len(rounds)} round(s): {pending} dispute(s) waiting on "
                        f"triage, at or past max_disputes={bound}. Scanning cannot clear a "
                        f"dispute, and each one holds the gate shut",
                        fg=typer.colors.YELLOW, err=True)
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
        _report_round_spend(paths, n)
        _report_dispute_backlog(paths, profile, n)


def _report_round_spend(paths: Paths, round_no: int) -> None:
    """What this round cost, printed as it ends -- the moment the next round is a choice."""
    records = [r for r in store.load_spend(paths) if r.round == round_no]
    total = converge.tally(records)
    if not total.calls or total.unmeasured == total.calls:
        return
    phases = "  ".join(f"{phase} ${s.cost_usd:,.2f} ({s.calls})"
                       for phase, s in converge.spend_by(records, "phase").items())
    typer.echo(f"round {round_no}: cost ${total.cost_usd:,.2f} across {total.calls} call(s) -- {phases}")


def _report_dispute_backlog(paths: Paths, profile: Profile, round_no: int) -> None:
    """Say how much work the loop has handed back, as it accumulates rather than at the end.

    Nothing a later round does can clear a dispute, so a backlog growing quietly in the
    background is a budget being spent on rounds that cannot open the gate.
    """
    pending = converge.disputed_pending(store.load_findings(paths))
    if not pending:
        return
    bound = profile.convergence.max_disputes
    limit = f" of max_disputes={bound}" if bound is not None else ""
    typer.secho(f"round {round_no}: {len(pending)} dispute(s){limit} waiting on triage; "
                f"the gate stays shut until they are ruled on", fg=typer.colors.YELLOW, err=True)


def _summary(paths: Paths) -> None:
    findings = store.load_findings(paths)
    counts = converge.status_counts(findings)
    typer.echo("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts["status"].items())))



def main() -> None:
    try:
        app()
    except (StoreError, ShardError, AgentError, merge.MergeError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
