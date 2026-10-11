from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opencode_orch_tools.py"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

HANDOFF = (
    "## Status\ncomplete\n\n## Summary\nDone.\n\n## Files Changed\n- a.py\n\n"
    "## Claims / Findings\nNone.\n\n## Commands Reported\n- true\n\n## Caveats / Blockers\nNone."
)


def run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # Unit tests must not depend on whether srt is installed on the host; sandbox tests opt in.
    if args and args[0] == "launch" and "--sandbox" not in args:
        args = (*args, "--sandbox", "none")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
    )


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


def fake_opencode(path: Path, script: str) -> Path:
    """Write a fake ``opencode`` executable whose ``run`` behavior is ``script`` (Python)."""
    body = f"""#!{sys.executable}
import json, os, sys, time
if sys.argv[1:2] == ["--version"]:
    print("0.0.0-fake")
    sys.exit(0)
args = sys.argv[1:]
prompt = sys.stdin.read()
SID = "ses_fake123"
def emit(kind, **fields):
    print(json.dumps({{"type": kind, "timestamp": 0, "sessionID": SID, **fields}}), flush=True)
def text(t, mid="msg_2"):
    emit("text", part={{"type": "text", "messageID": mid, "text": t}})
def step(reason, mid="msg_2"):
    emit("step_start", part={{"type": "step-start", "messageID": mid}})
    emit("step_finish", part={{"type": "step-finish", "messageID": mid, "reason": reason,
         "tokens": {{"total": 10, "input": 7, "output": 3, "cache": {{"read": 1, "write": 0}}}},
         "cost": 0.001}})
{script}
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def write_journal(run_dir: Path, records: list) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "journal.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({"recorded_at": "2026-10-10T00:00:00Z", **record}) + "\n")
