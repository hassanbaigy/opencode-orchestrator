"""Parse ``opencode run --format json`` event streams.

OpenCode emits one JSON object per line with ``type``, ``timestamp``, ``sessionID`` and either a
``part`` (``step_start``, ``step_finish``, ``tool_use``, ``text``, ``reasoning``) or an ``error``.
The stream has no explicit end-of-run event: ``opencode run`` exits when the session goes idle. A
run that answered normally ends with ``step_finish`` whose ``part.reason`` is ``stop``. A run that
was cut short (for example by a permission auto-rejection) ends with a different finish reason, so
stream status alone is provisional until the launcher's exit record exists.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

PARSER_VERSION = "0.1.0"

RUN_EVENT_TYPES = {
    "step_start",
    "step_finish",
    "tool_use",
    "text",
    "reasoning",
    "error",
}

# Finish reasons that mean the model chose to stop (AI SDK vocabulary).
STOP_REASONS = {"stop", "end_turn", "end"}
# Finish reasons that mean another step follows.
CONTINUE_REASONS = {"tool-calls", "tool_calls", "tool_use"}

ASK_REJECTED_MARKER = "rejected permission"
RULE_DENIED_MARKER = "specified a rule which prevents"
# Tool output that shows the OS sandbox (srt) blocked an action: a filesystem EPERM or the proxy
# refusing a non-allowlisted host.
SANDBOX_BLOCK_MARKERS = ("Operation not permitted", "Tunnel connection failed: 403")


@dataclass(frozen=True)
class EventRecord:
    event: Dict[str, object]
    event_type: str


@dataclass
class StreamSummary:
    status: str = "idle"
    event_counts: Counter = field(default_factory=Counter)
    known_count: int = 0
    parse_errors: int = 0
    unknown_event_types: Set[str] = field(default_factory=set)
    session_id: Optional[str] = None
    steps: int = 0
    finish_reason: Optional[str] = None
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    permission_rejections: List[str] = field(default_factory=list)
    rule_denials: List[str] = field(default_factory=list)
    sandbox_blocks: List[str] = field(default_factory=list)
    tokens: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    error: object = None
    texts: List[tuple] = field(default_factory=list)  # (messageID, text) in emission order
    terminal: Optional[EventRecord] = None

    @property
    def event_count(self) -> int:
        return sum(self.event_counts.values())

    def consume(self, record: EventRecord) -> None:
        kind = record.event_type
        self.event_counts[kind] += 1
        if kind in {"<invalid-json>", "<non-object>"}:
            self.parse_errors += 1
        if kind in RUN_EVENT_TYPES:
            self.known_count += 1
            if self.status == "idle":
                self.status = "starting"
        else:
            self.unknown_event_types.add(kind)

        event = record.event
        sid = event.get("sessionID")
        if isinstance(sid, str) and sid and self.session_id is None:
            self.session_id = sid
        part = event.get("part")
        part = part if isinstance(part, dict) else {}

        if kind == "step_start":
            self.status = "active"
            self.steps += 1
            self.terminal = None
        elif kind == "step_finish":
            reason = part.get("reason")
            self.finish_reason = reason if isinstance(reason, str) else None
            self._add_usage(part)
            if self.finish_reason in STOP_REASONS:
                self.status = "complete"
                self.terminal = record
            elif self.finish_reason in CONTINUE_REASONS:
                self.status = "active"
            else:
                # length, content-filter, error, other, unknown: the turn ended without an answer.
                self.status = "incomplete"
                self.terminal = record
        elif kind == "tool_use":
            tool = part.get("tool")
            self.tool_calls[tool if isinstance(tool, str) else "<unknown>"] += 1
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            output = " ".join(
                str(value) for value in (state.get("output"), state.get("error")) if value
            )
            if any(marker in output for marker in SANDBOX_BLOCK_MARKERS):
                self.sandbox_blocks.append(describe_tool_call(part, state))
            if state.get("status") == "error":
                self.tool_errors += 1
                message = str(state.get("error") or "")
                label = describe_tool_call(part, state)
                if ASK_REJECTED_MARKER in message:
                    self.permission_rejections.append(label)
                elif RULE_DENIED_MARKER in message:
                    self.rule_denials.append(label)
        elif kind == "text":
            text = part.get("text")
            if isinstance(text, str):
                message_id = part.get("messageID")
                self.texts.append((message_id if isinstance(message_id, str) else None, text))
        elif kind == "error":
            self.status = "failed"
            self.error = error_message(event)
            self.terminal = record

    def _add_usage(self, part: Dict[str, object]) -> None:
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            for key, value in tokens.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.tokens[key] = self.tokens.get(key, 0) + int(value)
                elif isinstance(value, dict):  # e.g. cache: {read, write}
                    for sub, subvalue in value.items():
                        if isinstance(subvalue, (int, float)) and not isinstance(subvalue, bool):
                            name = f"{key}_{sub}"
                            self.tokens[name] = self.tokens.get(name, 0) + int(subvalue)
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost += float(cost)

    @property
    def final_text(self) -> str:
        """Exact text of the last assistant message that produced text.

        A message may stream several text parts; they are joined with a blank line, in order,
        without any other modification.
        """
        if not self.texts:
            return ""
        last_id = self.texts[-1][0]
        if last_id is None:
            return self.texts[-1][1]
        return "\n\n".join(text for message_id, text in self.texts if message_id == last_id)

    def details(self) -> Dict[str, object]:
        details: Dict[str, object] = {"steps": self.steps}
        if self.finish_reason is not None:
            details["finish_reason"] = self.finish_reason
        if self.tool_calls:
            details["tool_calls"] = dict(sorted(self.tool_calls.items()))
        if self.tool_errors:
            details["tool_errors"] = self.tool_errors
        if self.permission_rejections:
            details["permission_rejections"] = self.permission_rejections
        if self.rule_denials:
            details["rule_denials"] = self.rule_denials
        if self.sandbox_blocks:
            details["sandbox_blocks"] = self.sandbox_blocks
        if self.tokens:
            details["tokens"] = dict(sorted(self.tokens.items()))
        if self.cost:
            details["cost"] = round(self.cost, 6)
        if self.error is not None:
            details["error"] = self.error
        if self.session_id is not None:
            details["session_id"] = self.session_id
        final = self.final_text
        if final:
            details["last_agent_message"] = final
        return details


def describe_tool_call(part: Dict[str, object], state: Dict[str, object]) -> str:
    tool = part.get("tool") if isinstance(part.get("tool"), str) else "tool"
    raw_input = state.get("input")
    raw_input = raw_input if isinstance(raw_input, dict) else {}
    for key in ("command", "filePath", "path", "url", "pattern"):
        value = raw_input.get(key)
        if isinstance(value, str) and value:
            return f"{tool}: {value[:160]}"
    return str(tool)


def error_message(event: Dict[str, object]) -> object:
    error = event.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            name = error.get("name")
            return f"{name}: {data['message']}" if isinstance(name, str) else data["message"]
        if isinstance(error.get("name"), str):
            return error["name"]
    return error if error is not None else event.get("message")


def json_dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def event_type(event: Dict[str, object]) -> str:
    value = event.get("type")
    return value if isinstance(value, str) else "<missing>"


def decode_event_line(line: str) -> Optional[EventRecord]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return EventRecord({"_parse_error": stripped[:200]}, "<invalid-json>")
    if not isinstance(event, dict):
        return EventRecord(
            {"_parse_error": "top-level JSON value is not an object"}, "<non-object>"
        )
    return EventRecord(event, event_type(event))


def summarize_stream(path: Path) -> StreamSummary:
    summary = StreamSummary()
    with path.open("rb") as handle:
        while True:
            raw_line = handle.readline()
            if raw_line == b"":
                break
            terminated = raw_line.endswith(b"\n")
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                if not terminated:
                    if summary.status == "idle":
                        summary.status = "starting"
                    break
                summary.consume(
                    EventRecord({"_parse_error": "invalid UTF-8"}, "<invalid-json>")
                )
                continue
            record = decode_event_line(line)
            if record is None:
                continue
            if not terminated and record.event_type == "<invalid-json>":
                # A partially written final line: the writer is still mid-event.
                if summary.status == "idle":
                    summary.status = "starting"
                break
            summary.consume(record)
    return summary


def compatibility(summary: StreamSummary) -> Dict[str, object]:
    warnings = [] if summary.event_count else ["no events found"]
    unknown_count = summary.event_count - summary.known_count
    confidence = (
        "low" if summary.event_count and unknown_count > summary.known_count else "high"
    )
    return {
        "parser_version": PARSER_VERSION,
        "parse_confidence": confidence,
        "unknown_event_types": sorted(summary.unknown_event_types),
        "warnings": warnings,
    }


def incompatible_message() -> str:
    return (
        f"ERROR: opencode run JSON events appear incompatible (parser {PARSER_VERSION}). "
        "Run state --dump-event-types and update the parser. Do not infer agent status."
    )


def read_exit_record(path: Path) -> Optional[Dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def classify(summary: StreamSummary, exit_record: Optional[Dict[str, object]]) -> str:
    """Final status once the process has exited, else the provisional stream status.

    ``complete``   - the process exited 0 and the last step finished with a stop reason.
    ``incomplete`` - the process exited 0 but the agent never produced a final answer
                     (permission auto-rejection, length limit, content filter, ...).
    ``failed``     - nonzero exit, timeout, launch failure, or an ``error`` event.
    ``stopped``    - the orchestrator stopped the run with ``stop``.
    """
    if exit_record is None:
        return summary.status
    if exit_record.get("stopped"):
        return "stopped"
    if exit_record.get("timed_out") or exit_record.get("launch_error"):
        return "failed"
    code = exit_record.get("exit_code")
    if summary.error is not None or (isinstance(code, int) and code != 0):
        return "failed"
    if summary.status == "complete":
        return "complete"
    return "incomplete"
