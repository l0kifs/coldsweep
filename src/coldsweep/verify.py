"""Fix verification. Whether a finding is fixed is decided by the repository, not by a report."""

from __future__ import annotations

from pathlib import Path

from .models import Finding, Profile, evidence_sha, normalize_snippet
from .shard import governed_files
from .spec import implemented_items, symbol_text


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


def verify_findings(repo: Path, profile: Profile, findings: list[Finding], round_no: int) -> dict[str, int]:
    """Re-check every ``fixed`` finding against its evidence, in the file its anchor names.

    Decides two kinds of finding. ``absence`` findings that carry evidence *and* whose anchor
    names a readable file the profile governs -- audited or editable -- are decided by looking
    for the offending snippet, inside the anchored symbol where one can be located and in the
    whole file otherwise. ``unimplemented-spec-item`` findings are decided by re-deriving the
    marker set, which is exhaustive over scope and costs one regex pass; they are anchored in
    the spec document and carry no snippet, so the snippet path can never decide them.

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
    governed = set(governed_files(repo, profile))
    # Re-derived once for the whole pass, not per finding: the sweep is over all of scope.
    spec_rule = profile.spec.unimplemented_rule_id if profile.spec else None
    implemented = implemented_items(repo, profile) if spec_rule else set()
    sources: dict[str, str | None] = {}
    for f in candidates:
        if spec_rule and f.rule_id == spec_rule:
            item = _spec_item_of(f)
            if item in implemented:
                _verified(stats, f, round_no, f"{item} is marked by an implementation in scope")
            else:
                _reopen(stats, f, round_no, f"nothing in scope carries a marker for {item}")
            continue
        if not f.evidence or profile.mode_of(f.rule_id) != "absence":
            stats["deferred"] += 1
            continue
        if f.file not in governed:
            _defer(stats, f, round_no, f"{f.file} is outside the profile scope")
            continue
        if f.file not in sources:
            sources[f.file] = _read(repo, f.file)
        source = sources[f.file]
        if source is None:
            _defer(stats, f, round_no, f"{f.file} could not be read")
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
        else:
            _reopen(stats, f, round_no, f"evidence still present in {where}, unchanged")
    return stats
