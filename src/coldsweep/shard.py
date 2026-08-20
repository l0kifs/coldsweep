"""Scope resolution and sharding. Deterministic and content-independent, so rounds compare."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .models import Profile, Scope, Shard

MAX_RECOMMENDED_FILES_PER_SHARD = 5


class ShardError(RuntimeError):
    pass


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a git-style path glob to a regex. ``**`` spans directories, ``*`` does not."""
    i, out, n = 0, ["(?s:"], len(pattern)
    while i < n:
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("/**", i):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                body = pattern[i + 1:close]
                body = ("^" + body[1:]) if body.startswith("!") else body
                out.append(f"[{body}]")
                i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append(r")\Z")
    return re.compile("".join(out))


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(_glob_to_regex(p).match(path) for p in patterns)


def git_files(repo: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files. Never a filesystem walk."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
    except FileNotFoundError as exc:
        raise ShardError("git is not on PATH; coldsweep resolves scope through git ls-files") from exc
    except subprocess.CalledProcessError as exc:
        raise ShardError(f"git ls-files failed in {repo}: {exc.stderr.strip()}") from exc
    seen: dict[str, None] = {}
    for entry in proc.stdout.split("\0"):
        if entry:
            seen.setdefault(entry, None)
    return sorted(seen)


def resolve_scope(repo: Path, scope: Scope) -> list[str]:
    files = git_files(repo)
    included = [f for f in files if matches_any(f, scope.include)]
    if scope.exclude:
        included = [f for f in included if not matches_any(f, scope.exclude)]
    return sorted(included)


def resolve_editable(repo: Path, profile: Profile) -> list[str]:
    """Files a fix agent may write. ``scope`` when the profile names no separate set."""
    return resolve_scope(repo, profile.editable or profile.scope)


def governed_files(repo: Path, profile: Profile) -> list[str]:
    """Every file the task acts on at all -- audited, editable, or both.

    Verification asks membership here rather than in ``scope``: a finding anchored in a file
    the task may repair but does not audit is still one whose fix can be proved.
    """
    return sorted(set(resolve_scope(repo, profile.scope)) | set(resolve_editable(repo, profile)))


def shard_id(files: list[str]) -> str:
    digest = hashlib.sha1("\n".join(sorted(files)).encode("utf-8")).hexdigest()[:8]
    return f"s-{digest}"


def test_paths(source: str, patterns: list[str]) -> list[str]:
    """Where a source file's tests would live by convention. Existence is not checked.

    Naming the paths a file has no tests at is how the finding says what to write.
    """
    path = Path(source)
    out: list[str] = []
    for pattern in patterns:
        candidate = pattern.format(stem=path.stem, parent=path.parent.name,
                                   path=path.with_suffix("").as_posix())
        if candidate not in out:
            out.append(candidate)
    return out


def paired_tests(repo: Path, source: str, patterns: list[str]) -> list[str]:
    """Tests responsible for one source file, by convention rather than by guesswork."""
    return [c for c in test_paths(source, patterns) if (repo / c).is_file()]


def build_shards(repo: Path, profile: Profile) -> list[Shard]:
    files = resolve_scope(repo, profile.scope)
    if profile.spec is not None:
        # The spec states the task; it is not an artefact under audit. An agent handed the
        # spec as a shard reports its own items back as findings, under doc-style anchors that
        # collide with nothing and close never.
        files = [f for f in files if f != profile.spec.path]
    size = profile.files_per_shard
    chunks = [files[i:i + size] for i in range(0, len(files), size)]
    if profile.mutation is not None:
        # A profile that judges tests must show the agent the tests. The pairing already
        # exists for mutation; without it here, rules like `vacuous-test` are asked about
        # files the agent was never given.
        patterns = profile.mutation.test_patterns
        chunks = [chunk + [t for f in chunk for t in paired_tests(repo, f, patterns)
                           if t not in chunk]
                  for chunk in chunks]
    return [Shard(id=shard_id(chunk), files=chunk) for chunk in chunks]


def shard_warnings(profile: Profile) -> list[str]:
    if profile.files_per_shard > MAX_RECOMMENDED_FILES_PER_SHARD:
        return [
            f"files_per_shard={profile.files_per_shard} exceeds the recommended maximum of "
            f"{MAX_RECOMMENDED_FILES_PER_SHARD}; enumeration exhaustiveness degrades sharply with scope"
        ]
    return []
