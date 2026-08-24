"""Deterministic prefilter. Exhaustive over its rule, so its coverage is real rather than claimed."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from pydantic import ValidationError

from .models import Finding, MechanicalCheck, Profile, RawFinding, Shard

CHUNK = 200


class MechanicalError(RuntimeError):
    pass


def _parse(payload: str, check: MechanicalCheck) -> list[RawFinding]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MechanicalError(f"mechanical check for {check.rule_id!r} did not emit JSON: {exc}") from exc
    items = data.get("findings", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise MechanicalError(f"mechanical check for {check.rule_id!r} must emit a list or {{'findings': [...]}}")
    out: list[RawFinding] = []
    for item in items:
        if not isinstance(item, dict):
            raise MechanicalError(f"mechanical check for {check.rule_id!r} emitted a non-object finding")
        payload_item = {**item, "rule_id": check.rule_id}
        payload_item.pop("id", None)
        try:
            out.append(RawFinding.model_validate(payload_item))
        except ValidationError as exc:
            raise MechanicalError(f"mechanical check for {check.rule_id!r} emitted an invalid finding:\n{exc}") from exc
    return out


def run_check(repo: Path, check: MechanicalCheck, files: list[str], timeout_s: int = 300) -> list[RawFinding]:
    if not files:
        return []
    found: list[RawFinding] = []
    for i in range(0, len(files), CHUNK):
        chunk = files[i:i + CHUNK]
        command = check.command.replace("{files}", " ".join(shlex.quote(f) for f in chunk))
        try:
            proc = subprocess.run(command, cwd=repo, shell=True, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MechanicalError(f"mechanical check for {check.rule_id!r} timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise MechanicalError(f"mechanical check for {check.rule_id!r} failed to launch: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise MechanicalError(f"mechanical check for {check.rule_id!r} produced non-UTF8 output: {exc}") from exc
        stdout = proc.stdout.strip()
        if not stdout:
            if proc.returncode != 0:
                raise MechanicalError(
                    f"mechanical check for {check.rule_id!r} exited {proc.returncode} with no output: "
                    f"{proc.stderr.strip()[:400]}"
                )
            continue
        found.extend(_parse(stdout, check))
    return found


def run_all(repo: Path, profile: Profile, shards: list[Shard], round_no: int) -> list[Finding]:
    """Run every configured check over the whole scope, then attribute findings back to shards."""
    if not profile.mechanical:
        return []
    file_to_shard = {f: s.id for s in shards for f in s.files}
    scope_files = sorted(file_to_shard)
    out: list[Finding] = []
    for check in profile.mechanical:
        for raw in run_check(repo, check, scope_files):
            shard = file_to_shard.get(raw.anchor.split("::", 1)[0].strip(), "mechanical")
            out.append(raw.to_finding(shard=shard, round_no=round_no, source="mechanical"))
    return out
