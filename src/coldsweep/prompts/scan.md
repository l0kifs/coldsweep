You are performing an independent audit of a small set of files.

You have no memory of previous audits and no access to previous results. That is deliberate:
your job is to re-derive the complete list of issues from the files alone. Do not assume any
prior work was done, and do not assume the files are already clean.

## Files in scope

{{files}}

Read every file in full before reporting. Report only about these files.

## Rule taxonomy

Report findings ONLY under these rule ids. This vocabulary is closed: if something is wrong
but does not fit a rule below, do not invent a rule id and do not report it.

{{rules}}

{{context}}
## What counts as a finding

Rules are one of two modes.

- `absence` — the file contains something that must not be there. A finding means: this exact
  snippet is present and is wrong. You must quote the offending snippet verbatim in `evidence`.
- `presence` — the file must contain a required artifact, and that artifact must be
  substantive. A finding means: the artifact is **missing**, or it is **present but vacuous**
  (a docstring that restates the signature, a test that asserts nothing, a section header with
  no content). Leave `evidence` null for presence findings.

Report only what is **missing or wrong**. Never report that something "could be better", "might
benefit from", or "should be expanded" — improvements are not findings, and reporting them makes
the audit oscillate forever.

## Anchors

Every finding names an anchor: a stable symbol path, never a line number.

- code: `path/to/module.py::ClassName::method_name`, or `path/to/module.py::function_name`,
  or `path/to/module.py` for a whole-file finding
- docs: `path/to/doc.md::top-heading/sub-heading` using lowercase hyphenated heading text

Use the file path exactly as it appears in the scope list above. Anchors containing line
numbers are rejected.

## Evidence

For `absence` findings, `evidence` is the offending snippet copied verbatim from the file:
enough lines to be unambiguous, and no more. Do not paraphrase it, do not reformat it, do not
add ellipses. Whitespace, comments and quote style are normalized away before comparison.

## Output

Return a single JSON object and nothing else. No prose before or after, no markdown fence.

```
{"findings": [
  {"rule_id": "<one of the rule ids above>",
   "anchor": "<stable symbol path>",
   "evidence": "<verbatim snippet, or null for presence rules>",
   "description": "<one sentence stating what is missing or wrong>"}
]}
```

If the files are clean under every rule, return `{"findings": []}`.

Be exhaustive within these files: report every distinct occurrence separately, even when several
share a rule. Two occurrences at different anchors are two findings.
