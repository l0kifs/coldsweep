"""Termination. The loop condition is a computation over finding sets, never a model judgement."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from .models import Finding, Profile, SpendRecord


class ConvergenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    converged: bool
    needs_triage: bool = False
    k: int
    max_rounds: int
    completed_rounds: list[int] = Field(default_factory=list)
    quiet_rounds: int = 0
    incomplete_rounds: list[int] = Field(default_factory=list)
    new_per_round: dict[str, int] = Field(default_factory=dict)
    open_ids: list[str] = Field(default_factory=list)
    disputed_pending_ids: list[str] = Field(default_factory=list)
    unclassified_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def is_unclassified(finding: Finding, profile: Profile) -> bool:
    """Whether a finding sits outside the profile's taxonomy.

    Always derived from the profile in hand, never stored on the finding. A stored flag goes
    stale the moment a rule is added or retired, and a stale flag here empties the bucket that
    the gate depends on.
    """
    return finding.rule_id not in profile.rule_ids


def open_blocking(findings: list[Finding], profile: Profile) -> list[Finding]:
    """Findings that still represent unfinished work.

    ``fixed`` blocks alongside ``open``: a fix is claimed, not confirmed, until a later round
    fails to re-derive it or ``coldsweep verify`` proves the evidence gone.
    """
    return [f for f in findings
            if not is_unclassified(f, profile) and f.status in ("open", "fixed")]


def disputed_pending(findings: list[Finding]) -> list[Finding]:
    """Disputed and not yet triaged. Adjudicated disputes are excluded from the gate."""
    return [f for f in findings if f.status == "disputed" and not f.adjudicated]


def unclassified_pending(findings: list[Finding], profile: Profile) -> list[Finding]:
    """Off-taxonomy findings awaiting triage. Excluded from the open check, gated separately."""
    return [f for f in findings if is_unclassified(f, profile) and f.status != "wontfix"]


def new_per_round(findings: list[Finding], rounds: list[int]) -> dict[int, int]:
    counts = Counter(f.first_seen_round for f in findings)
    return {r: counts.get(r, 0) for r in rounds}


def evaluate(findings: list[Finding], profile: Profile, rounds: list[int],
             extra_blockers: list[str] | None = None,
             incomplete: list[int] | None = None) -> ConvergenceReport:
    """K consecutive rounds of *full* coverage producing zero new findings, nothing left open.

    ``extra_blockers`` carries reasons that do not live in the finding set at all -- an
    unfrozen or drifted spec is the only current example. They shut the gate and, unlike a
    dispute, they are not something triage can clear.

    ``incomplete`` names rounds that were ingested without every shard reporting. Such a round
    is quiet for a reason that has nothing to do with the repository being clean, so it cannot
    count toward the window. Coverage is the one thing the gate reads outside the finding set,
    because a missing shard leaves no trace in it.
    """
    k = profile.convergence.k
    rounds = sorted(rounds)
    counts = new_per_round(findings, rounds)
    partial = {r for r in (incomplete or []) if r in counts}
    tail = rounds[-k:] if len(rounds) >= k else rounds

    def is_quiet(r: int) -> bool:
        return counts[r] == 0 and r not in partial

    quiet = 0
    for r in reversed(rounds):
        if is_quiet(r):
            quiet += 1
        else:
            break

    op = open_blocking(findings, profile)
    dp = disputed_pending(findings)
    uc = unclassified_pending(findings, profile)
    rounds_settled = len(rounds) >= k and all(is_quiet(r) for r in tail)

    reasons: list[str] = []
    if len(rounds) < k:
        reasons.append(f"only {len(rounds)} round(s) completed, need at least k={k}")
    else:
        noisy = ", ".join(f"round {r}: +{counts[r]}" for r in tail if counts[r])
        if noisy:
            reasons.append(f"new findings inside the last {k} round(s) ({noisy})")
        blind = ", ".join(str(r) for r in tail if r in partial)
        if blind:
            reasons.append(f"round(s) {blind} inside the last {k} were ingested with failed "
                           f"shards; incomplete coverage cannot count as a quiet round")
    if op:
        reasons.append(f"{len(op)} finding(s) still open or awaiting verification")
    if dp:
        reasons.append(f"{len(dp)} disputed finding(s) not adjudicated")
    if uc:
        reasons.append(f"{len(uc)} unclassified finding(s) in the bucket")
    external = list(extra_blockers or [])
    reasons.extend(external)

    # Scanning can move `open` findings and quiet rounds; it can never clear a dispute or an
    # off-taxonomy finding. Once those are all that is left, another round buys nothing.
    needs_triage = bool(rounds_settled and not op and (dp or uc) and not external)

    return ConvergenceReport(
        converged=not reasons,
        needs_triage=needs_triage,
        k=k,
        max_rounds=profile.convergence.max_rounds,
        completed_rounds=rounds,
        quiet_rounds=quiet,
        incomplete_rounds=sorted(partial),
        new_per_round={str(r): c for r, c in counts.items()},
        open_ids=sorted(f.id for f in op),
        disputed_pending_ids=sorted(f.id for f in dp),
        unclassified_ids=sorted(f.id for f in uc),
        reasons=reasons,
    )


def status_counts(findings: list[Finding]) -> dict[str, Counter]:
    by_status: Counter = Counter(f.status for f in findings)
    by_rule: Counter = Counter(f.rule_id for f in findings)
    by_source: Counter = Counter(f.source for f in findings)
    return {"status": by_status, "rule": by_rule, "source": by_source}


@dataclass(frozen=True)
class Spend:  # pylint: disable=too-many-instance-attributes
    """What a task has spent, aggregated from its ledger.

    Every token class is carried separately rather than pre-summed: measured on this
    repository, cache traffic is the majority of the bill, so a caller reading only input and
    output would draw the wrong conclusion about where the money goes.

    ``unmeasured`` is reported rather than folded in: an agent command that emits no envelope
    produces real cost and no record of it, and a total that quietly excluded those calls would
    read as complete when it is a lower bound.
    """

    calls: int = 0
    unmeasured: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_tokens + self.cache_read_tokens)

    @property
    def complete(self) -> bool:
        """Whether every call in this bucket reported what it cost."""
        return self.unmeasured == 0


def tally(records: Iterable[SpendRecord]) -> Spend:
    totals = dict.fromkeys(
        ("cost_usd", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"), 0.0)
    calls = unmeasured = failed = 0
    for record in records:
        calls += 1
        failed += not record.ok
        unmeasured += not record.usage.measured
        for field in totals:
            totals[field] += getattr(record.usage, field) or 0
    return Spend(calls=calls, unmeasured=unmeasured, failed=failed,
                 cost_usd=round(totals["cost_usd"], 4),
                 **{k: int(v) for k, v in totals.items() if k != "cost_usd"})


def spend_by(records: Iterable[SpendRecord], key: str) -> dict[str, Spend]:
    """Tally grouped by one field of the record -- ``phase``, ``model`` or ``round``."""
    buckets: dict[str, list[SpendRecord]] = {}
    for record in records:
        buckets.setdefault(str(getattr(record, key)), []).append(record)
    return {k: tally(v) for k, v in sorted(buckets.items())}
