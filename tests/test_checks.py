from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HANDOFF, fake_opencode, make_repo, run_cli

# First turn writes a bug; a resumed turn (fix prompt on stdin) fixes it.
SELF_FIXING = (
    "fixing = 'Automatic check failure' in prompt\n"
    "if fixing:\n"
    "    assert '--session' in args and 'ses_fake123' in args, args\n"
    "    assert 'exit 1' in prompt and 'BROKEN' in prompt, prompt\n"
    "open('value.txt', 'w').write('ok' if fixing else 'BROKEN')\n"
    f"step('stop')\ntext({HANDOFF!r})\n"
)
CHECK = "python3 -c \"import sys; v=open('value.txt').read(); print(v); sys.exit(v != 'ok')\""


def launch(tmp: Path, script: str, *extra: str):
    repo = make_repo(tmp / "repo")
    binary = fake_opencode(tmp / "opencode", script)
    execution = tmp / "exec"
    execution.mkdir()
    (execution / "prompt.md").write_text("task", encoding="utf-8")
    result = run_cli(
        "launch", "--execution-dir", str(execution), "--worktree", str(repo),
        "--opencode-bin", str(binary), *extra,
    )
    path = execution / "exit.json"
    return result, execution, (json.loads(path.read_text()) if path.exists() else None), repo


class CheckTests(unittest.TestCase):
    def test_failing_check_triggers_a_fix_round_until_it_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, repo = launch(
                Path(tmp), SELF_FIXING, "--check", CHECK, "--fix-rounds", "2"
            )
            fix_prompt = (execution / "fix-round-01.md").read_text()
            sessions = {json.loads(line)["sessionID"]
                        for line in (execution / "events.jsonl").read_text().splitlines()}
            value = (repo / "value.txt").read_text()
            logs = sorted(p.name for p in execution.glob("check-*.log"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record["fix_rounds_used"], 1)
        self.assertEqual(record["check_rounds"], 2)
        self.assertTrue(record["checks_passed"])
        self.assertEqual(record["checks"][0]["exit_code"], 0)
        self.assertNotIn("full_tail", record["checks"][0])
        self.assertIn("Do not delete, skip, or weaken tests", fix_prompt)
        self.assertIn("## Status", fix_prompt)
        self.assertEqual(sessions, {"ses_fake123"})
        self.assertEqual(value, "ok")
        self.assertEqual(logs, ["check-r00-01.log", "check-r01-01.log"])
        self.assertEqual(json.loads(result.stdout)["checks_passed"], True)

    def test_checks_without_fix_rounds_report_failure(self) -> None:
        stubborn = f"open('value.txt', 'w').write('BROKEN')\nstep('stop')\ntext({HANDOFF!r})\n"
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(Path(tmp), stubborn, "--check", CHECK)
            self.assertFalse((execution / "fix-round-01.md").exists())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(record["status"], "complete")
        self.assertFalse(record["checks_passed"])
        self.assertEqual(record["fix_rounds_used"], 0)
        self.assertIn("BROKEN", record["checks"][0]["output_tail"])

    def test_fix_rounds_stop_at_the_limit(self) -> None:
        stubborn = f"open('value.txt', 'w').write('BROKEN')\nstep('stop')\ntext({HANDOFF!r})\n"
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(
                Path(tmp), stubborn, "--check", CHECK, "--fix-rounds", "2"
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(record["fix_rounds_used"], 2)
        self.assertEqual(record["check_rounds"], 3)
        self.assertFalse(record["checks_passed"])

    def test_checks_skipped_when_agent_does_not_finish(self) -> None:
        script = "emit('step_start', part={})\nemit('step_finish', part={'reason': 'length'})\n"
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), script, "--check", CHECK)
        self.assertEqual(record["status"], "incomplete")
        self.assertIsNone(record["checks_passed"])

    def test_check_environment_has_no_secrets(self) -> None:
        check = "python3 -c \"import os,sys; sys.exit('OPENROUTER_API_KEY' in os.environ)\""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            binary = fake_opencode(Path(tmp) / "opencode", f"step('stop')\ntext({HANDOFF!r})\n")
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                    "--opencode-bin", str(binary), "--check", check,
                    env={"OPENROUTER_API_KEY": "sk-or-v1-not-for-checks-123456"})
            record = json.loads((execution / "exit.json").read_text())
        self.assertTrue(record["checks_passed"])

    def test_fix_rounds_require_a_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), "pass", "--fix-rounds", "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--fix-rounds needs", result.stdout)
        self.assertIsNone(record)


class SetupTests(unittest.TestCase):
    def test_setup_runs_before_agent_and_is_recorded(self) -> None:
        script = (
            "assert open('installed.txt').read() == 'yes'\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(
                Path(tmp), script, "--setup", "printf yes > installed.txt"
            )
            log_exists = (execution / "setup-01.log").exists()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record["setup"][0]["exit_code"], 0)
        self.assertTrue(log_exists)

    def test_failing_setup_aborts_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(
                Path(tmp), "raise SystemExit('agent must not run')", "--setup", "exit 7"
            )
            self.assertFalse((execution / "events.jsonl").exists())
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["setup"][0]["exit_code"], 7)
        self.assertIsNone(record)


class SandboxedCheckTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("srt"), "needs srt on macOS")
    def test_checks_run_inside_the_sandbox(self) -> None:
        check = (
            "python3 -c \"import pathlib,sys\n"
            "try:\n    (pathlib.Path.home()/'oc-check-escape.txt').write_text('x'); sys.exit(0)\n"
            "except OSError:\n    sys.exit(3)\""
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(
                Path(tmp), f"step('stop')\ntext({HANDOFF!r})\n", "--check", check,
                "--sandbox", "srt", "--model", "opencode/big-pickle",
            )
            escaped = (Path.home() / "oc-check-escape.txt").exists()
            if escaped:
                (Path.home() / "oc-check-escape.txt").unlink()
        self.assertFalse(escaped)
        self.assertEqual(record["checks"][0]["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
