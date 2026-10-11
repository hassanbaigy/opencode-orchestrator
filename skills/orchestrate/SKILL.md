---
name: opencode-orchestrator-orchestrate
description: Run a focused headless OpenCode-agent execution, monitoring, review, or verification phase.
---

# Claude-OpenCode Orchestration

Claude coordinates and verifies focused agent work. OpenCode handles scoped implementation and
review headlessly through `opencode run`, with any model OpenCode can reach (DeepSeek, GLM, Kimi,
Gemini, GPT, local models, ...). Prefer OpenCode as the first mover for bounded coding tasks.

Use this skill for one focused agent cycle inside an orchestration run. The workflow skill owns
planning, run initialization, task decomposition, closure, and reporting. Return to it when the
focused phase is complete.

## Durable Run

Keep run material under:

```text
.opencode-orchestrator/runs/<run-id>/
  journal.jsonl
  <provider>-<role>-<NN>/execution-<NN>/
    prompt.md
    events.jsonl
    stderr.log
    exit.json
    handoff.md
  evidence/                 # optional
  report.md                 # after run_closed
```

`journal.jsonl` is Claude's append-only orchestration journal. Read
`${CLAUDE_PLUGIN_ROOT}/docs/orchestration-contract.md` before creating or interpreting journal
entries; it owns record fields, authority, validation, and closure semantics.

Capture each execution's exact prompt, raw events, exit record, and exact handoff. Always launch
through the bundled `launch` command, which captures all of them. Never synthesize a log or rewrite
a handoff. Keep small observations inline and create `evidence/` only when material output must be
retained.

## Focused Agent Cycle

1. Read the complete journal, current task, and relevant references before acting.
2. Confirm the task's acceptance criteria and allowed/owned `files`.
3. Compare active task files and shared resources before parallel work; serialize overlap or use a
   worktree.
4. Reuse a relevant agent or create a named one (`opencode-impl-01`, `opencode-review-01`, ...).
   For an independent review, start a fresh agent and native session, preferably on a different
   model family from the implementer.
5. Give the agent feedback: pass `--setup` for dependency installs (sandboxed agents cannot install)
   and `--check` for the task's mechanical acceptance commands (types, lint, scoped tests), usually
   with `--fix-rounds 2`. Resolve the execution's absolute worktree, full HEAD, and attached branch when present and
   include them in its record. Write the prompt ending with the block from
   `references/handoff-prompt.md`, save it exactly, and append `execution` before launch.
6. Launch with the bundled `launch` command in the background and wait for its completion
   notification, or watch with `monitor`. Do not edit files owned by the active agent. If the
   prompt turns out to be wrong mid-flight, decide between letting it finish and resuming with a
   correction, or `stop` it (see `references/monitoring.md`).
7. Inspect `exit.json`, the exact handoff, and the repository, then append terminal
   `execution_result`.
8. Evaluate acceptance criteria and record material checks as `verification`.
9. Record only consequential resolutions or user dependencies as `decision`.
10. After evaluating the criteria, append `complete` when they are satisfied, `failed` when they
    are conclusively unmet and no in-scope recovery remains, or `blocked` when a user or external
    dependency prevents completion or judgment. Otherwise keep the task `active` and return the
    unresolved work to the workflow.

Routine bounded work needs OpenCode implementation plus Claude verification. Add a fresh reviewer
only for material risk or a distinct unresolved question; do not repeat identical reviews.

## Safety Boundary

Two layers protect the machine. The launcher always applies a permission policy through
`OPENCODE_PERMISSION` (file tools stay in the worktree, `.env` files are unreadable, network tools
and outward-facing shell commands such as `git push`, `gh`, publishing, cloud CLIs, `sudo`, `ssh`,
`docker`, `curl`/`wget` are denied). When Anthropic's sandbox-runtime (`srt`) is installed, it also
runs OpenCode inside an OS sandbox (Seatbelt on macOS, bubblewrap on Linux): writes only to the
worktree, its Git metadata, OpenCode's data, tool caches and temp; no reads of credential stores or
`.env` files; network only to the model provider's API and `models.dev`. `--sandbox auto` (the
default) uses it whenever `srt` is on `PATH`; check `exit.json.sandbox.kind` and treat `none` as
policy-only protection. Use `--allow-network` (package registries) or `--sandbox-allow-domain` only
with explicit user authorization. Never pass secrets in prompts; when a provider key must come from
a secret manager, inject it into the launch command's environment only.

## Reference Map

Read only what the current phase needs:

- `references/monitoring.md`: launching, resuming, exit records, monitoring, and handoffs.
- `references/review.md`: verification and independent review.
- `references/consensus.md`: consensus and decision outcomes.
- `references/compute.md`: parallel ownership, worktrees, and compute gating.
- `references/models.md`: choosing models and variants, credentials, and observed model behavior.
- `references/handoff-prompt.md`: the required handoff block to end every prompt with.
