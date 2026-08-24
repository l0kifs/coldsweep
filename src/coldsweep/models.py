"""Data model for coldsweep. Every agent boundary and every persisted record is a pydantic model."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal["open", "fixed", "verified", "lapsed", "wontfix", "disputed"]
"""``verified`` is proof: the repository was checked and the evidence is gone.
``lapsed`` is silence: K independent scans stopped re-deriving the finding, which closes it
but proves nothing. Collapsing the two would hide how much of a green run came from evidence
and how much from nobody mentioning it again."""
Mode = Literal["absence", "presence"]
Source = Literal["agent", "mechanical"]
MergeMethod = Literal["new", "exact", "fuzzy", "adjudicated", "mechanical", "stale", "reopen", "status"]

BLOCKING_STATUSES: frozenset[str] = frozenset({"open", "fixed"})

_COMMENT_RE = re.compile(r"(?<!\\)(#|//).*$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def normalize_snippet(text: str) -> str:
    """Normalize whitespace, comments and string quoting out of an evidence snippet.

    The result is the canonical form hashed into ``evidence_sha`` and the form searched for
    when verifying that offending code is gone. Two snippets that differ only in formatting,
    comments or quote style must normalize to the same string.
    """
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = _COMMENT_RE.sub("", raw)
        line = line.replace("'", '"')
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def evidence_sha(snippet: str) -> str:
    """sha1 of the normalized snippet. Stable across reformatting of the same offending code."""
    return hashlib.sha1(normalize_snippet(snippet).encode("utf-8")).hexdigest()


def derive_id(rule_id: str, anchor: str, ev_sha: str | None) -> str:
    """Identity is derived, never assigned: an identical finding re-derives an identical id.

    ``description`` is excluded by construction -- agents phrase the same finding differently
    every run, so including it would make every round produce all-new findings.
    """
    digest = hashlib.sha1((anchor + (ev_sha or "")).encode("utf-8")).hexdigest()[:8]
    return f"{rule_id}-{digest}"


def anchor_file(anchor: str) -> str:
    """The file part of an anchor. ``pkg/mod.py::Class::method`` -> ``pkg/mod.py``."""
    return anchor.split("::", 1)[0].strip()


class Event(BaseModel):
    """One entry in a finding's audit trail. Every merge and status decision appends one."""

    model_config = ConfigDict(extra="forbid")

    round: int
    action: str
    method: MergeMethod | None = None
    score: float | None = None
    detail: str = ""


class Finding(BaseModel):
    """One work item. The unit of state, and the only place completion is tracked."""

    model_config = ConfigDict(extra="forbid")

    id: str
    rule_id: str
    anchor: str
    evidence_sha: str | None = None
    evidence: str | None = None
    description: str = ""
    shard: str = ""
    status: Status = "open"
    source: Source = "agent"
    adjudicated: bool = False
    first_seen_round: int = 0
    last_seen_round: int = 0
    reopen_baseline: int = 0
    """Reopens triage has already ruled on.

    The oscillation guard counts from here rather than from zero. Deleting the reopen events
    themselves would reset the same counter, at the cost of the record of what actually
    happened -- and the audit trail is the one thing in a finding that is only ever appended to.
    """
    history: list[Event] = Field(default_factory=list)

    @field_validator("anchor")
    @classmethod
    def _anchor_has_no_line_numbers(cls, value: str) -> str:
        if re.search(r"::L?\d+(-\d+)?$", value):
            raise ValueError(f"anchor must be a symbol path, not a line reference: {value!r}")
        if not value.strip():
            raise ValueError("anchor must not be empty")
        return value.strip()

    @property
    def file(self) -> str:
        return anchor_file(self.anchor)

    @property
    def reopens_logged(self) -> int:
        """Every reopen in the trail, including the ones triage has since forgiven."""
        return sum(1 for e in self.history if e.action == "reopen")

    @property
    def reopen_count(self) -> int:
        """Reopens since the last triage reset -- what the oscillation guard counts."""
        return self.reopens_logged - self.reopen_baseline

    def log(self, round_no: int, action: str, method: MergeMethod | None = None,
            score: float | None = None, detail: str = "") -> None:
        # pylint does not resolve pydantic Field defaults to their runtime type
        self.history.append(  # pylint: disable=no-member
            Event(round=round_no, action=action, method=method, score=score, detail=detail))


class RawFinding(BaseModel):
    """A finding as reported by a scan agent or a mechanical check, before identity is derived.

    Agents never see ids, statuses or rounds -- they report observations only.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    anchor: str
    evidence: str | None = None
    description: str = ""

    @field_validator("rule_id", "anchor")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    def to_finding(self, shard: str, round_no: int, source: Source = "agent") -> Finding:
        normalized = normalize_snippet(self.evidence) if self.evidence else None
        sha = evidence_sha(self.evidence) if normalized else None
        return Finding(
            id=derive_id(self.rule_id, self.anchor, sha),
            rule_id=self.rule_id,
            anchor=self.anchor,
            evidence_sha=sha,
            evidence=normalized,
            description=self.description,
            shard=shard,
            status="open",
            source=source,
            first_seen_round=round_no,
            last_seen_round=round_no,
        )


class ScanResult(BaseModel):
    """The schema a scan agent must return. Anything else is a schema violation and is retried."""

    model_config = ConfigDict(extra="forbid")

    findings: list[RawFinding] = Field(default_factory=list)


class Usage(BaseModel):
    """What one agent subprocess cost, read off its result envelope.

    Every field is independently optional, and ``None`` is not zero. An agent command that
    emits no envelope -- a stub, a wrapper, anything that is not the configured CLI -- leaves
    them all unset, and "nobody measured this call" must stay distinguishable from "this call
    was free". Aggregation counts the unmeasured rather than adding them in as zeros.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: float | None = None

    @property
    def measured(self) -> bool:
        """Whether the envelope reported anything at all about this call."""
        return any(v is not None for v in self.model_dump().values())

    @property
    def tokens(self) -> int | None:
        """Every token the call was billed for, cache included.

        Cache traffic is not a footnote: measured on this repository it is the majority of the
        bill, so a token total that counted only input and output would understate a scan by
        roughly four times.
        """
        parts = [self.input_tokens, self.output_tokens,
                 self.cache_creation_tokens, self.cache_read_tokens]
        return sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None


class SpendRecord(BaseModel):
    """One agent subprocess, and what it cost. Appended to ``spend.jsonl``.

    Per *attempt*, not per successful call: a retry is another subprocess and another bill, and
    a phase that fails after three attempts is the most expensive kind of round there is.
    """

    model_config = ConfigDict(extra="forbid")

    round: int
    phase: str
    model: str = ""
    attempt: int = 1
    ok: bool = True
    usage: Usage = Field(default_factory=Usage)


class ShardResult(BaseModel):
    """One shard's scan outcome, raw. Persisted to ``.coldsweep/runs/<round>.json``."""

    model_config = ConfigDict(extra="forbid")

    shard: str
    files: list[str]
    ok: bool = True
    error: str = ""
    attempts: int = 1
    model: str = ""
    source: Source = "agent"
    findings: list[RawFinding] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    """What this shard's agent call cost. All-``None`` for a mechanical shard, which spawns no
    agent, and for a shard whose agent never returned an envelope."""


class ScanRound(BaseModel):
    """Raw output of one full round of scanning, before any merge."""

    model_config = ConfigDict(extra="forbid")

    round: int
    profile_version: int = 1
    shards: list[ShardResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every shard must return a result -- a missing shard fails the round."""
        return bool(self.shards) and all(s.ok for s in self.shards)

    @property
    def failed_shards(self) -> list[str]:
        return sorted({s.shard for s in self.shards if not s.ok})


class MergeStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: MergeMethod
    finding_id: str
    detail: str = ""
    score: float | None = None


class RunRecord(BaseModel):
    """Audit record of one ingest.

    Convergence derives from findings.jsonl and reads exactly one thing here: ``failed_shards``.
    A shard that never reported leaves no trace in the finding set, so coverage is the one fact
    about a round that cannot be recovered from the findings alone.
    """

    model_config = ConfigDict(extra="forbid")

    round: int
    ingested: int = 0
    new: int = 0
    exact: int = 0
    fuzzy: int = 0
    adjudicated: int = 0
    reopened: int = 0
    unclassified: int = 0
    disputed: int = 0
    stale_closed: int = 0
    failed_shards: list[str] = Field(default_factory=list)
    decisions: list[MergeStat] = Field(default_factory=list)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mode: Mode
    description: str = ""
    decided_by: Literal["agent", "code"] = "agent"
    """Who owns this rule.

    ``code`` means a deterministic subsystem -- mechanical, mutation or spec traceability --
    is exhaustive over it. Such a rule is kept out of the scan prompt entirely: handing an
    agent a rule that code already decides invites it to report the same item under a
    different anchor, which merges as a duplicate at best and contradicts the exhaustive
    answer at worst. Agents handle only the tail.
    """


class MechanicalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    command: str


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include: list[str] = Field(default_factory=lambda: ["**/*.py"])
    exclude: list[str] = Field(default_factory=list)


class Convergence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int = Field(default=2, ge=1)
    max_rounds: int = Field(default=8, ge=1)


class Models(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scan: str = "sonnet"
    fix: str = "sonnet"
    scan_alt: str | None = None
    adjudicate: str = "sonnet"


class AgentConfig(BaseModel):
    """How to invoke the agent. Overridable so the loop can be driven by a stub in tests."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(default_factory=lambda: ["claude", "-p", "--output-format", "json"])
    scan_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep", "Glob"])
    fix_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep", "Glob", "Edit", "Write"])
    permission_mode: str = "acceptEdits"
    parallelism: int = Field(default=4, ge=1)
    retries: int = Field(default=2, ge=0)
    timeout_s: int = Field(default=900, ge=1)
    extra_args: list[str] = Field(default_factory=list)
    append_flags: bool = True


class MutationConfig(BaseModel):
    """The mutation-testing subsystem's settings.

    A component, not a config file: the runtime, the cache and the shard strategy all live in
    ``coldsweep.mutation``. This only parameterises them.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    test_command: str = "python -m pytest -q -x {tests}"
    test_patterns: list[str] = Field(default_factory=lambda: ["tests/test_{stem}.py"])
    operators: list[str] = Field(
        default_factory=lambda: ["comparison", "arithmetic", "boolean", "constant", "return"])
    timeout_s: int = Field(default=120, ge=1)
    baseline_timeout_s: int = Field(default=600, ge=1)
    max_mutants_per_anchor: int = Field(default=12, ge=1)
    stop_at_first_survivor: bool = True


class SpecConfig(BaseModel):
    """Spec authoring and freeze settings for a features task.

    Item ids are written explicitly in the document rather than derived from heading text, so
    retitling an item does not silently re-identify it and break its traceability.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = "SPEC.md"
    item_pattern: str = r"^###\s+(?P<id>[A-Za-z][A-Za-z]*-\d+)\s+(?P<title>.+?)\s*$"
    marker_pattern: str = r"spec:\s*(?P<id>[A-Za-z][A-Za-z]*-\d+)"
    unimplemented_rule_id: str
    stale_reference_rule_id: str | None = None

    @field_validator("item_pattern", "marker_pattern")
    @classmethod
    def _compiles_with_an_id_group(cls, value: str) -> str:
        try:
            compiled = re.compile(value, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        if "id" not in compiled.groupindex:
            raise ValueError("pattern must contain a named group (?P<id>...)")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str = "default"
    scope: Scope = Field(default_factory=Scope)
    editable: Scope | None = None
    """Files a fix agent may write, when that is not the same set as the audited files.

    Auditing a file and repairing it are the same act only for rules whose remedy lives where
    the problem does. A rule about test quality is anchored in the source it fails to pin, and
    fixed in a test file that `scope` deliberately excludes -- so with the two collapsed, the
    fix agent is handed the one file it must not edit. Defaults to ``scope``.
    """
    files_per_shard: int = Field(default=1, ge=1)
    convergence: Convergence = Field(default_factory=Convergence)
    models: Models = Field(default_factory=Models)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    rules: list[Rule] = Field(default_factory=list)
    mechanical: list[MechanicalCheck] = Field(default_factory=list)
    mutation: MutationConfig | None = None
    spec: SpecConfig | None = None
    fix_scope: Literal["file", "task"] = "file"

    @field_validator("rules")
    @classmethod
    def _unique_rule_ids(cls, value: list[Rule]) -> list[Rule]:
        seen = {r.id for r in value}
        if len(seen) != len(value):
            raise ValueError("duplicate rule ids in profile taxonomy")
        return value

    @model_validator(mode="after")
    def _editable_needs_task_scope(self) -> Profile:
        """A per-file fix phase hands the agent the anchor's own file, which a separate editable
        set is a statement that it must not edit. The two cannot both be meant."""
        if self.editable is not None and self.fix_scope == "file":
            raise ValueError("`editable` names a different set of files from `scope`, but "
                             "`fix_scope: file` sends the fix agent to the anchor's own file; "
                             "set `fix_scope: task`")
        return self

    @property
    def rule_ids(self) -> set[str]:
        return {r.id for r in self.rules}

    @property
    def agent_rules(self) -> list[Rule]:
        """The tail left to agents, after every deterministic subsystem has had its say."""
        return [r for r in self.rules if r.decided_by == "agent"]

    @property
    def budget_bounded(self) -> bool:
        """True when no rule has a deterministic decider.

        Not a prediction that the gate stays shut: a profile whose agents find nothing goes
        quiet and converges like any other. What it lacks is anything that *forces* the gate to
        close. Open-ended agent rules were measured plateauing rather than decaying -- 44% of
        findings appeared in exactly one pass of five -- so such a profile is run to a round
        budget and read, rather than driven to a gate that may never open.
        """
        return bool(self.rules) and not any(r.decided_by == "code" for r in self.rules)

    def rule(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def mode_of(self, rule_id: str) -> Mode | None:
        rule = self.rule(rule_id)
        return rule.mode if rule else None


class Shard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    files: list[str]


class Mutant(BaseModel):
    """One deterministic source mutation.

    ``id`` is derived from the file, the enclosing symbol, the operator and the occurrence
    index -- never from a line number, so a mutant keeps its identity when code moves.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    file: str
    anchor: str
    operator: str
    original: str
    mutated: str
    start: int
    end: int

    @property
    def display(self) -> str:
        return f"{self.original!r} -> {self.mutated!r}"


class MutantResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutant_id: str
    outcome: Literal["killed", "survived", "timeout", "no_tests", "error"]
    duration_s: float = 0.0
    detail: str = ""

    @property
    def survived(self) -> bool:
        """A timeout means the mutant was detected by hanging the suite -- that counts as killed."""
        return self.outcome in ("survived", "no_tests")


class MutationShard(BaseModel):
    """One source file paired with the tests responsible for it.

    The pairing is the shard strategy: it decides which tests run against a mutant and doubles
    as the cache key, so editing one test file does not invalidate every other file's results.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    tests: list[str]
    mutants: list[Mutant] = Field(default_factory=list)


class SpecItem(BaseModel):
    """One addressable unit of the spec. ``sha`` covers the body, so drift is detectable."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    body: str
    anchor: str
    sha: str


class SpecLock(BaseModel):
    """The freeze record: which items existed, and what they said, when work began.

    Committed, not derived. It is a decision -- the moment the target stopped moving.
    """

    model_config = ConfigDict(extra="forbid")

    spec: str
    frozen_round: int = 0
    items: dict[str, str] = Field(default_factory=dict)


class SpecDrift(BaseModel):
    """What changed in the spec since the freeze. Any of these makes the loop meaningless."""

    model_config = ConfigDict(extra="forbid")

    added: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def reasons(self) -> list[str]:
        out = []
        if self.added:
            out.append(f"{len(self.added)} spec item(s) added since the freeze: {', '.join(self.added)}")
        if self.changed:
            out.append(f"{len(self.changed)} frozen spec item(s) reworded: {', '.join(self.changed)}")
        if self.removed:
            out.append(f"{len(self.removed)} frozen spec item(s) deleted: {', '.join(self.removed)}")
        return out


class SpecReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: int = 0
    frozen: int = 0
    implemented: int = 0
    unimplemented: int = 0
    stale_markers: int = 0
    drift: SpecDrift = Field(default_factory=SpecDrift)


class MutationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shards: int = 0
    mutants: int = 0
    killed: int = 0
    survived: int = 0
    no_tests: int = 0
    errors: int = 0
    cached: int = 0
    probes_cached: int = 0
    skipped: int = 0
    unexercised: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


class FixOutcome(BaseModel):
    """One fix agent verdict. ``disputed`` is a first-class answer -- a fix agent that
    disagrees with a finding must say so rather than guess at an edit."""

    model_config = ConfigDict(extra="forbid")

    id: str
    outcome: Literal["fixed", "disputed"]
    detail: str = ""


class FixResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[FixOutcome] = Field(default_factory=list)


class Adjudication(BaseModel):
    """The only question an adjudication agent is ever asked: same item, yes or no."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["same", "different"]
    reason: str = ""

    @property
    def same(self) -> bool:
        return self.verdict == "same"
