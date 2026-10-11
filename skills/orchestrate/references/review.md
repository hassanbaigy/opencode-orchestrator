# Review And Verification

Use [the orchestration contract](../../../docs/orchestration-contract.md) for journal fields.

## Verify Agent Work

1. Read the handoff as claims.
1. Inspect the actual diff and changed files. Compare them with the task's declared `files`.
1. Check `exit.json.test_definitions`. A passing suite with removed tests is not a pass: confirm
   each removed test's behavior is genuinely gone, or send the agent back to restore it.
1. Evaluate every acceptance criterion with an observed check. Do not promote `Commands Reported`
   to a passing verification.
1. Launcher check results (`exit.json.checks`) are observed runs, not agent claims. Cite them as
   evidence for a criterion when the command is the right one and the worktree has not changed since
   the execution finished; rerun them otherwise.
1. Record each criterion as a `verification`. When a criterion is mechanically checkable (types,
   tests, lint, build), run the check; install dependencies outside the sandbox if needed. Record
   `passed` only on an observed run. If it cannot be run, record `inconclusive`, name the command
   that would settle it, and carry it into `run_closed.risks`; reading the code is not a type check.
1. On failure, preserve the record, send the exact finding and observation to the relevant agent,
   and record the recheck separately.
1. Mark the task terminal only after all criteria are evaluated.

Base verification on repository state or observed output. Use handoffs and model findings to choose
what to inspect. Keep concise observations inline; use `evidence/` only for lengthy material worth
retaining.

## Independent OpenCode Review

For the first independent review:

- Start a fresh named `opencode-review-NN` agent and native session; never resume the implementation
  session.
- Prefer a different model family from the implementer, so the reviewer does not share its blind
  spots.
- Provide the goal, acceptance criteria, constraints, and exact review target.
- Do not provide the implementer handoff, claimed test results, earlier review verdicts, or Claude's
  tentative conclusion.
- Save the prompt and append the execution before launch. Capture the event stream, exit record and
  exact handoff.

Prefer a detached worktree at the reviewed commit, so the review never touches the implementation
worktree:

```bash
git worktree add --detach ../<repo>-review-<short-sha> <sha>
EXECUTION_DIR="$REPO/.opencode-orchestrator/runs/<run-id>/opencode-review-01/execution-01"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/opencode_orch_tools.py" launch \
  --execution-dir "$EXECUTION_DIR" \
  --worktree <absolute-review-worktree> \
  --model <provider/model> \
  --profile review
```

Write the exact commit SHA and its parent into `prompt.md` and instruct OpenCode to review that
snapshot with `git show <sha>` and not to edit or commit. The `review` profile denies edit tools and
Git commands that move or rewrite the target, but a shell can still write files: check
`exit.json.worktree_changed` and `git -C <review-worktree> status` afterward, and treat a
`profile_violation` as a material caveat.

For an uncommitted review, put the base HEAD SHA and exact reviewed files in `prompt.md`, run the
same launch in that working tree, and reserve those files and shared resources as described in
[`compute.md`](compute.md).

Verify review findings against the repository. Reuse the session for a targeted recheck; start a
fresh reviewer only for a distinct unresolved question.
