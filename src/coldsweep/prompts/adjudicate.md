Compare two audit findings and decide whether they describe the same underlying work item.

They were produced by independent audits of the same file, so the same problem may be worded
very differently. Judge the underlying item, not the wording.

## Finding A

- rule: {{a_rule}}
- anchor: {{a_anchor}}
- description: {{a_description}}
- evidence:
```
{{a_evidence}}
```

## Finding B

- rule: {{b_rule}}
- anchor: {{b_anchor}}
- description: {{b_description}}
- evidence:
```
{{b_evidence}}
```

## Decision

Answer `same` only if fixing one necessarily fixes the other. If they are two distinct problems
that happen to sit near each other, or you cannot tell, answer `different`.

`different` is the safe answer: it costs one cheap duplicate re-check, while a wrong `same`
silently deletes a real work item.

You are not being asked whether either finding is correct, whether the work is complete, or
whether anything else exists. Only whether these two are the same item.

## Output

Return a single JSON object and nothing else. No prose before or after, no markdown fence.

```
{"verdict": "same" | "different", "reason": "<one sentence>"}
```
