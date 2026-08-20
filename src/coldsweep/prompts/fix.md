You are resolving a specific, closed list of already-identified issues.

Do not audit. Do not look for additional problems. Do not refactor anything not named below.
Anything you change beyond these items makes the next audit round noisier and costs the user
another full cycle.

## Files you may edit

{{files}}

## Rules referenced

{{rules}}

## Findings to resolve

{{findings}}

## How to resolve

- `absence` findings: the quoted evidence must no longer exist in the repository. Removing the
  snippet is not enough on its own — the underlying problem must actually be fixed, otherwise
  the next round re-derives it at a new anchor.
- `presence` findings: add the required artifact, and make it substantive. A docstring that
  restates the signature, or a test that asserts nothing, will be re-reported as vacuous on the
  next round and will cost another cycle.

Make the smallest change that resolves each item. Preserve existing behaviour, style and public
API unless the finding itself is about those.

If a finding is wrong — the code is correct as written, or the rule does not apply here — do not
edit anything for it and mark it `disputed` in your output with a one-sentence reason. Guessing
at a fix for a finding you believe is wrong is worse than disputing it.

## Output

Return a single JSON object and nothing else. No prose before or after, no markdown fence.

```
{"results": [
  {"id": "<finding id exactly as given>",
   "outcome": "fixed" | "disputed",
   "detail": "<one sentence: what you changed, or why the finding is wrong>"}
]}
```

Every finding id listed above must appear exactly once in `results`.
