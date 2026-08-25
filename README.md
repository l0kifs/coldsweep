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

Requires Python 3.12+, `git`, and the `claude` CLI on PATH for the agent phases. For a
repository that is not Python, add the grammars — see [Languages](#languages):

```sh
uv add "coldsweep[languages]"
```

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

That early stop needs *nothing else* to be open, so on a task that keeps finding work the
dispute backlog grows underneath it — measured on this repository, 0, 12, 18 then 24 across four
rounds, every one of them holding the gate shut. The count is now reported after each round, and
`convergence.max_disputes` stops the run once the backlog reaches a size you would rather triage
than keep paying past. It is unset by default: the right bound is a judgement about the
repository, not a number this tool can pick.

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
| `coldsweep verify` | Re-check fixed findings against their evidence; re-runs a `mutation:` sweep when one is configured |
| `coldsweep mutants` | Run the mutation subsystem alone and report unpinned symbols |
| `coldsweep spec freeze` | Record what every spec item says right now |
| `coldsweep spec status` | Every spec item, its freeze state, and what claims to implement it |
| `coldsweep status [--json]` | Counts by status and rule; unclassified and disputed; what the task has spent |
| `coldsweep converged [--half H]` | Exit 0/1, no output. `--half decidable` gates on the rules a subsystem decides |
| `coldsweep languages` | Which languages resolve to symbols here, and which need a grammar |
| `coldsweep adjudicate` | Triage disputed and unclassified findings; settles subsystem-decided disputes against re-derived evidence first |
| `coldsweep run` | The full loop until converged or `max_rounds` |

All of them except `task list` and `languages` take `--task`/`-t`.

## State

Lives in the target repo, not here, and is scoped per task:

```
.coldsweep/
  .gitignore                     # ignores tasks/*/index.sqlite
  tasks/
    harden-io/
      profile.yaml               # committed -- the taxonomy is the task statement
      findings.jsonl             # committed -- source of truth, one finding per line, by id
      spend.jsonl                # committed -- one line per agent subprocess, and what it cost
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
A fix agent is handed only the slice of that set its own work item needs — the profile's
source-to-test pairing says where a fix belongs — and groups whose slices share a file run in
sequence. Two agents that both read a file, decide, and write it back whole would leave only
the later write, while both reported success.
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

**Disputes** — a dispute under a rule a subsystem owns is settled against that subsystem before
anyone is asked. An anchor it no longer reports is `verified`: the objection was right and the
work is done. An anchor it still reports stays disputed and still pending, annotated with the
re-derivation, because "three fix attempts failed" is a question about effort, not fact — and
the gate must not open over a symbol nothing pins. Only agent-decided disputes, and that
remainder, reach a person. Measured on a Python `tests` task: of 15 disputes, 2 settled without
asking, 12 were confirmed as real work, and all 6 the oscillation guard raised were among the 12.

A finding with no snippet is closed by re-running whatever decided it: a spec item against the
marker set, an `untested-behaviour` finding against the mutation survivor set. Both are
exhaustive over their scope, so an anchor missing from the re-derived set is proof rather than
silence — and this is the only reason a `tests` task closes anything on evidence at all.
Measured on a two-file C# project, the fix turned 25% evidence-backed closure into 100%. It
costs no extra work inside `coldsweep run`: the sweep runs against the post-fix tree, which is
the tree the next round's scan measures, so that scan comes back from the cache.

Verification searches the anchored symbol, falling back to its file when the anchor names no
symbol. Searching wider cannot tell a fix from an unrelated copy of the same snippet, and a
common idiom appearing twice in one module would otherwise let the untouched copy reopen a
finding about the fixed one — measured on this repository, 7.4% of snippets recur inside their
own file. Code moved instead of fixed is caught by the next round re-deriving it at its new
anchor. An anchor outside scope, or in a file that cannot be read, is deferred — never verified.

A surviving snippet is not proof that a fix failed. Whole classes of remedy are **additive** —
handling wrapped around a call, validation added after a read — and leave the cited line exactly
where it was; in the `issues` taxonomy those rules were 69 of 85 findings. So a snippet that is
still present reopens the finding only when the symbol around it is unchanged too. If the symbol
changed, the two cases are indistinguishable from the text and the finding is deferred to the
next round's fresh derivation. That removes a false failure without inventing a proof: an
additive fix closes by lapsing, not by evidence.

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

Because the two halves behave differently, they are gated separately. On a mixed profile the
global gate can never open -- every measured run ended with the agent half still producing
findings -- so a single verdict lets the plateau hide an answer the deterministic half already
reached:

```
coldsweep converged --task features                  # 1: the agent rules are still going
coldsweep converged --task features --half decidable # 0: every frozen spec item is implemented
```

`coldsweep status` prints both halves whenever a profile has rules in each. A profile whose
rules are all agent-decided has nothing to split and prints nothing extra. Naming a half that no
rule falls into exits 2, not 0: there is no answer there to report.

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
  max_disputes: null      # optional; stop once this many disputes await triage
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
  build_command: null            # compiled languages: gate each mutant on the build first
  stop_at_first_survivor: true
```

```sh
coldsweep mutants --task pin-behaviour
```

```
7 mutant(s) over 1 file(s) in 1.4s
  killed 4   survived 3   no tests 0   not built 0   errors 0

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

The flag is also what the split gate reads: `--half decidable` is exactly the `decided_by: code`
rules, `--half budgeted` exactly the rest.

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

## Cost

Every agent subprocess is billed to the task's ledger, `spend.jsonl` — one line per *attempt*,
because a retry is another subprocess and another bill, and a phase that exhausts its retries is
the most expensive outcome there is. Scan, fix and adjudicate all go through it; there is no
unbilled path.

`coldsweep run` prints each round's cost as the round ends, which is the moment buying another
one is a choice:

```
round 3: ingested 19 -> new 17, exact 1, fuzzy 1, adjudicated 0 of 12 call(s), reopened 0, stale-closed 1
round 3: cost $9.82 across 23 call(s) -- fix $3.50 (8)  scan $6.31 (15)
```

Adjudication is counted by calls made, not merges produced. A pair the adjudicator rules
*different* merges nothing and moves no counter, but it was still an agent subprocess — a round
that spent 85 of them used to report `adjudicated 0`.

```
$ coldsweep status --task harden-io
spend over 102 agent call(s)
  cost       $44.99
  tokens     26,878,672  (in 1,151  out 742,423  cache write 4,535,822  read 21,599,276)
  by phase   adjudicate $1.03 (6)  fix $16.19 (35)  scan $27.77 (61)
  by round   1: $12.11  2: $11.78  3: $9.82  4: $11.29
```

Figures come from the agent command's own result envelope, never from a price table here, so
they cannot drift as rates change. An agent command that emits no envelope — a stub, a wrapper —
records `null`, not zero, and `status` says the totals are a lower bound rather than reporting a
run as free. On a subscription account the envelope's USD is API-equivalent, not billed.

Cache traffic is reported separately because it is where the money goes: a fresh subprocess per
shard re-writes the prompt prefix into the cache every time, which is what makes the per-call
floor roughly $0.08 before an agent reads anything, charged per shard per round.

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
5. Merge errs toward duplicates, never toward loss — and the similarity fallback is skipped
   entirely for `decided_by: code` rules, whose anchors are machine-derived and whose
   descriptions are templates, so scoring them could only lose real items.
6. Every round is a full independent re-derivation.
7. `findings.jsonl` is the source of truth; SQLite is rebuildable.
8. No command acts on a task it was not given; no task inherits another's rounds or findings.

## Languages

Scope resolution, identity, merge and convergence are language-neutral: shards come from
`git ls-files`, and evidence hashing normalizes `#`, `//` and `/* */` comments. Two things need
a parser -- locating the symbol an anchor names, so `verify` can search inside it rather than
across the whole file, and generating mutants.

| Language | Extensions | Needs |
|---|---|---|
| Python | `.py` | nothing; the stdlib `ast` path |
| C# | `.cs` | `coldsweep[languages]` |
| TypeScript / JavaScript | `.ts .tsx .js .jsx .mts .cts .mjs .cjs` | `coldsweep[languages]` |
| Go | `.go` | `coldsweep[languages]` |
| Rust | `.rs` | `coldsweep[languages]` |
| Java | `.java` | `coldsweep[languages]` |
| anything else | — | a table entry in `syntax.py` plus a test |

All of them resolve symbols and generate mutants.

```
uv add "coldsweep[languages]"
coldsweep languages          # what resolves here
```

Grammars ship as one wheel per language, each carrying its own compiled parser. Deliberately
not `tree-sitter-language-pack`: it downloads grammars into a user cache on first use, and a
tool whose claim is that its decisions are deterministic cannot resolve symbols differently
depending on whether the machine had network access.

A language with no support is never an error. `verify` defers instead of deciding, and mutation
skips the file -- so the only symptom is work quietly not happening, which is what
`coldsweep languages` exists to show. Adding one is a table entry in `syntax.py` and a test
against a real sample; do not add one from a grammar's documentation, because node type names
differ between grammars in ways only parsing reveals and a wrong entry returns nothing, which
reads exactly like a clean file.

**Anchors are qualified enough to stay unique.** A Go method takes its receiver
(`Repo::Count` and `Store::Count`, not two `Count`s), a Rust method takes its `impl` type, and a
C# namespace or a Rust `mod` contributes nothing — an agent writes `Repo::Count`, never
`App.Core::Repo::Count`. This is not cosmetic: two symbols on one anchor derive one id, and
merge absorbs the second finding without a trace.

**Mutation in a statically typed language is narrower on purpose.** Only type-preserving
mutations are generated -- operator and literal swaps, never the classic `return null`. A mutant
that does not compile exits non-zero exactly like a failing test, so it would be recorded as
*killed* and the symbol reported as pinned by tests that never ran against it. Set
`build_command` to gate on the compiler as well: a mutant that fails to build is recorded
`not_built` and counts as evidence about neither the suite nor the symbol.

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
- `coldsweep verify` runs the test suite when the profile has a `mutation:` block, because that
  is what deciding those findings costs. Standalone, that is a slow command; inside
  `coldsweep run` it is paid for by the next round's cache hit.
- Presence mode verifies non-vacuity by model judgement, except where a `mutation:` block
  gives it a deterministic predicate.
- The mutation runtime is serial within a working tree. The cache, not parallelism, is what
  makes repeat rounds cheap: mutant verdicts, the baseline run and the import sentinel are all
  keyed by the source, tests and command they were judged against, so an unchanged round runs
  no test suite at all.
- `stop_at_first_survivor` bounds cost by stopping once a symbol is known to be unpinned, so
  the reported survivor list is a sample rather than the full set. Turn it off for a complete
  enumeration.
- Outside Python, mutation is restricted to type-preserving operators, so a symbol pinned only
  against a type change is not caught. `build_command` costs one build per mutant and is the
  only honest way to run the wider set — without it a rejected mutant is indistinguishable from
  a caught one.
- `+` is only withheld where an operand is a string *literal*. String concatenation over
  variables — common in Java and C# — still yields a `-` mutant that does not typecheck, so set
  `build_command` on those or read `killed` as an upper bound.
- The import sentinel proves a different thing per language: for Python, that the tests never
  imported the module; for a compiled language, that the file was never built. Both mean the
  same for the verdict, but the second is the weaker claim.
- A language with no grammar installed is silent, not loud: `verify` defers and mutation skips
  the file. Run `coldsweep languages` before trusting a non-Python task.
- A features task inherits its spec's incompleteness silently: the loop checks that every
  frozen item is implemented, never that the set of items is complete.
- Cost scales as rounds x shards, and a round that finds nothing still pays for a full scan.
  `coldsweep status` reports what a task has actually spent; see
  [docs/measurements.md](docs/measurements.md) for measured figures.

## Measurements

[docs/measurements.md](docs/measurements.md) records one real end-to-end `coldsweep run` per
profile type against this repository — convergence curves, cost per round and per shard, where
each profile's gate stuck, and everything needed to repeat the run. Run 3 adds the language
port measured on C# and Rust, and the Python `tests` profile the earlier runs skipped: 86%
evidence-backed closure against run 1's 0%, and two more defects, one of them a silent deletion
of work items in `merge`.

## Tests

The core is fully testable with zero LLM calls; the end-to-end suite drives the real CLI
against a scratch repo using a deterministic stub agent.

```sh
uv run pytest
uv run ruff check .
uv run pylint src/coldsweep
```
