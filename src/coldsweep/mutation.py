"""Mutation testing: the honest predicate for `presence` rules about tests.

Coverage is trivially reward-hackable -- a test that imports a module and asserts nothing
scores the same as one that pins its behaviour. Mutation testing asks the only question that
cannot be gamed: if the source is changed, does the suite notice?

Three pieces, per spec S14:

- **runtime** -- apply one mutant, run the paired tests, restore, classify
- **cache** -- keyed by mutant identity plus the source and test content it was judged against
- **shard strategy** -- one source file paired with the tests responsible for it, which is both
  the unit of work and the unit of cache invalidation

Findings are emitted per *anchor*, not per mutant: "nothing pins the behaviour of this
function" is one work item, however many mutations demonstrate it.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path

from . import syntax
from .models import (
    Mutant,
    MutantResult,
    MutationConfig,
    MutationReport,
    MutationShard,
    Profile,
    RawFinding,
)
from .shard import paired_tests, resolve_scope, test_paths

BACKUP_SUFFIX = ".coldsweep-mutant-backup"

COMPARISON = {
    "is not": "is", "not in": "in", "is": "is not", "in": "not in",
    "==": "!=", "!=": "==", "<=": ">", ">=": "<", "<": ">=", ">": "<=",
}
ARITHMETIC = {"//": "/", "**": "*", "+": "-", "-": "+", "*": "/", "/": "*", "%": "*"}
BOOLEAN = {"and": "or", "or": "and"}


class MutationError(RuntimeError):
    pass


def invalidate_bytecode(path: Path) -> None:
    """Drop any cached bytecode for a file whose contents just changed underneath it.

    CPython validates a ``.pyc`` by source mtime and size. A mutation that keeps the file the
    same length, restored inside the same mtime tick, leaves a stale cache that the next
    interpreter happily imports -- so the suite would then be judging the mutant, not the
    original. Deleting the cache entry is the only reliable answer.
    """
    if path.suffix != ".py":
        return  # no other language caches compiled output beside its source
    stem = path.stem
    cache = path.parent / "__pycache__"
    try:
        if cache.is_dir():
            for entry in cache.glob(f"{stem}.*.pyc"):
                entry.unlink(missing_ok=True)
        path.with_suffix(".pyc").unlink(missing_ok=True)
    except OSError as exc:
        raise MutationError(f"{path}: cannot invalidate bytecode cache: {exc}") from exc


def _sentinel_for(file: str) -> bytes:
    """Content that makes a file fail to load, for the "do the tests touch this at all" probe.

    What the probe proves differs by language, and the finding text says so. For Python the
    sentinel raises on import, so a still-green suite never imported the module. For a compiled
    language the sentinel fails the build, so a still-green suite never *built* the file -- a
    weaker claim, but the same conclusion: mutants of it would survive for a reason that has
    nothing to do with test quality.
    """
    language = syntax.language_for(file)
    return language.sentinel if language is not None else syntax.PYTHON.sentinel


def _line_starts(source: bytes) -> list[int]:
    starts = [0]
    for i, byte in enumerate(source):
        if byte == 0x0A:
            starts.append(i + 1)
    return starts


def _offset(starts: list[int], lineno: int, col: int) -> int:
    return starts[lineno - 1] + col


def _anchor_of(path: str, stack: list[str]) -> str:
    return "::".join([path, *stack]) if stack else path


def _find_token(span: bytes, table: dict[str, str]) -> tuple[int, str] | None:
    """Locate an operator inside the source between two nodes.

    ``ast`` gives no position for operator nodes, so the token is recovered from the gap
    between the operands. Longest match first, so ``is not`` never reads as ``is``.
    """
    text = span.decode("utf-8", "replace")
    if "#" in text:
        return None
    for token in table:
        index = text.find(token)
        if index == -1:
            continue
        before = text[index - 1] if index else " "
        after = text[index + len(token)] if index + len(token) < len(text) else " "
        if token[0].isalpha() and (before.isalnum() or after.isalnum() or before == "_" or after == "_"):
            continue
        return len(text[:index].encode("utf-8")), token
    return None


class _Collector(ast.NodeVisitor):
    """Walks one module and yields every mutation the configured operators allow."""

    def __init__(self, path: str, source: bytes, operators: set[str]) -> None:
        self.path = path
        self.source = source
        self.starts = _line_starts(source)
        self.operators = operators
        self.stack: list[str] = []
        self.found: list[tuple[str, str, str, int, int]] = []  # anchor, operator, mutated, start, end

    def _span(self, node: ast.AST) -> tuple[int, int]:
        return (_offset(self.starts, node.lineno, node.col_offset),
                _offset(self.starts, node.end_lineno, node.end_col_offset))

    def _record(self, operator: str, mutated: str, start: int, end: int) -> None:
        self.found.append((_anchor_of(self.path, self.stack), operator, mutated, start, end))

    def _between(self, left: ast.AST, right: ast.AST, table: dict[str, str], operator: str) -> None:
        _, left_end = self._span(left)
        right_start, _ = self._span(right)
        if right_start <= left_end:
            return
        hit = _find_token(self.source[left_end:right_start], table)
        if hit is None:
            return
        rel, token = hit
        self._record(operator, table[token], left_end + rel, left_end + rel + len(token.encode("utf-8")))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    def visit_Compare(self, node: ast.Compare) -> None:
        if "comparison" in self.operators:
            operands = [node.left, *node.comparators]
            for left, right in pairwise(operands):
                self._between(left, right, COMPARISON, "comparison")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if "arithmetic" in self.operators:
            self._between(node.left, node.right, ARITHMETIC, "arithmetic")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if "boolean" in self.operators:
            for left, right in pairwise(node.values):
                self._between(left, right, BOOLEAN, "boolean")
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if "unary" in self.operators and isinstance(node.op, ast.Not):
            start, _ = self._span(node)
            operand_start, _ = self._span(node.operand)
            if operand_start > start:
                self._record("unary", "", start, operand_start)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        returns_value = node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None)
        if "return" in self.operators and returns_value:
            start, end = self._span(node.value)
            self._record("return", "None", start, end)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # a docstring is documentation, not behaviour
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if "constant" not in self.operators:
            return
        value = node.value
        start, end = self._span(node)
        if value is True:
            self._record("constant", "False", start, end)
        elif value is False:
            self._record("constant", "True", start, end)
        elif isinstance(value, int) and not isinstance(value, bool):
            self._record("constant", str(value + 1), start, end)
        elif isinstance(value, float):
            self._record("constant", repr(value + 1.0), start, end)
        elif isinstance(value, str):
            self._record("constant", '""' if value else '"coldsweep"', start, end)


def mutant_id(file: str, anchor: str, operator: str, original: str, occurrence: int) -> str:
    digest = hashlib.sha1(
        f"{file}\0{anchor}\0{operator}\0{original}\0{occurrence}".encode()).hexdigest()[:10]
    return f"m-{digest}"


def _sites(path: str, source: bytes, config: MutationConfig) -> list[tuple[str, str, str, int, int]]:
    """Raw ``(anchor, operator, replacement, start, end)`` tuples for one file.

    Python goes through ``_Collector``, which is the reference implementation and the one every
    published measurement was taken against. Everything else goes through tree-sitter, which
    offers a narrower operator set on purpose -- see ``syntax.mutation_sites``.

    A file whose extension names no configured language has nothing to mutate. It must not fall
    through to the Python parser: doing so reports a Go file as unparseable Python, which reads
    as a corrupt source rather than as a language this build does not cover.
    """
    language = syntax.language_for(path)
    if language is None:
        return []
    if language is not syntax.PYTHON:
        return [(_anchor_of(path, path_in_file.split("::")) if path_in_file else path,
                 operator, mutated, start, end)
                for path_in_file, operator, mutated, start, end
                in syntax.mutation_sites(source, path, set(config.operators))]
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise MutationError(f"{path}: cannot parse for mutation: {exc}") from exc
    collector = _Collector(path, source, set(config.operators))
    collector.visit(tree)
    return collector.found


def generate_mutants(path: str, source: bytes, config: MutationConfig) -> list[Mutant]:
    """Every mutation of one file, in source order. Deterministic, so rounds compare."""
    found = _sites(path, source, config)

    seen: dict[tuple[str, str, str], int] = {}
    mutants: list[Mutant] = []
    per_anchor: dict[str, int] = {}
    for anchor, operator, mutated, start, end in sorted(found, key=lambda item: item[3]):
        original = source[start:end].decode("utf-8", "replace")
        if original == mutated:
            continue
        if per_anchor.get(anchor, 0) >= config.max_mutants_per_anchor:
            continue
        per_anchor[anchor] = per_anchor.get(anchor, 0) + 1
        key = (anchor, operator, original)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        mutants.append(Mutant(
            id=mutant_id(path, anchor, operator, original, occurrence),
            file=path, anchor=anchor, operator=operator,
            original=original, mutated=mutated, start=start, end=end,
        ))
    return mutants


def apply_mutant(source: bytes, mutant: Mutant) -> bytes:
    return source[:mutant.start] + mutant.mutated.encode("utf-8") + source[mutant.end:]


def sha_of(paths: list[Path]) -> str:
    digest = hashlib.sha1()
    for path in sorted(paths):
        digest.update(str(path).encode("utf-8"))
        try:
            content = path.read_bytes() if path.is_file() else b"<missing>"
        except OSError as exc:
            raise MutationError(f"{path}: cannot read for mutation: {exc}") from exc
        digest.update(content)
    return digest.hexdigest()


def build_mutation_shards(repo: Path, profile: Profile) -> list[MutationShard]:
    """One shard per in-scope source file, carrying its mutants and its paired tests."""
    if profile.mutation is None:
        return []
    config = profile.mutation
    shards: list[MutationShard] = []
    for source in resolve_scope(repo, profile.scope):
        # A file whose language has no grammar installed yields no mutants, and a file with no
        # mutants yields no shard. That is silence, not a clean result, so it is reported by
        # `coldsweep languages` rather than left to look like a file nothing could go wrong in.
        if not syntax.resolves(source):
            continue
        path = repo / source
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise MutationError(f"{source}: cannot read for mutation: {exc}") from exc
        mutants = generate_mutants(source, body, config)
        if not mutants:
            continue
        shards.append(MutationShard(
            id=f"mut-{hashlib.sha1(source.encode()).hexdigest()[:8]}",
            source=source, tests=paired_tests(repo, source, config.test_patterns), mutants=mutants,
        ))
    return shards


class MutationCache:
    """Derived and rebuildable. Keyed by what could change a verdict, and nothing else."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS results (
        mutant_id TEXT NOT NULL,
        source_sha TEXT NOT NULL,
        tests_sha TEXT NOT NULL,
        command_sha TEXT NOT NULL,
        outcome TEXT NOT NULL,
        duration_s REAL NOT NULL,
        detail TEXT NOT NULL,
        PRIMARY KEY (mutant_id, source_sha, tests_sha, command_sha)
    );
    CREATE TABLE IF NOT EXISTS probes (
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        source_sha TEXT NOT NULL,
        tests_sha TEXT NOT NULL,
        command_sha TEXT NOT NULL,
        ok INTEGER NOT NULL,
        detail TEXT NOT NULL,
        PRIMARY KEY (kind, source, source_sha, tests_sha, command_sha)
    );
    """

    def __init__(self, path: Path) -> None:
        con = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path)
            con.executescript(self.SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            if con is not None:
                con.close()
            raise MutationError(f"{path}: cannot open mutation cache: {exc}") from exc
        self.con = con

    def get(self, key: tuple[str, str, str, str]) -> MutantResult | None:
        try:
            row = self.con.execute(
                "SELECT outcome, duration_s, detail FROM results "
                "WHERE mutant_id=? AND source_sha=? AND tests_sha=? AND command_sha=?", key).fetchone()
        except sqlite3.Error as exc:
            raise MutationError(f"cannot read mutation result from cache: {exc}") from exc
        if row is None:
            return None
        return MutantResult(mutant_id=key[0], outcome=row[0], duration_s=row[1], detail=row[2])

    def get_probe(self, kind: str, key: tuple[str, str, str, str]) -> tuple[bool, str] | None:
        """A cached whole-suite verdict: the baseline run, or the import sentinel.

        Neither judges a mutant, and both cost a full suite execution per source file per
        round. Keyed by everything that could change the answer, so a round that changes
        nothing pays for neither.
        """
        try:
            row = self.con.execute(
                "SELECT ok, detail FROM probes WHERE kind=? AND source=? AND source_sha=? "
                "AND tests_sha=? AND command_sha=?", (kind, *key)).fetchone()
        except sqlite3.Error as exc:
            raise MutationError(f"cannot read mutation probe from cache: {exc}") from exc
        return None if row is None else (bool(row[0]), row[1])

    def put_probe(self, kind: str, key: tuple[str, str, str, str], ok: bool, detail: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO probes VALUES (?,?,?,?,?,?,?)",
                         (kind, *key, int(ok), detail))
        self.con.commit()

    def put(self, key: tuple[str, str, str, str], result: MutantResult) -> None:
        try:
            self.con.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?)",
                             (*key, result.outcome, result.duration_s, result.detail))
            self.con.commit()
        except sqlite3.Error as exc:
            raise MutationError(f"cannot write mutation result to cache: {exc}") from exc

    def close(self) -> None:
        self.con.close()


def run_tests(repo: Path, config: MutationConfig, tests: list[str], timeout_s: int,
              command: str | None = None) -> tuple[int, str]:
    """Run the configured test command over `tests`. Returns (exit code, tail of the output).

    ``command`` overrides which command runs, so the build gate goes through the same process
    handling -- timeout, environment, output tail -- as the suite it gates.
    """
    command = (command or config.test_command).replace(
        "{tests}", " ".join(shlex.quote(t) for t in tests))
    # Never let a run leave bytecode behind for the next one to import.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(command, cwd=repo, shell=True, capture_output=True,
                              text=True, timeout=timeout_s, check=False, env=env)
    except subprocess.TimeoutExpired:
        return (-1, "timed out")
    except OSError as exc:
        raise MutationError(f"cannot run test command {command!r}: {exc}") from exc
    return (proc.returncode, (proc.stdout + proc.stderr)[-400:])


def reject_failing_fixes(repo: Path, profile: Profile, sources: list[str]) -> dict[str, str]:
    """Run the tests paired with each source a fix claimed to resolve; report the red ones.

    A profile with a ``mutation:`` block states its remedy as a test, so ``fixed`` cannot be
    taken on the fix agent's word: a test that does not pass proves nothing about the symbol it
    names. Without this the failure surfaces a round later, as the mutation baseline refusing
    to run against a red suite -- by which point the findings are already recorded as fixed and
    the round that recorded them has been paid for.

    Keyed by source file, because that is the unit the pairing is defined over. A source whose
    pattern matches no test file is not checked: nothing was claimed about a test that does not
    exist.
    """
    if profile.mutation is None:
        return {}
    config = profile.mutation
    failed: dict[str, str] = {}
    for source in sorted(set(sources)):
        tests = paired_tests(repo, source, config.test_patterns)
        if not tests:
            continue
        code, detail = run_tests(repo, config, tests, config.baseline_timeout_s)
        if code != 0:
            failed[source] = detail
    return failed


def restore_interrupted(repo: Path, lock_path: Path) -> list[str]:
    """Put back the file an interrupted mutation run left swapped out.

    One file read in the common case, so every command can afford to call it. A killed run
    otherwise leaves a mutant sitting in the working tree with nothing to announce it.
    """
    if not lock_path.is_file():
        return []
    restored: list[str] = []
    try:
        target_rel = lock_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MutationError(f"{lock_path}: cannot read mutation lock file: {exc}") from exc
    if target_rel:
        target = (repo / target_rel).resolve()
        repo_resolved = repo.resolve()
        if target == repo_resolved or repo_resolved not in target.parents:
            raise MutationError(f"lock file names a path outside the repo: {target_rel!r}")
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if backup.is_file():
            try:
                shutil.move(str(backup), str(target))
            except OSError as exc:
                raise MutationError(f"{target_rel}: cannot restore mutant backup: {exc}") from exc
            invalidate_bytecode(target)
            restored.append(target_rel)
    lock_path.unlink(missing_ok=True)
    return restored


class MutationRunner:
    """Applies mutants in the working tree, one at a time, and always puts it back.

    Serial by construction: mutating a file in place is not parallel-safe within one working
    tree, and the cache is what makes repeat rounds cheap instead.
    """

    def __init__(self, repo: Path, config: MutationConfig, cache: MutationCache,
                 lock_path: Path | None = None) -> None:
        self.repo = repo
        self.config = config
        self.cache = cache
        self.lock_path = lock_path
        self.command_sha = hashlib.sha1(config.test_command.encode()).hexdigest()

    def restore_orphans(self) -> list[str]:
        """Sweep the whole tree for anything a killed process left mutated.

        Broader and slower than ``restore_interrupted``; run once at the start of a mutation
        run, where the cost is irrelevant next to the test executions that follow.
        """
        restored = []
        failures: list[str] = []
        for backup in sorted(self.repo.rglob(f"*{BACKUP_SUFFIX}")):
            target = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
            try:
                shutil.move(str(backup), str(target))
                invalidate_bytecode(target)
            except (OSError, MutationError) as exc:
                failures.append(f"{backup.relative_to(self.repo)}: {exc}")
                continue
            restored.append(str(target.relative_to(self.repo)))
        if self.lock_path is not None:
            self.lock_path.unlink(missing_ok=True)
        if failures:
            raise MutationError(
                "could not restore every orphaned mutant backup:\n  "
                + "\n  ".join(failures))
        return restored

    @contextmanager
    def _swapped(self, path: Path, original: bytes, replacement: bytes) -> Iterator[None]:
        """Hold a file's content swapped for the duration of one test run, and always undo it."""
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            if self.lock_path is not None:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.lock_path.write_text(str(path.relative_to(self.repo)), encoding="utf-8")
            try:
                backup.write_bytes(original)
                path.write_bytes(replacement)
                invalidate_bytecode(path)
                yield
            finally:
                path.write_bytes(original)
                invalidate_bytecode(path)
                backup.unlink(missing_ok=True)
        except OSError as exc:
            raise MutationError(f"{path}: cannot swap mutant into place: {exc}") from exc
        finally:
            if self.lock_path is not None:
                self.lock_path.unlink(missing_ok=True)

    def _run_tests(self, tests: list[str], timeout_s: int) -> tuple[int, str]:
        return run_tests(self.repo, self.config, tests, timeout_s)

    def _builds(self) -> tuple[bool, str]:
        """Whether the mutant currently in the tree compiles. ``(True, "")`` when unconfigured."""
        if not self.config.build_command:
            return True, ""
        code, output = run_tests(self.repo, self.config, [], self.config.baseline_timeout_s,
                                 command=self.config.build_command)
        return code == 0, output

    def shard_key(self, shard: MutationShard) -> tuple[str, str, str, str]:
        """Everything that could change any verdict about this shard."""
        try:
            source_bytes = (self.repo / shard.source).read_bytes()
        except OSError as exc:
            raise MutationError(f"{shard.source}: cannot read for mutation: {exc}") from exc
        return (shard.source,
                hashlib.sha1(source_bytes).hexdigest(),
                sha_of([self.repo / t for t in shard.tests]),
                self.command_sha)

    def exercised_by_tests(self, shard: MutationShard, key: tuple[str, str, str, str],
                           report: MutationReport) -> bool:
        """Whether the paired tests import the module at all.

        A sentinel, not a heuristic: the module is replaced by an import-time error and the
        tests are run. If they still pass, they never imported it, and every mutant that
        follows would "survive" for a reason that has nothing to do with test quality.
        """
        cached = self.cache.get_probe("sentinel", key)
        if cached is not None:
            report.probes_cached += 1
            return cached[0]
        path = self.repo / shard.source
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise MutationError(f"{shard.source}: cannot read for mutation: {exc}") from exc
        with self._swapped(path, original, _sentinel_for(shard.source)):
            code, _ = self._run_tests(shard.tests, self.config.timeout_s)
        self.cache.put_probe("sentinel", key, code != 0, "")
        return code != 0

    def baseline(self, shard: MutationShard, key: tuple[str, str, str, str],
                 report: MutationReport) -> None:
        """A red suite makes every verdict meaningless, so refuse to mutate against one."""
        if not shard.tests:
            return
        cached = self.cache.get_probe("baseline", key)
        if cached is not None:
            report.probes_cached += 1
            ok, output = cached
        else:
            code, output = self._run_tests(shard.tests, self.config.baseline_timeout_s)
            ok = code == 0
            self.cache.put_probe("baseline", key, ok, "" if ok else output)
        if not ok:
            raise MutationError(
                "baseline test run failed; mutation results would be meaningless against a red "
                f"suite. Fix the suite first.\n  tests: {' '.join(shard.tests)}"
                f"\n  output: {output.strip()}")

    def run_shard(self, shard: MutationShard, key: tuple[str, str, str, str],
                  report: MutationReport) -> Iterator[MutantResult]:
        path = self.repo / shard.source
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise MutationError(f"{shard.source}: cannot read for mutation: {exc}") from exc
        _, source_sha, tests_sha, _ = key
        survivors_by_anchor: set[str] = set()

        for mutant in shard.mutants:
            if self.config.stop_at_first_survivor and mutant.anchor in survivors_by_anchor:
                report.skipped += 1
                continue

            mutant_key = (mutant.id, source_sha, tests_sha, self.command_sha)
            result = self.cache.get(mutant_key)
            if result is not None:
                report.cached += 1
            elif not shard.tests:
                result = MutantResult(mutant_id=mutant.id, outcome="no_tests",
                                      detail=f"no test file matches {shard.source}")
                self.cache.put(mutant_key, result)
            else:
                result = self._judge(path, original, mutant, shard)
                self.cache.put(mutant_key, result)

            report.mutants += 1
            if result.outcome == "survived":
                report.survived += 1
            elif result.outcome == "no_tests":
                report.no_tests += 1
            elif result.outcome == "not_built":
                report.not_built += 1
            elif result.outcome == "error":
                report.errors += 1
            else:
                report.killed += 1
            if result.survived:
                survivors_by_anchor.add(mutant.anchor)
            yield result

    def _judge(self, path: Path, original: bytes, mutant: Mutant, shard: MutationShard) -> MutantResult:
        started = time.monotonic()
        try:
            with self._swapped(path, original, apply_mutant(original, mutant)):
                built, build_output = self._builds()
                if not built:
                    # Never "killed": the suite never ran. Recording a rejected mutant as caught
                    # would report the symbol as pinned by tests that never saw it.
                    return MutantResult(mutant_id=mutant.id, outcome="not_built",
                                        duration_s=time.monotonic() - started,
                                        detail=build_output.strip()[:200])
                code, output = self._run_tests(shard.tests, self.config.timeout_s)
        except OSError as exc:
            return MutantResult(mutant_id=mutant.id, outcome="error", detail=str(exc),
                                duration_s=time.monotonic() - started)

        elapsed = time.monotonic() - started
        if code == -1:
            return MutantResult(mutant_id=mutant.id, outcome="timeout", duration_s=elapsed,
                                detail="suite hung on the mutant, which counts as detecting it")
        if code == 0:
            return MutantResult(mutant_id=mutant.id, outcome="survived", duration_s=elapsed,
                                detail=f"suite passed with {mutant.display}")
        return MutantResult(mutant_id=mutant.id, outcome="killed", duration_s=elapsed,
                            detail=output.strip()[:200])


def run(repo: Path, profile: Profile, cache_path: Path,
        lock_path: Path | None = None) -> tuple[list[RawFinding], MutationReport]:
    """Run the whole subsystem and return findings, one per anchor whose behaviour nothing pins."""
    if profile.mutation is None:
        return [], MutationReport()
    config = profile.mutation
    report = MutationReport()
    started = time.monotonic()

    shards = build_mutation_shards(repo, profile)
    report.shards = len(shards)
    cache = MutationCache(cache_path)
    runner = MutationRunner(repo, config, cache, lock_path)

    by_mutant = {m.id: m for shard in shards for m in shard.mutants}
    survivors: dict[str, list[Mutant]] = {}
    untested: set[str] = set()
    with_tests = [s for s in shards if s.tests]
    unexercised: list[str] = []
    try:
        runner.restore_orphans()
        for shard in shards:
            key = runner.shard_key(shard)
            runner.baseline(shard, key, report)
            if shard.tests and not runner.exercised_by_tests(shard, key, report):
                unexercised.append(shard.source)
                continue
            for result in runner.run_shard(shard, key, report):
                # "no test file exists" and "the tests missed this mutation" are different work
                # items. Describing the first as the second hands the fix agent a false premise.
                if result.outcome == "no_tests":
                    untested.add(shard.source)
                elif result.outcome == "survived":
                    survivors.setdefault(by_mutant[result.mutant_id].anchor, []).append(
                        by_mutant[result.mutant_id])
    finally:
        cache.close()
    report.unexercised = unexercised

    # One unexercised file is a gap in that file's tests. Every file unexercised is a broken
    # harness -- the test command is not importing the tree under mutation at all -- and
    # reporting that as "nothing pins these symbols" would be confidently wrong.
    if with_tests and len(report.unexercised) == len(with_tests):
        raise MutationError(
            "the test command does not exercise the code under mutation: every file's paired "
            "tests still pass when the file is replaced by an import-time error.\n"
            f"  command: {config.test_command}\n"
            f"  files:   {', '.join(report.unexercised)}\n"
            "  Usually the suite is importing an installed copy of the package rather than "
            "this working tree. Fix the command before trusting any mutation result.")

    # One rejected mutant is a mutation this language will not express. Every mutant rejected is
    # a broken build command, and "no survivors" would then read as "the suite pins everything"
    # when nothing was ever run against it.
    if report.mutants and report.not_built == report.mutants:
        raise MutationError(
            "every mutant failed to build, so no mutant was ever judged by the suite.\n"
            f"  build command: {config.build_command}\n"
            "  Fix the build command before trusting any mutation result.")

    report.duration_s = round(time.monotonic() - started, 3)
    findings = [_finding(config, anchor, mutants) for anchor, mutants in sorted(survivors.items())]
    findings.extend(_unexercised_finding(config, shard) for shard in shards
                    if shard.source in report.unexercised)
    findings.extend(_untested_finding(config, shard) for shard in shards
                    if shard.source in untested)
    return findings, report


def _untested_finding(config: MutationConfig, shard: MutationShard) -> RawFinding:
    """One file, one work item: write the tests. Not one item per symbol nothing pins."""
    return RawFinding(
        rule_id=config.rule_id,
        anchor=shard.source,
        evidence=None,
        description=(f"No test file exists for this module, so no mutation of it could be "
                     f"judged at all. Expected one of: "
                     f"{', '.join(test_paths(shard.source, config.test_patterns))}."),
    )


def _unexercised_finding(config: MutationConfig, shard: MutationShard) -> RawFinding:
    return RawFinding(
        rule_id=config.rule_id,
        anchor=shard.source,
        evidence=None,
        description=(f"The paired tests ({', '.join(shard.tests)}) never import this module: the "
                     f"suite still passes when it is replaced by an import-time error."),
    )


def _finding(config: MutationConfig, anchor: str, mutants: list[Mutant]) -> RawFinding:
    shown = ", ".join(m.display for m in mutants[:3])
    more = f", and {len(mutants) - 3} more" if len(mutants) > 3 else ""
    return RawFinding(
        rule_id=config.rule_id,
        anchor=anchor,
        evidence=None,
        description=(f"The test suite passes with the behaviour of this symbol changed "
                     f"({shown}{more}), so nothing pins it."),
    )
