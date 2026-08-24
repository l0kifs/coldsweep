"""Agent invocation. One subprocess per shard, so every scan starts from a fresh context."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .merge import Adjudicator
from .models import (
    Adjudication,
    AgentConfig,
    Finding,
    FixResult,
    Profile,
    ScanResult,
    Shard,
    ShardResult,
    SpendRecord,
    Usage,
)
from .spec import SpecError, spec_context

PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)


class AgentError(RuntimeError):
    pass


def render(template: str, **values: str) -> str:
    try:
        text = (PROMPT_DIR / template).read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentError(f"cannot read prompt template {template!r}: {exc}") from exc
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model response.

    Agents are told to emit bare JSON; they intermittently wrap it in prose or a fence anyway.
    Recovering it here is cheaper than burning a retry.
    """
    text = text.strip()
    if not text:
        raise AgentError("empty agent response")
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start, depth = -1, 0
    raise AgentError(f"no JSON object in agent response: {text[:300]!r}")


def unwrap_envelope(stdout: str) -> str:
    """Unwrap ``claude -p --output-format json``. Non-envelope output is passed through."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if not isinstance(payload, dict):
        return stdout
    if payload.get("is_error"):
        raise AgentError(f"agent reported an error: {str(payload.get('result'))[:300]}")
    result = payload.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result)
    return stdout


def extract_usage(stdout: str) -> Usage:
    """Read what a call cost off its result envelope.

    Every field is taken independently and left ``None`` when the envelope does not carry it,
    so a bare response yields an all-``None`` ``Usage``. Zeros here would be a claim the tool
    cannot support: an agent command that reports nothing is not an agent command that was free.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return Usage()
    if not isinstance(payload, dict):
        return Usage()
    counts = payload.get("usage")
    counts = counts if isinstance(counts, dict) else {}

    def number(value: object) -> float | None:
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    def count(key: str) -> int | None:
        value = number(counts.get(key))
        return int(value) if value is not None else None

    return Usage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_creation_tokens=count("cache_creation_input_tokens"),
        cache_read_tokens=count("cache_read_input_tokens"),
        cost_usd=number(payload.get("total_cost_usd")),
        duration_ms=number(payload.get("duration_ms")),
    )


@dataclass(frozen=True)
class Phase:
    name: str
    model: str
    tools: list[str]
    permission_mode: str


def build_argv(cfg: AgentConfig, phase: Phase) -> list[str]:
    argv = list(cfg.command)
    if cfg.append_flags:
        argv += ["--model", phase.model]
        if phase.tools:
            argv += ["--tools", ",".join(phase.tools)]
        argv += ["--permission-mode", phase.permission_mode]
    argv += list(cfg.extra_args)
    return argv


def fix_lanes(groups: dict[str, list[Finding]],
              targets: dict[str, list[str]]) -> list[list[str]]:
    """Partition group keys into lanes that share no writable file.

    Transitive by construction: two groups that never touch the same file still share a lane if
    each shares one with a third. Union-find rather than a per-file lock so the whole schedule
    is decided before any agent starts, and the result is deterministic for a given input.
    """
    parent = {key: key for key in groups}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    owner: dict[str, str] = {}
    for key in sorted(groups):
        for file in targets.get(key) or [key]:
            root = find(key)
            if file in owner:
                parent[find(owner[file])] = root
            owner[file] = key
    lanes: dict[str, list[str]] = {}
    for key in sorted(groups):
        lanes.setdefault(find(key), []).append(key)
    return [lanes[root] for root in sorted(lanes)]


class Runner:
    """Owns every subprocess coldsweep spawns."""

    def __init__(self, repo: Path, profile: Profile,
                 ledger: Callable[[SpendRecord], None] | None = None,
                 round_no: int = 0) -> None:
        self.repo = repo
        self.profile = profile
        self.cfg = profile.agent
        self._sem = asyncio.Semaphore(self.cfg.parallelism)
        self._ledger = ledger
        self._round = round_no

    def _record(self, phase: Phase, attempt: int, usage: Usage, ok: bool) -> None:
        """Bill one subprocess. Every phase goes through here, so nothing spends unrecorded.

        A ledger write failure (disk full, permissions) must not take the rest of a running
        ``asyncio.gather()`` batch down with it -- the spend line is lost, but sibling
        scan/fix/adjudicate results are not, and the failure is still reported, not hidden.

        Caught broadly on purpose. The ledger is a side channel supplied by the caller, and
        narrowing this to one exception type is how it stopped working the first time: the
        store began reporting write failures as ``StoreError`` and an ``except OSError`` here
        silently became unreachable.
        """
        if self._ledger is None:
            return
        record = SpendRecord(round=self._round, phase=phase.name, model=phase.model,
                             attempt=attempt, ok=ok, usage=usage)
        try:
            self._ledger(record)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"warning: could not record spend for {phase.name} phase (attempt {attempt}): "
                 f"{exc}", file=sys.stderr)

    def scan_phase(self, round_no: int) -> Phase:
        """Alternate to ``scan_alt`` on even rounds -- same-family agents share blind spots.

        Read-only-ness comes from the tool set, not the permission mode: scan agents are handed
        no tool that can write.
        """
        models = self.profile.models
        model = models.scan
        if models.scan_alt and round_no % 2 == 0:
            model = models.scan_alt
        return Phase("scan", model, self.cfg.scan_tools, self.cfg.permission_mode)

    def fix_phase(self) -> Phase:
        return Phase("fix", self.profile.models.fix, self.cfg.fix_tools, self.cfg.permission_mode)

    def adjudicate_phase(self) -> Phase:
        return Phase("adjudicate", self.profile.models.adjudicate, self.cfg.scan_tools,
                     self.cfg.permission_mode)

    async def _exec(self, phase: Phase, prompt: str) -> str:
        argv = build_argv(self.cfg, phase)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.repo),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AgentError(f"{phase.name} agent failed to start ({shlex.join(argv)}): {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")),
                                              timeout=self.cfg.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AgentError(f"{phase.name} agent timed out after {self.cfg.timeout_s}s "
                             f"({shlex.join(argv)})") from None
        except BaseException:
            # Any other failure here (cancellation included) still leaves the process running
            # unless we reap it ourselves -- only the timeout path above does that on its own.
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise AgentError(f"{phase.name} agent exited {proc.returncode}: {err.decode('utf-8', 'replace')[:400]}")
        return out.decode("utf-8", "replace")

    async def call(self, phase: Phase, prompt: str, schema: type[T]) -> tuple[T, int, Usage]:
        """Invoke an agent and enforce the schema. Retries on violation, then fails loudly.

        Each attempt is billed as it happens, whether or not it produced a usable answer. A
        schema violation still ran a subprocess and still cost what it cost, and a phase that
        exhausts its retries is the most expensive outcome there is -- so it is exactly the one
        that must not be missing from the ledger.
        """
        last: Exception | None = None
        for attempt in range(1, self.cfg.retries + 2):
            async with self._sem:
                try:
                    raw = await self._exec(phase, prompt)
                except AgentError as exc:
                    # No envelope: the process died or never answered. Recorded as unmeasured.
                    self._record(phase, attempt, Usage(), ok=False)
                    last = exc
                    continue
                usage = extract_usage(raw)
                try:
                    parsed = schema.model_validate(extract_json(unwrap_envelope(raw)))
                except (AgentError, ValidationError) as exc:
                    self._record(phase, attempt, usage, ok=False)
                    last = exc
                    continue
                self._record(phase, attempt, usage, ok=True)
                return parsed, attempt, usage
        raise AgentError(f"{phase.name} agent failed after {self.cfg.retries + 1} attempt(s): {last}")

    def context(self, files: list[str]) -> str:
        """Extra task statement for the shard. Never prior findings, rounds, or diffs."""
        return spec_context(self.repo, self.profile, files)

    def _rules_block(self) -> str:
        lines = [f"- `{rule.id}` ({rule.mode}): {rule.description}".rstrip()
                 for rule in self.profile.agent_rules]
        return "\n".join(lines) or "- (profile defines no rules)"

    async def scan_shard(self, shard: Shard, phase: Phase) -> ShardResult:
        try:
            prompt = render(
                "scan.md",
                files="\n".join(f"- `{f}`" for f in shard.files),
                rules=self._rules_block(),
                context=self.context(shard.files),
            )
            result, attempts, usage = await self.call(phase, prompt, ScanResult)
        except (AgentError, SpecError) as exc:
            return ShardResult(shard=shard.id, files=shard.files, ok=False, error=str(exc),
                               attempts=self.cfg.retries + 1, model=phase.model)
        return ShardResult(shard=shard.id, files=shard.files, ok=True, attempts=attempts,
                           model=phase.model, findings=result.findings, usage=usage)

    async def scan(self, shards: list[Shard], round_no: int) -> list[ShardResult]:
        phase = self.scan_phase(round_no)
        if not self.profile.agent_rules:
            # Every rule belongs to a deterministic subsystem; there is no tail to ask about.
            return [ShardResult(shard=s.id, files=s.files, ok=True, attempts=0, model=phase.model)
                    for s in shards]
        return list(await asyncio.gather(*(self.scan_shard(s, phase) for s in shards)))

    def _new_file_note(self, files: list[str]) -> str:
        """Whether the remedy may live in a file that does not exist yet, and where.

        A rule whose fix is a missing artefact -- a test that was never written -- cannot be
        resolved by editing existing files. A profile that separates its editable set from its
        audited one is exactly the profile where that happens.

        The permitted paths are this agent's own slice, never the profile's whole pattern list:
        a note that widened the licence past the files above would put two agents back in the
        same file, which is the collision the slice exists to prevent.
        """
        if self.profile.editable is None:
            return "Do not create new files: edit only the files listed above."
        paths = ", ".join(f"`{f}`" for f in files)
        return ("A new file may be created when the remedy has nowhere else to live, provided "
                f"it is one of: {paths}.")

    async def fix_group(self, key: str, findings: list[Finding],
                        editable: list[str] | None = None) -> FixResult:
        listing = "\n".join(
            f"- id: `{f.id}`\n  rule: `{f.rule_id}` ({self.profile.mode_of(f.rule_id) or 'unknown'})\n"
            f"  anchor: `{f.anchor}`\n  problem: {f.description}"
            + (f"\n  evidence:\n```\n{f.evidence}\n```" if f.evidence else "")
            for f in findings
        )
        files = editable if editable is not None else [key]
        prompt = render("fix.md", files="\n".join(f"- `{f}`" for f in files),
                        new_files=self._new_file_note(files), rules=self._rules_block(),
                        findings=listing)
        result, _, _ = await self.call(self.fix_phase(), prompt, FixResult)
        return result

    async def fix(self, groups: dict[str, list[Finding]],
                  slices: dict[str, list[str]] | None = None) -> dict[str, FixResult | AgentError]:
        """Work every group, never letting two agents hold the same file at once.

        ``slices`` says which files each group may write. Groups whose slices are disjoint can
        run together; groups that share a file run one after another, because both would read
        it, decide, and write it back whole, and the later write would erase the earlier one.
        Without this the loss is silent: every agent reports ``fixed`` and the finding set
        records work that is no longer in the tree.
        """
        targets = slices if slices is not None else {key: [key] for key in groups}

        async def lane(keys: list[str]) -> dict[str, FixResult | AgentError]:
            out: dict[str, FixResult | AgentError] = {}
            for key in keys:
                try:
                    out[key] = await self.fix_group(key, groups[key], targets.get(key))
                except AgentError as exc:
                    out[key] = exc
            return out

        done = await asyncio.gather(*(lane(keys) for keys in fix_lanes(groups, targets)))
        return {key: result for chunk in done for key, result in chunk.items()}

    async def adjudicate_pair(self, a: Finding, b: Finding) -> Adjudication:
        prompt = render(
            "adjudicate.md",
            a_rule=a.rule_id, a_anchor=a.anchor, a_description=a.description, a_evidence=a.evidence or "(none)",
            b_rule=b.rule_id, b_anchor=b.anchor, b_description=b.description, b_evidence=b.evidence or "(none)",
        )
        result, _, _ = await self.call(self.adjudicate_phase(), prompt, Adjudication)
        return result

    def adjudicator(self) -> Adjudicator:
        """Sync adapter used by merge. Any failure resolves to ``different`` -- never toward loss.

        Must be called from sync code: it opens its own event loop per pair. Failures are left to
        propagate: ``merge`` already catches them around this call and records the real reason,
        which a catch here would erase before merge ever saw it.
        """
        def decide(a: Finding, b: Finding) -> bool:
            return asyncio.run(self.adjudicate_pair(a, b)).same
        return decide
