# coldsweep

Automates the fresh-context re-verification cycle for open-ended agent tasks.

An agent that has just finished a task judges its own completeness from a context that
already asserts the task is done, so same-session self-review reliably returns "no issues".
A fresh-context re-run of the *same* search finds the gaps. `coldsweep` runs that loop for you:

- the model **generates findings**
- code **compares finding sets** and decides termination

The model is never asked "are you done?".

## Install

```sh
uv sync
uv run coldsweep --help
```

Requires Python 3.12+, `git`, and the `claude` CLI on PATH for the agent phases.

## Quickstart

```sh
cd /path/to/target-repo
coldsweep init issues --task harden-io   # create a task from a profile template
$EDITOR .coldsweep/tasks/harden-io/profile.yaml   # the rule taxonomy IS the task statement
coldsweep shard      --task harden-io    # check what the scope actually resolves to
coldsweep run        --task harden-io    # scan -> ingest -> fix -> verify, until converged
coldsweep status     --task harden-io    # counts, unclassified bucket, pending disputes
coldsweep converged  --task harden-io; echo $?    # 0 or 1, prints nothing -- the gate
```

`coldsweep run` exits non-zero when it cannot converge, and says why.

It stops early when the only thing left is triage. A dispute or an off-taxonomy finding cannot
be cleared by scanning, so once nothing is open and the quiet window has closed, the loop halts
and points at `coldsweep adjudicate` rather than paying for rounds that cannot change the outcome.
Findings that genuinely keep reappearing still run out the `max_rounds` budget.

### Tasks

Work is organised into named tasks, each with its own taxonomy, finding set and round
history. Running a second task over the same repository is just another task:

```sh
coldsweep init docs --task document-api
coldsweep run       --task document-api
coldsweep task list
```

**There is no default task.** Every command that touches state requires `--task` (or
`COLDSWEEP_TASK` in the environment) and fails loudly without it. A task inherits nothing from any
other: a fresh task has zero completed rounds, so its gate starts shut and cannot be opened by
a previous task's history.

## Commands

| Command | Does |
|---|---|
| `coldsweep init <template> --task <t>` | Create a task from `issues`, `docs`, `tests`, `features`, or a YAML path |
| `coldsweep task list` | Every task, its round count and whether its gate is open |
| `coldsweep shard` | Print the resolved shard list |
| `coldsweep scan [--round N]` | Mechanical checks plus one full agent scan; writes `runs/<N>.json` |
| `coldsweep ingest runs/<N>.json` | Validate, merge, update `findings.jsonl` |
| `coldsweep fix [--rule R]` | Work open findings |
| `coldsweep verify` | Re-check fixed findings against their evidence |
| `coldsweep mutants` | Run the mutation subsystem alone and report unpinned symbols |
| `coldsweep spec freeze` | Record what every spec item says right now |
| `coldsweep spec status` | Every spec item, its freeze state, and what claims to implement it |
| `coldsweep status [--json]` | Counts by status and rule; unclassified and disputed |
| `coldsweep converged` | Exit 0/1, no output |
| `coldsweep adjudicate` | Triage disputed and unclassified findings |
| `coldsweep run` | The full loop until converged or `max_rounds` |

All of them except `task list` take `--task`/`-t`.

## State

Lives in the target repo, not here, and is scoped per task:

```
.coldsweep/
  .gitignore                     # ignores tasks/*/index.sqlite
  tasks/
    harden-io/
      profile.yaml               # committed -- the taxonomy is the task statement
      findings.jsonl             # committed -- source of truth, one finding per line, by id
      runs/<round>.json          # committed -- raw scan output
      runs/<round>.ingest.json   # committed -- merge audit record
      index.sqlite               # gitignored -- derived, rebuildable at any time
    document-api/
      ...
```

Retire a task by deleting its directory; nothing outside it refers to it.

## Concepts

**Finding** — one work item, identified by `(rule_id, anchor, evidence_sha)`. Identity is
derived, so the same finding re-derives the same id every round and merge is a dict lookup.
`description` is excluded by construction: agents phrase the same finding differently every
run.

**Anchor** — a stable symbol path (`pkg/mod.py::Class::method`), never a line number, so it
survives the edits the loop itself performs.

**Scope vs editable** — `scope` is what gets audited; `editable` is what a fix agent may write.
They are the same set by default, and must not be for a task whose remedies live elsewhere: a
rule about test quality is anchored in the source it fails to pin and fixed in a test file, so
the `tests` profile audits `src/**` and edits `tests/**` — the fix agent never touches the code
under test. Verification reads the union, so a fix in an editable-but-unaudited file can still
be proved. A separate `editable` set requires `fix_scope: task`; per-file fixing sends the agent
to the anchor's own file, which is the file it was told not to edit.

**Mode** — per rule, not per profile. `absence` verifies an offending snippet is gone;
`presence` verifies a required artifact exists *and is non-vacuous*.

**Convergence** — K consecutive rounds of *full coverage* producing zero new findings, with
nothing open, no untriaged dispute, and an empty unclassified bucket. Computed per task, over
that task's rounds only. Taxonomy membership is derived from the profile on every read, never
stored on a finding, so retiring a rule moves its findings straight back into the unclassified
bucket. A round ingested with `--force` after a shard failed is still a round, but it never
counts as quiet: half a scan reporting nothing new is not evidence that there is nothing new.

**Closure** — `verified` is proof, `lapsed` is silence. `coldsweep verify` reads the file the
anchor names and confirms the offending snippet is gone; that is `verified`. A finding that K
consecutive scans simply stopped re-deriving is `lapsed` — closed, but nothing inspected the
repository to close it. `coldsweep status` counts them separately, so a green run shows how
much of its green came from evidence.

Verification searches the anchor's file, not the whole repository. Repo-wide matching cannot
tell a fix from an unrelated file that happens to contain the same snippet, so one surviving
instance of a common idiom would reopen every finding under its rule. Code moved instead of
fixed is caught by the next round re-deriving it at its new anchor. An anchor outside scope, or
in a file that cannot be read, is deferred — never verified.

That path needs an offending snippet to look for, so it can only decide `absence` findings. A
rule a deterministic subsystem owns does not need one: the sweep that produced the finding is
exhaustive over scope, so re-running it *is* the check. `unimplemented-spec-item` is verified
that way — the marker set is re-derived, and an item nothing marks reopens instead of lapsing.

**Shard** — a deterministic subset of scope handed to one agent invocation, resolved through
`git ls-files`. One file per shard by default; enumeration exhaustiveness degrades sharply
above about five.

## What converges and what doesn't

The gate closes when K consecutive passes produce nothing new. Whether that ever happens
depends on whether the rule has a decidable answer, and the difference is large enough to
change how you use each profile.

Measured on this repository: five independent passes with the `issues` profile over four dense
modules, with **no code changed between passes**.

```
re-derived in N of 5 passes        new findings per round
  5/5   6  (11%)                     25 -> 8 -> 6 -> 9 -> 6
  4/5   6                            plateaus; never reaches zero
  3/5   6
  2/5  12                          model families (sonnet x3, opus x2)
  1/5  24  (44%)                     found by both: 43%
```

The run did not converge, and would not have: every fresh pass still contributed six to nine
previously unseen findings. Nothing is wrong with the single-pass findings — several were
reproduced as real bugs — they sit below the **enumeration floor**, where each pass samples
from a large space of defensible observations. Repetition cannot separate "real but rarely
spotted" from "marginal judgement call", so more rounds buy coverage, not certainty.

A rule a subsystem decides behaves completely differently. `untested-behaviour` (mutation) and
`unimplemented-spec-item` (spec traceability) are exhaustive over their scope and return the
same set every pass, so their quiet window closes on the first repeat.

| Rule kind | Examples | Across passes | What the loop gives you |
|---|---|---|---|
| `decided_by: code` | `untested-behaviour`, `unimplemented-spec-item`, `stale-spec-reference` | identical every pass | convergence is meaningful; the gate is a real answer |
| `decided_by: agent` | `missing-error-handling`, `vacuous-test`, `vacuous-implementation` | plateaus | `max_rounds` is a budget; the gate stays shut |

In practice:

- `issues` and `docs` are **budget-bounded** — neither has a deterministic decider. They do not
  behave alike, though: measured end to end, `issues` plateaus (31, 22, 17, 15 new per round)
  while `docs` decays (8, 6, 2, 1). Set `max_rounds` to what you are willing to spend, run it,
  and read `coldsweep status`. A non-zero exit is the expected ending, not a failure.
- `tests` and `features` converge on their deterministic half. The agent rules alongside them
  are still budget-bounded, so the gate reflects the decidable part.
- `scan_alt` earns its cost: 57% of findings came from only one model family. Same-family
  agents really do share blind spots.
- Whenever a rule becomes expressible deterministically, move it to a subsystem and mark it
  `decided_by: code`. That is the only way a rule joins the convergent half.

## Profile

```yaml
version: 1
scope:
  include: ["src/**/*.py"]
  exclude: ["**/migrations/**"]
editable:                 # optional; defaults to scope. Requires fix_scope: task
  include: ["tests/**/*.py"]
files_per_shard: 1
convergence:
  k: 2
  max_rounds: 8
models:
  scan: sonnet
  fix: sonnet
  scan_alt: opus          # optional; used on even rounds
agent:
  parallelism: 4
  retries: 2
rules:
  - id: missing-error-handling
    mode: absence
    description: "..."    # injected into the scan prompt
mechanical: []
```

Profiles are per-repo by design. A taxonomy generic enough to span repos merges badly in all
of them, so there is no shared cross-project rule package.

### Mutation testing

For `presence` rules about tests, existence is a weak signal and coverage is trivially
reward-hackable — a test that calls a function and asserts nothing scores the same as one that
pins its behaviour. The honest predicate is: change the source, and see whether the suite
notices.

```yaml
mutation:
  rule_id: untested-behaviour
  test_command: "python -m pytest -q -x {tests}"
  test_patterns: ["tests/test_{stem}.py"]
  operators: ["comparison", "arithmetic", "boolean", "constant", "return"]
  stop_at_first_survivor: true
```

```sh
coldsweep mutants --task pin-behaviour
```

```
7 mutant(s) over 1 file(s) in 1.4s
  killed 4   survived 3   no tests 0   errors 0

nothing pins these 1 symbol(s)
  src/pricing.py::discount
    The test suite passes with the behaviour of this symbol changed
    ('100' -> '101', 'and' -> 'or', 'total' -> 'None'), so nothing pins it.
```

That file had one test and full statement coverage of `discount`.

Three pieces, all in `src/coldsweep/mutation.py`:

- **Runtime** — applies one mutant in the working tree, runs the paired tests, and always
  restores. Two things are refused rather than measured: a red baseline, and a suite that never
  imports the code. The second is checked with a sentinel — the module is replaced by an
  import-time error, and if the tests still pass they demonstrably never reached it, so every
  "survivor" that followed would be meaningless. One file failing that check is a finding
  against that file; every file failing it is a broken test command, and the run stops and says
  so. Serial by construction, since mutating a file in place is not parallel-safe.
- **Cache** — keyed by mutant identity plus the source, tests and command it was judged
  against, so nothing is re-run until something that could change the verdict changes.
  Derived and gitignored; delete it to rebuild. Alongside it, a lock file names whichever file
  is currently swapped out, so a run killed mid-mutation is detected and undone by the next
  command you type — not left sitting in your working tree.
- **Shard strategy** — one source file paired with the tests responsible for it. The pairing
  is the unit of work, the unit of cache invalidation, and the scan shard: rules like
  `vacuous-test` ask about test files, so a profile with a `mutation:` block hands the agent
  each source file together with its tests.

Findings are emitted per **symbol**, not per mutant: "nothing pins the behaviour of this
function" is one work item however many mutations demonstrate it. Because identity is
`(rule_id, anchor, None)`, a mutation finding and an agent finding about the same symbol merge
on exact identity rather than on a similarity guess.

Mutants are generated from the AST with stable ids derived from file, symbol, operator and
occurrence index — never from line numbers, so they survive code moving. Docstrings and
annotations are never mutated.

### Feature specs

A features task states its work as a spec document instead of a rule taxonomy. That brings two
problems the other profiles do not have.

**The target moves.** Convergence means nothing if the spec can grow while the loop runs, so
the spec is frozen before work begins and any later edit is a detected event:

```sh
coldsweep spec freeze --task ship-retries     # froze 3 spec item(s) from SPEC.md
coldsweep spec status --task ship-retries
```

```
SPEC.md: 3 item(s), frozen at round 1
  FR-1     traced         Bounded retries
           -> src/retry.py::call_with_retries
  FR-2     unimplemented  Exponential backoff
  FR-3     unimplemented  Retry budget
```

Until the spec is frozen, `coldsweep scan` refuses to run and the gate stays shut. Reword a frozen
item and both shut again until you re-freeze deliberately. Reflowing a paragraph is not a
change; changing what it says is.

**Traceability is reward-hackable.** A marker comment is cheap to write and proves nothing, so
markers only *address* an item:

```python
def call_with_retries(fn):
    # spec: FR-1
    ...
```

An item with no marker anywhere in scope is an `unimplemented-spec-item` finding, decided by
code and exhaustive. Whether a marked implementation actually delivers what the item says is a
`presence` judgement an agent makes against the frozen item text. A marker naming an item that
no longer exists is a `stale-spec-reference`, also decided by code.

Item ids are written in the document rather than derived from heading text, so retitling an
item does not silently re-identify it:

```markdown
### FR-1 Bounded retries

A failed call is retried at most three times before the error is raised to the caller.
```

**Standing limit: the loop never validates the spec itself.** An incomplete spec converges
cleanly. `coldsweep spec status` says so every time it runs.

### Who decides a rule

Every rule declares its owner:

```yaml
rules:
  - id: untested-behaviour
    mode: presence
    decided_by: code      # a deterministic subsystem is exhaustive over this
  - id: vacuous-test
    mode: absence         # decided_by: agent is the default
```

Rules marked `decided_by: code` never reach the scan prompt. Handing an agent a rule that a
subsystem already answers exhaustively invites it to report the same item under a different
anchor — a duplicate at best, a contradiction of the exhaustive answer at worst. Agents handle
only the tail. A profile whose rules are all `decided_by: code` spawns no scan agents at all.

### Mechanical checks

Deterministic checks run before each round, exhaustive over their rule:

```yaml
mechanical:
  - rule_id: deprecated-api
    command: "ast-grep scan --json {files}"
```

Output is a JSON list of `{anchor, evidence?, description?}` (or `{"findings": [...]}`), and
is attributed to the configured `rule_id`. Whenever a rule becomes expressible as an AST
pattern, move it here — agents should only handle the tail.

## Stop hook

Optional, for interactive sessions. A thin wrapper over `coldsweep converged` with no logic of
its own:

```json
{"hooks": {"Stop": [{"hooks": [
    {"type": "command", "command": "coldsweep-stop-hook --task harden-io"}]}]}}
```

It names its task like every other entry point; a hook that guessed would be the same stale
state the tool exists to remove.

## Invariants

1. Completion is never decided by a model.
2. Scan agents never see prior findings, prior rounds, or diffs.
3. `description` is never used for identity.
4. No file-level completion state exists — status lives on findings only.
5. Merge errs toward duplicates, never toward loss.
6. Every round is a full independent re-derivation.
7. `findings.jsonl` is the source of truth; SQLite is rebuildable.
8. No command acts on a task it was not given; no task inherits another's rounds or findings.

## Known limits

- Convergence across passes is not proof of completeness. A systematically invisible issue
  class stays invisible at any K. Mitigated, not solved, by `scan_alt`.
- Open-ended agent rules do not converge at all: measured at 44% of findings appearing in
  exactly one pass of five, with the new-finding rate plateauing rather than decaying. Raising
  K slows closure without improving the signal. See
  [What converges and what doesn't](#what-converges-and-what-doesnt).
- In `absence` mode a *partial* fix changes the evidence and therefore the id, so it surfaces
  as a new finding rather than a still-open one. Errs toward extra work, not silent loss.
- A finding whose anchor names a file the profile neither audits nor may edit can never be
  verified, only lapsed. Widen `editable` rather than accepting silence as proof.
- Presence mode verifies non-vacuity by model judgement, except where a `mutation:` block
  gives it a deterministic predicate.
- The mutation runtime is serial within a working tree. The cache, not parallelism, is what
  makes repeat rounds cheap: mutant verdicts, the baseline run and the import sentinel are all
  keyed by the source, tests and command they were judged against, so an unchanged round runs
  no test suite at all.
- `stop_at_first_survivor` bounds cost by stopping once a symbol is known to be unpinned, so
  the reported survivor list is a sample rather than the full set. Turn it off for a complete
  enumeration.
- A features task inherits its spec's incompleteness silently: the loop checks that every
  frozen item is implemented, never that the set of items is complete.
- Cost scales as rounds x shards.

## Measurements

[docs/measurements.md](docs/measurements.md) records one real end-to-end `coldsweep run` per
profile type against this repository — convergence curves, cost per round and per shard, where
each profile's gate stuck, and everything needed to repeat the run.

## Tests

The core is fully testable with zero LLM calls; the end-to-end suite drives the real CLI
against a scratch repo using a deterministic stub agent.

```sh
uv run pytest
uv run ruff check .
uv run pylint src/coldsweep
```
