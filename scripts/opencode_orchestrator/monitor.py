from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .events import (
    StreamSummary,
    classify,
    compatibility,
    json_dumps,
    read_exit_record,
    summarize_stream,
)
from .journal import (
    TERMINAL_EXECUTION_STATUSES,
    execution_key,
    read_journal,
    resolve_run_path,
)

RUNS_DIR = Path(".opencode-orchestrator") / "runs"
NOTIFY = {
    "complete": "opencode_agent_complete",
    "failed": "opencode_agent_failed",
    "incomplete": "opencode_agent_incomplete",
    "stopped": "opencode_agent_stopped",
}


@dataclass(frozen=True)
class MonitorTarget:
    path: Path
    exit_path: Path
    agent: Optional[str] = None
    execution: Optional[str] = None


def error_payload(message: str, *, path: Optional[Path] = None) -> Dict[str, object]:
    payload: Dict[str, object] = {"type": "monitor_error", "message": message}
    if path is not None:
        payload["path"] = str(path)
    return payload


def inflight_targets(run_dir: Path) -> Tuple[List[MonitorTarget], List[Dict[str, object]]]:
    if not run_dir.is_dir():
        return [], [error_payload("run directory does not exist or is not a directory", path=run_dir)]

    records, issues = read_journal(run_dir / "journal.jsonl", allow_partial_final_line=True)
    if issues:
        return [], [
            error_payload(
                f"could not read run journal: {'; '.join(issues)}", path=run_dir / "journal.jsonl"
            )
        ]

    completed = {
        key
        for record in records
        if record.get("type") == "execution_result"
        for key in [execution_key(record)]
        if key is not None and record.get("status") in TERMINAL_EXECUTION_STATUSES
    }
    targets: List[MonitorTarget] = []
    errors: List[Dict[str, object]] = []
    for record in records:
        if record.get("type") != "execution":
            continue
        key = execution_key(record)
        if key is None:
            errors.append(error_payload("execution entry is missing agent or execution"))
            continue
        if key in completed or record.get("event_source") == "claude":
            continue
        if record.get("event_source") not in {None, "opencode"}:
            errors.append(
                error_payload(
                    f"execution {key[0]}/{key[1]} uses unsupported event source; "
                    "only opencode run streams are supported"
                )
            )
            continue
        value = record.get("events")
        if not isinstance(value, str) or not value:
            errors.append(error_payload(f"execution {key[0]}/{key[1]} has no events file"))
            continue
        try:
            path = resolve_run_path(run_dir, value)
            exit_value = record.get("exit")
            exit_path = (
                resolve_run_path(run_dir, exit_value)
                if isinstance(exit_value, str) and exit_value
                else path.parent / "exit.json"
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(
                error_payload(f"execution {key[0]}/{key[1]} has an invalid events path: {exc}")
            )
            continue
        targets.append(MonitorTarget(path, exit_path, key[0], key[1]))
    return targets, errors


def resolve_monitor_targets(
    args: argparse.Namespace,
) -> Tuple[List[MonitorTarget], List[Dict[str, object]]]:
    selectors = bool(args.log) + bool(args.run_id or args.repo)
    if selectors != 1:
        return [], [
            error_payload(
                "provide exactly one target: --repo with --run-id or one or more --log paths"
            )
        ]
    if args.log:
        targets = []
        for value in args.log:
            path = Path(value).expanduser()
            targets.append(MonitorTarget(path, path.parent / "exit.json"))
        return targets, []
    if bool(args.run_id) != bool(args.repo):
        return [], [error_payload("--repo and --run-id must be provided together")]
    return inflight_targets(Path(args.repo).expanduser() / RUNS_DIR / args.run_id)


def monitor_payload(
    kind: str, target: MonitorTarget, summary: StreamSummary
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "type": kind,
        "path": str(target.path),
        "source": "opencode",
        "session_id": summary.session_id or "unknown",
        "mtime": int(target.path.stat().st_mtime),
    }
    if target.agent:
        payload["agent"] = target.agent
    if target.execution:
        payload["execution"] = target.execution
    return payload


def emit_monitor(payload: Dict[str, object]) -> None:
    print(json_dumps(payload), flush=True)


def scan_monitor_target(target: MonitorTarget, stale_seconds: int) -> str:
    path = target.path
    try:
        if not path.is_file():
            # The launcher creates events.jsonl at process start; before that the run is pending.
            if target.exit_path.is_file():
                emit_monitor(error_payload("exit record exists but event stream is missing", path=path))
                return "error"
            emit_monitor(error_payload("event stream does not exist or is not a file", path=path))
            return "error"
        summary = summarize_stream(path)
        compat = compatibility(summary)
        if compat["parse_confidence"] == "low":
            payload = monitor_payload("opencode_agent_unknown", target, summary)
            payload["compatibility"] = compat
            if summary.parse_errors:
                payload["parse_errors"] = summary.parse_errors
            emit_monitor(payload)
            return "unknown"

        exit_record = read_exit_record(target.exit_path) if target.exit_path.is_file() else None
        status = classify(summary, exit_record)
        if exit_record is not None and status in NOTIFY:
            payload = monitor_payload(NOTIFY[status], target, summary)
            payload["exit_record"] = True
            for key in (
                "exit_code", "timed_out", "finish_reason", "handoff_written", "stop_reason",
                "checks_passed", "fix_rounds_used",
            ):
                if key in exit_record:
                    payload[key] = exit_record[key]
            for key in ("handoff_ok", "handoff_missing_headings", "handoff_empty_sections"):
                if exit_record.get(key) not in (None, []):
                    payload[key] = exit_record[key]
            for key in ("profile_violation", "secret_exposure"):
                if exit_record.get(key):
                    payload[key] = exit_record[key]
            if summary.permission_rejections:
                payload["permission_rejections"] = summary.permission_rejections
            if summary.error is not None:
                payload["error"] = summary.error
            if summary.tokens:
                payload["tokens"] = dict(sorted(summary.tokens.items()))
            if summary.parse_errors:
                payload["parse_errors"] = summary.parse_errors
            emit_monitor(payload)
            return status

        idle_seconds = int(time.time() - path.stat().st_mtime)
        if stale_seconds >= 0 and idle_seconds >= stale_seconds:
            payload = monitor_payload("opencode_agent_stale", target, summary)
            payload["idle_seconds"] = idle_seconds
            payload["stream_status"] = summary.status
            payload["exit_record"] = exit_record is not None
            emit_monitor(payload)
            return "stale"
    except (OSError, RuntimeError, ValueError) as exc:
        emit_monitor(error_payload(f"could not read event stream: {exc}", path=path))
        return "error"
    return "active"


def command_monitor(args: argparse.Namespace) -> int:
    done: Set[MonitorTarget] = set()
    failed_seen = False
    unknown_seen = False
    while True:
        targets, errors = resolve_monitor_targets(args)
        if errors:
            for error in errors:
                emit_monitor(error)
            return 1
        if not targets:
            return 0

        for target in [target for target in targets if target not in done]:
            outcome = scan_monitor_target(target, args.stale_seconds)
            if outcome != "active":
                done.add(target)
            failed_seen = failed_seen or outcome in {"failed", "incomplete"}
            unknown_seen = unknown_seen or outcome == "unknown"
            if outcome == "error":
                return 1

        if args.once or all(target in done for target in targets):
            if unknown_seen:
                return 2
            return 1 if failed_seen and args.fail_on_agent_failure else 0
        time.sleep(max(0.1, args.poll_interval))
