# Convergence and cost, measured end to end

Measured 2026-08-24 against `d31287f`, one real `coldsweep run` per profile type, fix phase
enabled, on this repository.

## What existed before this

The README carried one measurement: five independent `issues`-style scan passes over four
modules with **no code changed between passes** and **no fix phase**. It established the
enumeration floor (44% of findings appeared in exactly one pass of five) and nothing else.

Not measured, and now measured here:

- convergence of a **real end-to-end loop**, where the fix phase changes the code the next
  round scans — the only mode the tool actually ships in;
- the `docs`, `tests` and `features` profiles at all;
- **cost**, in any unit. `runner.py` reads `claude -p --output-format json` and keeps only the
  `result` field, dropping `total_cost_usd`, `usage` and `duration_ms` on the floor. The tool
  could not report its own price, so the price had to be captured outside it.

## Headline

Four task types, `max_rounds=4`, `k=2`, `files_per_shard=1`, `parallelism=4`, `scan`/`fix` on
sonnet, no `scan_alt`.

| Type | Shards | New findings per round | Gate | USD | Agent calls | Elapsed |
|---|---|---|---|---|---|---|
| `issues` | 15 | 31 → 22 → 17 → 15 | shut | 44.99 | 102 | 45 min |
| `docs` | 2 | 8 → 6 → 2 → 1 | shut | 15.91 | 14 | 29 min |
| `features` | 15 | 8 → 1 → 2 → **0** | shut | 34.31 | 76 | 29 min |
| `tests` | 13 | 123 (round 1 only — round 2 aborted) | shut | 80.48 | 208 | 96 min |

**No profile converged.** `tests` never reached round 2: its own fix phase left the suite red
and the mutation subsystem refused to measure against it, so its row is one round, not four.

USD is API-equivalent — the account is a Claude Max subscription, so nothing here was billed
per token. The real budget spent is rate limit; the dollar figure is what the same work would
cost on the API.

## Cost model, verified

Every agent call's envelope was captured and the totals recomputed from token counts at
standard Claude Sonnet 5 rates — input $3.00/Mtok, output $15.00/Mtok, 1h cache write 2× input
= $6.00/Mtok, cache read 0.1× input = $0.30/Mtok. Claude Code writes its cache at the 1h TTL,
so cache writes are billed at 2×, not 1.25×.

| Task | Model says | Measured | Δ | cache-write / output / cache-read share |
|---|---|---|---|---|
| `issues` | 44.83 | 44.99 | −0.3% | 61% / 25% / 14% |
| `docs` | 15.89 | 15.91 | −0.1% | 53% / 23% / 25% |
| `features` | 34.21 | 34.31 | −0.3% | 62% / 18% / 20% |
| `tests` | 80.22 | 80.48 | −0.3% | 60% / 15% / 25% |

The residual is the Haiku 4.5 side-calls the CLI makes on its own account.

**Roughly 60% of every bill is cache creation, and it buys nothing.** Each shard is a fresh
`claude -p` subprocess, so each one re-writes the system prompt, tool definitions and
`CLAUDE.md` into the cache. Measured directly with a trivial prompt (`Reply with exactly: OK`,
sonnet, `--tools Read`):

```
call 1   $0.1084   cache_creation 17954   cache_read     0   output 4
call 2   $0.0827   cache_creation 13458   cache_read  4494   output 4
call 3   $0.0828   cache_creation 13463   cache_read  4494   output 4
```

Only ~4.5k of ~18k prefix tokens survive between subprocesses. **The floor is $0.083 per agent
call before it reads a single line of the repository**, and a 15-shard scan therefore costs
$1.25 before doing any work. This is the price of invariant 6 ("every round is a full
independent re-derivation") and invariant 2 ("scan agents never see prior findings"), and it is
charged per shard per round.

Per-call scan cost, by profile: `issues` $0.46, `features` $0.42, `tests` $0.48, `docs` $1.49
(two shards, but they are `README.md` at 18KB and a 318-line spec).

### A quiet round is not a cheap round

`features` round 4 produced **zero** findings and still cost **$6.02** — 15 scan calls, no fix,
no adjudication. Convergence at `k=2` therefore has a fixed price: two consecutive full scans
of everything, whatever they find. Budget as `rounds × shards × per-shard-scan`, and add
`k × shards × per-shard-scan` for the closing evidence.

## Per type

### `issues` — 15 shards, 4 rounds, $44.99

| Round | Raw | New | Fixed | Disputed | Verified | Reopened | USD (scan/fix/adj) | Elapsed |
|---|---|---|---|---|---|---|---|---|
| 1 | 32 | 31 | 31 | 0 | 18 | 13 | 12.11 (6.44/4.88/0.79) | 724s |
| 2 | 22 | 22 | 23 | 12 | 16 | 7 | 11.78 (7.35/4.19/0.24) | 682s |
| 3 | 19 | 17 | 17 | 6 | 8 | 9 | 9.82 (6.31/3.50/—) | 555s |
| 4 | 17 | 15 | 18 | 6 | 10 | 8 | 11.29 (7.67/3.62/—) | 677s |

Final: `verified 52, disputed 24, open 8, lapsed 1`. Exit 1, "budget spent".
Code changed: 8 files, +170/−61 in `src/`.

The new-finding rate decays — 31, 22, 17, 15 — but the decay is shallow and clearly not headed
for zero within any budget worth paying. This reproduces the README's enumeration-floor result
under the real loop: fixing findings does not stop the next fresh pass from finding more.

Two things the no-fix measurement could not have shown:

- **Fixes do not hold.** Across the four rounds, 37 fixed findings were reopened by
  verification — roughly 40% of everything the fix agent claimed. `verified` counts only what
  `coldsweep verify` re-read and confirmed gone.
- **The dispute backlog grows monotonically** — 0, 12, 18, 24 — and nothing in the loop clears
  it. 24 of 85 findings (28%) ended disputed and unadjudicated, and a disputed finding holds
  the gate shut forever until a human runs `coldsweep adjudicate`.

### `docs` — 2 shards, 4 rounds, $15.91

| Round | Raw | New | Exact dup | Stale-closed | Verified | Deferred | USD | Elapsed |
|---|---|---|---|---|---|---|---|---|
| 1 | 11 | 8 | 3 | 0 | 3 | 5 | 4.36 | 561s |
| 2 | 10 | 6 | 4 | 0 | 0 | 11 | 4.85 | 462s |
| 3 | 7 | 2 | 5 | 5 | 0 | 6 | 3.35 | 380s |
| 4 | 1 | 1 | 0 | 4 | 0 | 3 | 3.36 | 338s |

Final: `lapsed 9, verified 3, fixed 3, disputed 2`. Exit 1, "budget spent".

**The shipped `docs.yaml` says this profile plateaus. It does not.** The template's header
comment asserts "the new-finding rate plateaued rather than reaching zero. The gate will stay
shut" — but the measured curve is 8, 6, 2, 1, decaying fast enough that one or two more rounds
would plausibly have closed the quiet window. What actually kept the gate shut at round 4 was
2 unadjudicated disputes and 3 open findings, not an inexhaustible supply of new ones.

Documentation rules are more decidable than issue rules because the document is a closed
target: "this symbol has no entry" runs out of symbols. That distinction is real and the
template misstates it.

### `features` — 15 shards, 6 spec items, 4 rounds, $34.31

Spec: a 6-item `SPEC.md` (reproduced below) stating the cost-accounting capability this
measurement proved was missing. `coldsweep spec freeze` before round 1.

| Round | Spec impl/unimpl | Agent raw | New | Stale-closed | Verified | Deferred | USD | Elapsed |
|---|---|---|---|---|---|---|---|---|
| 1 | 0 / 6 | 2 | 8 | 0 | 0 | 7 | 12.60 | 550s |
| 2 | 6 / 0 | 1 | 1 | 0 | 0 | 8 | 6.42 | 378s |
| 3 | 6 / 0 | 2 | 2 | 7 | 0 | 3 | 9.27 | 531s |
| 4 | 6 / 0 | 0 | **0** | 1 | 0 | 2 | 6.02 | 256s |

Final: `lapsed 8, fixed 2, disputed 1`. Exit 1, "hard stop … without converging".
Code changed: 7 files, +227/−16.

**The deterministic half converged in exactly one round, as designed.** All six
`unimplemented-spec-item` findings were resolved by round 1's fix phase and never reappeared;
rounds 2–4 produced zero spec findings and zero stale markers. This is the README's claim about
`decided_by: code` holding up under measurement.

The agent tail (`vacuous-implementation`) is what kept the gate shut: 5 findings across rounds
1–3, with round 3 still contributing 2. Round 4 was genuinely quiet — one more quiet round and
the only thing standing between this task and a green gate would have been the single
unadjudicated dispute.

The loop implemented the spec: `runner.py::extract_usage`, `merge.py::_round_cost`,
`cli.py::status` and a `Usage` model, plus `# spec: FR-n` markers on the three pre-existing
behaviours. That code is gone — it lived in a worktree under the agent scratchpad and was never
committed, so the wipe took it with everything else. Only the diffstat above survives.

### `features`, re-run after the verification fix — 3 rounds, $26.73

Repeated against `fabd20f` with the same `SPEC.md`, the same profile and the same
`max_rounds=4`, once `verify` could decide the rules a subsystem owns (defect 2 below).

| Round | Spec impl/unimpl | Agent raw | New | Fixed | Verified | Deferred | USD |
|---|---|---|---|---|---|---|---|
| 1 | 0 / 6 | 2 | 8 | 7 (1 disputed) | **6** | 1 | 18.27 |
| 2 | 6 / 0 | 0 | 0 | — nothing open | 0 | 1 | ~5 |
| 3 | 6 / 0 | 0 | 0 | — nothing open | 0 | 0 | ~4 |

```
stopping after 3 round(s): 1 finding(s) need triage, and no further round can clear them
  - 1 disputed finding(s) not adjudicated
  disputed=1  lapsed=1  verified=6
```

| | Original | Re-run |
|---|---|---|
| Rounds | 4 (hard stop) | 3 (early stop: only triage left) |
| Cost | $34.31 | $26.73 |
| verified / lapsed | 0 / 8 | **6 / 1** |
| Evidence-backed closure | 0% | **75%** |

All six spec findings closed on evidence **in round 1**, not a round later — `verify` runs
straight after `fix` and sees the markers the fix just wrote. The one remaining deferral is the
`vacuous-implementation` agent finding, correctly deferred: nothing deterministic decides it.

Two other behaviours showed up for the first time here, both of them correct:

- **The early stop fired.** Round 3 ended the run rather than buying a fourth round that could
  not have changed the outcome, because the only thing left was an unadjudicated dispute.
- **The dispute was right.** A scan agent reported `runner.py::unwrap_envelope` for dropping
  usage; the fix agent replied that `unwrap_envelope` is text-only by design and `call()`
  separately runs `extract_usage(raw)` on the same envelope. That is true of the code the loop
  had just written — the finding was anchored on the wrong symbol.

`coldsweep adjudicate --accept-disputes` then closed the triage, and:

```
$ coldsweep converged --task bench-features; echo $?
0
```

**This is the only task in this document whose gate opened.** It needed a working verification
path for its deterministic rules, and one human decision on one dispute.

The implementation the loop produced — a `Usage` model, `runner.extract_usage`, per-round cost
in the round record, spend in `coldsweep status` — is committed on branch `bench/features2` as
`de112f3`. Unreviewed, and kept as evidence rather than offered as a patch.

### `tests` — 13 shards, 1 round, $80.48

Round 2 never scanned. The loop stopped itself:

```
error: baseline test run failed; mutation results would be meaningless against a red suite.
  tests: tests/test_cli.py
  E       assert [1, 2, 3] == 3
round 2: scan failed; stopping rather than ingesting partial coverage
```

**Round 1's own fix phase broke the test suite**, and round 2's mutation baseline caught it.
The guard worked exactly as designed — that part is good news. The reason it had something to
catch is not.

| Phase | Calls | USD | Avg | Agent wall |
|---|---|---|---|---|
| mutation (no LLM) | — | 0.00 | — | 37 min |
| scan | 13 | 6.24 | $0.480 | 15 min |
| adjudicate | 85 | 14.74 | $0.173 | 17 min |
| fix | 110 | 59.50 | $0.541 | 139 min |
| **total** | **208** | **80.48** | | **170 min** (59 min elapsed) |

```
mutation 616 mutant(s) over 13 file(s) -- killed 473, survived 112, no tests 31,
cached 0, 2228.945s
```

- **The deterministic decider reproduced exactly.** This run and the killed first attempt
  returned identical verdicts — 616 mutants, 473 killed, 112 survived, 31 unexercised — in
  2229s vs 2441s. `decided_by: code` means what it claims: same input, same answer, ±9% wall
  clock. Nothing else in this document reproduces.
- **37 minutes of wall clock, $0.00.** Serial by construction: a mutant is applied to the
  working tree, so it cannot be parallelised.
- 112 survivors + 31 unexercised → **115 deterministic findings** against **35** from the
  agents. Code out-produced the models 3:1, and one of the two agent rules — `vacuous-test` —
  produced nothing at all. Final histogram: `untested-behaviour` 97, `untested-error-path` 26,
  `vacuous-test` 0.
- Ingest: 150 raw → 123 new, 9 exact, 18 fuzzy.
- **Adjudication cost more than scanning.** 85 LLM adjudication calls at $14.74 — against 6
  across all four rounds of `issues`. Mutation findings and agent findings land on the same
  symbols, so merge fires the adjudicator on every fuzzy pair. On this profile adjudication is
  41% of calls and 18% of spend, and it is invisible in the CLI output.
- Fix: 123 findings over **108 work items** — 108 separate agent invocations, because
  `fix_scope: task` groups by anchor, not by file. Fix alone was 74% of the bill.

Result: `fixed 114, disputed 9`, `verified 0, reopened 0, deferred to next round 114`.

The fix phase wrote **+1207/−16 lines across 14 test files**, taking the suite from 349 tests to
518. Four of the new tests fail against unmodified source — the profile makes `src/**` read-only,
so these are simply wrong expectations:

```
FAILED tests/test_cli.py::test_status_reports_counts_and_convergence_as_json
FAILED tests/test_cli.py::test_adjudicate_can_add_an_unclassified_rule_to_the_taxonomy
FAILED tests/test_shard.py::test_governed_files_unions_scope_and_editable
FAILED tests/test_shard.py::test_governed_files_includes_editable_files_outside_scope
```

Nothing between the fix agents and the next round runs the suite. For a profile whose entire
remedy is *writing tests*, the fix phase has no success predicate at all: an agent reports
`fixed`, the finding is marked fixed, and whether the test it wrote even passes is discovered
one round later by a subsystem that exists for a different purpose. 114 findings were marked
fixed on nothing but the agent's own say-so.

## What this measurement says

**1. Nothing converged, and no two types failed the same way.**

| Type | Why the gate stayed shut |
|---|---|
| `issues` | genuinely inexhaustible — 15 new findings still arriving in round 4 |
| `docs` | ran out of budget while still decaying; 2 disputes + 3 open |
| `features` | round 4 was quiet; needed one more round, plus 1 dispute cleared |
| `tests` | its own fix phase broke the suite; round 2 refused to run |

Lumping `docs` in with `issues` as "budget-bounded" is not supported by the data.

**2. Evidence-backed closure varies from 98% to 0%.**

| Type | verified | lapsed | share closed on evidence |
|---|---|---|---|
| `issues` | 52 | 1 | 98% |
| `docs` | 3 | 9 | 25% |
| `features` | 0 | 8 | **0%** — 75% after the fix below |
| `tests` | 0 | 0 | **0%** — 114 still deferred at the stop |

`issues` is `absence` mode with anchors inside its own scope, so `coldsweep verify` can re-read
the file and prove the snippet is gone. `features` never verified anything at all across four
rounds, and the reason is structural rather than a scope misconfiguration: `verify_findings`
decided only `absence` findings carrying evidence, so every `presence` finding — including the
six exhaustively-decided spec items — was deferred before scope was even consulted, and lapsed.
A deterministic rule has no snippet to search for and never needed one.

**3. The two profiles with a separate `editable` set verify nothing.** `features` and `tests`
both use `fix_scope: task`, both anchor findings in files their own scope excludes, and both
closed exactly zero findings on evidence. Every "fixed" in those two tasks is an agent's
unchecked self-report.

**4. Cost is dominated by a cache the design cannot reuse.** 60% cache creation, and the fresh
context invariant is what forces it. Reducing `files_per_shard` — the setting that most improves
enumeration quality — multiplies this floor directly.

## Defects this run surfaced

Items 1, 2, 3, 4 and 6 are fixed; 5, 7 and 8 stand.

1. ~~**`runner.py` discards the cost envelope.**~~ `unwrap_envelope` returned only `result`, so
   everything in this document had to be captured by wrapping the `claude` binary. **Fixed** —
   every agent subprocess is billed to a per-task `spend.jsonl`, one line per *attempt*, via the
   single choke point `Runner.call`, so scan, fix and adjudicate are all covered. `coldsweep
   status` reports total, per phase and per round; `coldsweep run` reports each round's cost as
   it ends. An agent command that emits no envelope records `null`, never zero. Cross-checked
   against an independent wrapper on the same binary: identical to the cent.
2. ~~**`features.yaml` cannot verify its own deterministic findings.**~~ Every
   `unimplemented-spec-item` finding lapsed. The cause is one step earlier than scope, and
   widening scope would have changed nothing: `verify_findings` skips *every* `presence`
   finding before the scope check runs, because it has no offending snippet to search for.
   **Fixed** — a rule a deterministic subsystem owns does not need a snippet, since the sweep
   that produced the finding is exhaustive over scope. `spec.implemented_items` re-derives the
   marker set and `verify` decides on it: marked → `verified`, unmarked → `reopen`, with the
   same oscillation guard as the snippet path. Confirmed by re-running the task: 6 verified
   instead of 0, in one round fewer and for $7.58 less, and its gate opened.
3. ~~**`docs.yaml`'s header comment contradicts the measurement.**~~ It claimed a plateau; the
   measured curve decays 8 → 6 → 2 → 1. **Fixed** — the template and the README now state the
   difference between the two budget-bounded profiles.
4. ~~**The fix phase has no success predicate.**~~ Demonstrated, not theorised: `tests` round 1
   marked 114 findings fixed, wrote +1207 lines across 14 test files, and left 4 tests failing.
   Nothing checked. It surfaced one round later, from the mutation baseline guard — which
   exists to protect mutation results, not to catch this. **Fixed** —
   `mutation.reject_failing_fixes` runs each claimed source's paired tests after the fix phase;
   a source whose tests fail keeps its findings `open`, logged as `fix-unproven`, and `coldsweep
   fix` reports the count. A source with no paired test file is not checked: nothing was claimed
   about a test that does not exist.
5. **Concurrent fix agents share one editable set, and silently overwrite each other.** With
   `fix_scope: task`, `cli.py:fix` groups by anchor and hands *every* group the same resolved
   `editable` list, then runs them `parallelism`-wide. On `tests` that is 108 agents, 4 at a
   time, each free to write any file under `tests/**`. Nothing serialises them.

   **Reproduced** (harness below). Four findings on four different source symbols, one shared
   remedy file, a stub fix agent doing the read-think-write a real agent does:

   | `parallelism` | tests surviving in the file | findings marked `fixed` |
   |---|---|---|
   | 1 | 4 | 4 |
   | 2 | **2** | 4 |
   | 4 | **1** | 4 |

   The loss scales with `parallelism`, and every run reports `fixed 4, disputed 0, failed
   files 0`. The loop claims complete success while three quarters of the work is gone. It is
   recoverable — the next round re-derives what was destroyed — but it costs a full round, and
   in between the finding set says the work is done.

   The fix-phase gate added for defect 4 does **not** cover this. It runs the paired tests and
   the surviving tests pass; a green suite cannot prove a test that was never written exists.
   Still open.
6. ~~**LLM adjudication is an unreported cost centre.**~~ 85 calls and $14.74 on `tests` round 1
   alone — 41% of its calls — because mutation and agent findings collide on the same symbols.
   The round reported `adjudicated 0`, because that counter only ever counted pairs the
   adjudicator ruled *the same*: a ruling of "different" merges nothing and moved no counter,
   while still being a paid agent call. **Fixed** — `RunRecord.adjudicator_calls` counts every
   invocation and ingest now prints `adjudicated 0 of 85 call(s)`, with the cost in the round's
   spend line.
7. **The dispute backlog is unbounded and blocks the gate.** `issues` ended with 24 disputed
   findings after 4 rounds, growing every round, with no automatic adjudication in `run`.
8. **~40% of fixes do not survive verification** on `issues`, and the loop's only response is
   to spend another round.

## Limits of this measurement

- One repository, one commit, 3,543 LOC of Python. Nothing here generalises to a different
  codebase without re-running it.
- `n=1` per type for the agent-decided rules. Agent output is nondeterministic; the
  round-by-round curves would differ on a rerun. The convergence *shapes* are the claim, not the
  exact counts. The one thing measured twice — the mutation subsystem — returned identical
  verdicts both times.
- `tests` is **one round**, not four, and it did not stop for budget: its own output broke the
  precondition of round 2. Its cost row is a single round and is not comparable with the
  four-round rows above; a four-round `tests` task would be far the most expensive of the four.
- Four rounds is a floor, not a plateau: at `k=2`, three rounds is the theoretical minimum for
  a converged verdict, so `max_rounds=4` leaves exactly one round of slack. `features` needed
  five.
- `issues` rounds 2–4 and all of `docs` ran concurrently in separate worktrees, so their
  *elapsed* times include contention for CPU and rate limit. Per-call `wall_s` and all USD and
  token figures are unaffected. `issues` round 1 (724s) ran alone.
- No `scan_alt`. The README's 57%-single-family result means a two-model configuration would
  find more per round and cost roughly the same per call.
- The raw per-call JSONL for `issues`, `docs` and `features` was lost when the harness's
  scratchpad was wiped; the aggregates in this document were captured before the wipe and are
  exact, but cannot be re-derived. The rerun harness writes to
  `/Users/skonovalov/dev/coldsweep-bench/` for this reason.

---

# Repeating this

## Environment

| | |
|---|---|
| Repo | `github.com/l0kifs/coldsweep` at `d31287f15b279895dbb5942f650fe1755412ccce` |
| Host | macOS 26.6.1, arm64, 16 GB |
| Python | 3.12 (`.python-version`); driver venv 3.11.3 |
| `uv` | 0.12.3 |
| `git` | 2.50.1 |
| `claude` CLI | 2.1.220 |
| Account | Claude Max, `default_claude_max_20x`, `hasExtraUsageEnabled: false` |
| Deps | pydantic 2.13.4, typer 0.27.1, rapidfuzz 3.14.5, pyyaml 6.0.3, pytest 9.1.1 |
| Suite | 349 tests, 84s |

`total_cost_usd` from the CLI is API-equivalent, not billed, on a subscription account.

## Layout

```sh
export BR=$HOME/dev/coldsweep-bench
mkdir -p $BR/{bin,logs,wt}
```

One git worktree per type, all from the same base commit, so the types do not contaminate each
other through the fix phase:

```sh
cd /path/to/coldsweep
for t in issues docs tests features; do
  git worktree add -b bench/$t $BR/wt/$t d31287f
done
uv sync                                  # driver venv, in the main checkout
cd $BR/wt/tests && uv sync               # the tests profile shells out to pytest
```

## The meter

coldsweep drops the cost envelope, so the `claude` binary is wrapped. Save as
`$BR/bin/claude-meter`, `chmod +x`:

```python
#!/usr/bin/env python3
"""Transparent wrapper around the `claude` CLI that records the result envelope.

Forwards stdin, argv, stdout, stderr and the exit code unchanged, and appends one JSONL record
per call. Phase is read off the prompt (the three prompt templates have distinct first lines);
round off the task's runs/ directory -- a scan runs before runs/<N>.json is written and a fix
after it, so completed = len(runs/*.json) gives round = completed+1 for a scan, completed for
a fix.
"""
from __future__ import annotations

import json, os, re, subprocess, sys, time
from pathlib import Path

LOG = Path(os.environ["COLDSWEEP_METER_LOG"])
REAL = os.environ.get("COLDSWEEP_METER_REAL", "claude")
TASK = os.environ.get("COLDSWEEP_TASK", "")
RUN_JSON = re.compile(r"^\d+\.json$")

PHASES = (("You are performing an independent audit", "scan"),
          ("You are resolving a specific", "fix"),
          ("Compare two audit findings", "adjudicate"))


def phase_of(prompt: str) -> str:
    head = prompt.lstrip()[:120]
    return next((n for p, n in PHASES if head.startswith(p)), "unknown")


def completed_rounds() -> int:
    runs = Path.cwd() / ".coldsweep" / "tasks" / TASK / "runs"
    return sum(1 for p in runs.iterdir() if RUN_JSON.match(p.name)) if runs.is_dir() else 0


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def main() -> int:
    prompt, argv = sys.stdin.buffer.read(), sys.argv[1:]
    phase, before = phase_of(prompt.decode("utf-8", "replace")), completed_rounds()

    started = time.time()
    proc = subprocess.run([REAL, *argv], input=prompt, capture_output=True, check=False)
    elapsed = time.time() - started

    record = {"ts": started, "task": TASK, "phase": phase,
              "round": before + 1 if phase == "scan" else before,
              "model_flag": flag(argv, "--model"), "tools_flag": flag(argv, "--tools"),
              "wall_s": round(elapsed, 3), "exit": proc.returncode,
              "prompt_bytes": len(prompt), "stdout_bytes": len(proc.stdout),
              "stderr_tail": proc.stderr.decode("utf-8", "replace")[-400:] if proc.returncode else ""}
    try:
        envelope = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeError):
        envelope = None
    if isinstance(envelope, dict):
        record.update({k: envelope.get(k) for k in
                       ("is_error", "subtype", "duration_ms", "duration_api_ms", "num_turns",
                        "total_cost_usd", "usage")})
        record["model_usage"] = envelope.get("modelUsage")
        record["result_chars"] = len(envelope["result"]) if isinstance(envelope.get("result"), str) else None
    else:
        record["envelope"] = "unparsable"

    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    sys.stdout.buffer.write(proc.stdout); sys.stdout.buffer.flush()
    sys.stderr.buffer.write(proc.stderr); sys.stderr.buffer.flush()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
```

The shim classifies by prompt prefix and by counting `runs/*.json`; both are stable as long as
`src/coldsweep/prompts/*.md` keep their opening lines and `store.save_round` keeps writing
`runs/<N>.json`.

## Tasks

```sh
CS=/path/to/coldsweep/.venv/bin/coldsweep
for t in issues docs tests features; do
  $CS init $t --task bench-$t -C $BR/wt/$t
done
```

Then in each `$BR/wt/$t/.coldsweep/tasks/bench-$t/profile.yaml`:

```yaml
agent:
  command: ["<BR>/bin/claude-meter", "-p", "--output-format", "json"]   # was ["claude", ...]
convergence:
  max_rounds: 4                                                        # was 8
```

and in the `tests` profile only, so the mutation subsystem uses the worktree's own venv:

```yaml
mutation:
  test_command: ".venv/bin/python -m pytest -q -x {tests}"
```

Everything else is the shipped template, unmodified: `files_per_shard: 1`, `k: 2`,
`parallelism: 4`, `retries: 2`, `timeout_s: 900`, `models.scan`/`fix`/`adjudicate` = sonnet,
`scan_alt: null`.

## The features spec

`$BR/wt/features/SPEC.md`, frozen with `coldsweep spec freeze --task bench-features` before the
first round. Six items: FR-1..FR-3 describe behaviour that already existed and needed only
`# spec:` markers; FR-4..FR-6 were genuinely unimplemented.

```markdown
# coldsweep — cost accounting spec

Every phase of a coldsweep run spawns agent subprocesses, and `coldsweep run` currently reports
rounds and findings but never what they cost. "Cost scales as rounds x shards" is a documented
limit with no number behind it: a user cannot tell whether the next round is worth buying.

This spec states what the tool must record and report so that question has an answer.

### FR-1 Every agent subprocess is a fresh context

Each shard is handed to its own agent invocation, so no scan sees another shard's context,
another round's findings, or any diff. The invocation is a subprocess of the configured agent
command, and its result is validated against a schema before it is used.

### FR-2 A schema violation is retried, then fails loudly

An agent response that does not parse or does not satisfy its schema is retried up to the
configured retry count. When the last attempt still fails, the failure is raised rather than
being turned into an empty result, because an empty result is indistinguishable from a clean
shard and would silently count as coverage.

### FR-3 A failed shard never counts as coverage

A round in which any shard failed is not a full re-derivation of the finding set. Such a round
is recorded as incomplete and never counts toward the quiet window that opens the gate.

### FR-4 The result envelope's usage is retained

The agent command emits a result envelope carrying token usage, a total cost in USD and a
duration alongside the model's answer. Those fields are read off the envelope and kept on the
shard result, instead of being discarded when the answer is unwrapped. A run whose agent
command emits no envelope, or an envelope without those fields, records their absence rather
than a zero.

### FR-5 Every round record carries its own cost

A saved round record states the tokens, the USD cost and the wall-clock duration spent
producing it, aggregated from that round's shard results and broken down by phase, so the cost
of one round can be read from the round record alone without re-running anything.

### FR-6 `coldsweep status` reports spend

`coldsweep status` reports what the task has spent so far: total USD, total tokens, and the
per-round cost of the rounds completed. Its `--json` form carries the same numbers as fields.
```

## Running

One task at a time, or at most two concurrently — four tasks at `parallelism: 4` is 16
concurrent `claude` processes, which will not fit in 16 GB.

```sh
cd $BR/wt/$t
COLDSWEEP_METER_LOG=$BR/logs/$t.jsonl COLDSWEEP_TASK=bench-$t \
  nohup coldsweep run --task bench-$t --max-rounds 4 > $BR/logs/$t.stdout 2>&1 &
```

`nohup` matters: these runs are hours long, and an interrupted one loses everything not yet
written to `.coldsweep/`.

Exit 1 is the expected ending. `coldsweep spec freeze --task bench-features` must run before
the `features` task, or `scan` refuses.

## Reading the numbers

`$BR/bin/analyse.py` joins the meter log against each task's `.coldsweep/` state:

- `runs/<N>.json` → shards, per-round raw finding counts, agent vs deterministic, model used
- `runs/<N>.ingest.json` → `new`, `exact`, `fuzzy`, `stale_closed`, `failed_shards` — `new` is
  the convergence signal
- `findings.jsonl` → final status and rule histogram
- meter JSONL → USD, tokens and wall clock, split by round and phase

Elapsed per round is `max(ts + wall_s) − min(ts)` over that round's calls; summing `wall_s`
gives agent-seconds, which at `parallelism: 4` runs about 3.2× higher.

The script and the raw logs live in `$BR`. The recovered aggregates from the first session are
in `$BR/logs/recovered.json`.

## Reproducing the concurrent-fix race (defect 5)

Deterministic, no LLM calls. The stub holds the file for a fixed interval between read and
write, so the outcome is repeatable rather than a coin flip; a real agent's Read-then-Write is
the same race with a variable window, and Read-then-Edit is the same race with a narrower one.

`$BR/repro/racy_fix_agent.py` — reads the rendered fix prompt on stdin, appends one test per
finding to a shared target, reports `fixed`:

```python
prompt = sys.stdin.read()
ids = re.findall(r"^- id: `([^`]+)`$", prompt, re.MULTILINE)

body = TARGET.read_text() if TARGET.is_file() else ""     # read
time.sleep(HOLD_S)                                        # decide
for finding_id in ids:                                    # write back, whole file
    body += f"def test_{finding_id.replace('-', '_')}():\n    assert True\n\n"
TARGET.write_text(body)

print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps({"results": [
                      {"id": i, "outcome": "fixed", "detail": "..."} for i in ids]})}))
```

`$BR/repro/race-profile.yaml` is the minimal profile with the shape the defect needs — audited
and editable sets differing, `fix_scope: task`, `retries: 0`, no `mutation:` block so the run
needs no pytest. The agent command is the stub with `append_flags: false`.

`$BR/repro/run.sh <parallelism>` builds a scratch git repo with four source files and one empty
`tests/test_shared.py`, seeds `findings.jsonl` with four open findings on four distinct anchors,
then runs `coldsweep fix` with `RACE_TARGET` pointing at the shared file:

```sh
RACE_TARGET="$REPO/tests/test_shared.py" RACE_HOLD_S=0.4 coldsweep fix --task race -C "$REPO"
grep -c '^def test_' "$REPO/tests/test_shared.py"     # 4 at parallelism 1, 1 at parallelism 4
```

Run `run.sh 1` first: it is the control, and all four tests must survive it. If they do not, the
stub or the seed is wrong, not the tool.

## Cleaning up

```sh
cd /path/to/coldsweep
git worktree remove --force $BR/wt/<t>      # or: git worktree prune
git branch -D bench/<t>                     # after salvaging anything worth keeping
```
