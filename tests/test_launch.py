from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import HANDOFF, fake_opencode, git, make_repo, run_cli
from opencode_orchestrator.launch import build_permission, handoff_shape


def launch(tmp: Path, script: str, *extra: str, prompt: str = "do it"):
    repo = make_repo(tmp / "repo")
    binary = fake_opencode(tmp / "opencode", script)
    execution = tmp / "run" / "opencode-impl-01" / "execution-01"
    execution.mkdir(parents=True)
    (execution / "prompt.md").write_text(prompt, encoding="utf-8")
    result = run_cli(
        "launch",
        "--execution-dir", str(execution),
        "--worktree", str(repo),
        "--opencode-bin", str(binary),
        *extra,
    )
    exit_record = (
        json.loads((execution / "exit.json").read_text())
        if (execution / "exit.json").exists()
        else None
    )
    return result, execution, exit_record, repo


class LaunchTests(unittest.TestCase):
    def test_complete_run_writes_exact_handoff_and_exit_record(self) -> None:
        script = (
            "assert prompt == 'do it'\n"
            "perm = json.loads(os.environ['OPENCODE_PERMISSION'])\n"
            "assert perm['edit'] == 'allow' and perm['external_directory'] == 'deny'\n"
            "assert '--format' in args and 'json' in args\n"
            "emit('step_start', part={'messageID': 'msg_2'})\n"
            f"text({HANDOFF!r})\n"
            "emit('step_finish', part={'messageID': 'msg_2', 'reason': 'stop', "
            "'tokens': {'total': 5}, 'cost': 0.5})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(Path(tmp), script, "--model", "x/y")
            handoff = (execution / "handoff.md").read_text()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(handoff, HANDOFF)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["session_id"], "ses_fake123")
        self.assertTrue(record["handoff_ok"])
        self.assertFalse(record["worktree_changed"])
        self.assertEqual(record["tokens"], {"total": 5})
        self.assertIn("--model", record["argv"])
        self.assertEqual(json.loads(result.stdout)["type"], "opencode_agent_complete")

    def test_permission_auto_rejection_is_incomplete(self) -> None:
        script = (
            "emit('step_start', part={})\n"
            "emit('tool_use', part={'tool': 'bash', 'state': {'status': 'error', "
            "'input': {'command': 'rm x'}, "
            "'error': 'The user rejected permission to use this specific tool call.'}})\n"
            "emit('step_finish', part={'reason': 'tool-calls'})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(Path(tmp), script)
            self.assertFalse((execution / "handoff.md").exists())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["permission_rejections"], ["bash: rm x"])

    def test_nonzero_exit_and_error_event_fail(self) -> None:
        script = (
            "emit('error', error={'name': 'ProviderAuthError', "
            "'data': {'message': 'key missing'}})\n"
            "sys.exit(1)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), script)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], 1)
        self.assertEqual(record["error"], "ProviderAuthError: key missing")

    def test_timeout_kills_the_process(self) -> None:
        script = "emit('step_start', part={})\ntime.sleep(30)\n"
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), script, "--timeout", "1")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["status"], "failed")
        self.assertLess(record["duration_seconds"], 20)

    def test_review_profile_flags_worktree_changes(self) -> None:
        script = (
            "perm = json.loads(os.environ['OPENCODE_PERMISSION'])\n"
            "assert perm['edit'] == 'deny'\n"
            "open('a.py', 'w').write('x = 2\\n')\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), script, "--profile", "review")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(record["worktree_changed"])
        self.assertIn("review execution changed the worktree", record["profile_violation"])
        self.assertEqual(record["git_status_after"], [" M a.py"])

    def test_edit_to_an_already_dirty_file_is_detected(self) -> None:
        # Regression: git status lines stay identical when a modified file is modified again.
        script = (
            "open('a.py', 'a').write('y = 3\\n')\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "a.py").write_text("x = 2\n", encoding="utf-8")  # dirty before launch
            (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
            binary = fake_opencode(Path(tmp) / "opencode", script)
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                    "--opencode-bin", str(binary), "--profile", "review")
            record = json.loads((execution / "exit.json").read_text())
        self.assertEqual(record["git_status_before"], record["git_status_after"])
        self.assertNotEqual(record["fingerprint_before"], record["fingerprint_after"])
        self.assertTrue(record["worktree_changed"])
        self.assertIn("profile_violation", record)

    def test_untracked_content_edit_is_detected(self) -> None:
        from opencode_orchestrator.launch import worktree_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "new.txt").write_text("one", encoding="utf-8")
            before = worktree_fingerprint(repo)
            (repo / "new.txt").write_text("two", encoding="utf-8")
            after = worktree_fingerprint(repo)
            unchanged = worktree_fingerprint(repo)
        self.assertNotEqual(before, after)
        self.assertEqual(after, unchanged)

    def test_review_profile_flags_new_commits(self) -> None:
        script = (
            "import subprocess\n"
            "subprocess.run(['git', '-c', 'user.email=a@b', '-c', 'user.name=a', "
            "'commit', '--allow-empty', '-qm', 'sneaky'], check=True)\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), script, "--profile", "review")
        self.assertNotEqual(record["head_after"], record["head_at_launch"])
        self.assertIn("profile_violation", record)

    def test_refuses_to_reuse_an_execution(self) -> None:
        script = f"step('stop')\ntext({HANDOFF!r})\n"
        with tempfile.TemporaryDirectory() as tmp:
            first, execution, _, repo = launch(Path(tmp), script)
            again = run_cli(
                "launch", "--execution-dir", str(execution), "--worktree", str(repo),
                "--opencode-bin", str(Path(tmp) / "opencode"),
            )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(again.returncode, 2)
        self.assertIn("immutable", again.stdout)

    def test_refuses_missing_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            execution = Path(tmp) / "exec"
            execution.mkdir()
            result = run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn("prompt.md", result.stdout)

    def test_resume_passes_session_and_default_title(self) -> None:
        script = (
            "assert args[args.index('--session') + 1] == 'ses_prev'\n"
            "assert args[args.index('--title') + 1] == 'opencode-impl-01/execution-01'\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), script, "--session", "ses_prev")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(record["resumed_session"], "ses_prev")

    def test_launch_does_not_dirty_the_worktree_when_run_dir_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            exclude = repo / ".git" / "info" / "exclude"
            exclude.write_text("/.opencode-orchestrator/\n", encoding="utf-8")
            binary = fake_opencode(Path(tmp) / "opencode", f"step('stop')\ntext({HANDOFF!r})\n")
            execution = repo / ".opencode-orchestrator" / "runs" / "r1" / "a" / "execution-01"
            execution.mkdir(parents=True)
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            run_cli(
                "launch", "--execution-dir", str(execution), "--worktree", str(repo),
                "--opencode-bin", str(binary), "--profile", "review",
            )
            record = json.loads((execution / "exit.json").read_text())
            self.assertEqual(git(repo, "status", "--porcelain"), "")
        self.assertFalse(record["worktree_changed"])


class ConfigContentTests(unittest.TestCase):
    ROUTING = '{"provider":{"sort":"price","quantizations":["fp8"]}}'

    def test_model_options_merge_with_inherited_config_and_redact_keys(self) -> None:
        script = (
            "cfg = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
            "opts = cfg['provider']['openrouter']['models']['deepseek/deepseek-v4-pro']['options']\n"
            "assert opts['provider']['sort'] == 'price', opts\n"
            "assert cfg['provider']['openrouter']['options']['apiKey'] == 'sk-or-secret-value-123456'\n"
            "assert cfg['share'] == 'disabled'\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        inherited = json.dumps(
            {"share": "disabled",
             "provider": {"openrouter": {"options": {"apiKey": "sk-or-secret-value-123456"}}}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            binary = fake_opencode(Path(tmp) / "opencode", script)
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            result = run_cli(
                "launch", "--execution-dir", str(execution), "--worktree", str(repo),
                "--opencode-bin", str(binary), "--model", "openrouter/deepseek/deepseek-v4-pro",
                "--model-options", self.ROUTING, env={"OPENCODE_CONFIG_CONTENT": inherited},
            )
            record = json.loads((execution / "exit.json").read_text())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recorded = record["opencode_config_content"]
        self.assertEqual(recorded["provider"]["openrouter"]["options"]["apiKey"], "<redacted>")
        self.assertEqual(
            recorded["provider"]["openrouter"]["models"]["deepseek/deepseek-v4-pro"]["options"],
            json.loads(self.ROUTING),
        )
        self.assertNotIn("sk-or-secret-value-123456", json.dumps(record))

    def test_no_config_recorded_when_none_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), f"step('stop')\ntext({HANDOFF!r})\n")
        self.assertIsNone(record["opencode_config_content"])

    def test_invalid_options_are_rejected_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_model, execution, record, _ = launch(
                Path(tmp), "raise SystemExit('must not run')", "--model-options", self.ROUTING
            )
            self.assertFalse((execution / "events.jsonl").exists())
        self.assertEqual(no_model.returncode, 2)
        self.assertIn("requires --model", no_model.stdout)
        self.assertIsNone(record)
        with tempfile.TemporaryDirectory() as tmp:
            bad_json, _, _, _ = launch(
                Path(tmp), "raise SystemExit('must not run')",
                "--model", "a/b", "--model-options", "{not json",
            )
        self.assertEqual(bad_json.returncode, 2)
        self.assertIn("invalid OpenCode config", bad_json.stdout)


class TestDefinitionTests(unittest.TestCase):
    def test_deleted_tests_are_flagged_but_moves_are_not(self) -> None:
        from opencode_orchestrator.launch import test_definition_changes

        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "test_a.py").write_text(
                "def test_keep():\n    pass\n\ndef test_moved():\n    pass\n\n"
                "def test_gone():\n    pass\n",
                encoding="utf-8",
            )
            (repo / "b.test.ts").write_text("it('handles empty', () => {})\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "tests")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "test_a.py").write_text("def test_keep():\n    pass\n", encoding="utf-8")
            (repo / "test_b.py").write_text(
                "def test_moved():\n    pass\n\ndef test_new():\n    pass\n", encoding="utf-8"
            )
            (repo / "b.test.ts").write_text("", encoding="utf-8")
            git(repo, "add", "-A")
            changes = test_definition_changes(repo, base)
        self.assertEqual(changes["removed"], 2)
        self.assertEqual(
            sorted(changes["removed_names"]),
            ["b.test.ts::handles empty", "test_a.py::test_gone"],
        )
        self.assertEqual(changes["added"], 2)
        self.assertEqual(changes["likely_rewritten"], [])
        self.assertEqual(len(changes["removed_without_replacement"]), 2)

    def test_one_new_test_cannot_explain_several_deletions(self) -> None:
        from opencode_orchestrator.launch import test_definition_changes

        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "test_b.py").write_text(
                "".join(f"def test_batch_case_{n}():\n    pass\n\n" for n in range(5)),
                encoding="utf-8",
            )
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "t")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "test_b.py").write_text("def test_batch_cases():\n    pass\n", encoding="utf-8")
            changes = test_definition_changes(repo, base)
        self.assertEqual(len(changes["likely_rewritten"]), 1)
        self.assertEqual(len(changes["removed_without_replacement"]), 4)

    def test_rewritten_test_is_paired_with_its_replacement(self) -> None:
        from opencode_orchestrator.launch import test_definition_changes

        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "test_m.py").write_text(
                "def test_where_all_is_just_the_soft_delete_guard():\n    pass\n\n"
                "def test_unrelated_cache_eviction():\n    pass\n",
                encoding="utf-8",
            )
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "t")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "test_m.py").write_text(
                "def test_where_all_includes_soft_delete_and_archived_guard():\n    pass\n",
                encoding="utf-8",
            )
            changes = test_definition_changes(repo, base)
        self.assertEqual(changes["removed"], 2)
        self.assertEqual(
            [pair["removed"] for pair in changes["likely_rewritten"]],
            ["test_m.py::test_where_all_is_just_the_soft_delete_guard"],
        )
        self.assertEqual(
            changes["removed_without_replacement"], ["test_m.py::test_unrelated_cache_eviction"]
        )

    def test_launch_records_test_definitions(self) -> None:
        script = (
            "open('test_x.py', 'w').write('')\n"
            f"step('stop')\ntext({HANDOFF!r})\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "test_x.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "t")
            binary = fake_opencode(Path(tmp) / "opencode", script)
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            result = run_cli("launch", "--execution-dir", str(execution), "--worktree", str(repo),
                             "--opencode-bin", str(binary))
            record = json.loads((execution / "exit.json").read_text())
        self.assertEqual(record["test_definitions"]["removed_names"], ["test_x.py::test_one"])
        self.assertEqual(json.loads(result.stdout)["tests_removed"], 1)


class SecretExposureTests(unittest.TestCase):
    def test_real_env_secret_in_output_is_flagged_by_name_only(self) -> None:
        secret = "sk-or-v1-0123456789abcdef0123456789"
        script = (
            "key = os.environ['OPENROUTER_API_KEY']\n"
            "step('stop')\n"
            "text('leaked ' + key + '\\n## Status\\nx')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            binary = fake_opencode(Path(tmp) / "opencode", script)
            execution = Path(tmp) / "exec"
            execution.mkdir()
            (execution / "prompt.md").write_text("p", encoding="utf-8")
            result = run_cli(
                "launch", "--execution-dir", str(execution), "--worktree", str(repo),
                "--opencode-bin", str(binary), env={"OPENROUTER_API_KEY": secret},
            )
            record = json.loads((execution / "exit.json").read_text())
        self.assertEqual(record["secret_exposure"], ["OPENROUTER_API_KEY"])
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn(secret, result.stdout)

    def test_fixture_lookalike_is_not_flagged(self) -> None:
        script = f"step('stop')\ntext('uses sk-or-test fixture\\n' + {HANDOFF!r})\n"
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), script)
        self.assertEqual(record["secret_exposure"], [])


class StartupRetryTests(unittest.TestCase):
    LOCKED_ONCE = (
        "counter = os.path.join(os.path.dirname(sys.argv[0]), 'attempts')\n"
        "n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "open(counter, 'w').write(str(n + 1))\n"
        "if n == 0:\n"
        "    sys.stderr.write('Error: Unexpected error\\n\\ndatabase is locked\\n')\n"
        "    sys.exit(1)\n"
        f"step('stop')\ntext({HANDOFF!r})\n"
    )

    def test_locked_database_at_startup_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, execution, record, _ = launch(Path(tmp), self.LOCKED_ONCE)
            attempts = (Path(tmp) / "attempts").read_text()
            self.assertEqual((execution / "stderr.log").read_text(), "")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(attempts, "2")
        self.assertEqual(record["startup_retries"], 1)
        self.assertEqual(record["status"], "complete")

    def test_retries_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, record, _ = launch(Path(tmp), self.LOCKED_ONCE, "--startup-retries", "0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(record["startup_retries"], 0)
        self.assertEqual(record["status"], "failed")

    def test_lock_after_work_started_is_not_retried(self) -> None:
        script = (
            "emit('step_start', part={})\n"
            "sys.stderr.write('database is locked')\n"
            "sys.exit(1)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), script)
        self.assertEqual(record["startup_retries"], 0)
        self.assertEqual(record["status"], "failed")

    def test_other_startup_failures_are_not_retried(self) -> None:
        script = "sys.stderr.write('Error: model not found')\nsys.exit(1)\n"
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record, _ = launch(Path(tmp), script)
        self.assertEqual(record["startup_retries"], 0)


class PermissionTests(unittest.TestCase):
    def test_profiles(self) -> None:
        implement = build_permission("implement", allow_network=False, extra_deny=["rm -rf *"])
        review = build_permission("review", allow_network=False, extra_deny=[])
        readonly = build_permission("readonly", allow_network=True, extra_deny=[])
        self.assertEqual(implement["edit"], "allow")
        self.assertEqual(implement["bash"]["*"], "allow")
        for pattern in ("git push*", "gh *", "curl *", "*.env", "rm -rf *"):
            self.assertEqual(implement["bash"][pattern], "deny", pattern)
        self.assertNotIn("git commit*", implement["bash"])
        self.assertEqual(review["edit"], "deny")
        self.assertEqual(review["bash"]["git commit*"], "deny")
        # readonly keeps the bash tool (free tiers reject requests without it) behind an allowlist.
        self.assertEqual(readonly["bash"]["*"], "deny")
        self.assertEqual(readonly["bash"]["git show*"], "allow")
        self.assertEqual(readonly["bash"]["*>*"], "deny")
        self.assertEqual(readonly["bash"]["*.env"], "deny")
        keys = list(readonly["bash"])
        self.assertLess(keys.index("cat *"), keys.index("*.env"))
        self.assertEqual(readonly["webfetch"], "allow")
        self.assertEqual(implement["webfetch"], "deny")
        self.assertEqual(implement["read"]["*.env"], "deny")
        self.assertEqual(implement["read"]["*.env.example"], "allow")
        # Order matters: OpenCode applies the last matching rule, so the catch-all comes first.
        self.assertEqual(next(iter(implement["bash"])), "*")

    def test_installs_denied_only_when_requested(self) -> None:
        sandboxed = build_permission("implement", allow_network=False, extra_deny=[],
                                     deny_installs=True)
        plain = build_permission("implement", allow_network=False, extra_deny=[])
        for pattern in ("pnpm install*", "npm ci*", "uv sync*", "pip install*"):
            self.assertEqual(sandboxed["bash"][pattern], "deny", pattern)
            self.assertNotIn(pattern, plain["bash"])
        # Running already-installed tools (npx tsc, npx eslint) stays allowed.
        self.assertNotIn("npx *", sandboxed["bash"])

    def test_network_flag_lifts_curl_deny(self) -> None:
        networked = build_permission("implement", allow_network=True, extra_deny=[])
        self.assertNotIn("curl *", networked["bash"])
        self.assertEqual(networked["bash"]["git push*"], "deny")

    def test_unknown_profile(self) -> None:
        with self.assertRaises(ValueError):
            build_permission("yolo", allow_network=False, extra_deny=[])


class HandoffShapeTests(unittest.TestCase):
    def test_complete_handoff(self) -> None:
        shape = handoff_shape(HANDOFF)
        self.assertTrue(shape["handoff_ok"])
        self.assertFalse(shape["handoff_has_preamble"])

    def test_answer_above_empty_headings(self) -> None:
        text = "Found a bug in pages.py\n\n" + "\n".join(
            line for line in HANDOFF.splitlines() if line.startswith("## ")
        )
        shape = handoff_shape(text)
        self.assertFalse(shape["handoff_ok"])
        self.assertEqual(len(shape["handoff_empty_sections"]), 6)
        self.assertTrue(shape["handoff_has_preamble"])

    def test_missing_heading(self) -> None:
        shape = handoff_shape(HANDOFF.replace("## Commands Reported", "## Commands"))
        self.assertEqual(shape["handoff_missing_headings"], ["## Commands Reported"])
        self.assertFalse(shape["handoff_ok"])


if __name__ == "__main__":
    unittest.main()
