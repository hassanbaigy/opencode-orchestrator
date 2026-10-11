#!/usr/bin/env python3
"""Command-line interface for OpenCode launch, agent inspection and run validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .events import (
    classify,
    compatibility,
    incompatible_message,
    json_dumps,
    read_exit_record,
    summarize_stream,
)
from .journal import validate_run
from .launch import add_launch_parser, add_stop_parser
from .monitor import command_monitor


def emit(payload: object, as_json: bool) -> None:
    print(json_dumps(payload) if as_json else payload)


def command_state(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser() if args.file else None
    if path is None or not path.is_file():
        print(
            json_dumps(
                {
                    "type": "state_error",
                    "session_id": args.session_id,
                    "path": str(path) if path is not None else None,
                    "message": "event stream does not exist or is not a file",
                }
            )
        )
        return 1
    try:
        summary = summarize_stream(path)
    except (OSError, RuntimeError) as exc:
        print(
            json_dumps(
                {
                    "type": "state_error",
                    "session_id": args.session_id,
                    "path": str(path),
                    "message": f"could not read event stream: {exc}",
                }
            )
        )
        return 1
    compat = compatibility(summary)
    base = {
        "session_id": summary.session_id or args.session_id,
        "source": "opencode",
        "path": str(path),
    }

    if args.dump_event_types:
        emit(
            {
                **base,
                "event_types": dict(sorted(summary.event_counts.items())),
                "compatibility": compat,
            },
            args.json,
        )
        return 2 if compat["parse_confidence"] == "low" else 0

    if compat["parse_confidence"] == "low":
        emit({**base, "status": "unknown", "compatibility": compat}, args.json)
        print(incompatible_message(), file=sys.stderr)
        return 2

    exit_path = Path(args.exit).expanduser() if args.exit else path.parent / "exit.json"
    exit_record = read_exit_record(exit_path) if exit_path.is_file() else None
    payload = {
        **base,
        "status": classify(summary, exit_record),
        "exit_record": exit_record is not None,
        "details": summary.details(),
        "compatibility": compat,
    }
    emit(payload, args.json)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    payload = validate_run(Path(args.run_dir))
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch and inspect headless OpenCode agents and validate orchestration runs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_launch_parser(subparsers)
    add_stop_parser(subparsers)

    state_parser = subparsers.add_parser("state", help="Classify an OpenCode agent state.")
    state_parser.add_argument("session_id")
    state_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    state_parser.add_argument("--file", help="opencode run --format json event stream path.")
    state_parser.add_argument(
        "--exit", help="Launcher exit record (default: exit.json beside the stream)."
    )
    state_parser.add_argument(
        "--dump-event-types", action="store_true", help="Print observed event types."
    )
    state_parser.set_defaults(func=command_state)

    monitor_parser = subparsers.add_parser(
        "monitor", help="Watch in-flight agent event streams from the prompt-first run layout."
    )
    monitor_parser.add_argument("--run-id", help="Run id under .opencode-orchestrator/runs.")
    monitor_parser.add_argument("--repo", help="Repository root paired with --run-id.")
    monitor_parser.add_argument(
        "--log", action="append", dest="log", help="Explicit event stream path. Repeatable."
    )
    monitor_parser.add_argument("--once", action="store_true", help="Scan once and exit.")
    monitor_parser.add_argument(
        "--stale-seconds", type=int, default=600, help="Emit stale after this many idle seconds."
    )
    monitor_parser.add_argument(
        "--poll-interval", type=float, default=30.0, help="Seconds between watch scans."
    )
    monitor_parser.add_argument(
        "--fail-on-agent-failure",
        action="store_true",
        help="Exit nonzero when a watched agent fails or ends incomplete.",
    )
    monitor_parser.set_defaults(func=command_monitor)

    validate_parser = subparsers.add_parser(
        "validate", help="Check prompt-first run structure without making acceptance judgments."
    )
    validate_parser.add_argument("run_dir", help="Run directory containing journal.jsonl.")
    validate_parser.set_defaults(func=command_validate)

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    return args.func(args)
