"""A deterministic stand-in for `claude -p --output-format json`.

Reads a rendered coldsweep prompt on stdin, does the mechanical equivalent of what a scan or fix
agent would do, and answers in the same envelope the real CLI emits. Lets the whole loop --
including retries, schema enforcement and the fix/verify round trip -- run with zero LLM calls.

STUB_MODE:
  ok        (default) behave, reporting only rules the prompt's taxonomy contains
  rogue     report every rule it knows regardless of the taxonomy, to exercise the gate
  stubborn  dispute every finding instead of fixing it, so triage is the only way forward
  garbage   emit prose instead of JSON, so schema enforcement and retries are exercised
  flaky     emit garbage once per shard, then behave
  badschema emit well-formed JSON that violates the schema
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

FILE_RE = re.compile(r"^- `([^`]+)`$", re.MULTILINE)
ID_RE = re.compile(r"^- id: `([^`]+)`$", re.MULTILINE)
TAXONOMY_RE = re.compile(r"^- `([^`]+)` \((?:absence|presence)\)", re.MULTILINE)
FINDING_RULE_RE = re.compile(r"^  rule: `([^`]+)`", re.MULTILINE)
DEF_RE = re.compile(r"^(\s*)def\s+(\w+)")


def envelope(payload: dict) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": json.dumps(payload)})


def enclosing_def(lines: list[str], index: int) -> str | None:
    for i in range(index, -1, -1):
        match = DEF_RE.match(lines[i])
        if match:
            return match.group(2)
    return None


def scan_file(path: str, rules: set[str]) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    found: list[dict] = []
    if "swallowed-exception" in rules:
        found.extend(_swallowed(path, lines))
    if "undocumented-public-symbol" in rules:
        found.extend(_undocumented(path, lines))
    return found


def _swallowed(path: str, lines: list[str]) -> list[dict]:
    found: list[dict] = []
    for i, line in enumerate(lines):
        if re.match(r"\s*except\b.*:\s*$", line) and i + 1 < len(lines) and lines[i + 1].strip() == "pass":
            symbol = enclosing_def(lines, i)
            found.append({
                "rule_id": "swallowed-exception",
                "anchor": f"{path}::{symbol}" if symbol else path,
                "evidence": f"{line}\n{lines[i + 1]}",
                "description": "The exception is discarded, so the caller cannot tell the call failed.",
            })
    return found


def _undocumented(path: str, lines: list[str]) -> list[dict]:
    found: list[dict] = []
    for i, line in enumerate(lines):
        match = DEF_RE.match(line)
        if not match or match.group(2).startswith("_"):
            continue
        body = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not body.startswith(('"""', "'''")):
            found.append({
                "rule_id": "undocumented-public-symbol",
                "anchor": f"{path}::{match.group(2)}",
                "evidence": None,
                "description": f"{match.group(2)} has no docstring saying what it does.",
            })
    return found


def fix_file(path: str, rules: set[str]) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        match = DEF_RE.match(line)
        if match and not match.group(2).startswith("_") and "undocumented-public-symbol" in rules:
            body = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if not body.startswith(('"""', "'''")):
                indent = match.group(1) + "    "
                out.append(f'{indent}"""Return the {match.group(2)} result, raising on failure."""')
    text = "\n".join(out) + "\n"
    if "swallowed-exception" in rules:
        text = re.sub(r"(\n(\s*)except\b[^\n]*:\n)\2    pass\n", r"\1\2    raise\n", text)
    Path(path).write_text(text, encoding="utf-8")


def flaky_gate(key: str) -> bool:
    """True when this invocation should misbehave. One failure per key, then success.

    One marker file per key, created exclusively -- shards run concurrently, so a single
    shared state file would race and fail the same shard twice.
    """
    root = Path(os.environ.get("STUB_STATE", ".stub-state"))
    root.mkdir(parents=True, exist_ok=True)
    marker = root / hashlib.sha1(key.encode()).hexdigest()
    try:
        marker.open("x").close()
    except FileExistsError:
        return False
    return True


def main() -> None:
    prompt = sys.stdin.read()
    mode = os.environ.get("STUB_MODE", "ok")
    files = FILE_RE.findall(prompt)
    key = "|".join(files) or "none"

    if mode == "garbage" or (mode == "flaky" and flaky_gate(key)):
        print("I looked at the files and everything seems fine to me.")
        return
    if mode == "badschema":
        print(envelope({"findings": [{"rule": "nope", "where": "somewhere"}]}))
        return

    if "## Findings to resolve" in prompt:
        if mode == "stubborn":
            print(envelope({"results": [{"id": fid, "outcome": "disputed",
                                         "detail": "the code is correct as written"}
                                        for fid in ID_RE.findall(prompt)]}))
            return
        wanted = set(FINDING_RULE_RE.findall(prompt))
        for path in files:
            fix_file(path, wanted)
        results = [{"id": fid, "outcome": "fixed", "detail": "rewrote the offending code"}
                   for fid in ID_RE.findall(prompt)]
        print(envelope({"results": results}))
        return

    if "## Decision" in prompt:
        print(envelope({"verdict": "different", "reason": "stub adjudicator never merges"}))
        return

    known = {"swallowed-exception", "undocumented-public-symbol"}
    taxonomy = known if mode == "rogue" else set(TAXONOMY_RE.findall(prompt)) & known
    findings: list[dict] = []
    for path in files:
        findings.extend(scan_file(path, taxonomy))
    print(envelope({"findings": findings}))


if __name__ == "__main__":
    main()
