"""Fix verification. Whether a finding is fixed is decided by the repository, not by a report."""

from __future__ import annotations

from pathlib import Path

from .models import Finding, Profile, normalize_snippet
from .shard import resolve_scope


def _normalized_corpus(repo: Path, profile: Profile) -> list[str]:
    """Every in-scope file, normalized, kept separate.

    Whole-repo rather than per-anchor, because code that was moved instead of fixed is still
    present. Kept per file rather than concatenated, because joining them lets the tail of one
    file and the head of the next form a snippet that exists in neither.
    """
    chunks: list[str] = []
    for rel in resolve_scope(repo, profile.scope):
        path = repo / rel
        try:
            chunks.append(normalize_snippet(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return chunks


def evidence_present(corpus: str | list[str], evidence: str | None) -> bool:
    if not evidence:
        return False
    needle = normalize_snippet(evidence)
    haystacks = [corpus] if isinstance(corpus, str) else corpus
    return any(needle in text for text in haystacks)


def verify_findings(repo: Path, profile: Profile, findings: list[Finding], round_no: int) -> dict[str, int]:
    """Re-check every ``fixed`` finding against its evidence.

    Only decides for ``absence`` findings that carry evidence -- those have a deterministic
    predicate. ``presence`` findings carry none and are resolved by a later round failing to
    re-derive them.
    """
    stats = {"verified": 0, "reopened": 0, "deferred": 0}
    candidates = [f for f in findings if f.status == "fixed"]
    if not candidates:
        return stats
    corpus = _normalized_corpus(repo, profile)
    for f in candidates:
        if not f.evidence or profile.mode_of(f.rule_id) != "absence":
            stats["deferred"] += 1
            continue
        if evidence_present(corpus, f.evidence):
            f.status = "open"
            f.log(round_no, "reopen", method="stale", detail="evidence still present in repository")
            stats["reopened"] += 1
            if f.reopen_count > 2:
                f.status = "disputed"
                f.adjudicated = False
                f.log(round_no, "dispute", detail=f"oscillation guard: {f.reopen_count} reopens")
        else:
            f.status = "verified"
            f.log(round_no, "verify", detail="evidence absent from repository")
            stats["verified"] += 1
    return stats
