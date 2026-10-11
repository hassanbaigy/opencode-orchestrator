---
name: opencode-orchestrator-workflow
description: Run the full headless OpenCode orchestration workflow end to end - plan, assign OpenCode agents, verify, close, report.
---

# Workflow

Use this skill for one complete run. This skill owns the lifecycle from planning through the final
report. Use `${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/SKILL.md` for each focused OpenCode-agent
execution, review, or verification cycle.

## Run Initialization

From the target Git worktree, exclude run data locally before creating it:

```bash
REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"
EXCLUDE_FILE="$(git rev-parse --git-path info/exclude)"
grep -qxF '/.opencode-orchestrator/' "$EXCLUDE_FILE" ||
  printf '\n/.opencode-orchestrator/\n' >> "$EXCLUDE_FILE"
grep -qxF '/.opencode-orchestrator/' "$EXCLUDE_FILE"
git check-ignore -q .opencode-orchestrator/.ignore-check
git rev-parse HEAD
git branch --show-current
git status --short --untracked-files=all
opencode --version
srt --version   # OS sandbox; absent means policy-only protection
```

Use only this local exclude; do not edit the tracked `.gitignore`. Record the concise original goal,
`REPO`, full starting HEAD, attached branch when the branch output is nonempty, and exact status
lines as `goal`, `repo`, `repo_head`, optional `repo_branch`, and `repo_status` in `run_started`.
Do not create the run unless both exclude checks succeed. Initially dirty paths are pre-existing
user work; if planned work overlaps them, use an isolated clean worktree or get user direction
rather than claiming those changes.

If `srt` is missing, tell the user that agents will run with policy-only protection and offer
`npm install -g @anthropic-ai/sandbox-runtime`; record their choice as a `decision` when it matters.

Choose models before planning executions. Read
`${CLAUDE_PLUGIN_ROOT}/skills/orchestrate/references/models.md`. Use the model the user named;
otherwise use OpenCode's configured default. Confirm that the provider's credentials are available
(for example `OPENROUTER_API_KEY` for `openrouter/...` models) without printing the secret.

## Full Workflow

1. Inspect the repository and user context to understand the goal and relevant constraints.
2. Perform Run Initialization, create `.opencode-orchestrator/runs/<run-id>/journal.jsonl`, and
   append `run_started` with the concise original goal, absolute repository path, captured Git
   baseline, plugin ref, and available Claude Code and OpenCode versions.
3. Claude turns the goal into a concrete plan with expected deliverables, acceptance criteria,
   risks, and verification paths.
4. Ask an OpenCode agent to review Claude's plan when a second opinion materially reduces risk;
   record that review as a task and focused agent cycle. For a consequential or hard-to-reverse
   design choice, first ask a fresh OpenCode agent (ideally a different model family from the
   implementer) to propose an approach from only the goal, constraints, and acceptance criteria.
   Claude compares the results using evidence rather than agent count and finalizes the plan.
5. Split the finalized plan into active `task` entries with goals, acceptance criteria, and
   allowed/owned `files`. Serialize overlapping work or use isolated worktrees.
6. For each task, use the orchestrate skill to assign or resume an OpenCode agent, capture its
   prompt, events, exit record, and handoff, and independently verify the result. Repeat focused fix
   or review cycles as needed.
7. Record only consequential resolutions or user dependencies as `decision`. Append a terminal
   `task` entry only after its acceptance criteria have been evaluated.
8. When every task is terminal, re-read the complete journal and inspect the final repository state
   and diff.
9. Run the descriptive close check:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/opencode_orch_tools.py" validate \
  .opencode-orchestrator/runs/<run-id>
```

10. Resolve omissions that can be corrected by appending, and inspect every non-passing
    verification. Never rewrite journal history. If a duplicate identity or another structural
    conflict cannot be corrected by appending, retain the run and start a successor as defined by
    the orchestration contract. Otherwise append one final `run_closed` entry with
    `judgment: passed|blocked`, the exact validation result, unresolved risks, and follow-ups.
    Validation detects omissions; Claude decides acceptance.
11. After `run_closed`, invoke `${CLAUDE_PLUGIN_ROOT}/skills/report/SKILL.md` to create `report.md`
    once.

The canonical close sequence is `validate → run_closed → report.md`. Validation never decides
acceptance, and the final report never repairs or rewrites journal history.
