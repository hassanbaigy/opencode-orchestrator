"""Launch one headless OpenCode execution and capture its prompt-first artifacts.

``opencode run`` has no equivalent of ``codex exec --output-last-message``, and a permission left at
``ask`` is auto-rejected in headless mode while the process still exits 0. This launcher closes both
gaps: it feeds ``prompt.md`` on stdin, captures raw events, writes the final assistant message as
``handoff.md`` and records an ``exit.json`` that lets Claude and the monitor classify the execution.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import sandbox as sbx
from .events import classify, json_dumps, summarize_stream

HANDOFF_HEADINGS = [
    "## Status",
    "## Summary",
    "## Files Changed",
    "## Claims / Findings",
    "## Commands Reported",
    "## Caveats / Blockers",
]

# Bash patterns denied in every profile. These guard against outward-facing or privileged actions;
# they are a policy layer, not an OS sandbox.
BASE_BASH_DENY = [
    "git push*",
    "git remote*",
    "gh *",
    "npm publish*",
    "pnpm publish*",
    "yarn publish*",
    "sudo *",
    "ssh *",
    "scp *",
    "docker *",
    "kubectl *",
    "helm *",
    "terraform *",
    "vercel *",
    "infisical *",
    "aws *",
    "gcloud *",
]
NETWORK_BASH_DENY = ["curl *", "wget *"]
# Keep secrets out of shell reads too; the read-tool rules alone are bypassable with ``cat``.
SECRET_BASH_DENY = ["*.env", "*.env *", "*/.env*", "* .env*", "*printenv*", "env", "env *"]
# Review agents must not move the target they are reviewing.
REVIEW_BASH_DENY = [
    "git commit*",
    "git reset*",
    "git checkout*",
    "git switch*",
    "git stash*",
    "git rebase*",
    "git merge*",
    "git cherry-pick*",
    "git apply*",
    "git restore*",
    "git clean*",
]
# ``readonly`` keeps the bash tool but only for inspection. Denying bash outright removes the tool
# from the request, which OpenCode's free tier rejects ("can only be used from within OpenCode").
READONLY_BASH_ALLOW = [
    "ls*",
    "pwd",
    "tree*",
    "cat *",
    "head *",
    "tail *",
    "wc *",
    "file *",
    "grep *",
    "rg *",
    "find *",
    "git status*",
    "git log*",
    "git show*",
    "git diff*",
    "git blame*",
    "git branch*",
    "git rev-parse*",
    "git ls-files*",
    "git grep*",
]
READONLY_BASH_DENY = ["*>*", "*find * -delete*", "*find * -exec*", "*tee *", "*xargs *"]
# Dependency installs cannot succeed inside the srt sandbox without --allow-network (registries
# are blocked, and srt protects .vscode/ paths many packages ship), yet a failed pnpm install
# still left a 1.5 GB partial node_modules in a real run. Deny them up front instead.
INSTALL_BASH_DENY = [
    # Leading wildcards also catch "cd web && pnpm install" and "python3 -m pip install".
    "*pnpm install*", "pnpm i", "pnpm i *", "*pnpm add *",
    "*npm install*", "npm i", "npm i *", "*npm ci*",
    "yarn", "*yarn install*", "*yarn add *",
    "*bun install*", "*bun add *",
    "*uv sync*", "*uv add *", "*pip install*", "*pip3 install*",
    "*poetry install*",
]
SECRET_READ_DENY = ["*.env", "*.env.*", "**/.env", "**/.env.*"]
SECRET_READ_ALLOW = [
    "*.env.example", "**/.env.example", "*.env.sample", "**/.env.sample",
    "*.env.template", "**/.env.template", "*.env.dist", "**/.env.dist",
]

PROFILES = ("implement", "review", "readonly")


def build_permission(
    profile: str, *, allow_network: bool, extra_deny: List[str], deny_installs: bool = False
) -> Dict[str, object]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}")

    read: Dict[str, str] = {"*": "allow"}
    for pattern in SECRET_READ_DENY:
        read[pattern] = "deny"
    for pattern in SECRET_READ_ALLOW:
        read[pattern] = "allow"

    rules: Dict[str, str] = {"*": "deny" if profile == "readonly" else "allow"}
    if profile == "readonly":
        for pattern in READONLY_BASH_ALLOW:
            rules[pattern] = "allow"
    # Denies come after allows: OpenCode applies the last matching rule.
    deny = list(BASE_BASH_DENY) + SECRET_BASH_DENY
    if not allow_network:
        deny += NETWORK_BASH_DENY
    if profile == "review":
        deny += REVIEW_BASH_DENY
    if profile == "readonly":
        deny += READONLY_BASH_DENY
    if deny_installs:
        deny += INSTALL_BASH_DENY
    deny += extra_deny
    for pattern in deny:
        rules[pattern] = "deny"
    bash = rules

    network = "allow" if allow_network else "deny"
    return {
        "read": read,
        "edit": "allow" if profile == "implement" else "deny",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": bash,
        "task": "allow",
        "todoread": "allow",
        "todowrite": "allow",
        "lsp": "allow",
        "skill": "allow",
        "webfetch": network,
        "websearch": network,
        "codesearch": network,
        "external_directory": "deny",
        "doom_loop": "deny",
        "question": "deny",
    }


REDACT_KEY_PATTERN = re.compile(r"(key|token|secret|password|authorization)", re.IGNORECASE)


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if REDACT_KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def deep_merge(base: Dict[str, object], extra: Dict[str, object]) -> Dict[str, object]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def build_config_content(
    inherited: Optional[str], model: Optional[str], model_options: Optional[str]
) -> Optional[Dict[str, object]]:
    """Merge ``--model-options`` into any inherited ``OPENCODE_CONFIG_CONTENT``.

    ``--model-options`` is a JSON object placed at ``provider.<provider>.models.<model>.options``,
    e.g. OpenRouter routing: ``{"provider": {"sort": "price", "quantizations": ["fp8"]}}``.
    """
    config: Dict[str, object] = {}
    if inherited:
        value = json.loads(inherited)
        if not isinstance(value, dict):
            raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        config = value
    if model_options:
        if not model or "/" not in model:
            raise ValueError("--model-options requires --model in provider/model form")
        options = json.loads(model_options)
        if not isinstance(options, dict):
            raise ValueError("--model-options must be a JSON object")
        provider_id, model_id = model.split("/", 1)
        config = deep_merge(
            config, {"provider": {provider_id: {"models": {model_id: {"options": options}}}}}
        )
    return config or None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opencode_version(binary: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = result.stdout.strip().splitlines()
    return version[-1] if result.returncode == 0 and version else None


def git_value(worktree: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def git_status_lines(worktree: Path) -> Optional[List[str]]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def worktree_fingerprint(worktree: Path) -> Optional[str]:
    """Hash of HEAD, every tracked change against HEAD, and every untracked file's content.

    ``git status`` only says a path is modified, so an edit to an already-dirty file leaves it
    unchanged; the fingerprint changes on any content edit.
    """
    digest = hashlib.sha256()
    try:
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, timeout=60, check=False,
        )
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff"],
            capture_output=True, timeout=300, check=False,
        )
        untracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or diff.returncode != 0 or untracked.returncode != 0:
        return None
    digest.update(head.stdout)
    digest.update(diff.stdout)
    for name in sorted(filter(None, untracked.stdout.split(b"\0"))):
        digest.update(b"\0" + name + b"\0")
        try:
            digest.update(hashlib.sha256((worktree / os.fsdecode(name)).read_bytes()).digest())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def handoff_shape(handoff: str) -> Dict[str, object]:
    """Describe how far a handoff is from the required six-section shape.

    Weaker models sometimes write their answer above the headings and leave every section empty,
    so heading presence alone is not enough.
    """
    lines = handoff.splitlines()
    positions = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped in HANDOFF_HEADINGS and stripped not in positions:
            positions[stripped] = index
    missing = [heading for heading in HANDOFF_HEADINGS if heading not in positions]
    ordered = sorted(positions.items(), key=lambda item: item[1])
    empty = []
    for number, (heading, start) in enumerate(ordered):
        end = ordered[number + 1][1] if number + 1 < len(ordered) else len(lines)
        if not any(line.strip() for line in lines[start + 1 : end]):
            empty.append(heading)
    preamble = bool(ordered) and any(line.strip() for line in lines[: ordered[0][1]])
    return {
        "handoff_missing_headings": missing,
        "handoff_empty_sections": empty,
        "handoff_has_preamble": preamble,
        "handoff_ok": not missing and not empty,
    }


def build_command(args: argparse.Namespace, binary: str, worktree: Path) -> List[str]:
    command = [binary, "run", "--format", "json", "--dir", str(worktree)]
    if args.model:
        command += ["--model", args.model]
    if args.variant:
        command += ["--variant", args.variant]
    if args.agent:
        command += ["--agent", args.agent]
    if args.session:
        command += ["--session", args.session]
    title = args.title or f"{Path(args.execution_dir).parent.name}/{Path(args.execution_dir).name}"
    command += ["--title", title]
    if args.thinking:
        command.append("--thinking")
    return command


SECRET_ENV_PATTERN = re.compile(r"(API_KEY|APIKEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
MIN_SECRET_LENGTH = 16


def exposed_secrets(env: Dict[str, str], paths: List[Path]) -> List[str]:
    """Names of secret-looking environment variables whose exact value appears in run output.

    Only names are returned; values are never logged. Short values are skipped to avoid noise.
    """
    secrets = {
        name: value
        for name, value in env.items()
        if SECRET_ENV_PATTERN.search(name) and len(value) >= MIN_SECRET_LENGTH
    }
    if not secrets:
        return []
    found = set()
    for path in paths:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        for name, value in secrets.items():
            if value.encode("utf-8", "replace") in content:
                found.add(name)
    return sorted(found)


TEST_DEFINITION_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\("),  # Python
    re.compile(r"^\s*(?:it|test)(?:\.\w+)?\s*\(\s*(['\"`])(.+?)\1"),  # JS/TS
    re.compile(r"^\s*func\s+(Test\w+)\s*\("),  # Go
]


def test_definition_names(line: str) -> List[str]:
    for pattern in TEST_DEFINITION_PATTERNS:
        match = pattern.search(line)
        if match:
            return [match.group(match.lastindex or 1)]
    return []


def test_definition_changes(worktree: Path, base: Optional[str]) -> Optional[Dict[str, object]]:
    """Test definitions removed or added between ``base`` and the current worktree.

    Agents sometimes make a suite pass by deleting tests and describe it as merging or
    simplifying. A definition counts as removed only if its name does not reappear anywhere in
    the diff, so moves and renames-in-place are not flagged.
    """
    if not base:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--unified=0", "--no-color", base, "--"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    removed: List[str] = []
    added: List[str] = []
    current_file = ""
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            current_file = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("-"):
            removed += [f"{current_file}::{name}" for name in test_definition_names(line[1:])]
        elif line.startswith("+"):
            added += [f"{current_file}::{name}" for name in test_definition_names(line[1:])]
    # New files are invisible to ``git diff``; scan untracked (non-ignored) files as additions.
    try:
        untracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True, timeout=120, check=False,
        )
        untracked_names = (
            [os.fsdecode(name) for name in untracked.stdout.split(b"\0") if name]
            if untracked.returncode == 0
            else []
        )
    except (OSError, subprocess.SubprocessError):
        untracked_names = []
    for name in untracked_names:
        try:
            text = (worktree / name).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            added += [f"{name}::{test}" for test in test_definition_names(line)]
    added_names = {entry.split("::", 1)[1] for entry in added}
    gone = [entry for entry in removed if entry.split("::", 1)[1] not in added_names]
    # A removed test with a similarly named new test in the same file is most likely rewritten
    # (behavior changed on purpose); report those separately so real deletions stand out.
    new_entries = [entry for entry in added if entry not in removed]
    renames = []
    unmatched = []
    # One-to-one, best pairs first, so one new test cannot explain away several deletions.
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, entry.split("::", 1)[1], other.split("::", 1)[1]).ratio(),
             entry, other)
            for entry in gone
            for other in new_entries
            if entry.split("::", 1)[0] == other.split("::", 1)[0]
        ),
        reverse=True,
    )
    paired_removed = set()
    paired_added = set()
    for ratio, entry, other in scored:
        if ratio < 0.6 or entry in paired_removed or other in paired_added:
            continue
        paired_removed.add(entry)
        paired_added.add(other)
        renames.append({"removed": entry, "added": other, "similarity": round(ratio, 2)})
    unmatched = [entry for entry in gone if entry not in paired_removed]
    return {
        "removed": len(gone),
        "added": len(added),
        "removed_names": gone[:50],
        "likely_rewritten": renames[:50],
        "removed_without_replacement": unmatched[:50],
    }


STARTUP_LOCK_MARKERS = ("database is locked", "SQLITE_BUSY")


def is_startup_lock_failure(
    exit_code: Optional[int],
    events: Path,
    stderr_path: Path,
    events_offset: int = 0,
    stderr_offset: int = 0,
) -> bool:
    if exit_code in (0, None):
        return False
    try:
        if events.stat().st_size > events_offset:
            return False
        diagnostics = stderr_path.read_bytes()[stderr_offset:].decode("utf-8", "replace")
    except OSError:
        return False
    return any(marker in diagnostics for marker in STARTUP_LOCK_MARKERS)


HANDOFF_BLOCK = """## Final response format (required)

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
Anything unverified, skipped, risky, or needing a decision, or `None.`"""

CHECK_OUTPUT_TAIL = 6000


def tail_text(path: Path, limit: int = CHECK_OUTPUT_TAIL) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def run_command(
    argv: List[str], cwd: Path, env: Dict[str, str], timeout: int, log_path: Path
) -> Dict[str, object]:
    """Run one setup or check command, killing its whole process group on timeout."""
    started = time.monotonic()
    exit_code: Optional[int] = None
    timed_out = False
    error: Optional[str] = None
    try:
        with log_path.open("wb") as out:
            process = subprocess.Popen(
                argv, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout if timeout > 0 else None)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate(process)
                exit_code = process.returncode
    except OSError as exc:
        error = str(exc)
    result: Dict[str, object] = {
        "exit_code": exit_code,
        "seconds": round(time.monotonic() - started, 1),
        "timed_out": timed_out,
        "log": log_path.name,
        "output_tail": tail_text(log_path, 2000),
    }
    if error:
        result["error"] = error
    return result


def build_fix_prompt(failures: List[Dict[str, object]], round_number: int, rounds: int) -> str:
    parts = [
        f"Automatic check failure (fix round {round_number} of {rounds}). After your last turn "
        "these commands were run in the worktree and failed:",
        "",
    ]
    for failure in failures:
        parts += [
            f"### `{failure['command']}` (exit {failure['exit_code']}"
            + (", timed out" if failure.get("timed_out") else "") + ")",
            "```",
            str(failure.get("full_tail") or failure.get("output_tail") or "").rstrip(),
            "```",
            "",
        ]
    parts += [
        "Fix the causes in the code. Do not delete, skip, or weaken tests or checks, and do not "
        "change lint, type-check, or test configuration to make them pass. Re-run the failing "
        "commands yourself to confirm, then reply with the handoff.",
        "",
        HANDOFF_BLOCK,
        "",
    ]
    return "\n".join(parts)


def signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (OSError, ProcessLookupError):
        try:
            process.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


def terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait()


def command_launch(args: argparse.Namespace) -> int:
    execution_dir = Path(args.execution_dir).expanduser().resolve()
    prompt = execution_dir / "prompt.md"
    events = execution_dir / "events.jsonl"
    stderr_path = execution_dir / "stderr.log"
    handoff = execution_dir / "handoff.md"
    exit_path = execution_dir / "exit.json"
    worktree = Path(args.worktree).expanduser().resolve()

    def fail(message: str) -> int:
        print(json_dumps({"type": "launch_error", "message": message, "path": str(execution_dir)}))
        return 2

    if not prompt.is_file() or prompt.stat().st_size == 0:
        return fail("prompt.md is missing or empty; save the exact prompt before launch")
    for existing in (events, handoff, exit_path):
        if existing.exists():
            return fail(
                f"{existing.name} already exists; executions are immutable, "
                "use the next execution-NN directory"
            )
    if not worktree.is_dir():
        return fail(f"worktree does not exist: {worktree}")
    binary = shutil.which(args.opencode_bin) or args.opencode_bin
    if shutil.which(binary) is None and not Path(binary).is_file():
        return fail(f"opencode binary not found: {args.opencode_bin}")

    if args.profile not in PROFILES:
        return fail(f"unknown profile {args.profile!r}")
    if args.fix_rounds and not args.check:
        return fail("--fix-rounds needs at least one --check command")

    # Setup (e.g. dependency installs) runs before the agent, outside the sandbox, with the
    # caller's environment: sandboxed agents cannot reach package registries.
    setup_results: List[Dict[str, object]] = []
    for number, setup_command in enumerate(args.setup or [], start=1):
        result = run_command(
            ["/bin/sh", "-c", setup_command], worktree, dict(os.environ), args.check_timeout,
            execution_dir / f"setup-{number:02d}.log",
        )
        result["command"] = setup_command
        setup_results.append(result)
        if result["exit_code"] != 0:
            print(
                json_dumps(
                    {"type": "launch_error", "message": f"setup command failed: {setup_command}",
                     "setup": setup_results, "path": str(execution_dir)}
                )
            )
            return 2
    command = build_command(args, binary, worktree)
    env = dict(os.environ)
    try:
        config_content = build_config_content(
            env.get("OPENCODE_CONFIG_CONTENT"), args.model, args.model_options
        )
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        return fail(f"invalid OpenCode config: {exc}")
    if config_content is not None:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config_content)

    sandbox_record: Dict[str, object] = {"kind": "none"}
    srt_prefix: List[str] = []
    mode = args.sandbox
    srt = sbx.srt_binary()
    if mode == "auto":
        mode = "srt" if srt and sbx.sandbox_supported() else "none"
        if mode == "none":
            sandbox_record["warning"] = (
                "no OS sandbox: install @anthropic-ai/sandbox-runtime (srt) on macOS or Linux"
            )
    if mode == "srt":
        if not srt:
            return fail("--sandbox srt requested but srt is not installed")
        model = args.model or sbx.configured_model(worktree, env)
        try:
            settings = sbx.build_settings(
                profile=args.profile,
                worktree=worktree,
                run_root=sbx.run_root_for(execution_dir),
                model=model,
                allow_network=args.allow_network,
                extra_domains=args.sandbox_allow_domain or [],
                extra_write=args.sandbox_allow_write or [],
                skip_provider_lookup=bool(args.sandbox_allow_domain) and not model,
            )
        except ValueError as exc:
            return fail(f"sandbox: {exc}")
        settings_path = execution_dir / "sandbox.json"
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        srt_prefix = [srt, "--settings", str(settings_path), "--"]
        command = [*srt_prefix, *command]
        dependency_warnings = sbx.missing_dependencies(worktree)
        if dependency_warnings:
            print(
                json_dumps(
                    {
                        "type": "launch_warning",
                        "message": "dependencies not installed; agents cannot install them "
                        "inside the sandbox. Install them outside the sandbox first.",
                        "projects": dependency_warnings[:8],
                        "more": max(0, len(dependency_warnings) - 8),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
        sandbox_record = {
            # Monorepos list many projects; only the ones the task touches matter.
            "dependency_warnings": dependency_warnings[:20],
            "kind": "srt",
            "version": sbx.srt_version(srt),
            "settings": settings_path.name,
            "allowed_domains": settings["network"]["allowedDomains"],  # type: ignore[index]
            "worktree_writable": args.profile != "readonly",
        }

    try:
        permission = build_permission(
            args.profile,
            allow_network=args.allow_network,
            extra_deny=args.deny or [],
            deny_installs=sandbox_record.get("kind") == "srt" and not args.allow_network,
        )
    except ValueError as exc:
        return fail(str(exc))
    env["OPENCODE_PERMISSION"] = json.dumps(permission)

    record: Dict[str, object] = {
        "argv": command,
        "sandbox": sandbox_record,
        "setup": setup_results,
        "profile": args.profile,
        "allow_network": bool(args.allow_network),
        "model": args.model,
        "variant": args.variant,
        "agent": args.agent,
        "resumed_session": args.session,
        # OPENCODE_PERMISSION overrides any "permission" block here (verified with OpenCode 1.18).
        "opencode_config_content": redact(config_content) if config_content else None,
        "worktree": str(worktree),
        "head_at_launch": git_value(worktree, "rev-parse", "HEAD"),
        "git_status_before": git_status_lines(worktree),
        "fingerprint_before": worktree_fingerprint(worktree),
        "opencode_version": opencode_version(binary),
        "started_at": now_iso(),
    }
    pid_path = execution_dir / "launcher.pid"
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": record["started_at"]}) + "\n",
        encoding="utf-8",
    )
    # ``stop`` sends SIGTERM to this launcher. The handler only signals OpenCode's process group:
    # waiting on the child here could deadlock with the main thread's Popen.wait lock.
    current: Dict[str, object] = {"process": None, "stopped": False}

    def on_stop(signum: int, frame: object) -> None:
        current["stopped"] = True
        process = current["process"]
        if isinstance(process, subprocess.Popen) and process.poll() is None:
            signal_group(process, signal.SIGTERM)
            killer = threading.Timer(15.0, signal_group, args=(process, signal.SIGKILL))
            killer.daemon = True
            killer.start()

    previous_handler = signal.signal(signal.SIGTERM, on_stop)
    started = time.monotonic()
    state: Dict[str, object] = {"timed_out": False, "startup_retries": 0}
    deadline = started + args.timeout if args.timeout > 0 else None

    def run_agent(agent_command: List[str], stdin_path: Path, append: bool) -> Optional[int]:
        retries = 0
        while True:
            events_offset = events.stat().st_size if append and events.exists() else 0
            stderr_offset = stderr_path.stat().st_size if append and stderr_path.exists() else 0
            mode = "ab" if append else "wb"
            try:
                with stdin_path.open("rb") as stdin, events.open(mode) as stdout, stderr_path.open(
                    mode
                ) as stderr:
                    process = subprocess.Popen(
                        agent_command,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        cwd=str(worktree),
                        env=env,
                        start_new_session=True,
                    )
                    current["process"] = process
                    if current["stopped"]:
                        signal_group(process, signal.SIGTERM)
                    remaining = None if deadline is None else max(1.0, deadline - time.monotonic())
                    try:
                        code = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        state["timed_out"] = True
                        terminate(process)
                        code = process.returncode
                    except KeyboardInterrupt:
                        terminate(process)
                        raise
            except OSError as exc:
                record["launch_error"] = str(exc)
                return None
            if (
                state["timed_out"]
                or current["stopped"]
                or retries >= args.startup_retries
                or not is_startup_lock_failure(
                    code, events, stderr_path, events_offset, stderr_offset
                )
            ):
                state["startup_retries"] = int(state["startup_retries"]) + retries  # type: ignore[arg-type]
                return code
            # Concurrent OpenCode processes contend for its local SQLite store. The run died
            # before emitting any event, so nothing happened and a retry is safe.
            retries += 1
            time.sleep(min(30.0, 2.0 * retries + random.uniform(0, 2.0)))

    # Checks run inside the same sandbox (agent-written tests must not run with full user
    # privileges) and without secret-looking environment variables.
    check_env = {k: v for k, v in env.items() if not SECRET_ENV_PATTERN.search(k)}

    def run_checks(round_number: int) -> List[Dict[str, object]]:
        results = []
        for number, check_command in enumerate(args.check or [], start=1):
            log_path = execution_dir / f"check-r{round_number:02d}-{number:02d}.log"
            result = run_command(
                [*srt_prefix, "/bin/sh", "-c", check_command], worktree, check_env,
                args.check_timeout, log_path,
            )
            result["command"] = check_command
            if result["exit_code"] != 0:
                result["full_tail"] = tail_text(log_path)
            results.append(result)
        return results

    exit_code = run_agent(command, prompt, append=False)
    check_rounds: List[List[Dict[str, object]]] = []
    fix_rounds_used = 0
    while args.check and exit_code == 0 and not state["timed_out"] and not current["stopped"]:
        current_summary = summarize_stream(events)
        if current_summary.status != "complete":
            break
        results = run_checks(fix_rounds_used)
        check_rounds.append(results)
        failures = [result for result in results if result["exit_code"] != 0]
        if not failures or fix_rounds_used >= args.fix_rounds or not current_summary.session_id:
            break
        fix_rounds_used += 1
        fix_prompt = execution_dir / f"fix-round-{fix_rounds_used:02d}.md"
        fix_prompt.write_text(
            build_fix_prompt(failures, fix_rounds_used, args.fix_rounds), encoding="utf-8"
        )
        resumed = argparse.Namespace(**{**vars(args), "session": current_summary.session_id})
        exit_code = run_agent(
            [*srt_prefix, *build_command(resumed, binary, worktree)], fix_prompt, append=True
        )
    timed_out = bool(state["timed_out"])
    record["startup_retries"] = state["startup_retries"]
    if check_rounds:
        final_checks = check_rounds[-1]
        record["checks"] = [
            {key: value for key, value in result.items() if key != "full_tail"}
            for result in final_checks
        ]
        record["checks_passed"] = all(result["exit_code"] == 0 for result in final_checks)
        record["check_rounds"] = len(check_rounds)
    elif args.check:
        record["checks"] = []
        record["checks_passed"] = None  # the agent did not finish, so checks did not run
    record["fix_rounds_used"] = fix_rounds_used
    signal.signal(signal.SIGTERM, previous_handler)
    if current["stopped"]:
        record["stopped"] = True
        request = execution_dir / "stop_request.json"
        try:
            record["stop_reason"] = json.loads(request.read_text(encoding="utf-8")).get("reason")
        except (OSError, ValueError, AttributeError):
            record["stop_reason"] = None

    summary = summarize_stream(events) if events.is_file() else None
    final_text = summary.final_text if summary is not None else ""
    if final_text.strip():
        handoff.write_text(final_text, encoding="utf-8")

    record.update(
        {
            "finished_at": now_iso(),
            "duration_seconds": round(time.monotonic() - started, 1),
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
    )
    if summary is not None:
        record["session_id"] = summary.session_id
        record["finish_reason"] = summary.finish_reason
        record["steps"] = summary.steps
        record["permission_rejections"] = summary.permission_rejections
        record["rule_denials"] = summary.rule_denials
        record["sandbox_blocks"] = summary.sandbox_blocks
        record["tokens"] = dict(sorted(summary.tokens.items()))
        record["cost"] = round(summary.cost, 6)
        if summary.error is not None:
            record["error"] = summary.error
    record["head_after"] = git_value(worktree, "rev-parse", "HEAD")
    record["git_status_after"] = git_status_lines(worktree)
    # Bash can write files even when the edit tool is denied, so non-implement profiles are
    # checked after the fact rather than trusted.
    record["fingerprint_after"] = worktree_fingerprint(worktree)
    worktree_changed = (
        record["git_status_after"] != record["git_status_before"]
        or record["head_after"] != record["head_at_launch"]
        or record["fingerprint_after"] != record["fingerprint_before"]
    )
    record["worktree_changed"] = worktree_changed
    if args.profile != "implement" and worktree_changed:
        record["profile_violation"] = (
            f"{args.profile} execution changed the worktree; inspect before trusting its findings"
        )
    record["test_definitions"] = test_definition_changes(worktree, record.get("head_at_launch"))
    record["secret_exposure"] = exposed_secrets(env, [events, handoff, stderr_path])
    record["handoff_written"] = handoff.is_file()
    if handoff.is_file():
        record.update(handoff_shape(final_text))
    status = classify(summary, record) if summary is not None else "failed"
    record["status"] = status
    exit_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        pid_path.unlink()
    except OSError:
        pass

    print(
        json_dumps(
            {
                "type": f"opencode_agent_{status}",
                "execution_dir": str(execution_dir),
                "session_id": record.get("session_id"),
                "finish_reason": record.get("finish_reason"),
                "exit_code": exit_code,
                "timed_out": timed_out,
                "handoff_written": record["handoff_written"],
                "handoff_ok": record.get("handoff_ok", False),
                "handoff_missing_headings": record.get("handoff_missing_headings", []),
                "handoff_empty_sections": record.get("handoff_empty_sections", []),
                "permission_rejections": record.get("permission_rejections", []),
                "rule_denials": record.get("rule_denials", []),
                "worktree_changed": record.get("worktree_changed"),
                "profile_violation": record.get("profile_violation"),
                "secret_exposure": record.get("secret_exposure", []),
                "tests_removed": (record.get("test_definitions") or {}).get("removed", 0),
                "tests_removed_without_replacement": len(
                    (record.get("test_definitions") or {}).get("removed_without_replacement", [])
                ),
                "sandbox": sandbox_record.get("kind"),
                "sandbox_blocks": record.get("sandbox_blocks", []),
                "error": record.get("error") or record.get("launch_error"),
                "stop_reason": record.get("stop_reason"),
                "checks_passed": record.get("checks_passed"),
                "fix_rounds_used": fix_rounds_used,
            }
        )
    )
    return 0 if status == "complete" and record.get("checks_passed") is not False else 1


def add_launch_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "launch",
        help="Run one headless OpenCode execution from <execution-dir>/prompt.md.",
    )
    parser.add_argument("--execution-dir", required=True, help="Directory holding prompt.md.")
    parser.add_argument("--worktree", required=True, help="Absolute worktree OpenCode runs in.")
    parser.add_argument("--model", "-m", help="provider/model, e.g. openrouter/deepseek/deepseek-v4-pro.")
    parser.add_argument(
        "--model-options",
        help="JSON merged into provider.<p>.models.<m>.options for --model, e.g. OpenRouter "
        'routing \'{"provider":{"sort":"price","quantizations":["fp8","bf16"]}}\'.',
    )
    parser.add_argument("--variant", help="Provider reasoning effort, e.g. high, max, minimal.")
    parser.add_argument("--agent", help="OpenCode primary agent (default: build).")
    parser.add_argument("--session", help="Resume this native OpenCode session id.")
    parser.add_argument("--title", help="Session title (defaults to agent/execution name).")
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="implement",
        help="Permission profile: implement (edit+bash), review (bash, no edits), readonly.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow webfetch/websearch and curl/wget. Requires explicit user authorization.",
    )
    parser.add_argument(
        "--deny", action="append", help="Extra bash pattern to deny (repeatable), e.g. 'rm -rf *'."
    )
    parser.add_argument(
        "--timeout", type=int, default=3600, help="Kill the run after this many seconds (0 = none)."
    )
    parser.add_argument(
        "--setup",
        action="append",
        help="Shell command run in the worktree before the agent, outside the sandbox "
        "(repeatable), e.g. 'pnpm install --frozen-lockfile --prefer-offline'.",
    )
    parser.add_argument(
        "--check",
        action="append",
        help="Shell command run in the worktree after the agent finishes, inside the sandbox "
        "(repeatable), e.g. 'pnpm exec tsc --noEmit'. Results go to exit.json.",
    )
    parser.add_argument(
        "--fix-rounds",
        type=int,
        default=0,
        help="On failing checks, resume the same session with the failures up to N times.",
    )
    parser.add_argument(
        "--check-timeout", type=int, default=900, help="Seconds per setup/check command."
    )
    parser.add_argument(
        "--sandbox",
        choices=("auto", "srt", "none"),
        default="auto",
        help="OS sandbox: srt (Seatbelt/bubblewrap + domain-allowlist proxy), none, or auto "
        "(srt when installed).",
    )
    parser.add_argument(
        "--sandbox-allow-domain",
        action="append",
        help="Extra network domain the sandbox allows (repeatable), e.g. a custom provider host.",
    )
    parser.add_argument(
        "--sandbox-allow-write",
        action="append",
        help="Extra writable path inside the sandbox (repeatable).",
    )
    parser.add_argument(
        "--startup-retries",
        type=int,
        default=4,
        help="Retries when OpenCode dies at startup on a locked local database (no events).",
    )
    parser.add_argument("--thinking", action="store_true", help="Also capture reasoning events.")
    parser.add_argument("--opencode-bin", default="opencode", help="OpenCode executable.")
    parser.set_defaults(func=command_launch)


def command_stop(args: argparse.Namespace) -> int:
    execution_dir = Path(args.execution_dir).expanduser().resolve()
    exit_path = execution_dir / "exit.json"
    pid_path = execution_dir / "launcher.pid"

    def emit(payload: Dict[str, object], code: int) -> int:
        print(json_dumps({"execution_dir": str(execution_dir), **payload}))
        return code

    if exit_path.exists():
        return emit({"type": "stop_noop", "message": "execution already finished"}, 0)
    try:
        pid = int(json.loads(pid_path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return emit({"type": "stop_error", "message": "no launcher.pid; is it running?"}, 2)
    try:
        command_line = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        command_line = ""
    if "opencode_orch_tools.py" not in command_line or " launch" not in command_line:
        return emit(
            {"type": "stop_error", "message": f"process {pid} is not this execution's launcher"}, 2
        )
    (execution_dir / "stop_request.json").write_text(
        json.dumps({"reason": args.reason, "requested_at": now_iso()}) + "\n", encoding="utf-8"
    )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if exit_path.exists():
            record = json.loads(exit_path.read_text(encoding="utf-8"))
            return emit(
                {"type": "opencode_agent_stopped", "status": record.get("status"),
                 "session_id": record.get("session_id"), "steps": record.get("steps"),
                 "cost": record.get("cost")},
                0,
            )
        time.sleep(0.5)
    return emit({"type": "stop_pending", "message": "stop sent; exit.json not written yet"}, 1)


def add_stop_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "stop", help="Stop an in-flight launch (e.g. its prompt is known to be wrong)."
    )
    parser.add_argument("--execution-dir", required=True, help="Execution directory to stop.")
    parser.add_argument("--reason", required=True, help="Why the execution is being stopped.")
    parser.add_argument("--wait", type=float, default=60.0, help="Seconds to wait for exit.json.")
    parser.set_defaults(func=command_stop)
