"""Fix verification. Whether a finding is fixed is decided by the repository, not by a report."""

from __future__ import annotations

from pathlib import Path

from . import mutation, syntax
from .models import Finding, Profile, evidence_sha, normalize_snippet
from .shard import ShardError, governed_files
from .spec import SpecError, implemented_items, symbol_text


def _read(repo: Path, rel: str) -> str | None:
    """One file's source, or ``None`` when it cannot be read.

    Raw, not normalized: the anchored symbol has to be located by parsing it first, and
    normalization is applied to whichever region that lookup settles on.
    """
    try:
        return (repo / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def evidence_present(text: str, evidence: str | None) -> bool:
    """Whether the offending snippet is still in this text, comparing normalized forms."""
    if not evidence:
        return False
    return normalize_snippet(evidence) in text


def _defer(stats: dict[str, int], finding: Finding, round_no: int, why: str) -> None:
    """A fix that cannot be checked stays claimed, never confirmed."""
    stats["deferred"] += 1
    finding.log(round_no, "defer", detail=f"{why}; cannot prove the evidence gone")


def _verified(stats: dict[str, int], finding: Finding, round_no: int, why: str) -> None:
    stats["verified"] += 1
    finding.status = "verified"
    finding.log(round_no, "verify", detail=why)


def _reopen(stats: dict[str, int], finding: Finding, round_no: int, why: str) -> None:
    """The fix did not hold. Repeated flapping is a dispute, not an eleventh attempt."""
    stats["reopened"] += 1
    finding.status = "open"
    finding.log(round_no, "reopen", detail=why)
    if finding.reopen_count > 2:
        finding.status = "disputed"
        finding.adjudicated = False
        finding.log(round_no, "dispute", detail=f"oscillation guard: {finding.reopen_count} reopens")


def _spec_item_of(finding: Finding) -> str:
    """The item id an `unimplemented-spec-item` finding is anchored on: ``SPEC.md::FR-1`` -> ``FR-1``."""
    return finding.anchor.split("::", 1)[-1].strip()


def _governed_source(repo: Path, finding: Finding, governed: set[str] | None,
                     cache: dict[str, str | None]) -> str | None:
    """The finding's file, read once per pass, or ``None`` when it cannot be used as evidence.

    One ``None`` for three cases the caller treats alike: scope unresolvable, file outside the
    governed set, file unreadable. None of them is proof about the fix either way.
    """
    if governed is None or finding.file not in governed:
        return None
    if finding.file not in cache:
        cache[finding.file] = _read(repo, finding.file)
    return cache[finding.file]


def _decide_spec_item(stats: dict[str, int], finding: Finding, round_no: int,
                      implemented: set[str] | None) -> None:
    """Decide one spec-item finding against the re-derived marker set.

    ``implemented`` is ``None`` when the sweep could not run at all, which is not evidence that
    the item is unmarked -- so the finding defers rather than reopening.
    """
    if implemented is None:
        _defer(stats, finding, round_no, "scope could not be resolved through git")
        return
    item = _spec_item_of(finding)
    if item in implemented:
        _verified(stats, finding, round_no, f"{item} is marked by an implementation in scope")
    else:
        _reopen(stats, finding, round_no, f"nothing in scope carries a marker for {item}")


def _decide_unpinned(stats: dict[str, int], finding: Finding, round_no: int,
                     unpinned: set[str] | None) -> None:
    """Decide one mutation finding against the re-derived survivor set.

    ``unpinned`` is every anchor the subsystem still reports. It is exhaustive over its scope,
    so an anchor missing from it is proof the symbol is now pinned -- not silence. That
    distinction is the whole reason this branch exists: without it the finding could only close
    by lapsing, and a `tests` task closed nothing on evidence.

    ``None`` means the subsystem could not run -- a red baseline, a broken build command -- which
    says nothing about the fix either way, so the finding defers.
    """
    if unpinned is None:
        _defer(stats, finding, round_no, "the mutation subsystem could not run")
        return
    if finding.anchor in unpinned:
        _reopen(stats, finding, round_no,
                f"the suite still passes with {finding.anchor} mutated")
    else:
        _verified(stats, finding, round_no,
                  f"no mutation of {finding.anchor} survives the suite")


def _rederive_unpinned(repo: Path, profile: Profile, cache: Path | None,
                       lock: Path | None) -> set[str] | None:
    """Every anchor the mutation subsystem still reports, or ``None`` when it could not run.

    Cost-neutral inside ``coldsweep run``. The pass runs against the post-fix tree, and the next
    round's scan then runs against that same tree, so its verdicts come back as cache hits: the
    same two passes per round as before, one of them moved earlier and turned into proof.
    """
    if profile.mutation is None or cache is None:
        return None
    try:
        survivors, _ = mutation.run(repo, profile, cache, lock)
    except mutation.MutationError:
        return None
    return {raw.anchor for raw in survivors}


def settle_disputes(repo: Path, profile: Profile, findings: list[Finding], round_no: int,
                    mutants_cache: Path | None = None,
                    mutation_lock: Path | None = None) -> dict[str, int]:
    """Decide the disputes a subsystem can decide, before a human is asked anything.

    A dispute under a `decided_by: code` rule contains a factual question the deciding subsystem
    already answers exhaustively: is this anchor still reported? Asking a person is asking them
    to re-derive by hand what the tool re-derives in one pass.

    Only the *settled* half is closed here. An anchor the subsystem no longer reports is
    ``verified`` -- the objection was right and the work is done. An anchor it still reports is
    left disputed and still pending, annotated with the confirmation, because "the fix phase
    tried three times and failed" is not a question about facts. Whether to keep paying for it
    is a policy call, and the gate must not open over a symbol nothing pins on the strength of a
    re-derivation that says exactly the opposite.

    Reopening those instead would be worse than leaving them: the oscillation guard disputed
    them precisely to stop the fix phase cycling on them, and reopening reinstates the cycle.
    """
    stats = {"verified": 0, "confirmed": 0, "undecidable": 0}
    # Which rules a subsystem owns, taken from the subsystem configs rather than from
    # `decided_by`. The two can disagree on a misconfigured profile, and the config that names
    # the rule is the one that can actually answer for it -- `verify_findings` reads the same
    # source, so a finding cannot be decidable in one pass and undecidable in the other.
    spec_rule = profile.spec.unimplemented_rule_id if profile.spec else None
    mutation_rule = profile.mutation.rule_id if profile.mutation else None
    owned = {r for r in (spec_rule, mutation_rule) if r}
    pending = [f for f in findings
               if f.status == "disputed" and not f.adjudicated and f.rule_id in owned]
    if not pending:
        return stats

    implemented: set[str] | None = None
    if spec_rule and any(f.rule_id == spec_rule for f in pending):
        try:
            implemented = implemented_items(repo, profile)
        except (ShardError, SpecError):
            implemented = None
    unpinned: set[str] | None = None
    if mutation_rule and any(f.rule_id == mutation_rule for f in pending):
        unpinned = _rederive_unpinned(repo, profile, mutants_cache, mutation_lock)

    for f in pending:
        if f.rule_id == spec_rule:
            settled, still_open = implemented is not None, _spec_item_of(f) not in (implemented or ())
            why = f"{_spec_item_of(f)} is marked by an implementation in scope"
        else:  # the only other member of `owned`
            settled, still_open = unpinned is not None, f.anchor in (unpinned or ())
            why = f"no mutation of {f.anchor} survives the suite"
        if not settled:
            stats["undecidable"] += 1
            continue
        if still_open:
            stats["confirmed"] += 1
            f.log(round_no, "adjudicated", method="stale",
                  detail="re-derived: still reported, so the dispute is about effort, not fact")
            continue
        stats["verified"] += 1
        f.status = "verified"
        f.adjudicated = True
        f.log(round_no, "verify", detail=f"dispute settled by re-derivation: {why}")
    return stats


def verify_findings(repo: Path, profile: Profile, findings: list[Finding], round_no: int,
                    mutants_cache: Path | None = None,
                    mutation_lock: Path | None = None) -> dict[str, int]:
    """Re-check every ``fixed`` finding against its evidence, in the file its anchor names.

    Decides three kinds of finding. ``absence`` findings that carry evidence *and* whose anchor
    names a readable file the profile governs -- audited or editable -- are decided by looking
    for the offending snippet, inside the anchored symbol where one can be located and in the
    whole file otherwise. The other two carry no snippet, so the snippet path can never decide
    them, and each is decided by re-running the subsystem that raised it: spec items against the
    marker set, mutation findings against the survivor set. Both are exhaustive over their
    scope, which is what makes an anchor's absence proof rather than silence.

    Re-running the mutation subsystem here needs ``mutants_cache``; without it those findings
    defer exactly as before. It is not an extra pass: it runs against the post-fix tree, which is
    the tree the next round's scan measures too, so that scan comes back from the cache.

    A surviving snippet is not by itself proof that a fix failed. Whole classes of remedy are
    additive -- handling wrapped around a call, validation added after a read -- and leave the
    cited line exactly where it was. So a snippet that is still present reopens the finding only
    when the symbol around it is also unchanged; if the symbol moved, the two cases cannot be
    told apart here and the finding is deferred to the next round's fresh derivation.

    Everything else is deferred: a claim that cannot be checked is never upgraded to a
    confirmation, and is resolved instead by a later round failing to re-derive the finding.

    The search is the anchor's file, not the whole repository. A repo-wide search cannot tell a
    fix from an unrelated file that happens to contain the same snippet, so one surviving
    instance of a common idiom would reopen every finding under its rule. Code that was moved
    rather than fixed is caught by the next round instead: a fresh scan re-derives it at its
    new anchor.
    """
    stats = {"verified": 0, "reopened": 0, "deferred": 0}
    candidates = [f for f in findings if f.status == "fixed"]
    if not candidates:
        return stats
    try:
        governed: set[str] | None = set(governed_files(repo, profile))
    except ShardError:
        governed = None
    # Re-derived once for the whole pass, not per finding: each sweep is over all of scope.
    spec_rule = profile.spec.unimplemented_rule_id if profile.spec else None
    implemented: set[str] | None = set()
    if spec_rule:
        try:
            implemented = implemented_items(repo, profile)
        except (ShardError, SpecError):
            implemented = None
    # Only when something actually needs it: the mutation sweep runs test suites, and a pass
    # that would decide nothing is pure cost.
    mutation_rule = profile.mutation.rule_id if profile.mutation else None
    unpinned: set[str] | None = None
    if mutation_rule and any(f.rule_id == mutation_rule for f in candidates):
        unpinned = _rederive_unpinned(repo, profile, mutants_cache, mutation_lock)
    sources: dict[str, str | None] = {}
    for f in candidates:
        if spec_rule and f.rule_id == spec_rule:
            _decide_spec_item(stats, f, round_no, implemented)
            continue
        if mutation_rule and f.rule_id == mutation_rule:
            _decide_unpinned(stats, f, round_no, unpinned)
            continue
        if not f.evidence or profile.mode_of(f.rule_id) != "absence":
            stats["deferred"] += 1
            continue
        source = _governed_source(repo, f, governed, sources)
        if source is None:
            _defer(stats, f, round_no,
                   "scope could not be resolved through git" if governed is None
                   else f"{f.file} is outside the profile scope, or could not be read")
            continue
        # The anchored symbol, not the whole file: the same idiom several times over in one
        # module would otherwise let an untouched copy reopen a finding about a fixed one.
        body = symbol_text(source, f.anchor)
        where = f.anchor if body is not None else f.file
        if not evidence_present(normalize_snippet(body if body is not None else source), f.evidence):
            _verified(stats, f, round_no, f"evidence absent from {where}")
        elif body is not None and f.pre_fix_sha and evidence_sha(body) != f.pre_fix_sha:
            _defer(stats, f, round_no,
                   f"{where} changed but the cited snippet is still there, which is what an "
                   f"additive remedy looks like")
        elif body is None and "::" in f.anchor and not syntax.resolves(f.file):
            # The snippet survives somewhere in the file, and no symbol could be located to say
            # whether it survives in *this* one. Outside a resolvable language those are the same
            # observation, and reopening on it fails a correct fix whenever the same idiom appears
            # twice in a file. Deferring costs one more round; reopening costs a real fix.
            _defer(stats, f, round_no,
                   f"no syntax support for {f.file}, so the snippet can only be searched "
                   f"file-wide, which cannot tell a fix from another copy of the same idiom")
        else:
            _reopen(stats, f, round_no, f"evidence still present in {where}, unchanged")
    return stats
