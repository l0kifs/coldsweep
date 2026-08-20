"""Fix verification. Whether a finding is fixed is decided by the repository, not by a report."""

from __future__ import annotations

from pathlib import Path

from .models import Finding, Profile, normalize_snippet
from .shard import governed_files


def _normalized_file(repo: Path, rel: str) -> str | None:
    """One file's normalized text, or ``None`` when it cannot be read."""
    try:
        return normalize_snippet((repo / rel).read_text(encoding="utf-8"))
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


def verify_findings(repo: Path, profile: Profile, findings: list[Finding], round_no: int) -> dict[str, int]:
    """Re-check every ``fixed`` finding against its evidence, in the file its anchor names.

    Only decides for ``absence`` findings that carry evidence *and* whose anchor names a
    readable file the profile governs -- audited or editable. Everything else is deferred: a
    claim that cannot be checked is never upgraded to a confirmation, and is resolved instead
    by a later round failing to re-derive the finding.

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
    texts: dict[str, str | None] = {}
    for f in candidates:
        if not f.evidence or profile.mode_of(f.rule_id) != "absence":
            stats["deferred"] += 1
            continue
        if f.file not in governed:
            _defer(stats, f, round_no, f"{f.file} is outside the profile scope")
            continue
        if f.file not in texts:
            texts[f.file] = _normalized_file(repo, f.file)
        text = texts[f.file]
        if text is None:
            _defer(stats, f, round_no, f"{f.file} could not be read")
            continue
        if evidence_present(text, f.evidence):
            f.status = "open"
            f.log(round_no, "reopen", detail=f"evidence still present in {f.file}")
            stats["reopened"] += 1
            if f.reopen_count > 2:
                f.status = "disputed"
                f.adjudicated = False
                f.log(round_no, "dispute", detail=f"oscillation guard: {f.reopen_count} reopens")
        else:
            f.status = "verified"
            f.log(round_no, "verify", detail=f"evidence absent from {f.file}")
            stats["verified"] += 1
    return stats
