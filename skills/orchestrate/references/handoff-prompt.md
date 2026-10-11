# Handoff Prompt Block

End every `prompt.md` with this block, verbatim. It was tuned against models that otherwise put
their answer above the headings and leave every section empty.

```markdown
## Final response format (required)

Your final message is the handoff. It must start with the line `## Status` and contain exactly
these six sections, in this order, each followed by content. Put all findings inside the sections;
write nothing before `## Status`. Write `None.` for a section with nothing to report.

## Status
complete | blocked | failed, plus one line on why.

## Summary
What you did, in two to five sentences.

## Files Changed
One path per line, or `None.`

## Claims / Findings
Each claim or finding with file:line and concrete evidence.

## Commands Reported
Each command you ran and its observed result (exit code, pass/fail counts).

## Caveats / Blockers
Anything unverified, skipped, risky, or needing a decision, or `None.`
```

When `exit.json.handoff_ok` is false, resume the same session (new execution) with only:

```markdown
Your previous final message did not follow the required format. Reply again with only the
handoff: start with `## Status`, include all six sections in order, and put content under each.
Do not run any tools.
```
