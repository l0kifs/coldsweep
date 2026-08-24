"""Spec authoring, freeze, and spec-item to implementation traceability.

A features task states its work as a spec document rather than a rule taxonomy. That creates
two problems the other profiles do not have.

**The target moves.** Convergence is meaningless if the spec can grow while the loop runs, so
the spec is *frozen*: every item's text is hashed into ``spec.lock`` before work begins, and
any later edit is a detected event that must be re-frozen deliberately.

**Traceability is reward-hackable.** A marker comment naming a spec item is cheap to write and
proves nothing, exactly like a coverage number. So markers only ever *address* an item -- they
say where its implementation claims to be. Whether that implementation is substantive is a
`presence` judgement made by an agent at the anchor the marker points to, and an item with no
marker at all is a deterministic finding.

Standing limit, per spec S14: **the loop never validates the spec itself.** An incomplete spec
converges cleanly. Nothing here changes that, and nothing here should pretend to.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from pydantic import ValidationError

from .models import (
    Profile,
    RawFinding,
    SpecConfig,
    SpecDrift,
    SpecItem,
    SpecLock,
    SpecReport,
)
from .shard import resolve_scope

_WS = re.compile(r"\s+")


class SpecError(RuntimeError):
    pass


def normalize_spec_text(text: str) -> str:
    """Collapse formatting so reflowing a paragraph is not mistaken for changing its meaning."""
    return _WS.sub(" ", text).strip()


def item_sha(body: str) -> str:
    return hashlib.sha1(normalize_spec_text(body).encode("utf-8")).hexdigest()


def parse_spec(text: str, config: SpecConfig) -> list[SpecItem]:
    """Split a spec document into addressable items.

    An item runs from its own heading to the next one. Ids come from the document, never from
    the heading text, so an item keeps its identity when it is retitled.
    """
    pattern = re.compile(config.item_pattern, re.MULTILINE)
    matches = list(pattern.finditer(text))
    items: list[SpecItem] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item_id = match.group("id")
        if item_id in seen:
            raise SpecError(f"duplicate spec item id {item_id!r} in {config.path}")
        seen.add(item_id)
        body = text[match.end():end]
        items.append(SpecItem(
            id=item_id,
            title=(match.groupdict().get("title") or "").strip(),
            body=body.strip(),
            anchor=f"{config.path}::{item_id}",
            sha=item_sha(body),
        ))
    return items


def load_spec(repo: Path, config: SpecConfig) -> list[SpecItem]:
    path = repo / config.path
    if not path.is_file():
        raise SpecError(f"no spec at {config.path}; a features task needs one before it can run")
    items = parse_spec(path.read_text(encoding="utf-8"), config)
    if not items:
        raise SpecError(
            f"{config.path} contains no items matching the configured pattern.\n"
            f"  pattern: {config.item_pattern}\n"
            f"  expected headings such as: ### FR-1 Session expiry")
    return items


def load_lock(path: Path) -> SpecLock | None:
    if not path.is_file():
        return None
    try:
        return SpecLock.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise SpecError(f"{path}: corrupt freeze record:\n{exc}") from exc


def drift_of(lock: SpecLock | None, items: list[SpecItem]) -> SpecDrift:
    """Everything that changed since the freeze. An absent lock means nothing is frozen yet."""
    current = {item.id: item.sha for item in items}
    frozen = lock.items if lock else {}
    return SpecDrift(
        added=sorted(set(current) - set(frozen)),
        changed=sorted(i for i in set(current) & set(frozen) if current[i] != frozen[i]),
        removed=sorted(set(frozen) - set(current)),
    )


# --- traceability ----------------------------------------------------------

def symbol_ranges(source: str) -> list[tuple[int, int, str]]:
    """Line ranges of every function and class, innermost last, for anchoring a marker."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ranges: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                path = [*stack, child.name]
                ranges.append((child.lineno, child.end_lineno or child.lineno, "::".join(path)))
                walk(child, path)
            else:
                walk(child, stack)

    walk(tree, [])
    return ranges


def symbol_text(source: str, anchor: str) -> str | None:
    """The source of the symbol an anchor names, or ``None`` when it cannot be located.

    ``None`` is the answer for a module-level anchor, a renamed or deleted symbol, and a file
    that no longer parses. Every caller treats it as "cannot tell" rather than as an empty
    symbol, because the three cases are indistinguishable here and none of them is evidence.
    """
    path = anchor.split("::", 1)[1] if "::" in anchor else ""
    if not path:
        return None
    lines = source.splitlines()
    for start, end, symbol in symbol_ranges(source):
        if symbol == path:
            return "\n".join(lines[start - 1:end])
    return None


def anchor_for(file: str, source: str, lineno: int) -> str:
    """The innermost symbol containing a line, as a stable anchor. Never a line number."""
    best_start, best_path = -1, ""
    for start, end, path in symbol_ranges(source) if file.endswith(".py") else []:
        if best_start < start <= lineno <= end:
            best_start, best_path = start, path
    return f"{file}::{best_path}" if best_path else file


def find_markers(repo: Path, profile: Profile) -> dict[str, list[tuple[str, str]]]:
    """Every spec marker in scope, as ``item_id -> [(file, anchor)]``."""
    if profile.spec is None:
        return {}
    pattern = re.compile(profile.spec.marker_pattern)
    out: dict[str, list[tuple[str, str]]] = {}
    for rel in resolve_scope(repo, profile.scope):
        if rel == profile.spec.path:
            continue
        try:
            source = (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            match = pattern.search(line)
            if match:
                out.setdefault(match.group("id"), []).append((rel, anchor_for(rel, source, lineno)))
    return out


def implemented_items(repo: Path, profile: Profile) -> set[str]:
    """Ids of frozen items something in scope claims to implement, re-derived from the repository.

    The marker sweep that produces `unimplemented-spec-item` findings is exhaustive over scope
    and costs a regex pass, so repeating it is the honest way to check whether such a finding
    was resolved. `verify` uses it for exactly that: a presence finding carries no offending
    snippet to look for, so without this it can only ever be deferred.
    """
    if profile.spec is None:
        return set()
    return {item_id for item_id, sites in find_markers(repo, profile).items() if sites}


def run(repo: Path, profile: Profile, lock: SpecLock | None) -> tuple[list[RawFinding], SpecReport]:
    """Deterministic half of a features task: which frozen items nothing claims to implement."""
    if profile.spec is None:
        return [], SpecReport()
    config = profile.spec
    items = load_spec(repo, config)
    markers = find_markers(repo, profile)
    report = SpecReport(items=len(items), frozen=len(lock.items) if lock else 0,
                        drift=drift_of(lock, items))

    findings: list[RawFinding] = []
    known = {item.id for item in items}
    for item in items:
        if markers.get(item.id):
            report.implemented += 1
            continue
        report.unimplemented += 1
        findings.append(RawFinding(
            rule_id=config.unimplemented_rule_id,
            anchor=item.anchor,
            evidence=None,
            description=(f"No implementation in scope claims spec item {item.id} "
                         f"({item.title}). Nothing carries its marker."),
        ))

    if config.stale_reference_rule_id:
        for item_id, sites in sorted(markers.items()):
            if item_id in known:
                continue
            report.stale_markers += len(sites)
            for _file, anchor in sites:
                findings.append(RawFinding(
                    rule_id=config.stale_reference_rule_id,
                    anchor=anchor,
                    evidence=f"spec: {item_id}",
                    description=(f"This marker names spec item {item_id}, which does not exist in "
                                 f"{config.path}. The item was renamed or deleted."),
                ))
    return findings, report


def spec_context(repo: Path, profile: Profile, files: list[str]) -> str:
    """The frozen spec items a shard claims to implement, for injection into the scan prompt.

    This is task statement, not prior findings: it is the same text every round, and it says
    nothing about what any previous round concluded.
    """
    if profile.spec is None:
        return ""
    try:
        items = {item.id: item for item in load_spec(repo, profile.spec)}
    except SpecError:
        return ""
    wanted = {item_id for item_id, sites in find_markers(repo, profile).items()
              if any(file in files for file, _ in sites)}
    relevant = [items[i] for i in sorted(wanted) if i in items]
    if not relevant:
        return ""
    blocks = "\n\n".join(f"### {item.id} {item.title}\n\n{item.body}" for item in relevant)
    return (
        "## Spec items these files claim to implement\n\n"
        "Each was frozen before work began. Judge whether the code actually delivers what the "
        "item states -- a marker comment naming an item is not an implementation.\n\n"
        f"{blocks}\n"
    )


def blockers(repo: Path, profile: Profile, lock_path: Path) -> list[str]:
    """Reasons the gate must stay shut regardless of the finding set."""
    if profile.spec is None:
        return []
    try:
        items = load_spec(repo, profile.spec)
    except SpecError as exc:
        return [str(exc).splitlines()[0]]
    lock = load_lock(lock_path)
    if lock is None:
        return [f"{profile.spec.path} is not frozen; run `coldsweep spec freeze` before the loop can converge"]
    return [f"{reason} -- re-freeze deliberately with `coldsweep spec freeze`"
            for reason in drift_of(lock, items).reasons()]


def freeze(repo: Path, profile: Profile, round_no: int) -> tuple[SpecLock, SpecDrift]:
    if profile.spec is None:
        raise SpecError("this task defines no `spec:` block; there is nothing to freeze")
    items = load_spec(repo, profile.spec)
    return (SpecLock(spec=profile.spec.path, frozen_round=round_no,
                     items={item.id: item.sha for item in items}),
            drift_of(None, items))
