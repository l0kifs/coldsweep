# Convergence and cost, measured end to end

Measured 2026-08-24 against `d31287f`, one real `coldsweep run` per profile type, fix phase
enabled, on this repository. Every defect it found was then fixed and three of the four types
were measured again at `24ab2fb` — see [Run 2](#run-2--the-same-measurement-against-the-repaired-tool).
Everything before that section describes the tool as it was, not as it is.

[Run 3](#run-3--the-language-port-and-the-tests-profile-measured-at-last) (2026-08-25) measures
the language port on C# and Rust, and finally runs the Python `tests` profile that run 2 skipped.
It found two more defects — one of them a silent deletion of work items in `merge` — and produced
the first open gate in this document, on a three-file project, for reasons that say nothing about
scale.

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

**No profile converged** — none of these four, none in run 2, and not the Python `tests` profile
when it was finally measured in run 3 either. The only task in this document that ever opened its
gate is the three-file C# project, which is not a counterexample to anything here. `tests` never reached round 2: its own fix phase left the suite red
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

## Run 2 — the same measurement against the repaired tool

Everything above describes `d31287f`. All eight defects it found were then fixed, and three of
the four types were measured again at `24ab2fb` with the same configuration: `max_rounds=4`,
`k=2`, `files_per_shard=1`, `parallelism=4`, sonnet, no `scan_alt`, the same 6-item `SPEC.md`,
and the same shard counts (15 / 2 / 15). `tests` was not re-run — at four rounds it costs more
than the other three together.

`max_disputes` was deliberately left unset, though it is one of the fixes: setting it would
have stopped `issues` early and destroyed round-for-round comparability.

| | `issues` | | `docs` | | `features` | |
|---|---|---|---|---|---|---|
| | run 1 | run 2 | run 1 | run 2 | run 1 | run 2 |
| New per round | 31,22,17,15 | 29,17,20,12 | 8,6,2,1 | 9,3,2,1 | 8,1,2,0 | 6,1,1,1 |
| Fixes claimed | 89 | 76 | 17 | 15 | 9 | 9 |
| **Reopened by verify** | **37** | **0** | 0 | 0 | 0 | 0 |
| verified / lapsed | 52 / 1 | 49 / 12 | 3 / 9 | 3 / 5 | 0 / 8 | **6 / 1** |
| Evidence-backed closure | 98% | 80% | 25% | 38% | **0%** | **86%** |
| Disputes at the end | 24 | **3** | 2 | 6 | 1 | 0 |
| Cost | $44.99 | $46.47 | $15.91 | $18.13 | $34.31 | $30.68 |

Three types, both runs: **$95.21 → $95.28**. Seven behaviour changes for a 0.07% difference in
price.

### What changed

**Verification stopped reporting correct fixes as failed.** `issues` went from 37 reopens in 89
fixes to **zero in 76**. That is the single largest behavioural difference in this document, and
it is the one whose mechanism was already established in isolation: the two additive-remedy
rules were 69 of 85 findings, and their correct fixes leave the cited line in place.

**Evidence-backed closure moved in both directions, as designed.** `issues` fell 98% → 80%:
false failures became honest defers, and a deferred fix closes by lapsing rather than by proof.
`features` went 0% → 86% in the other direction, because a rule a subsystem decides can now be
verified by re-deriving it. Both are the same principle — claim only what was checked — and the
fact that it lowers one number and raises another is the point.

**The dispute backlog collapsed, 24 → 3, and that was not predicted.** It is a second-order
effect of the verification fix: in run 1 a wrongly-reopened finding went back to `open`, was
fixed again, and eventually the fix agent refused and disputed it. Most of that backlog was the
tool arguing with itself. `docs` went the other way, 2 → 6, on a corpus its own earlier rounds
had rewritten.

**Adjudication became visible.** `issues` run 1 reported `adjudicated 0` while spending $1.03
across 6 calls; run 2 reports `adjudicated 0 of 4 call(s)` and $2.97 across 12.

### What did not change

**Convergence.** `issues` still contributed 12 new findings in round 4; `features` still drew
one `vacuous-implementation` finding per round to the end. None of the eight fixes was aimed at
the enumeration floor, and none moved it. Every gate that was shut is still shut.

**The cost shape.** Cache creation is 48–64% of every bill in both runs, and recomputing run 2
from token counts at Sonnet 5 rates reproduces all three totals to within 0.3%, exactly as it
did for run 1.

**`docs` decays rather than plateaus.** 8,6,2,1 then 9,3,2,1 — the claim the original
`docs.yaml` got wrong, now measured twice.

### What this comparison cannot tell you

Run 2 is not a controlled A/B. The tool changed **and so did the corpus it audits**: seven
commits added roughly 700 lines to `src/`, and `README.md` was substantially rewritten between
the runs. So the per-round finding counts are not strictly comparable — a difference there
could be the repair or could be the new code.

The claims that survive that objection are the ones with an independent mechanism: reopens
(the predicate was demonstrated wrong in isolation), evidence-backed closure (same), dispute
visibility, and the cost model. The convergence curves are weaker evidence, and `n` is still 1
per type per commit.

`tests` is missing from run 2 entirely, so the two fixes built specifically for it — the
fix-phase gate and the concurrent-write lanes — have unit-level reproductions behind them and no
end-to-end measurement.

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

All eight are fixed. Defect 9 was found afterwards, by reading run 2's output.

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

   **Fixed** — each group is now handed only the slice of the editable set its fix may write,
   taken from the profile's own source-to-test pairing, and `fix_lanes` partitions the groups so
   that any two sharing a file run in sequence while disjoint ones still run together. A profile
   with no pairing gets the whole set back and is serialised, which is correct: an agent free to
   write anywhere cannot safely run beside another. Same harness, `parallelism: 4`:

   | Profile | Before | After |
   |---|---|---|
   | no pairing | 1 of 4 survive | 4 of 4, one lane |
   | source-to-test pairing | 1 of 4 survive | 4 of 4, four lanes, full parallelism |
6. ~~**LLM adjudication is an unreported cost centre.**~~ 85 calls and $14.74 on `tests` round 1
   alone — 41% of its calls — because mutation and agent findings collide on the same symbols.
   The round reported `adjudicated 0`, because that counter only ever counted pairs the
   adjudicator ruled *the same*: a ruling of "different" merges nothing and moved no counter,
   while still being a paid agent call. **Fixed** — `RunRecord.adjudicator_calls` counts every
   invocation and ingest now prints `adjudicated 0 of 85 call(s)`, with the cost in the round's
   spend line.
7. ~~**The dispute backlog is unbounded and blocks the gate.**~~ `issues` ended with 24 disputed
   findings after 4 rounds, growing every round — 0, 12, 18, 24 — each one holding the gate
   shut and none of them clearable by scanning. The existing early stop could not fire, because
   it requires nothing else to be open and this task kept finding work, so the backlog grew
   underneath a run that was spending its budget on rounds that could not have opened the gate.

   **Fixed** — the pending count is reported after every round, and `convergence.max_disputes`
   stops the run once the backlog reaches it. Verified with the `stubborn` stub, which disputes
   everything: unbounded runs 3 rounds, `max_disputes: 2` stops after 1. Both exit non-zero and
   point at `coldsweep adjudicate`.

   Left unset by default. The bound is a judgement about the repository, and a default that
   stopped runs people wanted to continue would be a worse failure than the one it prevents.
8. ~~**~40% of fixes do not survive verification**~~ on `issues` — 37 of 89 reopened. Two
   causes, both measured, neither of them the agent failing:

   - The predicate searched the whole file. 7.4% of snippets in this repository recur inside
     their own file, so an untouched copy could reopen a finding about a fixed one.
   - **The bigger one: the `absence` predicate is wrong for an additive remedy.** Wrapping a
     call in `try/except`, or adding validation after a read, leaves the cited line exactly
     where it was. Demonstrated directly — a textbook fix for `missing-error-handling` and for
     `unvalidated-external-input` both still match their own evidence, and those two rules were
     **69 of the 85 findings** in that run. `swallowed-exception` and `resource-leak` rewrite
     the cited line and verify correctly.

   **Fixed** — the search is scoped to the anchored symbol, and a surviving snippet reopens a
   finding only when the symbol around it is unchanged. When the symbol changed, the case is
   deferred rather than reopened.

   Note what this does not do: it removes a false failure, it does not manufacture a proof. An
   additive fix now closes by lapsing rather than by evidence, so `issues` should show fewer
   reopens *and* a lower evidence-backed closure rate. Proving an additive remedy needs a
   predicate that asks whether the cited line is now guarded, which is rule-specific AST work
   and is not done here.

### 9. Fix agents drift a contract apart within a single round

Found by reviewing `bench2/issues` — 371 lines of error handling the `issues` task wrote into
`src/` across four rounds. The code is good: raw `OSError` and `sqlite3.Error` turned into
domain errors with context, `raise ... from exc` throughout, no silent fallbacks. Four of its
guards are dead.

`store.py`'s fix agent wrapped raw `OSError` and `sqlite3.Error` into `StoreError`. In the same
round, `cli.py`'s agent wrote guards on the opposite premise, and stated it in a docstring:

> atomic_write raises a raw OSError and rebuild_index a raw sqlite3.Error on failure — neither
> is a StoreError, so both must be caught here rather than left to crash past every caller.

True when that agent read the code. False by the time the round ended. `StoreError` is a
`RuntimeError`, so three `except OSError` guards in `cli.py` and one in `runner.py` can no
longer fire.

Three are cosmetic — `main()` catches `StoreError` anyway, so the user sees a generic message
instead of the intended one. The fourth is real: `Runner._record` guards the spend ledger
specifically so a write failure cannot take down the rest of an `asyncio.gather` batch, and
that is now exactly what it does.

A second pair coordinated *correctly* by luck: `runner.py` removed the adjudicator's local
`try/except` while `merge.py` added one around the call site. Complementary — but had only the
first landed, any adjudicator failure would crash ingest.

**Nothing detects this.** The 349-test suite passes; none of these paths is exercised. Every
later round re-derives findings from the files alone, so a scan agent sees the drifted code as
the new normal and has no reason to report it. And the cause is the design working as intended:
fresh context per shard is what makes enumeration honest, and it is exactly why a per-file fix
agent cannot see a contract it depends on changing next door.

The error handling itself was worth having and has been ported to `main` with the four guards
corrected. The defect is the class, not the instance: **a round's fix phase can leave the
repository self-inconsistent in a way no later round will report.**

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

## Run 3 — the language port, and the `tests` profile measured at last

Three tasks against the working tree at `ef15bd7` plus the language port. Two are scratch
projects built to exercise that port — a C# project through the full loop, a Rust project
through the mutation subsystem alone — and the third is the Python `tests` profile that run 2
skipped, on an isolated copy of this repository. `dotnet` 10.0.400, `cargo` 1.96.1. No Go: the
toolchain is not installed on this host.

| Task | Shards | New per round | Gate | USD |
|---|---|---|---|---|
| `cs-demo` (C#) | 2 | 4 → 0 → 0 | **open** | 3.21 |
| `rs-demo` (Rust) | 2 | mutation only, no agent calls | — | 0 |
| `py-bench` (Python `tests`) | 4 | 34 → 3 → 2 | shut | 63.30 |

**Read the C# convergence as a mechanism proof and nothing more.** Three files is not a
codebase. The claims that survive are the ones about *machinery* — anchors, mutant generation,
the build gate, the cache, the verify predicate, and the two defects below. The convergence
result is not one of them; see the limits at the end.

### `cs-demo` — 2 shards, 3 files, 3 rounds, $3.21

Two source files and one test file. Rules: `untested-behaviour` (`decided_by: code`, mutation)
and `vacuous-test` (agent). `k=2`, sonnet, parallelism 2, `stop_at_first_survivor: false`,
`build_command: dotnet build`.

| Round | Mutants | Killed | Survived | New | Fixed | Verified | Deferred | USD | Mutation |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 8 | 3 | 4 | 4 | 4 | 1 | 3 | 1.97 | cached |
| 2 | 8 | 8 | **0** | 0 | — | 0 | 3 | 0.62 | 42.7s |
| 3 | 8 | 8 | 0 | 0 | — | 0 | 0 | 0.62 | 0.06s |

Final: `verified 4, lapsed 0, open 0, disputed 0`. **`converged after 3 round(s)`, exit 0** —
both the global gate and `--half decidable`. Cold mutation pass: 32.1s; unchanged re-run 0.08s
on 8 mutant hits plus 2 probe hits.

The fix agent replaced the assertion-free `IsFreeIsCalled` with two asserting tests, added three
boundary cases for `Discount`, and wrote a new `tests/LoaderTests.cs`. Round 2's mutation pass
then killed all 8 mutants, which is what makes this a measurement rather than a self-report.

### `rs-demo` — mutation only, 11 mutants, no agent calls

| Symbol | Verdict |
|---|---|
| `src/pricing.rs::Pricing::discount` | survivors: `'100' -> '101'`, `'&&' -> '\|\|'` |
| `src/pricing.rs::Pricing::is_free` | survivors: `'==' -> '!='`, `'0' -> '1'` |
| `src/pricing.rs::Store::discount` | survivors: `'-' -> '+'`, `'1' -> '2'` |
| `src/loader.rs` | no paired test file |

killed 3, survived 6, no tests 2, **not built 0**, 13.9s.

`Pricing::discount` and `Store::discount` are two findings, not one. Both are `fn discount` and
neither `impl` block carries a name, so without taking the segment from the `impl` type they
derive the same anchor, the same id, and merge absorbs one of them with no trace. Same shape as
Go's method receiver. This is the measurement that pays for that mechanism.

### What the port got right

**Every generated mutant compiled.** `not built 0` in both languages, across 19 mutants. That is
the type-preservation restriction holding on real code rather than on a fixture.

**And it is load-bearing.** A `return null` spliced by hand into `int Discount(...)`:

```
dotnet build src/src.csproj   -> exit 1
dotnet test  tests/tests.csproj -> exit 1
```

Identical exit codes. Judged the way every other mutant is judged, that mutant is recorded
**killed**, and `Discount` is reported as pinned by tests that never ran against it. The
narrower operator set is not conservatism; it is the only thing standing between the subsystem
and a confidently wrong clean bill.

**Anchors survived the round trip.** `src/Pricing.cs::Pricing::IsFree` was written by the
mutation subsystem, matched by merge across three rounds, and resolved by `verify` — the whole
identity path, on a language the tool had never run against.

### The first defect: `verify` could not decide a mutation finding

**10.** Run 1's structural defect for `features`
— `verify_findings` deciding only `absence` findings with evidence — was fixed for
`unimplemented-spec-item` by re-deriving the marker set, and the same fix was never extended to
the mutation rule. `tests` was not re-run in run 2, so nothing caught it.

It showed up here as three findings frozen at `fixed` after round 2, while that same round's
mutation pass reported `survived 0` — exhaustive proof they were resolved, sitting unused. They
could only ever close by lapsing.

**Fixed** — `verify` now re-derives the survivor set and decides against it: anchor absent →
`verified`, still present → `reopen`, subsystem unable to run → `defer`. Evidence-backed closure
for this task went **25% → 100%**, and it is what let round 3 open the gate.

The sweep runs test suites, so it fires only when a `fixed` finding under that rule exists and a
cache path is supplied. It costs nothing inside `coldsweep run`: it measures the post-fix tree,
which is the tree the next round's scan measures, so that scan is a cache hit. Measured — the
verify sweep took **0.31s** and round 3's scan-time mutation pass **0.06s**, both fully cached.

### `py-bench` — the Python `tests` profile, 4 shards, 3 rounds, $63.30

The profile run 2 skipped, and the one the verify fix above was written for. Scoped to four
modules (`converge`, `merge`, `verify`, `syntax`) rather than all fourteen, on cost; run against
an isolated copy of the working tree at `ef15bd7` plus the port, with its own venv, so the fix
phase edits nothing real.

| Round | Mutants | Killed | Survived | New | Fixed | Verified | Reopened | Disputes | USD |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 203 | 164 | 39 | 34 | 30 | 8 | 0 | 0 | 25.67 |
| 2 | 282 | 258 | 24 | 3 | 18 | 7 | **10** | 6 | 20.50 |
| 3 | 299 | 278 | 21 | 2 | 14 | 3 | 5 | 15 | 17.13 |

Final: `verified 18, disputed 15, lapsed 3, fixed 2, open 1`. Exit 1 — three blockers, of which
15 unadjudicated disputes is the one nothing in the loop can clear. Mutant counts *rise* between
rounds because `stop_at_first_survivor` skips the rest of an anchor once one survives; as
symbols get pinned, fewer are skipped and more are actually judged.

**Run 1's `tests` failure did not recur.** That row was one round: the fix phase left the suite
red and the mutation baseline refused round 2. Here three fix phases ran and the suite ended
**477 passing**, up from 415. The fix-phase gate built for that defect works.

**Evidence-backed closure: 86%** — 18 verified against 3 lapsed. Run 1's `tests` closed **0%** of
its findings on evidence. That difference is the verify fix, and this is the measurement it was
missing.

**And it reopens as readily as it verifies.** Round 2 verified 7 and **reopened 10**: the fix
agent reported 18 fixes, and re-deriving the survivor set proved 10 of them had not landed. The
15 disputes are mostly those findings hitting the oscillation guard after three failed attempts.
A predicate that only ever confirmed would have recorded all 18.

### The second defect: merge deleted work items

**11. The similarity fallback silently absorbed distinct symbols.** `merge` auto-merges at
rapidfuzz ≥ 0.92 over `(anchor, description)`, gated to the same rule and file. Every mutation
finding shares a rule, shares a file, and carries the *same description template* — so the score
is decided by the anchor alone, and two short sibling symbols in one module clear the threshold.

Measured, round 1 of this run, 9 auto-merges. Four of them:

| Kept | Silently absorbed | Score |
|---|---|---|
| `verify.py::_decide_spec_item` | `verify.py::_decide_unpinned` | 0.934 |
| `syntax.py::_python_ranges` | `syntax.py::_tree_ranges` | 0.925 |
| `syntax.py::mutation_sites::binary` | `syntax.py::mutation_sites::unary` | 0.922 |
| `verify.py::_defer` | `verify.py::_reopen` | 0.965 |

These are unrelated functions. 21 work items were deleted across the three rounds, and the only
trace is a line in the absorbing finding's history. This is a direct breach of **invariant 5**
— merge errs toward duplicates, never toward loss — in the module the spec calls the
highest-risk one, and it went unseen through two prior measurement runs because neither ran a
profile whose findings are machine-generated.

A second cost: 29 adjudication calls, **$6.03**, of which 28 ruled "different". The 0.75–0.92
band is entirely spurious for this rule class.

**Fixed** — the similarity fallback and the adjudication call are skipped for any rule marked
`decided_by: code`. Such a rule is exhaustive and its anchors are machine-derived, so two
anchors are two work items by construction: there is no differently-phrased duplicate for the
fallback to catch, and nothing for it to do but lose things. Exact identity matching is
untouched, so a re-derived finding still merges.

### Limits of run 3

- **Three files.** The gate opened because `vacuous-test` ran out of things to say about two
  source files and one test file — not because the enumeration floor moved. On the 15-shard
  Python `issues` runs the agent half never went quiet once in eight rounds across two runs.
  Nothing here contradicts that, and nothing here should be read as convergence becoming
  achievable at scale.
- `n=1`, one commit, two scratch projects written for this measurement. They are not code
  anyone wrote for another purpose, which is exactly the property real repositories have.
- Round 1's mutation pass shows `cached` because a standalone `coldsweep mutants` had already
  run against the same tree. The honest cold number is 32.1s, measured separately.
- Cost is not comparable to runs 1 and 2: 2 shards against 15, and a taxonomy of two rules.
- Per-mutant wall time is dominated by `dotnet test` rebuilding, not by anything coldsweep does.
- **No Go measurement.** The table entry has unit tests against a parsed sample and no
  end-to-end run behind it, exactly as `tests` had before this.
- `py-bench` is **4 of the profile's 14 shards**, chosen for fast paired suites. The finding
  counts and the $63.30 are not comparable with run 1's 13-shard `tests` row; the mechanism
  results — closure rate, reopen rate, suite health, the merge defect — do not depend on scope.
- `py-bench` audits the code written earlier in this same session, including `syntax.py` and the
  two fixes above. That makes it a fair test of the machinery and a poor test of a *typical*
  module: new code has thinner tests than settled code, so 39 unpinned symbols from four files
  should not be read as a rate.
- Its 15 disputes are unadjudicated. Nothing here says whether they are real work or the
  oscillation guard giving up on findings that a fix agent cannot pin in three attempts.

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

## Run 3's projects

Both were scratch projects written for the measurement and are not retained — they lived in a
session scratchpad of the kind that already cost this document its run-1 per-call JSONL. The
sources are ~40 lines each and are described by symbol in the run 3 section; the part worth
copying is the profile, since nothing else in this document configures a non-Python task:

```yaml
scope: {include: ["src/**/*.cs"]}
editable: {include: ["tests/**/*.cs"]}
fix_scope: task
rules:
  - {id: untested-behaviour, mode: presence, decided_by: code, description: "..."}
  - {id: vacuous-test, mode: absence, description: "..."}
mutation:
  rule_id: untested-behaviour
  test_command: "dotnet test tests/tests.csproj -v q --nologo"
  build_command: "dotnet build src/src.csproj -v q --nologo"
  test_patterns: ["tests/{stem}Tests.cs"]
  stop_at_first_survivor: false
```

`test_command` deliberately contains no `{tests}`: `dotnet test` takes a project, not a file
list. `test_patterns` still has to match real files, because a source with no paired test is
`no_tests` rather than a judged shard — and it is what the cache keys on, so with this shape
editing an unpaired test file does not invalidate anything.

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
needs no pytest. `race-profile-paired.yaml` adds `mutation.test_patterns` so each source has its
own predictable test file; it is what shows that the repair preserves parallelism rather than
just serialising everything. The agent command is the stub with `append_flags: false`.

`$BR/repro/run.sh <parallelism>` builds a scratch git repo with four source files and one empty
`tests/test_shared.py`, seeds `findings.jsonl` with four open findings on four distinct anchors,
then runs `coldsweep fix` with `RACE_TARGET` pointing at the shared file:

```sh
run.sh 4 race-profile          # no pairing:  one lane, 4 survive
run.sh 4 race-profile-paired   # paired:      four lanes, 4 survive
git stash push -- src/ && run.sh 4 race-profile   # pre-fix: 1 survives
```

The stub writes to the first file the prompt lists under "Files you may edit", which is what a
real agent does — and when every agent is handed the same list, every agent picks the same file.
That is the whole defect in one line.

Run `run.sh 1` first: it is the control, and all four tests must survive it. If they do not, the
stub or the seed is wrong, not the tool.

## What each run left behind

Every worktree is committed to its own branch rather than deleted, because the first session's
output was lost when its worktree went away unreviewed:

| Branch | What |
|---|---|
| `bench/tests` | run 1 `tests`, round 1 only — 1207 lines of tests, 4 of them failing |
| `bench/features2` | the loop's own cost-accounting implementation, superseded by the hand-written one |
| `bench2/issues`, `bench2/docs`, `bench2/features` | run 2, four rounds each, with `.coldsweep/` state and spend ledgers |

All unreviewed agent output. Read before reusing.

## Cleaning up

```sh
cd /path/to/coldsweep
git worktree remove --force $BR/wt/<t>      # or: git worktree prune
git branch -D bench/<t>                     # after salvaging anything worth keeping
```
