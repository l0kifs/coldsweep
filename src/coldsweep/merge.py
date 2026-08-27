"""Merge scan output into the finding set.

Highest-risk module in the tool. A duplicate costs one cheap re-check; a false merge
silently deletes a real work item, which is the exact failure that sends the user back to
manual verification. Every ambiguous case therefore resolves toward *not* merging.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError
from rapidfuzz import fuzz

from .models import Finding, MergeStat, Profile, RunRecord, ScanRound

AUTO_MERGE = 0.92
ADJUDICATE_FLOOR = 0.75
MAX_REOPENS = 2

# Returns True when the two findings are the same underlying item. A bounded factual
# comparison -- never a question about completeness.
Adjudicator = Callable[[Finding, Finding], bool]


class MergeError(RuntimeError):
    pass


def similarity(a: Finding, b: Finding) -> float:
    """rapidfuzz ratio over the ``(anchor, description)`` pair, equally weighted.

    Only ever called for candidates already gated to the same ``rule_id`` and same file.
    """
    anchor_ratio = fuzz.ratio(a.anchor, b.anchor) / 100.0
    desc_ratio = fuzz.ratio(a.description, b.description) / 100.0
    return round((anchor_ratio + desc_ratio) / 2.0, 4)


def _candidates(incoming: Finding, existing: list[Finding], claimed: set[str]) -> list[tuple[float, Finding]]:
    """Similarity candidates, gated to same rule and same file, best first."""
    scored: list[tuple[float, Finding]] = []
    for other in existing:
        if other.id in claimed or other.id == incoming.id:
            continue
        if other.rule_id != incoming.rule_id or other.file != incoming.file:
            continue
        scored.append((similarity(incoming, other), other))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return scored


def _touch(target: Finding, round_no: int, method: str, score: float | None, detail: str,
           record: RunRecord) -> None:
    """Register that this round re-derived an existing finding, applying reopen semantics."""
    target.last_seen_round = max(target.last_seen_round, round_no)
    target.log(round_no, "seen", method=method, score=score, detail=detail)  # type: ignore[arg-type]

    if target.status in ("fixed", "verified", "lapsed"):
        target.status = "open"
        target.log(round_no, "reopen", detail=f"re-derived in round {round_no} after {method} match")
        record.reopened += 1
        if target.reopen_count > MAX_REOPENS:
            target.status = "disputed"
            target.adjudicated = False
            target.log(round_no, "dispute", detail=f"oscillation guard: {target.reopen_count} reopens")
            record.disputed += 1


def merge_round(
    existing: list[Finding],
    scan: ScanRound,
    profile: Profile,
    round_no: int,
    adjudicator: Adjudicator | None = None,
) -> tuple[list[Finding], RunRecord]:
    """Fold one round of raw scan output into the finding set.

    Pure: no filesystem, no network, no model calls except the injected ``adjudicator``.
    """
    findings = [f.model_copy(deep=True) for f in existing]
    by_id = {f.id: f for f in findings}
    record = RunRecord(round=round_no, failed_shards=scan.failed_shards)
    claimed: set[str] = set()
    taxonomy = profile.rule_ids
    exhaustive = {r.id for r in profile.rules if r.decided_by == "code"}

    for result in sorted(scan.shards, key=lambda s: (s.source, s.shard)):
        if not result.ok:
            continue
        for raw in result.findings:
            try:
                candidate = raw.to_finding(shard=result.shard, round_no=round_no, source=result.source)
            except ValidationError as exc:
                # RawFinding accepts an anchor Finding rejects (a line reference), so the
                # conversion is the first place a bad anchor is caught. Name the shard: the
                # scan output is the thing to re-run.
                raise MergeError(f"round {round_no}, shard {result.shard}: rule {raw.rule_id!r} "
                                 f"reported an unusable anchor {raw.anchor!r}: {exc}") from exc
            record.ingested += 1
            _ingest_one(candidate, findings, by_id, claimed, taxonomy, exhaustive, round_no,
                        adjudicator, record)

    record.unclassified = sum(1 for f in findings
                              if f.rule_id not in taxonomy and f.status != "wontfix")
    _close_stale(findings, profile, round_no, scan, record)
    return findings, record


def _ingest_one(
    candidate: Finding,
    findings: list[Finding],
    by_id: dict[str, Finding],
    claimed: set[str],
    taxonomy: set[str],
    exhaustive: set[str],
    round_no: int,
    adjudicator: Adjudicator | None,
    record: RunRecord,
) -> None:
    # 1. Taxonomy gate. Never invent rules -- an off-taxonomy rule_id is quarantined, not
    #    renamed. Membership is re-derived wherever it is needed, never written onto the record.
    unclassified = candidate.rule_id not in taxonomy

    # 2. Exact identity match. Identity is derived, so this is a dict lookup.
    hit = by_id.get(candidate.id)
    if hit is not None:
        if candidate.evidence and not hit.evidence:
            hit.evidence = candidate.evidence
        claimed.add(hit.id)
        _touch(hit, round_no, "exact", 1.0, "identity match", record)
        record.exact += 1
        record.decisions.append(MergeStat(method="exact", finding_id=hit.id, score=1.0))
        return

    # 3. Similarity fallback, gated to same rule_id and same file -- and never applied to a rule
    #    a subsystem decides. Such a rule is exhaustive and its anchors are machine-derived, so
    #    two distinct anchors are two distinct work items by construction; there is no
    #    differently-phrased duplicate for the fallback to catch. What it catches instead is
    #    short sibling symbols in one file: measured on this repository, it silently absorbed
    #    `_python_ranges` into `_tree_ranges` and `_defer` into `_reopen`, because a mutation
    #    finding's description is a template and rapidfuzz scores the pair above 0.92 on the
    #    anchor alone. That is loss, which invariant 5 does not permit.
    if candidate.rule_id in exhaustive:
        _new_finding(candidate, findings, by_id, record, round_no, unclassified)
        return

    scored = _candidates(candidate, findings, claimed)
    if scored:
        score, best = scored[0]
        if score >= AUTO_MERGE:
            claimed.add(best.id)
            _touch(best, round_no, "fuzzy", score, f"auto-merged {candidate.id}", record)
            best.log(round_no, "merge", method="fuzzy", score=score, detail=f"absorbed {candidate.id}")
            record.fuzzy += 1
            record.decisions.append(MergeStat(method="fuzzy", finding_id=best.id, score=score,
                                              detail=f"absorbed {candidate.id}"))
            return
        if score >= ADJUDICATE_FLOOR and adjudicator is not None:
            record.adjudicator_calls += 1
            try:
                same = bool(adjudicator(candidate, best))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Adjudicator failure is ambiguity, not certainty either way -- and every
                # ambiguous case resolves toward *not* merging, same as a "different" ruling.
                same = False
                detail = f"adjudicator error, treated as different from {best.id}: {exc}"
            else:
                detail = f"{'same' if same else 'different'} as {candidate.id}"
            record.decisions.append(
                MergeStat(method="adjudicated", finding_id=best.id if same else candidate.id, score=score,
                          detail=detail)
            )
            if same:
                claimed.add(best.id)
                _touch(best, round_no, "adjudicated", score, f"adjudicated same as {candidate.id}", record)
                best.log(round_no, "merge", method="adjudicated", score=score, detail=f"absorbed {candidate.id}")
                record.adjudicated += 1
                return
            candidate.log(round_no, "adjudicated", method="adjudicated", score=score, detail=detail)

    # 4. New finding. Also the resting place of the 0.75-0.92 band when no adjudicator is
    #    wired up: an unresolved maybe becomes a duplicate, never a merge.
    _new_finding(candidate, findings, by_id, record, round_no, unclassified)


def _new_finding(candidate: Finding, findings: list[Finding], by_id: dict[str, Finding],
                 record: RunRecord, round_no: int, unclassified: bool) -> None:
    candidate.log(round_no, "created", method="new",
                  detail="off-taxonomy rule_id" if unclassified else "")
    findings.append(candidate)
    by_id[candidate.id] = candidate
    record.new += 1
    record.decisions.append(MergeStat(method="new", finding_id=candidate.id))


def _close_stale(findings: list[Finding], profile: Profile, round_no: int, scan: ScanRound,
                 record: RunRecord) -> None:
    """Close work items that K consecutive full re-derivations no longer report.

    A code decision, not a model one: K independent fresh-context scans failing to re-derive
    a finding is the same evidence convergence itself runs on. Without it an unreproducible
    finding blocks the gate forever.

    Closed as ``lapsed``, never ``verified``: nothing here inspected the repository, so this is
    silence rather than proof, and the two must stay countable apart.

    A rule a subsystem owns is the exception, and closes as ``verified`` after a single round.
    That subsystem is exhaustive over its scope, so one *complete* pass that does not report an
    anchor has inspected the repository and found the work done -- proof, not silence, and no
    reason to wait K rounds for it. "Complete" is read off the round record rather than assumed:
    the pass reports a shard even when it finds nothing, and without that shard this treats the
    round as saying nothing at all.
    """
    if not scan.ok:
        return
    k = profile.convergence.k
    owned = {r for r in (profile.mutation.rule_id if profile.mutation else None,
                         profile.spec.unimplemented_rule_id if profile.spec else None,
                         *(c.rule_id for c in profile.mechanical)) if r}
    decided = any(s.source == "mechanical" and s.ok for s in scan.shards)
    for f in findings:
        if f.status not in ("open", "fixed") or f.last_seen_round >= round_no:
            continue
        if f.first_seen_round >= round_no:
            continue
        proven = decided and f.rule_id in owned
        if round_no - f.last_seen_round >= (1 if proven else k):
            f.status = "verified" if proven else "lapsed"
            f.log(round_no, "close", method="stale",
                  detail=(f"a complete pass of its decider no longer reports it in round {round_no}"
                          if proven else
                          f"not re-derived in {round_no - f.last_seen_round} consecutive rounds (k={k})"))
            record.stale_closed += 1
