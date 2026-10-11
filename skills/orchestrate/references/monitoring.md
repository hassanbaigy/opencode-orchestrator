# Launching And Monitoring OpenCode Agents

Use the bundled tools for launch and compact status snapshots. They parse event streams locally; do
not copy raw logs into Claude's context unless a focused inspection is needed for an ambiguous or
failed agent.

```bash
TOOLS="${CLAUDE_PLUGIN_ROOT}/scripts/opencode_orch_tools.py"
```

## Headless OpenCode

Create the next numbered execution under its named agent:

```text
opencode-impl-01/execution-01/
  prompt.md       # written by Claude before launch
  events.jsonl    # raw `opencode run --format json` stdout
  stderr.log      # OpenCode diagnostics (permission notices, provider errors)
  exit.json       # launcher process record
  handoff.md      # exact final assistant message
```

Save `prompt.md` and append `execution` with the absolute `worktree`, full `head`, and attached
`branch` when present before launch. Resolve the recorded Git values from the same path passed to
`--worktree`:

```bash
git -C <worktree> rev-parse --show-toplevel
git -C <worktree> rev-parse HEAD
git -C <worktree> branch --show-current
```

Then launch, normally with the Bash tool's `run_in_background` so Claude is notified on exit:

```bash
EXECUTION_DIR="$REPO/.opencode-orchestrator/runs/<run-id>/opencode-impl-01/execution-01"
python3 "$TOOLS" launch \
  --execution-dir "$EXECUTION_DIR" \
  --worktree <absolute-worktree> \
  --model <provider/model> [--variant high] \
  --profile implement \
  --timeout 3600
```

The launcher feeds `prompt.md` on stdin, sets `OPENCODE_PERMISSION` for the profile, writes the
artifacts above, prints one JSON line, and exits 0 only when the execution is `complete`. It refuses
to reuse an execution directory that already has events, an exit record, or a handoff.

Profiles:

| Profile | Edit tools | Shell | Use |
| --- | --- | --- | --- |
| `implement` | allowed in the worktree | allowed except denied patterns | implementation and fixes |
| `review` | denied | allowed except denied patterns and Git commands that move or rewrite the target | independent review that runs checks |
| `readonly` | denied | inspection allowlist only (`git show/log/diff`, `ls`, `cat`, `grep`, `find`, ...); redirection denied | plan review or design proposals |

Every profile denies reads of `.env` files, file access outside the worktree, network tools, and
outward-facing shell commands. Add patterns with repeatable `--deny '<glob>'`. Use
`--allow-network` only with explicit authorization.

### OS Sandbox

With `srt` installed (`npm install -g @anthropic-ai/sandbox-runtime`), `--sandbox auto` (default)
wraps OpenCode in an OS sandbox and writes the generated policy to `sandbox.json` beside the
execution:

| | implement | review | readonly |
| --- | --- | --- | --- |
| Worktree writes | yes | yes (changes are still flagged) | **no** (OS-enforced) |
| Git metadata | worktree git dir + shared objects/refs | worktree git dir | worktree git dir |
| Network | provider API + `models.dev` | same | same |

Everything else (home directory, other repositories, the orchestration run directory) is
read-only; credential stores (`~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.docker`,
`~/.gnupg`, `~/.netrc`, `~/.npmrc`, `~/.config/gh`, `~/.claude`, ...) and `.env` files are
unreadable. `--allow-network` adds package registries (PyPI, npm, GitHub); add other hosts with
`--sandbox-allow-domain` and writable paths with `--sandbox-allow-write`. A provider without a known
API domain fails before launch until its domain is allowed.

**Install dependencies before launching a sandboxed agent.** Agents cannot do it inside the
sandbox: registries are not allowlisted by default, and `srt` always blocks writes to `.vscode/`,
`.idea/`, `.git/hooks/` and `.git/config`, which many packages ship, so `pnpm install` fails with
`EPERM` even with `--allow-network`. Run the install (`pnpm install`, `uv sync`, ...) in the
worktree yourself first. Under the sandbox without `--allow-network`, the launcher also denies
install commands anywhere in a command line (`pnpm`/`npm`/`yarn`/`bun install`, `npm ci`,
`uv sync`, `pip install` including `python -m pip install`, ...) so a doomed install cannot leave gigabytes of partial `node_modules` behind. The launcher warns on stderr and records
`sandbox.dependency_warnings` when a project has a lockfile but no installed dependencies; in a
monorepo, act only on the projects the task touches. Use `--sandbox srt` to require the
sandbox and `--sandbox none` only when the user accepts policy-only protection.

Never launch `opencode run` without the launcher for orchestrated work: a permission left at `ask`
in the user's OpenCode config is auto-rejected headlessly, and the process still exits 0.

End every prompt with the block in [`handoff-prompt.md`](handoff-prompt.md), which requires
this concise handoff shape:

```markdown
## Status

## Summary

## Files Changed

## Claims / Findings

## Commands Reported

## Caveats / Blockers
```

## Setup, Checks, And Fix Rounds

Give the agent real feedback instead of letting it work blind. Agents without installed dependencies
cannot type-check, lint, or run tests, and ship errors only CI finds.

```bash
python3 "$TOOLS" launch ... \
  --setup "pnpm install --frozen-lockfile --prefer-offline" \
  --check "pnpm exec tsc --noEmit" \
  --check "pnpm exec eslint features/inbox" \
  --check "pnpm exec vitest run features/inbox" \
  --fix-rounds 2
```

- `--setup` (repeatable) runs in the worktree **before** the agent, **outside** the sandbox, with
  your environment, so installs can reach registries. A failing setup aborts before launch with a
  `launch_error`; logs are `setup-NN.log`.
- `--check` (repeatable) runs in the worktree **after** the agent finishes, **inside** the same
  sandbox and without secret-looking environment variables, because agent-written tests must not run
  with your privileges. Logs are `check-rNN-NN.log`; results are in `exit.json.checks` and
  `checks_passed`.
- `--fix-rounds N` resumes the same session with the failing commands and their output tails, up to
  N times, until the checks pass. Each round's exact prompt is saved as `fix-round-NN.md`, its events
  are appended to `events.jsonl`, and `handoff.md` is the final round's message. The fix prompt
  forbids deleting, skipping, or weakening tests or check configuration; still check
  `test_definitions`.

Choose checks that are fast and scoped to the task (the touched package, not the whole monorepo).
The launcher exits 0 only when the agent completed and every check passed.

## Reading The Exit Record

`exit.json.status` is the launcher's classification:

- `complete`: exit code 0 and the last step finished with reason `stop`.
- `incomplete`: exit code 0, but the agent stopped without a final answer. Check
  `permission_rejections`, `finish_reason` (`length`, `content-filter`, `tool-calls`, ...) and
  `stderr.log`. Resume the session with a targeted follow-up, or record the execution as failed or
  blocked.
- `failed`: nonzero exit, `error` event (for example `ProviderAuthError`), timeout, or launch error.
- `stopped`: the orchestrator stopped it with `stop` (see below).

Concurrent OpenCode processes share a local SQLite store and can die at startup with `database is
locked` before emitting any event. The launcher retries only that case (no events, lock message in
`stderr.log`; `--startup-retries`, default 4) and records `startup_retries`; nothing ran, so a retry
cannot duplicate work.

Also check:

- `checks_passed`, `checks`, `fix_rounds_used`: the launcher's own runs of your `--check` commands.
  `false` means the agent finished but the code still fails them; `null` means checks did not run
  because the agent did not finish.

- `test_definitions`: test functions/cases (`def test_*`, `it(`/`test(`, `func Test*`) removed since
  launch without reappearing elsewhere in the diff. Agents sometimes make a suite pass by deleting
  tests and call it "merging" or "simplifying". `likely_rewritten` pairs a removed test with a
  similarly named new test in the same file (usually a deliberate behavior change);
  `removed_without_replacement` is what needs scrutiny. Accept a removal only when the behavior it
  covered genuinely no longer exists, and record that reasoning.
- `sandbox.kind` (`srt` or `none`) and `sandbox_blocks`: tool calls whose output shows the OS sandbox
  refused a file operation or network host. Decide whether the task genuinely needs that access;
  widen the sandbox only with authorization, never by switching it off silently.

- `handoff_ok`, `handoff_missing_headings`, `handoff_empty_sections`: when `handoff_ok` is false,
  resume the same session with the repair request in [`handoff-prompt.md`](handoff-prompt.md);
  never edit `handoff.md`. The agent's work may still be fine, so inspect the repository either
  way.
- `rule_denials`: the agent tried a denied action; decide whether the task needs it.
- `worktree_changed` and `profile_violation`: a `review` or `readonly` execution changed the worktree
  (OpenCode shells can write files even when edit tools are denied). Inspect the diff, restore the
  review worktree, and treat the review's findings with caution.

- `secret_exposure`: names of secret-looking environment variables (`*API_KEY*`, `*TOKEN*`,
  `*SECRET*`, `*PASSWORD*`) whose exact value appears in the events, handoff, or stderr. Values are
  never logged. Treat any entry as a leak: stop sharing the run directory, tell the user, and
  rotate the credential. Key-shaped strings in test fixtures do not trigger it.

If the launcher could not write a handoff, recover the final message from the stream with `state`
or from `opencode export <session-id>`, and record the recovery in the execution result.

## Stop An Execution

Stop an in-flight execution when continuing would waste work or money, for example when its
prompt is known to be wrong or a dependency changed:

```bash
python3 "$TOOLS" stop --execution-dir "$EXECUTION_DIR" --reason "<why>"
```

`stop` signals the launcher recorded in `launcher.pid`, which terminates OpenCode's process group
and writes `exit.json` with `status: "stopped"`, `stop_reason`, and the `session_id`. Record the
execution as `failed` (or `blocked` when an external dependency forced the stop) with the reason as
a caveat. To continue with corrected instructions, resume that `session_id` as the next execution;
the agent keeps the context of its partial work. Prefer letting an execution finish when most of
its work stays valid and the correction is incremental.

## Resume A Session

Resume a relevant idle session as the next execution under the same agent:

```bash
EXECUTION_DIR="$REPO/.opencode-orchestrator/runs/<run-id>/opencode-impl-01/execution-02"
python3 "$TOOLS" launch \
  --execution-dir "$EXECUTION_DIR" \
  --worktree <absolute-worktree-from-previous-execution> \
  --model <same provider/model> \
  --session <session-id> \
  --profile implement
```

Read the absolute `worktree` and `session_id` from the preceding execution (or its `exit.json`).
Inspect the worktree's current HEAD and branch and record them in the new execution; the prior
`head` is a snapshot, so do not check out or reset to it merely because the worktree advanced. A
fresh native session requires a new named agent.

## Agent State And Monitor

Use `state` for a compact agent snapshot (it reads `exit.json` beside the stream when present):

```bash
python3 "$TOOLS" state <session-id> --file <events-jsonl> --json
```

Without an exit record the status is provisional: `active` may mean a step is running or that the
process already stopped. After context loss, call `state` again. Do not persist parser positions in
the journal.

Monitor an active run or explicit stream:

```bash
python3 "$TOOLS" monitor --repo <repo> --run-id <run-id>
python3 "$TOOLS" monitor --log <events-jsonl> --fail-on-agent-failure
```

Always select the target with `--run-id` plus its repository or with `--log`.

The monitor is read-only and emits `opencode_agent_complete`, `opencode_agent_stopped`,
`opencode_agent_incomplete`,
`opencode_agent_failed`, `opencode_agent_unknown`, `opencode_agent_stale`, or `monitor_error`. It
reports terminal states only once an exit record exists. Treat silence and staleness as ambiguous;
inspect the process, handoff, and repository before appending `execution_result`.
