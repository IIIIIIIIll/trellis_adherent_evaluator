"""Normalize omp session JSONL into the internal events.jsonl schema.

This module is the platform-specific quarantine layer (design.md: "platform
specifics are quarantined in trace.py") and the OWNER of the events.jsonl
contract. Every emitted event carries the full key set so graders can code
strictly against one shape:

    {"ts": float, "seq": int, "kind": str, "role": str, "tool": str,
     "args": dict, "result": str, "injection_kind": str, "snapshot": dict,
     "text": str, "agent": str, "usage": dict}

Field semantics (frozen fields per design.md; additive fields documented in
design.md's schema section):

- kind: tool_call | message | injection | snapshot | turn_end
- role: assistant | user | system  ("simulator" reserved for driver-side use)
- tool/args: tool_call only; ``result`` is attached from the matching
  ``toolResult`` session message (by toolCallId); orphan results are dropped.
- injection: only ``trellis-*`` custom_message customTypes become injection
  events (trellis-workflow-state -> "workflow-state",
  trellis-session-context -> "session-context"). omp's own
  ``eager-task-prelude`` and unknown customTypes are dropped; omp's
  ``async-result`` custom_message (sub-agent task result delivery) is a
  message event with role=system, not an injection.
- text: message/injection content (assistant/user text, injected text).
- agent: "" on parent-session events; agent name (nested file stem, e.g.
  "LineCounter") on events from nested sub-agent sessions (load_nested).
- usage: {} except on turn_end: summed per-turn assistant usage
  {input, output, cacheRead, cacheWrite, totalTokens, reasoningTokens, cost}.
- turn_end: emitted at each user-turn boundary -- before the next user
  message, and once at end of stream when the trailing turn had assistant
  activity. role=assistant; ts = last event ts of the turn.

Session entry types observed in the wild (omp 18.0.11, spike S2/S6):
title, session, session_init, model_change, thinking_level_change, message,
custom_message, custom. Unknown types are ignored (forward compatibility).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

#: custom_message customTypes that map to injection events (trellis-* only).
INJECTION_KINDS = {
    "trellis-workflow-state": "workflow-state",
    "trellis-session-context": "session-context",
}

#: custom_message customTypes delivered as system messages (kept, not injection).
SYSTEM_MESSAGE_CUSTOM_TYPES = {"async-result"}

_EVENT_KEYS = (
    "ts", "seq", "kind", "role", "tool", "args", "result",
    "injection_kind", "snapshot", "text", "agent", "usage",
)

_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoningTokens")


# ---------------------------------------------------------------- primitives

def _event(*, seq, ts, kind, role, tool="", args=None, result="",
           injection_kind="", snapshot=None, text="", agent="", usage=None):
    return {
        "ts": ts,
        "seq": seq,
        "kind": kind,
        "role": role,
        "tool": tool,
        "args": args if args is not None else {},
        "result": result,
        "injection_kind": injection_kind,
        "snapshot": snapshot if snapshot is not None else {},
        "text": text,
        "agent": agent,
        "usage": usage if usage is not None else {},
    }


def _entry_ts(entry) -> float:
    """Entry timestamp -> epoch seconds. Session entries carry ISO-8601 UTC
    strings; some message payloads carry a millisecond epoch instead."""
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e11 else float(ts)
    return time.time()


def _item_texts(content) -> list:
    """Text of all ``{"type": "text", "text": ...}`` content items, in order."""
    if not isinstance(content, list):
        return []
    return [item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"]


def _join_texts(texts) -> str:
    return "\n\n".join(t for t in texts if t)


def add_usage(total: dict, usage: dict) -> dict:
    """Fold one usage record into an accumulated total (single owner of the
    usage math; used for turn_end usage and driver cost capture).

    Accepts both shapes: session usage records (``cost`` is a sub-dict with a
    ``total`` field) and already-summed totals (``cost`` is a float), so
    per-turn totals fold without loss.
    """
    out = dict(total)
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            out[key] = out.get(key, 0) + value
    cost = usage.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
        total_cost = cost.get("total")
        out["cost"] = out.get("cost", 0.0) + total_cost
    elif isinstance(cost, (int, float)):
        out["cost"] = out.get("cost", 0.0) + cost
    return out


def summarize_usage(entries) -> dict:
    """Summed usage over all assistant messages in the given session entries."""
    total: dict = {}
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            total = add_usage(total, usage)
    return total


# ------------------------------------------------------------------- parsing

def parse_session(path) -> list:
    """Parse an omp session JSONL file into a list of raw entry dicts.

    Malformed lines are skipped (forward compatibility with truncated tails).
    """
    entries = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def normalize_entries(entries, *, seq_start: int = 1, agent: str = "") -> list:
    """Normalize raw session entries into events with monotonically increasing
    seq starting at ``seq_start``. ``agent`` labels nested sub-agent events.
    """
    events: list = []
    tool_by_id: dict = {}
    active_since_user = False
    turn_last_ts = 0.0
    turn_usage: dict = {}
    seq = seq_start

    def close_turn():
        nonlocal seq, active_since_user, turn_usage
        if not active_since_user:
            return
        events.append(_event(
            seq=seq, ts=turn_last_ts, kind="turn_end", role="assistant",
            usage=turn_usage, agent=agent,
        ))
        seq += 1
        active_since_user = False
        turn_usage = {}

    for entry in entries:
        etype = entry.get("type")
        ts = _entry_ts(entry)

        if etype == "message":
            message = entry.get("message") or {}
            role = message.get("role")
            content = message.get("content")

            if role == "assistant":
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "toolCall":
                        args = item.get("arguments")
                        if not isinstance(args, dict):
                            try:
                                args = json.loads(item.get("partialArgs") or "{}")
                            except json.JSONDecodeError:
                                args = {}
                        event = _event(seq=seq, ts=ts, kind="tool_call",
                                       role="assistant", tool=item.get("name", ""),
                                       args=args, agent=agent)
                        events.append(event)
                        if item.get("id"):
                            tool_by_id[item["id"]] = event
                        seq += 1
                    elif item_type == "text":
                        events.append(_event(seq=seq, ts=ts, kind="message",
                                             role="assistant",
                                             text=item.get("text", ""), agent=agent))
                        seq += 1
                    # "thinking" items are not observable conversation: dropped
                active_since_user = True
                turn_last_ts = ts
                usage = message.get("usage")
                if isinstance(usage, dict):
                    turn_usage = add_usage(turn_usage, usage)

            elif role == "user":
                close_turn()
                events.append(_event(seq=seq, ts=ts, kind="message", role="user",
                                     text=_join_texts(_item_texts(content)), agent=agent))
                seq += 1

            elif role == "toolResult":
                call = tool_by_id.get(message.get("toolCallId"))
                if call is not None:
                    call["result"] = _join_texts(_item_texts(content))
                # orphan results (no matching call) are dropped

        elif etype == "custom_message":
            custom_type = entry.get("customType", "")
            if custom_type in INJECTION_KINDS:
                events.append(_event(seq=seq, ts=ts, kind="injection", role="system",
                                     injection_kind=INJECTION_KINDS[custom_type],
                                     text=entry.get("content", ""), agent=agent))
                seq += 1
            elif custom_type in SYSTEM_MESSAGE_CUSTOM_TYPES:
                events.append(_event(seq=seq, ts=ts, kind="message", role="system",
                                     text=entry.get("content", ""), agent=agent))
                seq += 1
            # eager-task-prelude and unknown customTypes: dropped

        # title/session/session_init/model_change/thinking_level_change/custom:
        # not observable conversation, ignored.

    close_turn()
    return events


# ------------------------------------------------------------------- loading

def load_session(path) -> list:
    """Full-file convenience: parse + normalize one omp session JSONL."""
    return normalize_entries(parse_session(path))


def load_nested(session_dir) -> dict:
    """Load nested sub-agent session transcripts.

    Nested sessions live in a sibling directory named after the parent session
    file basename (spike S6): ``<session-dir>/<session-basename>/<agent>.jsonl``
    plus ``<agent>.md`` (final structured result; ignored here).

    Returns ``{agent_name: [events]}`` -- agent name is the file stem; events
    are labeled with ``agent`` and each agent's seq is continuous across all
    transcripts of that agent. Merging into the parent stream (and what to do
    with nested events) is the grader's decision.
    """
    session_dir = Path(session_dir)
    nested: dict = {}
    if not session_dir.is_dir():
        return nested
    for sub_dir in sorted(p for p in session_dir.iterdir() if p.is_dir()):
        for path in sorted(sub_dir.glob("*.jsonl")):
            agent = path.stem
            existing = nested.get(agent, [])
            events = normalize_entries(parse_session(path),
                                       seq_start=len(existing) + 1, agent=agent)
            nested[agent] = existing + events
    return nested


# -------------------------------------------------------------------- i/o

def write_events(events, path) -> None:
    """Write events as JSONL (one JSON object per line, UTF-8)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(path) -> list:
    """Read events.jsonl back into a list of event dicts."""
    events = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
