from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HANDOFF, fake_opencode, git, make_repo, run_cli
from opencode_orchestrator import sandbox as sbx
from opencode_orchestrator.events import summarize_stream


class SettingsTests(unittest.TestCase):
    def test_implement_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            run_root = repo / ".opencode-orchestrator" / "runs" / "r1"
            settings = sbx.build_settings(
                profile="implement", worktree=repo, run_root=run_root,
                model="openrouter/deepseek/deepseek-v4-pro", allow_network=False,
                extra_domains=["example.internal"], extra_write=[],
            )
            real_repo = str(repo.resolve())
        network, fs = settings["network"], settings["filesystem"]
        self.assertEqual(network["allowedDomains"], ["models.dev", "openrouter.ai", "example.internal"])
        self.assertNotIn("pypi.org", network["allowedDomains"])
        self.assertTrue(any(path.startswith(real_repo) or path == str(repo) for path in fs["allowWrite"]))
        self.assertIn(str(run_root), fs["denyWrite"])
        self.assertIn("~/.ssh", fs["denyRead"])
        self.assertIn("**/.env", fs["denyRead"])
        self.assertTrue(all(not path.startswith("~") for path in fs["allowWrite"]))

    def test_readonly_cannot_write_worktree_but_keeps_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            settings = sbx.build_settings(
                profile="readonly", worktree=repo, run_root=None, model="opencode/big-pickle",
                allow_network=True, extra_domains=[], extra_write=[],
            )
            git_dir = sbx.git_path(repo, "--git-dir")
        writes = settings["filesystem"]["allowWrite"]
        self.assertNotIn(str(repo), writes)
        self.assertIn(str(repo), settings["filesystem"]["denyWrite"])
        self.assertIn(git_dir, writes)
        self.assertIn("pypi.org", settings["network"]["allowedDomains"])
        self.assertIn("opencode.ai", settings["network"]["allowedDomains"])

    def test_worktree_shares_common_git_dir_only_for_implement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            linked = Path(tmp) / "linked"
            git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
            common = sbx.git_path(linked, "--git-common-dir")
            implement = sbx.build_settings(
                profile="implement", worktree=linked, run_root=None, model="opencode/x",
                allow_network=False, extra_domains=[], extra_write=[],
            )
            review = sbx.build_settings(
                profile="review", worktree=linked, run_root=None, model="opencode/x",
                allow_network=False, extra_domains=[], extra_write=[],
            )
        self.assertIn(common, implement["filesystem"]["allowWrite"])
        self.assertNotIn(common, review["filesystem"]["allowWrite"])

    def test_unknown_provider_needs_explicit_domain(self) -> None:
        with self.assertRaises(ValueError):
            sbx.provider_domains("acme/model-1")
        with self.assertRaises(ValueError):
            sbx.provider_domains(None)

    def test_run_root_detection(self) -> None:
        execution = Path("/r/.opencode-orchestrator/runs/run-1/opencode-impl-01/execution-01")
        self.assertEqual(sbx.run_root_for(execution), Path("/r/.opencode-orchestrator/runs/run-1"))
        self.assertIsNone(sbx.run_root_for(Path("/tmp/x/execution-01")))

    def test_configured_model_prefers_config_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = sbx.configured_model(
                Path(tmp), {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": "deepseek/v4"})}
            )
        self.assertEqual(model, "deepseek/v4")

    def test_sandbox_block_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {
                    "status": "completed", "input": {"command": "python3 x.py"},
                    "output": "PermissionError: [Errno 1] Operation not permitted: '/x'"}}})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(summarize_stream(path).sandbox_blocks, ["bash: python3 x.py"])


class EnvTemplateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("srt") and sys.platform == "darwin", "needs srt on macOS")
    def test_templates_readable_secrets_blocked_inside_srt(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            names = [".env", ".env.local", ".env.qa", ".env.example", ".env.sample",
                     "sub/.env", "sub/.env.example"]
            for name in names:
                (root / name).write_text("X=1\n", encoding="utf-8")
            settings = sbx.build_settings(
                profile="implement", worktree=root, run_root=None, model="opencode/x",
                allow_network=False, extra_domains=[], extra_write=[],
            )
            path = root / "s.json"
            path.write_text(json.dumps(settings), encoding="utf-8")
            probe = (
                "import os\n"
                f"for f in {names!r}:\n"
                "    try:\n"
                "        open(f).read(); os.stat(f); print(f, 'R')\n"
                "    except OSError:\n"
                "        print(f, 'B')\n"
            )
            out = subprocess.run(
                ["srt", "--settings", str(path), "--", sys.executable, "-c", probe],
                cwd=root, capture_output=True, text=True, timeout=120, check=False,
            ).stdout
        result = dict(line.split() for line in out.splitlines() if line.strip())
        self.assertEqual(
            result,
            {".env": "B", ".env.local": "B", ".env.qa": "B", ".env.example": "R",
             ".env.sample": "R", "sub/.env": "B", "sub/.env.example": "R"},
        )


class DependencyWarningTests(unittest.TestCase):
    def test_missing_node_modules_and_venv_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
            (root / "apps" / "agent").mkdir(parents=True)
            (root / "apps" / "agent" / "pyproject.toml").write_text("", encoding="utf-8")
            (root / "apps" / "agent" / "uv.lock").write_text("", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text("{}", encoding="utf-8")
            (root / "web" / "node_modules").mkdir()
            warnings = sbx.missing_dependencies(root)
        self.assertEqual(
            warnings, ["apps/agent: .venv missing (uv sync)", "frontend: node_modules missing"]
        )

    def test_no_lockfile_means_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(sbx.missing_dependencies(Path(tmp)), [])


class LaunchSandboxTests(unittest.TestCase):
    def test_none_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            binary = fake_opencode(Path(tmp) / "opencode", f"step('stop')\ntext({HANDOFF!r})\n")
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                    "--opencode-bin", str(binary))
            record = json.loads((execution / "exit.json").read_text())
        self.assertEqual(record["sandbox"], {"kind": "none"})

    def test_srt_mode_denies_installs_unless_network_allowed(self) -> None:
        if not sbx.srt_binary():
            self.skipTest("srt not installed")
        script = (
            "perm = json.loads(os.environ['OPENCODE_PERMISSION'])\n"
            "print(json.dumps({'type': 'text', 'sessionID': 's', 'part': {'messageID': 'm', "
            "'text': perm['bash'].get('*pnpm install*', 'absent')}}), flush=True)\n"
            "step('stop')\n"
        )
        results = {}
        for flag in ([], ["--allow-network"]):
            with tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(Path(tmp) / "repo")
                binary = fake_opencode(Path(tmp) / "opencode", script)
                execution = Path(tmp) / "exec"
                execution.mkdir()
                (execution / "prompt.md").write_text("p", encoding="utf-8")
                run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                        "--opencode-bin", str(binary), "--model", "opencode/big-pickle",
                        "--sandbox", "srt", *flag)
                results[bool(flag)] = (execution / "handoff.md").read_text()
        self.assertEqual(results[False], "deny")
        self.assertEqual(results[True], "absent")

    def test_srt_mode_rejects_unknown_provider_before_launch(self) -> None:
        if not sbx.srt_binary():
            self.skipTest("srt not installed")
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            result = run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                             "--model", "acme/m", "--sandbox", "srt")
            self.assertFalse((execution / "events.jsonl").exists())
        self.assertEqual(result.returncode, 2)
        self.assertIn("sandbox-allow-domain", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("srt"), "needs srt on macOS")
    def test_srt_enforces_filesystem_inside_real_sandbox(self) -> None:
        # The fake OpenCode runs inside srt and tries to escape; the launcher records the result.
        script = (
            "import pathlib\n"
            "out = []\n"
            "for name, target in [('home', pathlib.Path.home() / 'oc-sbx-escape.txt'), "
            "('worktree', pathlib.Path('inside.txt'))]:\n"
            "    try:\n"
            "        target.write_text('x'); out.append(name + '=ALLOWED')\n"
            "    except OSError as exc:\n"
            "        out.append(name + '=BLOCKED')\n"
            "emit('tool_use', part={'tool': 'bash', 'state': {'status': 'completed', "
            "'input': {'command': 'escape'}, 'output': ' '.join(out) + "
            "(' Operation not permitted' if 'home=BLOCKED' in out else '')}})\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            binary = fake_opencode(Path(tmp) / "opencode", script)
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            result = run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                             "--opencode-bin", str(binary), "--model", "opencode/big-pickle",
                             "--sandbox", "srt", "--sandbox-allow-write", str(Path(tmp)))
            record = json.loads((execution / "exit.json").read_text())
            escaped = (Path.home() / "oc-sbx-escape.txt").exists()
            inside = (repo / "inside.txt").exists()
            if escaped:
                os.remove(Path.home() / "oc-sbx-escape.txt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record["sandbox"]["kind"], "srt")
        self.assertFalse(escaped)
        self.assertTrue(inside)
        self.assertEqual(record["sandbox_blocks"], ["bash: escape"])


if __name__ == "__main__":
    unittest.main()
