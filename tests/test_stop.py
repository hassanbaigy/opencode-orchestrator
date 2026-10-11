from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import HANDOFF, SCRIPT, fake_opencode, make_repo, run_cli


def wait_for(path: Path, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


class StopTests(unittest.TestCase):
    def start(self, tmp: Path, script: str):
        repo = make_repo(tmp / "repo")
        binary = fake_opencode(tmp / "opencode", script)
        execution = tmp / "run" / "opencode-impl-01" / "execution-01"
        execution.mkdir(parents=True)
        (execution / "prompt.md").write_text("long task", encoding="utf-8")
        launcher = subprocess.Popen(
            [sys.executable, str(SCRIPT), "launch", "--execution-dir", str(execution),
             "--worktree", str(repo), "--opencode-bin", str(binary), "--sandbox", "none"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return launcher, execution

    def test_stop_ends_run_and_records_reason(self) -> None:
        script = (
            "open(os.path.join(os.path.dirname(sys.argv[0]), 'child.pid'), 'w')"
            ".write(str(os.getpid()))\n"
            "emit('step_start', part={})\n"
            "time.sleep(120)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            launcher, execution = self.start(Path(tmp), script)
            self.assertTrue(wait_for(execution / "launcher.pid"))
            self.assertTrue(wait_for(Path(tmp) / "child.pid"))
            child = int((Path(tmp) / "child.pid").read_text())
            result = run_cli("stop", "--execution-dir", str(execution), "--reason", "wrong spec")
            launcher.wait(timeout=30)
            record = json.loads((execution / "exit.json").read_text())
            pid_left = (execution / "launcher.pid").exists()
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload["type"], "opencode_agent_stopped")
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["stopped"])
        self.assertEqual(record["stop_reason"], "wrong spec")
        self.assertFalse(pid_left)
        self.assertEqual(launcher.returncode, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_stop_after_finish_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher, execution = self.start(Path(tmp), f"step('stop')\ntext({HANDOFF!r})\n")
            launcher.wait(timeout=30)
            result = run_cli("stop", "--execution-dir", str(execution), "--reason", "late")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["type"], "stop_noop")

    def test_stop_refuses_a_foreign_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp)
            (execution / "launcher.pid").write_text(json.dumps({"pid": os.getpid()}))
            result = run_cli("stop", "--execution-dir", tmp, "--reason", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not this execution's launcher", result.stdout)

    def test_stop_without_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("stop", "--execution-dir", tmp, "--reason", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no launcher.pid", result.stdout)


if __name__ == "__main__":
    unittest.main()
