# coldsweep — implementation spec

Tool that automates the fresh-context re-verification cycle for open-ended agent tasks.

## 1. Problem

An agent that completes a task judges its own completeness from a context that already
asserts the task is done. Same-session self-review reliably returns "no issues". A fresh
context re-run of the *same search* finds the gaps. Today this loop is run manually 3–4
times per task.

`coldsweep` makes the loop condition a computation instead of a model judgement:

- the model **generates findings**
- code **compares finding sets** and decides termination

The model is never asked "are you done?".

## 2. Core concepts

| Term | Meaning |
|---|---|
| **profile** | Per-repo config: rule taxonomy, scope, mode, mechanical rules |
| **rule** | One closed-vocabulary work-item type, e.g. `missing-error-handling` |
| **finding** | One work item: rule + anchor + evidence. Unit of state |
| **shard** | Deterministic subset of scope handed to one agent invocation |
| **round** | One full independent scan of all shards |
| **convergence** | K consecutive rounds producing zero new findings, with no `open` findings |

### Modes

| Mode | Verifies | Used by |
|---|---|---|
| `absence` | offending snippet is gone | issues, refactors, migrations |
| `presence` | required artifact exists **and is non-vacuous** | docs, tests |

Mode is per-rule, not per-profile — a profile may mix both.

## 3. Stack

- Python 3.12+, `uv`, Ruff, Pylint
- Pydantic v2 — all models and all agent I/O
- Typer — CLI
- pytest — core must be fully testable with **zero** LLM calls
- `rapidfuzz` — similarity fallback in merge
- SQLite (stdlib) — derived index only, never source of truth

No web framework. No async beyond `asyncio` for the shard runner.

## 4. Layout

```
coldsweep/
  models.py        # Finding, Profile, Rule, ScanResult, RunRecord
  store.py         # load/save findings.jsonl, id assignment
  merge.py         # identity, matching, merge decisions   <- highest-risk module
  converge.py      # round bookkeeping, termination check
  syntax.py        # extension -> symbol ranges and mutation sites
  shard.py         # scope -> deterministic shard list
  mechanical.py    # run profile-defined deterministic checks
  runner.py        # subprocess agent invocation, schema enforcement
  prompts/
    scan.md
    fix.md
    adjudicate.md
  cli.py
tests/
  fixtures/        # hand-written findings sets, no agent involved
```

Per-repo state lives in the target repo:

```
.coldsweep/
  profile.yaml         # committed
  findings.jsonl       # committed — source of truth
  runs/<round>.json    # committed — raw scan output per round
  index.sqlite         # gitignored — derived, rebuildable
```

`findings.jsonl`: one JSON object per line, sorted by `id`. Line-per-finding keeps diffs
and blame readable and makes appends conflict-free.

## 5. Data model

```python
class Finding(BaseModel):
    id: str                      # derived, deterministic — see 5.1
    rule_id: str                 # MUST be in profile taxonomy
    anchor: str                  # symbol path, never line numbers
    evidence_sha: str | None     # absence mode only
    description: str             # display only — NEVER used for matching
    shard: str
    status: Literal["open", "fixed", "verified", "wontfix", "disputed"]
    first_seen_round: int
    last_seen_round: int
    history: list[Event]
```

### 5.1 Identity

```
identity = (rule_id, anchor, evidence_sha)
id       = f"{rule_id}-{sha1(anchor + (evidence_sha or '')).hexdigest()[:8]}"
```

Identity is derived, so an identical finding re-derives an identical id and merge becomes
a dict lookup. `description` is excluded by construction — agents phrase the same finding
differently every run.

**Anchors** must be stable under the edits the loop itself performs:

- code: `path/to/module.py::Class::method`
- docs: `path/to/doc.md::normalized-heading-path`
- never line numbers

**`evidence_sha`**: sha1 of the offending snippet after normalizing whitespace, comments,
and string quoting. Doubles as fix verification — if the hash is still present in the repo,
the finding is not fixed regardless of what the fix agent reported.

Known limitation: in `absence` mode a *partial* fix changes the evidence and therefore the
id, so it surfaces as a new finding rather than a still-open one. Accepted — it errs toward
extra work, not silent loss.

## 6. Merge (`merge.py`)

Per ingested scan result, in order:

1. **Taxonomy gate.** `rule_id` not in profile → route to `unclassified`, exclude from
   convergence, surface in `status`. Never invent rules.
2. **Exact identity match** → update `last_seen_round`. Not a new finding.
3. **Similarity fallback**, only within the same `rule_id` and same file:
   - `rapidfuzz` ratio over `(anchor, description)` ≥ 0.92 → auto-merge
   - 0.75–0.92 → single LLM adjudication call per candidate pair, `same/different` only
   - below 0.75 → new finding
4. **Otherwise** → new finding, `status=open`, `first_seen_round=current`.

Rules:

- **Bias toward NOT merging.** A duplicate costs one cheap re-check. A false merge silently
  deletes a real work item — the failure that sends the user back to manual verification.
- Every merge decision is appended to the finding's `history` with method and score.
- The adjudication call answers "same underlying item, yes/no" — a bounded factual
  comparison. It is never asked about completeness.

## 7. Convergence (`converge.py`)

```
converged  ==  no findings in {open, disputed_pending}
           and last K rounds each produced 0 new findings
           and unclassified bucket is empty
```

- `K` from profile, default 2
- `max_rounds` from profile, default 8 — hard stop, exit non-zero, report state
- `wontfix` and adjudicated `disputed` are excluded from the check

`coldsweep converged` exits 0/1 and prints nothing else — it is the gate other tooling shells
out to.

**Oscillation guard.** If a finding transitions `fixed -> open` more than twice, force it to
`disputed` and stop re-fixing. Without this, style-preference findings loop forever.

## 8. Sharding (`shard.py`)

Scope from profile globs, resolved through `git ls-files` — never a filesystem walk, so
ignored files stay out. Shards are deterministic and content-independent so rounds are
comparable.

- default: 1 shard = 1 file
- profile may set `files_per_shard` (keep ≤ 5; enumeration exhaustiveness degrades sharply
  with scope)
- every shard must return a result; a missing shard fails the round rather than silently
  reducing coverage

There is **no file-level "checked" state anywhere in the schema.** Status lives on findings
only. A file-level flag lets an agent mark a file done after finding one of three issues,
making the rest permanently invisible.

## 9. Runner (`runner.py`)

One subprocess per shard:

```
claude -p --output-format json  < rendered scan prompt
```

- fresh context per shard by construction
- read-only tool set for scan; write access only for fix
- output parsed into `ScanResult`; schema violation → retry up to 2×, then fail the shard
- parallelism via `asyncio` semaphore, default 4
- model configurable per phase; at least one scan round per task **should** use a different
  model than the fixer — same-family agents share blind spots

The scan agent receives: rule taxonomy, shard files, mode. It does **not** receive
`findings.jsonl`, the previous round's output, or any diff. That isolation is the whole
mechanism — do not "help" it with prior context.

## 10. Mechanical prefilter (`mechanical.py`)

Profile may define deterministic checks run before each round:

```yaml
mechanical:
  - rule_id: missing-docstring
    command: "python -m tools.check_docstrings --json {files}"
  - rule_id: deprecated-api
    command: "ast-grep scan --json {files}"
```

Output is mapped to `Finding` with `source=mechanical`. These are exhaustive over their
rule, so their coverage is real rather than claimed. Agents handle only the tail. Whenever a
rule becomes expressible as an AST pattern, move it here.

## 11. Presence mode

For `presence` rules the artifact's existence is a weak signal — it may be vacuous
(docstrings restating the signature, tests asserting nothing). Requirements:

- scan prompt must check **quality, not existence**
- scan prompt must report **missing or wrong**, never "could be better" — this is the
  primary oscillation source
- for test rules, coverage is trivially reward-hackable; mutation testing (`mutmut`) is the
  honest predicate. Deferred to M3.

## 12. CLI

```
coldsweep init <profile-template>   scaffold .coldsweep/ in cwd
coldsweep shard                     print resolved shard list
coldsweep scan [--round N]          mechanical + agent scan, writes runs/<N>.json
coldsweep ingest runs/<N>.json      validate, merge, update findings.jsonl
coldsweep fix [--rule R]            work open findings
coldsweep verify                    re-check fixed findings against evidence_sha
coldsweep status                    counts by status/rule; lists unclassified + disputed
coldsweep converged [--half H]      exit 0/1 — the gate; H is decidable|budgeted
coldsweep languages                 which languages resolve to symbols here
coldsweep adjudicate                interactive triage of disputed + unclassified
coldsweep run                       full loop until converged or max_rounds
```

`coldsweep run` = `while ! converged && round < max_rounds: scan; ingest; fix; verify; round++`

## 13. Profile (per-repo)

```yaml
version: 1
scope:
  include: ["src/**/*.py"]
  exclude: ["**/migrations/**"]
files_per_shard: 1
convergence:
  k: 2
  max_rounds: 8
models:
  scan: <model>
  fix: <model>
  scan_alt: <different-family model>   # optional, used every other round
rules:
  - id: missing-error-handling
    mode: absence
    description: "..."      # injected into scan prompt
  - id: missing-docstring
    mode: presence
    description: "..."
mechanical: []
```

Profiles are per-repo by design. Do not build a shared cross-project taxonomy package —
a vocabulary generic enough to span repos merges badly in all of them.

## 14. Build order

**M0 — core, no LLM.** models, store, merge, converge, shard. Tests use hand-written
fixture finding sets: differently-phrased duplicates must merge; genuinely distinct findings
at the same anchor must not; the loop must terminate. Do not proceed until this is solid —
everything downstream inherits its correctness.

**M1 — issues profile.** runner + scan prompt + fix prompt. `absence` mode only. First
end-to-end `coldsweep run`.

**M2 — docs profile.** Exercises `presence` semantics and the vacuity check. This is the
decision point: if the identity/merge layer holds under presence mode, continue; if it
breaks, fix it here rather than letting M3/M4 inherit the breakage.

**M3 — tests profile.** Adds the mutation-testing subsystem (runtime, caching, own shard
strategy). A component, not a config file.

**M4 — features profile.** Adds spec authoring + freeze workflow. Convergence checks
spec-item ↔ implementation traceability. Note the standing limit: **the loop never validates
the spec itself** — an incomplete spec converges cleanly.

Write M1 and M2 as two concrete implementations and let the duplication be visible. Extract
the profile abstraction only after both exist. An abstraction designed for four hypothetical
profiles will fit none of them.

## 15. Optional: Stop hook

Thin wrapper shelling out to `coldsweep converged`, for interactive VS Code sessions. Must
contain no logic of its own. Check `stop_hook_active` and exit 0 when set, or the gate jams
shut permanently.

## 16. Invariants

1. Completion is never decided by a model.
2. Scan agents never see prior findings, prior rounds, or diffs.
3. `description` is never used for identity.
4. No file-level completion state exists.
5. Merge errs toward duplicates, never toward loss.
6. Every round is a full independent re-derivation, not an incremental update.
7. `findings.jsonl` is the source of truth; SQLite is rebuildable at any time.

## 17. Known limits — document, don't fix

- Convergence across passes is not proof of completeness. A systematically invisible issue
  class stays invisible at any K. Mitigated, not solved, by `scan_alt`.
- Presence mode verifies non-vacuity by model judgement until mutation testing lands.
- Feature profiles inherit spec incompleteness silently.
- Cost scales as rounds × shards. The tool trades tokens and wall-clock for the user no
  longer being the loop condition.
- Symbol resolution and mutant generation need a parser per language. Python uses stdlib
  `ast`; C#, TypeScript/JavaScript, Go, Rust and Java use tree-sitter grammars from the
  `languages` extra. A language without one is silent — `verify` defers and mutation skips the
  file. `coldsweep languages` reports it.
- Anchor uniqueness is per language: a Go method carries its receiver and a Rust method its
  `impl` type, because two symbols sharing an anchor derive one id and merge absorbs one of
  them. Wrappers an agent would not write — a C# namespace, a Rust `mod` — contribute nothing.
- In a statically typed language only type-preserving mutations are generated. A mutant that
  does not compile exits like a failing test, so without `build_command` it would be recorded
  as killed and the symbol reported as tested when nothing tested it.

## 18. Amendment: the gate is per half

§7 defines one global gate. Two measured end-to-end runs (docs/measurements.md) found that a
profile mixing `decided_by: code` rules with agent-decided ones can never open it: the agent
half was still producing new findings in the final round of every run, in both runs, for every
type. The deterministic half had settled rounds earlier and the single verdict hid it.

`evaluate` therefore reports `decidable` and `budgeted` alongside the global verdict, each
measured over the same rounds and the same K, differing only in rule set. The global gate is
unchanged and remains the strict answer. Unclassified findings and external blockers stay
global: an off-taxonomy rule id belongs to neither half, and a drifted spec is not a property
of one half's rules.

## 19. Amendment: a subsystem-decided rule is verified by re-deriving it

§5.1 makes `evidence_sha` the fix predicate, which only exists for `absence` findings. A rule a
subsystem decides carries no snippet, so `verify` could only ever defer it and the finding closed
by lapsing — silence, not proof.

Both such rules are now decided by re-running their decider: `unimplemented-spec-item` against
the marker set, and the `mutation:` rule against the survivor set. Each is exhaustive over its
scope, so an anchor missing from the re-derived set is proof. Measured on a two-file C# project,
this moved evidence-backed closure for a `tests` task from 25% to 100%.

The mutation sweep runs test suites, so it happens only when a `fixed` finding under that rule
exists and a cache path is supplied. It adds no work inside `coldsweep run`: it measures the
post-fix tree, which is the tree the next round's scan measures, so that scan is a cache hit.
Standalone `coldsweep verify` on such a profile is correspondingly slow, and that is the honest
price of the answer.

## 20. Amendment: the similarity fallback is for agent output only

§6 step 3 applies the rapidfuzz fallback to every rule. Measured on a Python `tests` task
(docs/measurements.md, run 3), that deleted 21 real work items: a subsystem's findings share a
rule, share a file, and carry one description template, so the score rests on the anchor alone
and two short sibling symbols clear the 0.92 auto-merge threshold. `_defer` absorbed `_reopen`.

Steps 3 and its adjudication call are now skipped for any rule marked `decided_by: code`. Such a
rule is exhaustive and its anchors are derived, not phrased, so two anchors are two work items by
construction — there is no differently-phrased duplicate to catch. Step 2, exact identity, is
unchanged and is what merges a re-derived finding across rounds.

The fallback exists because *agents* phrase the same finding differently every run. Applying it
where nothing is phrased was the error.

## 21. Amendment: a dispute a subsystem can decide is not triage

§12 makes `adjudicate` interactive triage for every dispute. For a rule a subsystem owns, half
of that is a factual question the subsystem already answers exhaustively, and asking a person is
asking them to re-derive by hand what one pass re-derives.

`adjudicate` now settles those first. Anchor no longer reported → `verified`, adjudicated, no
prompt. Anchor still reported → left disputed and still pending, annotated with the
confirmation. That half is deliberately *not* closed: the gate must not open over a symbol the
decider says nothing pins, and reopening it instead would restart the cycle the oscillation
guard exists to stop. What remains is a policy call — keep paying, or `wontfix` — which is what
a person is for.

The settleable set comes from the subsystem configs (`mutation.rule_id`,
`spec.unimplemented_rule_id`), not from `decided_by`. The two can disagree on a misconfigured
profile, and `verify` reads the same source, so a finding cannot be decidable in one pass and
undecidable in the other.

Measured (docs/measurements.md, run 3): of 15 disputes, 2 settled without asking, 12 confirmed
as real work, 1 left to a person because mutation has no opinion about an agent-decided rule.

## 22. Amendment: `decided_by` is derived from the subsystem configs, not asserted beside them

Three defects in a row (10, 11, and the adjudicate gap) were the same error: a rule a subsystem
owns, handled as though an agent decided it. Each was found by running the tool. The cause is
that `decided_by` was a second source of truth sitting beside `mutation.rule_id`,
`spec.unimplemented_rule_id` and `mechanical[].rule_id`, with nothing reconciling them.

A profile is now rejected unless every rule those configs name exists in the taxonomy and is
marked `decided_by: code`. A mismatch had been silent in three directions at once: the rule
reached the scan prompt, merge applied its similarity fallback to machine-generated anchors, and
the gate counted it in the budgeted half. A rule id no rule declares was worse -- the subsystem's
findings went to `unclassified` and held the gate shut with items triage cannot classify.

Two further sites follow from the same principle:

- **§6, stale closure.** A finding under an owned rule closes as `verified` after one round, not
  `lapsed` after K. The decider is exhaustive, so a complete pass that stops reporting an anchor
  is an inspection.
- **§10, the deterministic pass reports even when clean.** It previously emitted no shard when it
  found nothing, making "ran and found nothing" identical on the record to "never ran". Stale
  closure reads that difference, so it has to be recorded.
