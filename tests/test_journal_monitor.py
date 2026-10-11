from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FIXTURES, HANDOFF, run_cli, write_journal

AGENT = "opencode-impl-01"
EXEC = "execution-01"
BASE = f"{AGENT}/{EXEC}"


def execution_entry(**overrides) -> dict:
    entry = {
        "type": "execution", "agent": AGENT, "execution": EXEC, "task": "task-01",
        "provider": "opencode", "role": "implementation", "mode": "headless",
        "event_source": "opencode", "model": "opencode/big-pickle", "profile": "implement",
        "worktree": "/work/repo", "head": "0" * 40,
        "prompt": f"{BASE}/prompt.md", "events": f"{BASE}/events.jsonl",
        "exit": f"{BASE}/exit.json", "handoff": f"{BASE}/handoff.md",
    }
    entry.update(overrides)
    return entry


def closed_run(run_dir: Path) -> None:
    execution = run_dir / AGENT / EXEC
    execution.mkdir(parents=True)
    (execution / "prompt.md").write_text("p", encoding="utf-8")
    (execution / "events.jsonl").write_bytes((FIXTURES / "run_implement_handoff.jsonl").read_bytes())
    (execution / "exit.json").write_text('{"exit_code": 0, "status": "complete"}', encoding="utf-8")
    (execution / "handoff.md").write_text(HANDOFF, encoding="utf-8")
    write_journal(
        run_dir,
        [
            {"type": "run_started", "run_id": "r1", "goal": "g", "repo": "/work/repo",
             "repo_head": "0" * 40, "repo_status": []},
            {"type": "task", "id": "task-01", "status": "active", "goal": "g",
             "acceptance": ["tests pass"], "files": ["a.py"]},
            execution_entry(),
            {"type": "execution_result", "agent": AGENT, "execution": EXEC, "task": "task-01",
             "status": "complete", "session_id": "ses_x", "handoff": f"{BASE}/handoff.md",
             "exit": f"{BASE}/exit.json", "summary": "s", "files_changed": ["a.py"],
             "caveats": []},
            {"type": "verification", "id": "check-01", "task": "task-01",
             "criterion": "tests pass", "method": "command", "check": "true",
             "result": "passed", "observation": "exit 0"},
            {"type": "task", "id": "task-01", "status": "complete"},
        ],
    )


class ValidateTests(unittest.TestCase):
    def test_valid_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closed_run(Path(tmp))
            result = run_cli("validate", tmp)
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, payload)
        self.assertEqual(payload["issues"], [])

    def test_missing_exit_record_is_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closed_run(Path(tmp))
            (Path(tmp) / AGENT / EXEC / "exit.json").unlink()
            payload = json.loads(run_cli("validate", tmp).stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("exit file does not exist" in issue for issue in payload["issues"]))

    def test_in_flight_execution_and_active_task_are_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / AGENT / EXEC).mkdir(parents=True)
            (run_dir / AGENT / EXEC / "prompt.md").write_text("p", encoding="utf-8")
            write_journal(
                run_dir,
                [
                    {"type": "run_started", "run_id": "r1", "goal": "g"},
                    {"type": "task", "id": "task-01", "status": "active"},
                    execution_entry(events=None, exit=None),
                ],
            )
            payload = json.loads(run_cli("validate", tmp).stdout)
        issues = " ".join(payload["issues"])
        self.assertIn("has no terminal execution_result", issues)
        self.assertIn("task task-01 is not terminal", issues)

    def test_non_passing_verification_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            closed_run(Path(tmp))
            with (Path(tmp) / "journal.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "verification", "id": "check-02", "task": "task-01",
                    "criterion": "lint", "check": "ruff", "result": "failed",
                    "observation": "2 errors", "recorded_at": "t",
                }) + "\n")
            payload = json.loads(run_cli("validate", tmp).stdout)
        self.assertEqual(payload["non_passing_verifications"][0]["id"], "check-02")


class MonitorTests(unittest.TestCase):
    def make_inflight(self, tmp: Path, fixture: str, exit_record: dict | None) -> Path:
        repo = tmp / "repo"
        run_dir = repo / ".opencode-orchestrator" / "runs" / "r1"
        execution = run_dir / AGENT / EXEC
        execution.mkdir(parents=True)
        (execution / "prompt.md").write_text("p", encoding="utf-8")
        (execution / "events.jsonl").write_bytes((FIXTURES / fixture).read_bytes())
        if exit_record is not None:
            (execution / "exit.json").write_text(json.dumps(exit_record), encoding="utf-8")
        write_journal(
            run_dir,
            [
                {"type": "run_started", "run_id": "r1", "goal": "g"},
                {"type": "task", "id": "task-01", "status": "active"},
                execution_entry(),
            ],
        )
        return repo

    def monitor(self, repo: Path, *extra: str):
        result = run_cli("monitor", "--repo", str(repo), "--run-id", "r1", "--once", *extra)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, lines

    def test_complete_with_exit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_inflight(
                Path(tmp), "run_implement_handoff.jsonl",
                {"exit_code": 0, "handoff_written": True, "handoff_ok": True},
            )
            result, lines = self.monitor(repo)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines[0]["type"], "opencode_agent_complete")
        self.assertEqual(lines[0]["agent"], AGENT)
        self.assertTrue(lines[0]["exit_record"])

    def test_incomplete_fails_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_inflight(Path(tmp), "run_permission_rejected.jsonl", {"exit_code": 0})
            result, lines = self.monitor(repo, "--fail-on-agent-failure")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(lines[0]["type"], "opencode_agent_incomplete")
        self.assertEqual(lines[0]["permission_rejections"], ["bash: echo hi > x.txt"])

    def test_no_exit_record_stays_active_then_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_inflight(Path(tmp), "run_implement_handoff.jsonl", None)
            _, quiet = self.monitor(repo, "--stale-seconds", "3600")
            events = next((repo / ".opencode-orchestrator").rglob("events.jsonl"))
            old = time.time() - 7200
            os.utime(events, (old, old))
            _, stale = self.monitor(repo, "--stale-seconds", "60")
        self.assertEqual(quiet, [])
        self.assertEqual(stale[0]["type"], "opencode_agent_stale")
        self.assertFalse(stale[0]["exit_record"])

    def test_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_inflight(Path(tmp), "run_provider_auth_error.jsonl", {"exit_code": 1})
            _, lines = self.monitor(repo)
        self.assertEqual(lines[0]["type"], "opencode_agent_failed")
        self.assertIn("ProviderAuthError", lines[0]["error"])

    def test_requires_exactly_one_selector(self) -> None:
        result = run_cli("monitor", "--once")
        self.assertEqual(result.returncode, 1)
        self.assertIn("provide exactly one target", result.stdout)


if __name__ == "__main__":
    unittest.main()
