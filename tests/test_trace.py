"""Runner-layer tests: trace normalizer (schema owner), simulator policies,
snapshot, and driver turn loop.

The trace tests run against tests/fixtures/sample_session.jsonl -- a real omp
consent-loop session captured during spike S1c (consent question -> "Yes" ->
task.py create + prd.md write), so the frozen schema is validated against real
session-file reality, not synthetic data.
"""

import hashlib
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from evaluator import driver as driver_mod
from evaluator import simulator as sim
from evaluator import snapshot as snap
from evaluator import trace

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_session.jsonl"

EVENT_KEYS = {
    "ts", "seq", "kind", "role", "tool", "args", "result",
    "injection_kind", "snapshot", "text", "agent", "usage",
}


# ------------------------------------------------------ trace: real session

def test_fixture_is_real_consent_loop_capture():
    entries = [json.loads(line) for line in
               FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    custom_types = [e["customType"] for e in entries if e["type"] == "custom_message"]
    assert custom_types.count("eager-task-prelude") == 1
    assert custom_types.count("trellis-workflow-state") == 2
    assert custom_types.count("trellis-session-context") == 2
    roles = [e["message"]["role"] for e in entries if e["type"] == "message"]
    assert roles.count("user") == 2          # consent question + approval reply
    assert roles.count("toolResult") == 9


def test_normalize_real_session_schema():
    events = trace.load_session(FIXTURE)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(set(e) == EVENT_KEYS for e in events)
    assert all(isinstance(e["ts"], float) and e["ts"] > 0 for e in events)
    assert all(e["agent"] == "" for e in events)   # parent session: unlabeled
    kinds = {e["kind"] for e in events}
    assert {"message", "tool_call", "injection", "turn_end"} <= kinds
    assert kinds <= {"message", "tool_call", "injection", "turn_end"}


def test_breadcrumb_injections_present_per_user_turn():
    events = trace.load_session(FIXTURE)
    injections = [e for e in events if e["kind"] == "injection"]
    counts = {}
    for event in injections:
        counts[event["injection_kind"]] = counts.get(event["injection_kind"], 0) + 1
    assert counts == {"workflow-state": 2, "session-context": 2}

    user_indexes = [i for i, e in enumerate(events)
                    if e["kind"] == "message" and e["role"] == "user"]
    assert len(user_indexes) == 2
    for start, end in zip(user_indexes, user_indexes[1:] + [len(events)]):
        window = events[start:end]
        # each user turn carries its workflow-state breadcrumb after the user
        # message and before the turn ends
        breadcrumb = [e for e in window
                      if e["kind"] == "injection" and e["injection_kind"] == "workflow-state"]
        assert len(breadcrumb) == 1
        text = window[0]["text"]
        assert text and text != breadcrumb[0]["text"]
    # turns are separated: exactly one turn_end per user turn
    turn_ends = [e for e in events if e["kind"] == "turn_end"]
    assert len(turn_ends) == 2
    assert events[-1]["kind"] == "turn_end"


def test_tool_call_result_pairing():
    events = trace.load_session(FIXTURE)
    calls = [e for e in events if e["kind"] == "tool_call"]
    assert len(calls) == 9                      # 9 toolCall items, 9 toolResults
    assert all(e["role"] == "assistant" for e in calls)
    assert all(isinstance(e["args"], dict) and e["args"] for e in calls)
    assert all(isinstance(e["result"], str) and e["result"] for e in calls)
    assert {e["tool"] for e in calls} <= {"read", "glob", "bash", "write"}

    writes = [e for e in calls if e["tool"] == "write"]
    assert len(writes) == 1
    assert writes[0]["args"]["path"] == ".trellis/tasks/08-31-notes-tagging/prd.md"
    assert "Successfully wrote" in writes[0]["result"]

    creates = [e for e in calls if e["tool"] == "bash"
               and "task.py create \"Notes tagging feature\"" in e["args"].get("command", "")]
    assert len(creates) == 1
    # a `task.py create --help` inspection call must not read as a create
    help_calls = [e for e in calls if e["tool"] == "bash"
                  and "task.py create --help" in e["args"].get("command", "")]
    assert len(help_calls) == 1

    # call precedes everything of its result; assistant text present per turn
    assistant_texts = [e["text"] for e in events
                       if e["kind"] == "message" and e["role"] == "assistant"]
    assert any(t.startswith("Task created and planned") for t in assistant_texts)


def test_eager_task_prelude_excluded():
    raw = FIXTURE.read_text(encoding="utf-8")
    assert "eager-task-prelude" in raw           # present in the raw capture...
    events = trace.load_session(FIXTURE)
    assert all(e["injection_kind"] != "eager-task-prelude" for e in events)  # ...filtered out
    assert all("Task delegation enabled for this request" not in e["text"]
               for e in events)


def test_turn_end_carries_per_turn_usage():
    events = trace.load_session(FIXTURE)
    turn_ends = [e for e in events if e["kind"] == "turn_end"]
    assert len(turn_ends) == 2
    for event in turn_ends:
        assert event["usage"]["totalTokens"] > 0
        assert event["usage"]["cost"] > 0
    # usage math is owned by trace.add_usage: cost sums as a float
    total = {}
    for event in turn_ends:
        total = trace.add_usage(total, event["usage"])
    assert total["totalTokens"] == sum(e["usage"]["totalTokens"] for e in turn_ends)


# ------------------------------------------------------ trace: edge entries

def test_custom_message_filtering():
    entries = [
        {"type": "custom_message", "customType": "trellis-workflow-state",
         "content": "ws", "timestamp": "2026-08-31T13:00:00.000Z"},
        {"type": "custom_message", "customType": "eager-task-prelude",
         "content": "prelude", "timestamp": "2026-08-31T13:00:00.001Z"},
        {"type": "custom_message", "customType": "async-result",
         "content": "<task-result/>", "timestamp": "2026-08-31T13:00:00.002Z"},
        {"type": "custom_message", "customType": "brand-new-type",
         "content": "x", "timestamp": "2026-08-31T13:00:00.003Z"},
    ]
    events = trace.normalize_entries(entries)
    assert [(e["kind"], e["role"], e["injection_kind"], e["text"]) for e in events] == [
        ("injection", "system", "workflow-state", "ws"),
        ("message", "system", "", "<task-result/>"),
    ]


def test_orphan_tool_result_dropped_and_no_empty_turn_end():
    entries = [{"type": "message", "timestamp": "2026-08-31T13:00:00.000Z",
                "message": {"role": "toolResult", "toolCallId": "nope",
                            "content": [{"type": "text", "text": "orphan"}]}}]
    assert trace.normalize_entries(entries) == []


def test_load_nested_sub_agent_sessions(tmp_path):
    session_dir = tmp_path / "sessions"
    nested_dir = session_dir / "2026-08-31T13-46-44-335Z_01a05812-86ef-73eb-890b-b44503f5c7b0"
    nested_dir.mkdir(parents=True)
    entries = [
        {"type": "session_init", "timestamp": "2026-08-31T13:46:49.000Z"},
        {"type": "message", "timestamp": "2026-08-31T13:46:50.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "count lines"}]}},
        {"type": "message", "timestamp": "2026-08-31T13:46:51.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "toolCall", "id": "c1", "name": "bash",
                                  "arguments": {"command": "wc -l notes.py"}}]}},
        {"type": "message", "timestamp": "2026-08-31T13:46:52.000Z",
         "message": {"role": "toolResult", "toolCallId": "c1",
                     "content": [{"type": "text", "text": "40 notes.py"}]}},
        {"type": "message", "timestamp": "2026-08-31T13:46:53.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "40"}]}},
    ]
    (nested_dir / "LineCounter.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    (nested_dir / "LineCounter.md").write_text("structured result", encoding="utf-8")

    nested = trace.load_nested(session_dir)
    assert set(nested) == {"LineCounter"}          # .md sibling ignored
    events = nested["LineCounter"]
    assert all(e["agent"] == "LineCounter" for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    calls = [e for e in events if e["kind"] == "tool_call"]
    assert len(calls) == 1 and calls[0]["result"] == "40 notes.py"
    assert events[-1]["kind"] == "turn_end"

    assert trace.load_nested(tmp_path / "absent") == {}


def test_write_and_read_events_roundtrip(tmp_path):
    events = trace.load_session(FIXTURE)
    out = tmp_path / "nested" / "events.jsonl"
    trace.write_events(events, out)
    assert trace.read_events(out) == events
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    assert all(set(json.loads(line)) == EVENT_KEYS for line in lines)


# ------------------------------------------------------------- simulator

def test_consent_question_rule():
    assert sim.is_consent_question("May I create a Trellis task for this?")
    assert sim.is_consent_question("Proceed?\n")
    assert not sim.is_consent_question("Done. Task created.")
    # frozen rule: a turn that made tool calls is never a consent question
    assert not sim.is_consent_question("Shall I also refactor this?", has_tool_calls=True)
    assert not sim.is_consent_question("")


def test_policy_state_machines():
    question = "May I create a Trellis task for this and enter the planning phase?"

    approve_all = sim.UserSimulator(sim.POLICY_APPROVE_ALL)
    assert approve_all.reply(question, 0) == sim.APPROVE_REPLY
    assert approve_all.reply("Finished everything.", 1) is None
    assert approve_all.reply("Another one?", 2) == sim.APPROVE_REPLY

    reject = sim.UserSimulator(sim.POLICY_REJECT_TASK_CREATION)
    assert reject.reply(question, 0) == sim.REJECT_REPLY
    assert reject.reply("Are you sure you don't want a task?", 1) == sim.REJECT_REPLY

    reject_then = sim.UserSimulator(sim.POLICY_REJECT_FIRST_THEN_APPROVE)
    assert reject_then.reply(question, 0) == sim.REJECT_REPLY
    assert reject_then.reply(question, 1) == sim.APPROVE_REPLY

    changes = sim.UserSimulator(sim.POLICY_APPROVE_WITH_CHANGES,
                                objection_lines={1: "I still see duplicates in list."})
    assert changes.reply(question, 0) == sim.APPROVE_REPLY
    # scripted objection fires at its turn index even on a non-question turn
    assert changes.reply("Fixed the duplicate path.", 1, has_tool_calls=True) == \
        "I still see duplicates in list."
    assert changes.reply("All done.", 2) is None

    with pytest.raises(ValueError):
        sim.UserSimulator("bogus-policy")


# -------------------------------------------------------------- snapshot

def _make_template(tmp_path: Path) -> Path:
    template = tmp_path / "template"
    (template / "notes").mkdir(parents=True)
    (template / "notes" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (template / "README.md").write_text("fixture\n", encoding="utf-8")
    identity = ["-c", "user.email=fixture@test", "-c", "user.name=fixture"]
    subprocess.run(["git", *identity, "init", "-q"], cwd=template, check=True)
    subprocess.run(["git", "add", "-A"], cwd=template, check=True)
    subprocess.run(["git", "commit", "-qm", "init fixture"], cwd=template, check=True)
    return template


def test_take_snapshot_hashes_and_git_log(tmp_path):
    sandbox = driver_mod.materialize_sandbox(_make_template(tmp_path),
                                             tmp_path / "run" / "sandbox")
    first = snap.take_snapshot(sandbox)
    assert first["README.md"] == hashlib.sha256(b"fixture\n").hexdigest()
    assert "notes/core.py" in first
    assert first[snap.SNAPSHOT_GIT_LOG_KEY]          # git:log present on a repo

    (sandbox / "new.txt").write_text("x", encoding="utf-8")
    (sandbox / ".trellis" / ".runtime").mkdir(parents=True)
    (sandbox / ".trellis" / ".runtime" / "marker").write_text("noise", encoding="utf-8")
    second = snap.take_snapshot(sandbox)
    assert set(second) - set(first) == {"new.txt"}   # .runtime marker excluded

    subprocess.run(["git", "-C", str(sandbox), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(sandbox),
                    "-c", "user.email=fixture@test", "-c", "user.name=fixture",
                    "commit", "-qm", "change"], check=True)
    third = snap.take_snapshot(sandbox)
    assert third[snap.SNAPSHOT_GIT_LOG_KEY] != first[snap.SNAPSHOT_GIT_LOG_KEY]

    event = snap.snapshot_event(sandbox, ts=1234.5, seq=7)
    assert event["kind"] == "snapshot" and event["role"] == "system"
    assert event["seq"] == 7 and event["ts"] == 1234.5
    assert set(event) == EVENT_KEYS


def test_materialize_sandbox_resets_git_state(tmp_path):
    template = _make_template(tmp_path)
    sandbox = driver_mod.materialize_sandbox(template, tmp_path / "run" / "sandbox")
    assert (sandbox / "notes" / "core.py").exists()
    assert (sandbox / ".git").is_dir()
    status = subprocess.run(["git", "-C", str(sandbox), "status", "--porcelain"],
                            capture_output=True, text=True, check=True)
    assert status.stdout == ""                       # git reset left it clean

    with pytest.raises(FileExistsError):
        driver_mod.materialize_sandbox(template, sandbox)


# ---------------------------------------------------------------- driver

FAKE_OMP = textwrap.dedent(r"""
    #!/usr/bin/env python3
    # Fake omp -p: canned consent-loop stdout + session JSONL for driver tests.
    import json, os, sys, time, uuid
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    args = sys.argv[1:]
    cont = "--continue" in args
    session_dir = args[args.index("--session-dir") + 1]
    prompt = args[-1]
    time.sleep(float(os.environ.get("FAKE_OMP_SLEEP", "0")))

    def emit(obj):
        print(json.dumps(obj), flush=True)

    def entry(message):
        return {"type": "message", "id": uuid.uuid4().hex[:8],
                "timestamp": now(), "message": message}

    def assistant_message(text, usage=None, tool_call=None):
        content = []
        if tool_call is not None:
            content.append({"type": "toolCall", "id": tool_call["id"],
                            "name": tool_call["name"], "arguments": tool_call["arguments"]})
        content.append({"type": "text", "text": text})
        message = {"role": "assistant", "content": content}
        if usage:
            message["usage"] = usage
        return message

    usage1 = {"input": 100, "output": 20, "cacheRead": 0, "totalTokens": 120,
              "cost": {"total": 0.01}}
    usage2 = {"input": 200, "output": 50, "cacheRead": 0, "totalTokens": 250,
              "cost": {"total": 0.02}}
    question = "May I create a Trellis task for this and enter the planning phase?"
    ctx = {"type": "custom_message", "customType": "trellis-session-context",
           "content": "<session-context/>", "timestamp": now()}
    ws = {"type": "custom_message", "customType": "trellis-workflow-state",
          "content": "<workflow-state>[workflow-state:no_task]</workflow-state>",
          "timestamp": now()}

    if not cont:
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(
            session_dir, time.strftime("%Y-%m-%dT%H-%M-%S-000Z_") + str(uuid.uuid4()) + ".jsonl")
        entries = [
            {"type": "title", "v": 1, "title": "", "updatedAt": now()},
            {"type": "session", "version": 3, "id": str(uuid.uuid4()),
             "timestamp": now(), "cwd": os.getcwd()},
            ctx,
            entry({"role": "user", "content": [{"type": "text", "text": prompt}]}),
            ws,
            entry(assistant_message(question, usage=usage1)),
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        emit({"type": "message_end", "message": entries[-1]["message"]})
    else:
        existing = sorted(p for p in os.listdir(session_dir) if p.endswith(".jsonl"))
        path = os.path.join(session_dir, existing[-1])
        loop = os.environ.get("FAKE_OMP_LOOP") == "1"
        if loop:
            assistant = assistant_message("Shall I keep going?", usage=usage1)
            entries = [ctx, entry({"role": "user", "content": [{"type": "text", "text": prompt}]}),
                       ws, entry(assistant)]
        else:
            call = {"id": "call_fake1", "name": "read", "arguments": {"path": "notes.py"}}
            assistant = assistant_message("Done. Task created.", usage=usage2, tool_call=call)
            tool_result = {"role": "toolResult", "toolCallId": "call_fake1",
                           "toolName": "read",
                           "content": [{"type": "text", "text": "note file"}]}
            entries = [ctx, entry({"role": "user", "content": [{"type": "text", "text": prompt}]}),
                       ws, entry(assistant), entry(tool_result)]
        with open(path, "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        emit({"type": "message_end", "message": assistant})
        if not loop:
            emit({"type": "message_end", "message": tool_result})
""")


def _make_fake_omp(tmp_path: Path) -> str:
    script = tmp_path / "fake-omp"
    script.write_text(FAKE_OMP.lstrip("\n"), encoding="utf-8")  # shebang at byte 0
    script.chmod(0o755)
    return str(script)


def _make_driver(tmp_path: Path, **kwargs) -> driver_mod.Driver:
    sandbox = driver_mod.materialize_sandbox(_make_template(tmp_path),
                                             tmp_path / "run" / "sandbox")
    defaults = dict(omp_bin=_make_fake_omp(tmp_path), per_turn_timeout=60.0)
    defaults.update(kwargs)
    return driver_mod.Driver(sandbox, tmp_path / "run" / "sessions", **defaults)


def test_driver_run_session_consent_loop_end_to_end(tmp_path):
    drv = _make_driver(tmp_path)
    result = drv.run_session("Add a tagging feature",
                             sim.UserSimulator(sim.POLICY_APPROVE_ALL), max_turns=5)

    # frozen flag shape actually handed to omp (fake omp ignores extras)
    assert result.turns[0].is_consent_question and result.turns[0].text.endswith("?")
    assert not result.turns[0].has_tool_calls
    assert result.turns[1].has_tool_calls and not result.turns[1].is_consent_question
    assert result.turns[0].session_file == result.turns[1].session_file
    assert result.turns[1].session_file.exists()

    # per-turn cost deltas from session-file usage fields
    assert result.turns[0].usage["totalTokens"] == 120
    assert result.turns[1].usage["totalTokens"] == 250
    assert result.total_usage()["totalTokens"] == 370
    assert result.total_usage()["cost"] == pytest.approx(0.03)

    events = result.events
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    kinds = [e["kind"] for e in events]
    assert kinds.count("snapshot") == 2              # one per turn
    assert kinds.count("turn_end") == 2
    assert kinds.count("injection") == 4             # 2 session-context + 2 ws
    assert all(e["usage"]["totalTokens"] > 0 for e in events if e["kind"] == "turn_end")

    calls = [e for e in events if e["kind"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "read" and calls[0]["result"] == "note file"

    # the simulator's approval actually flowed into turn 2's user message
    user_texts = [e["text"] for e in events
                  if e["kind"] == "message" and e["role"] == "user"]
    assert user_texts == ["Add a tagging feature", sim.APPROVE_REPLY]

    # snapshot after every turn; last event is the final snapshot
    assert events[-1]["kind"] == "snapshot"
    assert events[-1]["snapshot"][snap.SNAPSHOT_GIT_LOG_KEY]
    assert "notes/core.py" in events[-1]["snapshot"]
    assert all(e["agent"] == "" for e in events)


def test_driver_enforces_max_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OMP_LOOP", "1")
    drv = _make_driver(tmp_path)
    result = drv.run_session("go", sim.UserSimulator(sim.POLICY_APPROVE_ALL),
                             max_turns=3)
    assert len(result.turns) == 3                    # approve_all keeps replying
    assert all(t.is_consent_question for t in result.turns)
    assert [e["seq"] for e in result.events] == list(range(1, len(result.events) + 1))
    assert [e["kind"] for e in result.events].count("snapshot") == 3


def test_driver_without_simulator_runs_single_turn(tmp_path):
    drv = _make_driver(tmp_path)
    result = drv.run_session("What does search do?", None)
    assert len(result.turns) == 1
    assert result.turns[0].is_consent_question       # loop stops: no one replies


def test_driver_enforces_per_turn_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OMP_SLEEP", "8")
    drv = _make_driver(tmp_path, per_turn_timeout=0.5)
    result = drv.run_turn("hello")
    assert result.timed_out
    assert result.exit_code is None
    assert result.duration_s < 8                     # killed well before sleep ends
    assert result.entries == [] and result.usage == {}


def test_stdout_parser_s1_message_end_shape():
    stdout = "\n".join([
        "not json at all",
        json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Shall I proceed?"}]}}),
    ])
    result = driver_mod._turn_from_stdout(stdout)
    assert result.text == "Shall I proceed?"
    assert not result.has_tool_calls and result.is_consent_question is False

    # user/toolResult payloads on stdout are ignored; mixed content keeps order
    mixed = json.dumps({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c1", "name": "bash",
                     "partialArgs": "{\"command\": \"ls\"}"},
                    {"type": "text", "text": "Done."}]}})
    result = driver_mod._turn_from_stdout(mixed)
    assert result.has_tool_calls
    assert result.tool_calls == [{"name": "bash", "args": {"command": "ls"}}]
    assert result.text == "Done."
