"""Agent invocation. One subprocess per shard, so every scan starts from a fresh context."""

from __future__ import annotations

import asyncio
import json
import shlex
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
)
from .spec import spec_context

PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)


class AgentError(RuntimeError):
    pass


def render(template: str, **values: str) -> str:
    text = (PROMPT_DIR / template).read_text(encoding="utf-8")
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


class Runner:
    """Owns every subprocess coldsweep spawns."""

    def __init__(self, repo: Path, profile: Profile) -> None:
        self.repo = repo
        self.profile = profile
        self.cfg = profile.agent
        self._sem = asyncio.Semaphore(self.cfg.parallelism)

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
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(self.repo),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")),
                                              timeout=self.cfg.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AgentError(f"{phase.name} agent timed out after {self.cfg.timeout_s}s "
                             f"({shlex.join(argv)})") from None
        if proc.returncode != 0:
            raise AgentError(f"{phase.name} agent exited {proc.returncode}: {err.decode('utf-8', 'replace')[:400]}")
        return out.decode("utf-8", "replace")

    async def call(self, phase: Phase, prompt: str, schema: type[T]) -> tuple[T, int]:
        """Invoke an agent and enforce the schema. Retries on violation, then fails loudly."""
        last: Exception | None = None
        for attempt in range(1, self.cfg.retries + 2):
            async with self._sem:
                try:
                    raw = await self._exec(phase, prompt)
                    return schema.model_validate(extract_json(unwrap_envelope(raw))), attempt
                except (AgentError, ValidationError) as exc:
                    last = exc
        raise AgentError(f"{phase.name} agent failed after {self.cfg.retries + 1} attempt(s): {last}")

    def context(self, files: list[str]) -> str:
        """Extra task statement for the shard. Never prior findings, rounds, or diffs."""
        return spec_context(self.repo, self.profile, files)

    def _rules_block(self) -> str:
        lines = [f"- `{rule.id}` ({rule.mode}): {rule.description}".rstrip()
                 for rule in self.profile.agent_rules]
        return "\n".join(lines) or "- (profile defines no rules)"

    async def scan_shard(self, shard: Shard, phase: Phase) -> ShardResult:
        prompt = render(
            "scan.md",
            files="\n".join(f"- `{f}`" for f in shard.files),
            rules=self._rules_block(),
            context=self.context(shard.files),
        )
        try:
            result, attempts = await self.call(phase, prompt, ScanResult)
        except AgentError as exc:
            return ShardResult(shard=shard.id, files=shard.files, ok=False, error=str(exc),
                               attempts=self.cfg.retries + 1, model=phase.model)
        return ShardResult(shard=shard.id, files=shard.files, ok=True, attempts=attempts,
                           model=phase.model, findings=result.findings)

    async def scan(self, shards: list[Shard], round_no: int) -> list[ShardResult]:
        phase = self.scan_phase(round_no)
        if not self.profile.agent_rules:
            # Every rule belongs to a deterministic subsystem; there is no tail to ask about.
            return [ShardResult(shard=s.id, files=s.files, ok=True, attempts=0, model=phase.model)
                    for s in shards]
        return list(await asyncio.gather(*(self.scan_shard(s, phase) for s in shards)))

    def _new_file_note(self) -> str:
        """Whether the remedy may live in a file that does not exist yet, and where.

        A rule whose fix is a missing artefact -- a test that was never written -- cannot be
        resolved by editing existing files. A profile that separates its editable set from its
        audited one is exactly the profile where that happens.
        """
        scope = self.profile.editable
        if scope is None:
            return "Do not create new files: edit only the files listed above."
        patterns = ", ".join(f"`{p}`" for p in scope.include)
        return ("A new file may be created when the remedy has nowhere else to live, provided "
                f"its path matches one of: {patterns}.")

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
                        new_files=self._new_file_note(), rules=self._rules_block(),
                        findings=listing)
        result, _ = await self.call(self.fix_phase(), prompt, FixResult)
        return result

    async def fix(self, groups: dict[str, list[Finding]],
                  editable: list[str] | None = None) -> dict[str, FixResult | AgentError]:
        keys = sorted(groups)
        async def one(key: str) -> FixResult | AgentError:
            try:
                return await self.fix_group(key, groups[key], editable)
            except AgentError as exc:
                return exc
        return dict(zip(keys, await asyncio.gather(*(one(k) for k in keys)), strict=True))

    async def adjudicate_pair(self, a: Finding, b: Finding) -> Adjudication:
        prompt = render(
            "adjudicate.md",
            a_rule=a.rule_id, a_anchor=a.anchor, a_description=a.description, a_evidence=a.evidence or "(none)",
            b_rule=b.rule_id, b_anchor=b.anchor, b_description=b.description, b_evidence=b.evidence or "(none)",
        )
        result, _ = await self.call(self.adjudicate_phase(), prompt, Adjudication)
        return result

    def adjudicator(self) -> Adjudicator:
        """Sync adapter used by merge. Any failure resolves to ``different`` -- never toward loss.

        Must be called from sync code: it opens its own event loop per pair.
        """
        def decide(a: Finding, b: Finding) -> bool:
            try:
                return asyncio.run(self.adjudicate_pair(a, b)).same
            except (AgentError, RuntimeError):
                return False
        return decide
