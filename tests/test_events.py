from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURES, run_cli
from opencode_orchestrator.events import classify, summarize_stream


class EventParserTests(unittest.TestCase):
    def test_real_stream_with_rule_denial_completes(self) -> None:
        summary = summarize_stream(FIXTURES / "run_complete_rule_denial.jsonl")
        self.assertEqual(summary.status, "complete")
        self.assertEqual(summary.finish_reason, "stop")
        self.assertEqual(summary.rule_denials, ["bash: git push origin main"])
        self.assertEqual(summary.permission_rejections, [])
        self.assertTrue(summary.session_id.startswith("ses_"))
        self.assertGreater(summary.tokens["total"], 0)
        self.assertIn("blocked by a permission rule", summary.final_text)

    def test_ask_auto_rejection_is_incomplete_once_process_exits(self) -> None:
        summary = summarize_stream(FIXTURES / "run_permission_rejected.jsonl")
        self.assertEqual(summary.permission_rejections, ["bash: echo hi > x.txt"])
        self.assertEqual(summary.finish_reason, "tool-calls")
        # Provisional while no exit record exists; incomplete once the process exited 0.
        self.assertEqual(classify(summary, None), "active")
        self.assertEqual(classify(summary, {"exit_code": 0}), "incomplete")

    def test_provider_error_is_failed(self) -> None:
        summary = summarize_stream(FIXTURES / "run_provider_auth_error.jsonl")
        self.assertEqual(summary.status, "failed")
        self.assertIn("ProviderAuthError", summary.error)
        self.assertEqual(classify(summary, {"exit_code": 1}), "failed")

    def test_complete_stream_fails_on_nonzero_exit_or_timeout(self) -> None:
        summary = summarize_stream(FIXTURES / "run_implement_handoff.jsonl")
        self.assertEqual(classify(summary, {"exit_code": 0}), "complete")
        self.assertEqual(classify(summary, {"exit_code": 1}), "failed")
        self.assertEqual(classify(summary, {"exit_code": 0, "timed_out": True}), "failed")

    def test_final_text_uses_only_last_message_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            lines = [
                {"type": "text", "sessionID": "s", "part": {"messageID": "m1", "text": "early"}},
                {"type": "text", "sessionID": "s", "part": {"messageID": "m2", "text": "A"}},
                {"type": "text", "sessionID": "s", "part": {"messageID": "m2", "text": "B"}},
            ]
            path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
            self.assertEqual(summarize_stream(path).final_text, "A\n\nB")

    def test_length_finish_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                '{"type":"step_start","part":{}}\n'
                '{"type":"step_finish","part":{"reason":"length"}}\n',
                encoding="utf-8",
            )
            summary = summarize_stream(path)
            self.assertEqual(summary.status, "incomplete")
            self.assertEqual(classify(summary, {"exit_code": 0}), "incomplete")

    def test_partial_final_line_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                '{"type":"step_start","part":{}}\n{"type":"step_finish","part":{"reason":"st',
                encoding="utf-8",
            )
            summary = summarize_stream(path)
            self.assertEqual(summary.status, "active")
            self.assertEqual(summary.parse_errors, 0)

    def test_state_reports_low_confidence_for_foreign_format(self) -> None:
        result = run_cli("state", "x", "--file", str(FIXTURES / "unknown_format.jsonl"), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "unknown")
        self.assertIn("incompatible", result.stderr)

    def test_state_reads_sibling_exit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "events.jsonl"
            stream.write_bytes((FIXTURES / "run_permission_rejected.jsonl").read_bytes())
            (Path(tmp) / "exit.json").write_text('{"exit_code": 0}', encoding="utf-8")
            result = run_cli("state", "x", "--file", str(stream), "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "incomplete")
        self.assertTrue(payload["exit_record"])
        self.assertEqual(payload["details"]["permission_rejections"], ["bash: echo hi > x.txt"])

    def test_dump_event_types(self) -> None:
        result = run_cli(
            "state", "x", "--file", str(FIXTURES / "run_complete_rule_denial.jsonl"),
            "--json", "--dump-event-types",
        )
        self.assertEqual(result.returncode, 0)
        types = json.loads(result.stdout)["event_types"]
        self.assertEqual(set(types), {"step_start", "step_finish", "tool_use", "text"})


if __name__ == "__main__":
    unittest.main()
