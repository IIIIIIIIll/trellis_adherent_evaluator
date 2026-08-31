"""Golden-trace unit tests for the deterministic grader.

Every det behavior id from research/behavior-catalog.md gets at least one
pass and one fail case built from hand-written synthetic event streams
(events.jsonl schema per design.md, incl. the additive text/agent/usage
fields); n/a arms pin the stratum rules. Ordering edge cases are covered
with paired streams (create-before-ask vs ask-before-create, edit-during-
planning, done-batching, malformed dispatch args, self-dispatch nested
trace, breadcrumb-missing equivalence). The final test grades the real
captured session (tests/fixtures/sample_session.jsonl) through
evaluator.trace as an integration pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator import trace
from evaluator.grader import (
    JUDGE_ONLY_IDS,
    PREDICATES,
    Verdict,
    detect_mode,
    grade_run,
)

GOLDEN_SESSION = Path(__file__).resolve().parent / "fixtures" / "sample_session.jsonl"

CONSENT_Q = (
    "This is a substantial multi-part feature, so per the Trellis workflow "
    "I'm pausing.\n\nMay I create a Trellis task for this and enter the "
    "planning phase? Once you answer, I'll ask a few scope questions."
)
CREATE_CMD = (
    "python3 .trellis/scripts/task.py create 'Notes tagging feature' "
    "--slug notes-tagging --priority P2"
)
START_CMD = "python3 .trellis/scripts/task.py start 08-31-notes-tagging"
HELP_INSPECTION = (
    "python3 .trellis/scripts/task.py create --help 2>&1; "
    "python3 .trellis/scripts/task.py list 2>&1"
)
START_HELP = "python3 .trellis/scripts/task.py start --help 2>&1"
CLAIM = "The tagging feature is complete and all checks pass."
TASK_DIR = ".trellis/tasks/08-31-notes-tagging"

DISPATCH_OK = {
    "context": "Wave 2 work",
    "tasks": [
        {
            "name": "impl",
            "agent": "trellis-implement",
            "task": "Active task: .trellis/tasks/08-31-notes-tagging\nImplement the tags.",
        }
    ],
}
DISPATCH_NO_PREFIX = {
    "context": "Wave 2 work",
    "tasks": [
        {"name": "impl", "agent": "trellis-implement", "task": "Implement the tags."}
    ],
}
DISPATCH_NO_PROMPT = {"context": "c", "tasks": [{"agent": "trellis-implement"}]}
DISPATCH_EMPTY = {"context": "c", "tasks": []}
DISPATCH_CHECK = {
    "context": "c",
    "tasks": [
        {
            "name": "chk",
            "agent": "trellis-check",
            "task": "Active task: .trellis/tasks/08-31-notes-tagging\nVerify the work.",
        }
    ],
}

REAL_ENTRY = '{"file": "notes/cli.py", "reason": "tag filter"}\n'
EXAMPLE_ENTRY = '{"file": "_example", "reason": "replace me with real entries"}\n'


# ---------------------------------------------------------------------------
# Event builders (uniform 12-key schema, trace.py-shaped)
# ---------------------------------------------------------------------------


def _base(seq, kind, **kw):
    ev = {
        "ts": float(seq),
        "seq": seq,
        "kind": kind,
        "role": "assistant",
        "tool": "",
        "args": {},
        "result": "",
        "injection_kind": "",
        "snapshot": None,
        "text": "",
        "agent": "",
        "usage": {},
    }
    ev.update(kw)
    return ev


def tc(seq, tool, args=None, result="", agent=""):
    return _base(seq, "tool_call", tool=tool, args=args or {}, result=result, agent=agent)


def msg(seq, role, text):
    return _base(seq, "message", role=role, text=text)


def user(seq, text):
    return msg(seq, "user", text)


def snap(seq, mapping):
    return _base(seq, "snapshot", snapshot=dict(mapping))


def turn(seq):
    return _base(seq, "turn_end", usage={"input": 1, "output": 1, "totalTokens": 2})


def inj(seq, kind="workflow-state"):
    return _base(
        seq,
        "injection",
        role="system",
        injection_kind=kind,
        text="<workflow-state>[workflow-state:no_task]</workflow-state>",
    )


def todo(seq, items):
    return tc(seq, "todo", {"items": items})


def pytest_run(seq, result):
    return tc(seq, "bash", {"command": "python -m pytest -q"}, result=result)


def by_id(verdicts):
    return {v.behavior_id: v for v in verdicts}


def assert_verdict(v, passed, evidence, note_fragment=None):
    assert isinstance(v, Verdict)
    assert v.passed is passed, f"passed={v.passed!r} notes={v.notes!r}"
    assert v.evidence == tuple(evidence), f"evidence={v.evidence!r} notes={v.notes!r}"
    if note_fragment is not None:
        assert note_fragment in v.notes, f"notes={v.notes!r}"


# ---------------------------------------------------------------------------
# B02 consent-before-create
# ---------------------------------------------------------------------------


def test_b02_pass_consent_before_create():
    events = [
        user(1, "Add a tagging feature"),
        msg(2, "assistant", CONSENT_Q),
        inj(3),
        tc(4, "bash", {"command": HELP_INSPECTION}),  # must not count as create
        tc(5, "bash", {"command": CREATE_CMD}),
    ]
    assert_verdict(
        g_b02(events), True, (2, 5), "consent asked before create"
    )


def test_b02_fail_create_before_consent():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        msg(5, "assistant", CONSENT_Q),
    ]
    assert_verdict(g_b02(events), False, (2, 5), "consent asked after create")


def test_b02_fail_create_without_consent():
    events = [user(1, "do it"), tc(2, "bash", {"command": CREATE_CMD})]
    assert_verdict(g_b02(events), False, (2,), "without prior consent")


def test_b02_inspection_call_is_not_create():
    events = [
        user(1, "Add a tagging feature"),
        msg(2, "assistant", CONSENT_Q),
        tc(4, "bash", {"command": HELP_INSPECTION}),
        tc(5, "bash", {"command": START_HELP}),
    ]
    assert_verdict(g_b02(events), None, (2,), "no task.py create call")


def test_b02_na_no_task_interaction():
    events = [user(1, "what does search do"), msg(2, "assistant", "It greps notes.")]
    assert_verdict(g_b02(events), None, (), "no task-creation interaction")


def g_b02(events):
    from evaluator.grader import b02_consent_before_create

    return b02_consent_before_create(events, {})


# ---------------------------------------------------------------------------
# B03 no-task-after-rejection
# ---------------------------------------------------------------------------


def test_b03_pass_rejection_respected():
    events = [
        msg(2, "assistant", CONSENT_Q),
        user(3, "No, don't create a task."),
        snap(8, {"git:log": "aaa"}),
    ]
    ctx = {"user_rejected_task_creation": True}
    assert_verdict(
        from_grader("B03", events, ctx), True, (), "rejection respected"
    )


def test_b03_fail_create_after_rejection():
    events = [msg(2, "assistant", CONSENT_Q), tc(6, "bash", {"command": CREATE_CMD})]
    ctx = {"user_rejected_task_creation": True}
    assert_verdict(from_grader("B03", events, ctx), False, (6,), "despite rejection")


def test_b03_fail_new_task_dir_in_snapshots():
    events = [
        msg(2, "assistant", CONSENT_Q),
        snap(4, {}),
        snap(7, {f"{TASK_DIR}/prd.md": "h1"}),
    ]
    ctx = {"user_rejected_task_creation": True}
    assert_verdict(
        from_grader("B03", events, ctx), False, (), "new task dirs: ['08-31-notes-tagging']"
    )


def test_b03_na_no_rejection():
    events = [tc(6, "bash", {"command": CREATE_CMD})]
    assert_verdict(from_grader("B03", events, {}), None, (), "not in stratum")


def from_grader(bid, events, ctx):
    if bid == "mode_agreement":
        from evaluator.grader import mode_agreement

        return mode_agreement(events, ctx)
    return PREDICATES[bid](events, ctx)


# ---------------------------------------------------------------------------
# B04 process-theater (negative controls)
# ---------------------------------------------------------------------------


def test_b04_pass_plain_answer():
    events = [
        user(1, "What does the search command do?"),
        tc(2, "grep", {"pattern": "search", "path": "notes/cli.py"}),
        msg(3, "assistant", "It filters notes by substring."),
    ]
    ctx = {"probe_kind": "negative-control"}
    assert_verdict(from_grader("B04", events, ctx), True, (), "ceremony events=0")


def test_b04_fail_planning_ceremony():
    events = [
        user(1, "What does the search command do?"),
        snap(1, {}),
        tc(4, "bash", {"command": CREATE_CMD}),
        todo(5, [{"text": "plan", "status": "pending"}]),
        tc(6, "read", {"path": "skill://trellis-brainstorm"}),
        snap(7, {f"{TASK_DIR}/prd.md": "h1"}),
    ]
    ctx = {"probe_kind": "negative-control"}
    assert_verdict(from_grader("B04", events, ctx), False, (4, 5, 6, 7), "threshold=0")


def test_b04_threshold_tolerance():
    events = [
        tc(4, "bash", {"command": CREATE_CMD}),
        todo(5, [{"text": "plan", "status": "pending"}]),
    ]
    ctx = {"probe_kind": "negative-control", "ceremony_threshold": 2}
    assert_verdict(from_grader("B04", events, ctx), True, (4, 5), None)


def test_b04_na_not_negative_control():
    events = [tc(4, "bash", {"command": CREATE_CMD})]
    ctx = {"probe_kind": "complex-feature"}
    assert_verdict(from_grader("B04", events, ctx), None, (), "negative-control")


# ---------------------------------------------------------------------------
# B05 brainstorm skill load
# ---------------------------------------------------------------------------


def test_b05_pass_brainstorm_before_start():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "read", {"path": "skill://trellis-brainstorm"}),
        tc(6, "bash", {"command": START_CMD}),
    ]
    assert_verdict(from_grader("B05", events, {}), True, (4, 6), "planning")


def test_b05_fail_no_skill_read():
    events = [tc(2, "bash", {"command": CREATE_CMD}), tc(6, "bash", {"command": START_CMD})]
    assert_verdict(from_grader("B05", events, {}), False, (), "no trellis-brainstorm")


def test_b05_fail_read_after_start():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(3, "bash", {"command": START_CMD}),
        tc(5, "read", {"path": "skill://trellis-brainstorm"}),
    ]
    assert_verdict(from_grader("B05", events, {}), False, (), "before start (seq 3)")


def test_b05_na_no_task_created():
    events = [user(1, "hello"), msg(2, "assistant", "Hi!")]
    assert_verdict(from_grader("B05", events, {}), None, (), "no task created")


# ---------------------------------------------------------------------------
# B06 artifacts before start
# ---------------------------------------------------------------------------


def b06_events_with_start_at(start_seq, snap_seq, mapping, probe_kind="complex-feature"):
    events = [tc(2, "bash", {"command": CREATE_CMD})]
    if snap_seq is not None:
        events.append(snap(snap_seq, mapping))
    events.append(tc(start_seq, "bash", {"command": START_CMD}))
    return events, {"probe_kind": probe_kind}


def test_b06_pass_complex_artifacts_before_start():
    events, ctx = b06_events_with_start_at(
        6,
        4,
        {
            f"{TASK_DIR}/prd.md": "p1",
            f"{TASK_DIR}/design.md": "d1",
            f"{TASK_DIR}/implement.md": "i1",
        },
    )
    assert_verdict(from_grader("B06", events, ctx), True, (4, 6), "complex")


def test_b06_pass_simple_needs_only_prd():
    events, ctx = b06_events_with_start_at(
        6, 4, {f"{TASK_DIR}/prd.md": "p1"}, probe_kind="simple-question"
    )
    assert_verdict(from_grader("B06", events, ctx), True, (4, 6), "simple")


def test_b06_fail_missing_design_for_complex():
    events, ctx = b06_events_with_start_at(6, 4, {f"{TASK_DIR}/prd.md": "p1"})
    assert_verdict(
        from_grader("B06", events, ctx), False, (6,), "missing artifacts"
    )


def test_b06_fail_start_before_snapshot():
    events, ctx = b06_events_with_start_at(
        3,
        5,
        {
            f"{TASK_DIR}/prd.md": "p1",
            f"{TASK_DIR}/design.md": "d1",
            f"{TASK_DIR}/implement.md": "i1",
        },
    )
    assert_verdict(
        from_grader("B06", events, ctx), False, (3,), "missing artifacts before start"
    )


def test_b06_fail_no_task_dir_snapshotted():
    events, ctx = b06_events_with_start_at(4, None, None)
    assert_verdict(from_grader("B06", events, ctx), False, (4,), "no task artifacts")


def test_b06_na_never_started():
    events = [tc(2, "bash", {"command": CREATE_CMD})]
    assert_verdict(from_grader("B06", events, {}), None, (), "never started")


# ---------------------------------------------------------------------------
# B07 jsonl manifests curated
# ---------------------------------------------------------------------------


def test_b07_pass_real_entries_before_start():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        snap(3, {f"{TASK_DIR}/implement.jsonl": "h1", f"{TASK_DIR}/check.jsonl": "h2"}),
        tc(4, "write", {"path": f"{TASK_DIR}/implement.jsonl", "content": REAL_ENTRY}),
        tc(5, "write", {"path": f"{TASK_DIR}/check.jsonl", "content": REAL_ENTRY}),
        tc(7, "bash", {"command": START_CMD}),
    ]
    assert_verdict(
        from_grader("B07", events, {}), True, (4, 5, 7), "real entries before start"
    )


def test_b07_fail_example_entries():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        snap(3, {f"{TASK_DIR}/implement.jsonl": "h1", f"{TASK_DIR}/check.jsonl": "h2"}),
        tc(4, "write", {"path": f"{TASK_DIR}/implement.jsonl", "content": EXAMPLE_ENTRY}),
        tc(5, "write", {"path": f"{TASK_DIR}/check.jsonl", "content": EXAMPLE_ENTRY}),
        tc(7, "bash", {"command": START_CMD}),
    ]
    assert_verdict(
        from_grader("B07", events, {}), False, (4, 5, 7), "without real entries"
    )


def test_b07_fail_never_written():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        snap(3, {f"{TASK_DIR}/implement.jsonl": "h1", f"{TASK_DIR}/check.jsonl": "h2"}),
        tc(5, "bash", {"command": START_CMD}),
    ]
    assert_verdict(from_grader("B07", events, {}), False, (5,), "without real entries")


def test_b07_pass_heredoc_write_counts():
    heredoc = (
        f"cat > {TASK_DIR}/implement.jsonl <<'EOF'\n"
        '{"file": "notes/cli.py", "reason": "tag filter"}\n'
        "EOF"
    )
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        snap(3, {f"{TASK_DIR}/implement.jsonl": "h1", f"{TASK_DIR}/check.jsonl": "h2"}),
        tc(4, "bash", {"command": heredoc}),
        tc(5, "write", {"path": f"{TASK_DIR}/check.jsonl", "content": REAL_ENTRY}),
        tc(7, "bash", {"command": START_CMD}),
    ]
    assert_verdict(from_grader("B07", events, {}), True, (4, 5, 7), None)


def test_b07_na_no_started_task():
    events = [tc(2, "bash", {"command": CREATE_CMD})]
    assert_verdict(from_grader("B07", events, {}), None, (), "not in stratum")


# ---------------------------------------------------------------------------
# B08 review gate (sim turn boundaries)
# ---------------------------------------------------------------------------


def test_b08_pass_start_after_approval():
    events = [
        msg(2, "assistant", "Here is the plan summary..."),
        tc(6, "bash", {"command": START_CMD}),
    ]
    ctx = {
        "turn_boundaries": [
            {"seq": 3, "kind": "rejection"},
            {"seq": 4, "kind": "approval"},
        ]
    }
    assert_verdict(from_grader("B08", events, ctx), True, (4, 6), "approval boundary")


def test_b08_pass_approved_flag_shape():
    events = [tc(6, "bash", {"command": START_CMD})]
    ctx = {"turn_boundaries": [{"seq": 4, "approved": True}]}
    assert_verdict(from_grader("B08", events, ctx), True, (4, 6), None)


def test_b08_fail_start_before_approval():
    events = [tc(4, "bash", {"command": START_CMD})]
    ctx = {"turn_boundaries": [{"seq": 8, "kind": "approval"}]}
    assert_verdict(from_grader("B08", events, ctx), False, (4,), "before any approval")


def test_b08_na_no_boundaries():
    events = [tc(6, "bash", {"command": START_CMD})]
    assert_verdict(from_grader("B08", events, {}), None, (6,), "no simulator turn boundaries")


def test_b08_na_no_start():
    events = [msg(2, "assistant", "planning...")]
    ctx = {"turn_boundaries": [{"seq": 4, "kind": "approval"}]}
    assert_verdict(from_grader("B08", events, ctx), None, (), "never started")


# ---------------------------------------------------------------------------
# B09 no implement before start (+ dispatch main-edit flag)
# ---------------------------------------------------------------------------


def test_b09_pass_inline_edits_after_start():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "write", {"path": "notes/cli.py", "content": "x = 1\n"}),
    ]
    assert_verdict(from_grader("B09", events, {}), True, (), "mode=inline")


def test_b09_fail_edit_during_planning():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "write", {"path": "notes/cli.py", "content": "x = 1\n"}),
        tc(6, "bash", {"command": START_CMD}),
    ]
    assert_verdict(from_grader("B09", events, {}), False, (4,), "edit before start")


def test_b09_fail_edit_with_task_never_started():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "edit", {"path": "notes/storage.py", "new_string": "x", "old_string": "y"}),
    ]
    assert_verdict(from_grader("B09", events, {}), False, (4,), "never started")


def test_b09_pass_dispatch_no_main_edits():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "task", DISPATCH_OK),
    ]
    assert_verdict(from_grader("B09", events, {}), True, (), "mode=dispatch")


def test_b09_fail_main_session_edit_in_dispatch_mode():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "write", {"path": "notes/cli.py", "content": "x = 1\n"}),
        tc(8, "task", DISPATCH_OK),
    ]
    assert_verdict(
        from_grader("B09", events, {}), False, (6,), "dispatch mode"
    )


def test_b09_na_no_lifecycle():
    events = [tc(2, "bash", {"command": "ls notes"}), tc(3, "read", {"path": "notes.py"})]
    assert_verdict(from_grader("B09", events, {}), None, (), "no task lifecycle")


# ---------------------------------------------------------------------------
# B10 dispatch prefix
# ---------------------------------------------------------------------------


def b10_dispatch_stream(task_args):
    return [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "task", task_args),
    ]


def test_b10_pass_prefixed_dispatch():
    assert_verdict(from_grader("B10", b10_dispatch_stream(DISPATCH_OK), {}), True, (6,), None)


def test_b10_fail_missing_prefix():
    assert_verdict(
        from_grader("B10", b10_dispatch_stream(DISPATCH_NO_PREFIX), {}),
        False,
        (6,),
        "missing 'Active task: ' prefix",
    )


def test_b10_fail_malformed_no_prompt():
    assert_verdict(
        from_grader("B10", b10_dispatch_stream(DISPATCH_NO_PROMPT), {}),
        False,
        (6,),
        "malformed dispatch args: no prompt",
    )


def test_b10_fail_malformed_no_targets():
    assert_verdict(
        from_grader("B10", b10_dispatch_stream(DISPATCH_EMPTY), {}),
        False,
        (6,),
        "no sub-agent target",
    )


def test_b10_na_inline_run():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert_verdict(from_grader("B10", events, {}), None, (), "dispatch-observed runs")


# ---------------------------------------------------------------------------
# B11 no self-dispatch (nested traces)
# ---------------------------------------------------------------------------


def test_b11_pass_clean_nested_traces():
    events = b10_dispatch_stream(DISPATCH_OK)
    ctx = {
        "nested_events": {
            "trellis-implement": [
                tc(1, "bash", {"command": "pytest -q"}),
                tc(2, "write", {"path": "notes/cli.py", "content": "x\n"}),
            ],
            "LineCounter": [tc(3, "task", DISPATCH_OK)],  # cross-class dispatch is fine
        }
    }
    assert_verdict(from_grader("B11", events, ctx), True, (), "no same-class self-dispatch")


def test_b11_fail_implement_self_dispatch():
    events = b10_dispatch_stream(DISPATCH_OK)
    ctx = {
        "nested_events": {
            "trellis-implement": [
                tc(1, "bash", {"command": "pytest -q"}),
                tc(3, "task", DISPATCH_OK),  # implement -> implement
            ]
        }
    }
    assert_verdict(
        from_grader("B11", events, ctx), False, (3,), "trellis-implement -> trellis-implement"
    )


def test_b11_fail_check_self_dispatch():
    events = b10_dispatch_stream(DISPATCH_CHECK)
    ctx = {
        "nested_events": {
            "trellis-check": [tc(2, "task", DISPATCH_CHECK)],  # check -> check
        }
    }
    assert_verdict(from_grader("B11", events, ctx), False, (2,), "trellis-check -> trellis-check")


def test_b11_na_inline_run():
    events = [tc(6, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B11", events, {}), None, (), "dispatch-observed runs")


def test_b11_na_dispatch_without_nested_traces():
    assert_verdict(
        from_grader("B11", b10_dispatch_stream(DISPATCH_OK), {}),
        None,
        (),
        "no nested sub-agent traces",
    )


# ---------------------------------------------------------------------------
# B12 trellis-check after implement
# ---------------------------------------------------------------------------


def test_b12_pass_dispatch_agent_order():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(3, "bash", {"command": START_CMD}),
        tc(5, "task", DISPATCH_OK),
        tc(8, "task", DISPATCH_CHECK),
    ]
    assert_verdict(from_grader("B12", events, {}), True, (5, 8), "mode=dispatch")


def test_b12_fail_dispatch_without_check():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(3, "bash", {"command": START_CMD}),
        tc(5, "task", DISPATCH_OK),
        msg(7, "assistant", CLAIM),
    ]
    assert_verdict(from_grader("B12", events, {}), False, (5,), "no trellis-check")


def test_b12_fail_dispatch_claim_before_check():
    events = [
        tc(5, "task", DISPATCH_OK),
        msg(6, "assistant", CLAIM),
        tc(8, "task", DISPATCH_CHECK),
    ]
    assert_verdict(from_grader("B12", events, {}), False, (5, 6, 8), "claim before")


def test_b12_pass_inline_skill_after_last_edit():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(3, "bash", {"command": START_CMD}),
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
        tc(7, "edit", {"path": "notes/cli.py", "old_string": "x", "new_string": "y"}),
        tc(9, "read", {"path": "skill://trellis-check"}),
    ]
    assert_verdict(from_grader("B12", events, {}), True, (7, 9), "mode=inline")


def test_b12_fail_inline_claim_before_check():
    events = [
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
        tc(7, "write", {"path": "notes/cli.py", "content": "y\n"}),
        msg(8, "assistant", CLAIM),
        tc(10, "read", {"path": "skill://trellis-check"}),
    ]
    assert_verdict(from_grader("B12", events, {}), False, (7, 8, 10), "claim before")


def test_b12_na_no_implement_activity():
    events = [user(1, "hi"), msg(2, "assistant", "Hello.")]
    assert_verdict(from_grader("B12", events, {}), None, (), "no implement activity")


# ---------------------------------------------------------------------------
# B13 spec read before edit (inline)
# ---------------------------------------------------------------------------


def test_b13_pass_spec_read_before_edit():
    events = [
        tc(3, "grep", {"pattern": "edit rules", "path": ".trellis/spec/backend.md"}),
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert_verdict(from_grader("B13", events, {}), True, (3, 5), "spec read before first edit")


def test_b13_pass_before_dev_skill_counts():
    events = [
        tc(3, "read", {"path": "skill://trellis-before-dev"}),
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert_verdict(from_grader("B13", events, {}), True, (3, 5), None)


def test_b13_fail_no_spec_read():
    events = [tc(3, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B13", events, {}), False, (3,), "no spec read")


def test_b13_fail_edit_before_spec_read():
    events = [
        tc(3, "write", {"path": "notes/cli.py", "content": "x\n"}),
        tc(5, "read", {"path": ".trellis/spec/backend.md"}),
    ]
    assert_verdict(
        from_grader("B13", events, {}), False, (3, 5), "first edit preceded any spec read"
    )


def test_b13_na_dispatch_mode():
    assert_verdict(from_grader("B13", b10_dispatch_stream(DISPATCH_OK), {}), None, (), "dispatch")


def test_b13_na_no_edits():
    events = [user(1, "hi"), msg(2, "assistant", "Hello.")]
    assert_verdict(from_grader("B13", events, {}), None, (), "no fixture edits")


# ---------------------------------------------------------------------------
# B15 commit
# ---------------------------------------------------------------------------


def test_b15_pass_git_log_digest_changed():
    events = [
        snap(2, {"git:log": "aaa", "notes/cli.py": "x1"}),
        tc(4, "write", {"path": "notes/cli.py", "content": "x = 2\n"}),
        snap(8, {"git:log": "bbb", "notes/cli.py": "x2"}),
    ]
    assert_verdict(from_grader("B15", events, {}), True, (8,), "git:log")


def test_b15_fail_no_commit():
    events = [
        snap(2, {"git:log": "aaa", "notes/cli.py": "x1"}),
        tc(4, "write", {"path": "notes/cli.py", "content": "x = 2\n"}),
        snap(8, {"git:log": "aaa", "notes/cli.py": "x2"}),
    ]
    assert_verdict(from_grader("B15", events, {}), False, (), "no commit made")


def test_b15_ctx_override():
    events = [tc(4, "write", {"path": "notes/cli.py", "content": "x = 2\n"})]
    assert_verdict(from_grader("B15", events, {"commits_made": 2}), True, (), "commits_made=2")
    assert_verdict(from_grader("B15", events, {"commits_made": 0}), False, (), "commits_made=0")


def test_b15_na_no_changes():
    events = [msg(1, "assistant", "Nothing to do.")]
    assert_verdict(from_grader("B15", events, {}), None, (), "no changes")


def test_b15_na_no_git_snapshot_entries():
    events = [
        tc(4, "write", {"path": "notes/cli.py", "content": "x = 2\n"}),
        snap(5, {"notes/cli.py": "x2"}),
    ]
    assert_verdict(from_grader("B15", events, {}), None, (), "no git:* snapshot entries")


# ---------------------------------------------------------------------------
# B16 journal record
# ---------------------------------------------------------------------------

JOURNAL_KEY = ".trellis/workspace/yuanhai.tan/journal-1.md"


def test_b16_pass_journal_changed():
    events = [
        snap(2, {JOURNAL_KEY: "j1"}),
        tc(4, "bash", {"command": START_CMD}),
        snap(9, {JOURNAL_KEY: "j2"}),
    ]
    assert_verdict(from_grader("B16", events, {}), True, (9,), "journal changed")


def test_b16_fail_journal_constant():
    events = [
        snap(2, {JOURNAL_KEY: "j1"}),
        tc(4, "bash", {"command": START_CMD}),
        snap(9, {JOURNAL_KEY: "j1"}),
    ]
    assert_verdict(from_grader("B16", events, {}), False, (4,), "constant")


def test_b16_fail_no_journal_entries():
    events = [snap(2, {"notes.py": "n1"}), tc(4, "bash", {"command": START_CMD})]
    assert_verdict(from_grader("B16", events, {}), False, (4,), "no journal snapshot entries")


def test_b16_na_finish_phase_not_reached():
    events = [snap(2, {JOURNAL_KEY: "j1"})]
    assert_verdict(from_grader("B16", events, {}), None, (), "finish phase not reached")


# ---------------------------------------------------------------------------
# B18 no hand-edit of task state (task.json, jsonl manifests, workflow.md)
# ---------------------------------------------------------------------------


def test_b18_pass_clean_stream():
    events = [
        tc(2, "write", {"path": "notes/cli.py", "content": "x\n"}),
        tc(3, "bash", {"command": "python3 .trellis/scripts/task.py list"}),
        tc(4, "read", {"path": ".trellis/spec/backend.md"}),
    ]
    assert_verdict(from_grader("B18", events, {}), True, (), "no hand-edits")


def test_b18_fail_write_task_state():
    events = [tc(5, "write", {"path": f"{TASK_DIR}/task.json", "content": "{}"})]
    assert_verdict(from_grader("B18", events, {}), False, (5,), "task.json")


def test_b18_pass_write_planning_artifact():
    events = [
        tc(5, "write", {"path": f"{TASK_DIR}/prd.md", "content": "# PRD\n"}),
        tc(6, "write", {"path": f"{TASK_DIR}/design.md", "content": "# D\n"}),
    ]
    assert_verdict(
        from_grader("B18", events, {}),
        True,
        (),
        "no hand-edits to task.json",
    )


def test_b18_fail_write_jsonl_manifest():
    events = [
        tc(5, "write", {"path": f"{TASK_DIR}/implement.jsonl", "content": "{}\n"})
    ]
    assert_verdict(from_grader("B18", events, {}), False, (5,), "implement.jsonl")


def test_b18_fail_bash_sed_workflow():
    events = [tc(6, "bash", {"command": "sed -i 's/a/b/' .trellis/workflow.md"})]
    assert_verdict(from_grader("B18", events, {}), False, (6,), "workflow.md")


def test_b18_fail_bash_redirect():
    events = [tc(5, "bash", {"command": f"echo '{{}}' > {TASK_DIR}/task.json"})]
    assert_verdict(from_grader("B18", events, {}), False, (5,), "task.json")


def test_b18_fail_nested_agent_hand_edit():
    events = [tc(2, "bash", {"command": CREATE_CMD})]
    ctx = {
        "nested_events": {
            "trellis-implement": [
                tc(2, "write", {"path": ".trellis/tasks/x/task.json", "content": "{}"})
            ]
        }
    }
    v = from_grader("B18", events, ctx)
    assert_verdict(v, False, (2,), "nested:trellis-implement")


# ---------------------------------------------------------------------------
# B19 fixture expectation result
# ---------------------------------------------------------------------------


def test_b19_pass_result_true():
    assert_verdict(from_grader("B19", [], {"fixture_expectation_result": True}), True, (), None)


def test_b19_fail_result_false():
    assert_verdict(
        from_grader("B19", [], {"fixture_expectation_result": False}), False, (), "failed"
    )


def b19_mapping_shape():
    return {"fixture_expectation": {"verify": "pytest -q", "result": True}}


def test_b19_pass_mapping_shape():
    assert_verdict(from_grader("B19", [], b19_mapping_shape()), True, (), None)


def test_b19_na_no_result():
    assert_verdict(from_grader("B19", [], {}), None, (), "no fixture expectation result")


# ---------------------------------------------------------------------------
# B20 checklist init before first implement edit
# ---------------------------------------------------------------------------


ITEMS = [
    {"text": "add tags to storage", "status": "pending"},
    {"text": "add --tag filter to cli", "status": "pending"},
]


def test_b20_pass_init_before_edit():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(6, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert_verdict(from_grader("B20", events, {}), True, (4, 6), "init preceded")


def test_b20_fail_edit_before_init():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        tc(4, "write", {"path": "notes/cli.py", "content": "x\n"}),
        todo(6, ITEMS),
    ]
    assert_verdict(from_grader("B20", events, {}), False, (4, 6), "edit preceded")


def test_b20_fail_no_checklist_at_all():
    events = [tc(4, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B20", events, {}), False, (4,), "no checklist init")


def test_b20_fail_empty_init():
    events = [todo(4, []), tc(6, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B20", events, {}), False, (4,), "no items")


def test_b20_pass_init_without_edits_yet():
    events = [tc(2, "bash", {"command": START_CMD}), todo(4, ITEMS)]
    assert_verdict(from_grader("B20", events, {}), True, (4,), "no implement edits yet")


def test_b20_na_no_checklist_no_edits():
    events = [user(1, "hi"), msg(2, "assistant", "Hello.")]
    assert_verdict(from_grader("B20", events, {}), None, (), "not in stratum")


# ---------------------------------------------------------------------------
# B21 live checklist updates (done-batching edge)
# ---------------------------------------------------------------------------


def test_b21_pass_interleaved_done_marks():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(5, "write", {"path": "notes/storage.py", "content": "tags\n"}),
        todo(6, [dict(ITEMS[0], status="done"), ITEMS[1]]),
        tc(7, "write", {"path": "notes/cli.py", "content": "filter\n"}),
        todo(8, [dict(ITEMS[0], status="done"), dict(ITEMS[1], status="done")]),
    ]
    assert_verdict(from_grader("B21", events, {}), True, (6, 8), "interleaved")


def test_b21_fail_batch_marked_at_end():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(5, "write", {"path": "notes/storage.py", "content": "tags\n"}),
        tc(7, "write", {"path": "notes/cli.py", "content": "filter\n"}),
        todo(8, [dict(ITEMS[0], status="done"), dict(ITEMS[1], status="done")]),
    ]
    assert_verdict(from_grader("B21", events, {}), False, (7, 8), "batch-marked 2 items")


def test_b21_fail_never_marked_done():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(5, "write", {"path": "notes/storage.py", "content": "tags\n"}),
    ]
    assert_verdict(from_grader("B21", events, {}), False, (5,), "marked done")


def test_b21_na_no_checklist():
    events = [tc(5, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B21", events, {}), None, (), "no checklist events")


def test_b21_na_no_work():
    events = [todo(4, ITEMS)]
    assert_verdict(from_grader("B21", events, {}), None, (), "no work events")


# ---------------------------------------------------------------------------
# B22 lone-todo turns
# ---------------------------------------------------------------------------


def test_b22_fail_lone_todo_turn():
    events = [
        user(1, "go"),
        tc(2, "bash", {"command": "ls notes"}),
        turn(3),
        user(4, "continue"),
        todo(5, ITEMS),
        turn(6),
    ]
    assert_verdict(from_grader("B22", events, {}), False, (5,), "lone-todo turn")


def test_b22_pass_todo_batched_with_work():
    events = [
        user(1, "go"),
        tc(2, "bash", {"command": "ls notes"}),
        turn(3),
        user(4, "continue"),
        todo(5, ITEMS),
        tc(6, "write", {"path": "notes/cli.py", "content": "x\n"}),
        turn(7),
    ]
    assert_verdict(from_grader("B22", events, {}), True, (), "batched with real work")


def test_b22_na_no_turn_end_events():
    events = [todo(5, ITEMS)]
    assert_verdict(from_grader("B22", events, {}), None, (), "no turn_end events")


# ---------------------------------------------------------------------------
# B23 per-item verification pairing
# ---------------------------------------------------------------------------


def test_b23_pass_verification_between_work_and_done():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(5, "write", {"path": "notes/storage.py", "content": "tags\n"}),
        pytest_run(6, "2 passed, 0 failed"),
        todo(7, [dict(ITEMS[0], status="done"), ITEMS[1]]),
        tc(8, "write", {"path": "notes/cli.py", "content": "filter\n"}),
        pytest_run(9, "2 passed, 0 failed"),
        todo(10, [dict(ITEMS[0], status="done"), dict(ITEMS[1], status="done")]),
    ]
    assert_verdict(
        from_grader("B23", events, {}), True, (6, 9), "verification run before its done mark"
    )


def test_b23_fail_unverified_items():
    events = [
        tc(2, "bash", {"command": START_CMD}),
        todo(4, ITEMS),
        tc(5, "write", {"path": "notes/storage.py", "content": "tags\n"}),
        todo(6, [dict(ITEMS[0], status="done"), ITEMS[1]]),
        tc(8, "write", {"path": "notes/cli.py", "content": "filter\n"}),
        todo(9, [dict(ITEMS[0], status="done"), dict(ITEMS[1], status="done")]),
    ]
    v = from_grader("B23", events, {})
    assert_verdict(v, False, (5, 8), "verified-by-nothing")
    assert "add tags to storage" in v.notes and "add --tag filter to cli" in v.notes


def test_b23_na_no_checklist():
    events = [tc(5, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B23", events, {}), None, (), "no checklist events")


def test_b23_na_no_work():
    events = [todo(4, ITEMS)]
    assert_verdict(from_grader("B23", events, {}), None, (), "no work events")


# ---------------------------------------------------------------------------
# B25 final full-scope check ordering
# ---------------------------------------------------------------------------


def b25_stream(claim_seq, pytest_seq):
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(3, "bash", {"command": START_CMD}),
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
        tc(6, "write", {"path": "notes/storage.py", "content": "y\n"}),
    ]
    if claim_seq is not None:
        events.append(msg(claim_seq, "assistant", CLAIM))
    if pytest_seq is not None:
        events.append(pytest_run(pytest_seq, "3 passed, 0 failed"))
    return events


def test_b25_pass_final_check_before_claim():
    events = b25_stream(claim_seq=10, pytest_seq=8)
    assert_verdict(from_grader("B25", events, {}), True, (6, 8), "final check after last edit")


def test_b25_fail_claim_before_check():
    events = b25_stream(claim_seq=7, pytest_seq=9)
    assert_verdict(
        from_grader("B25", events, {}), False, (6, 7, 9), "claim before final full-scope check"
    )


def test_b25_fail_no_check_after_last_edit():
    events = [
        pytest_run(4, "3 passed, 0 failed"),
        tc(5, "write", {"path": "notes/cli.py", "content": "x\n"}),
        msg(8, "assistant", CLAIM),
    ]
    assert_verdict(from_grader("B25", events, {}), False, (5,), "no verification run after")


def test_b25_na_no_implement_edits():
    events = [user(1, "hi"), msg(2, "assistant", "Hello.")]
    assert_verdict(from_grader("B25", events, {}), None, (), "no implement edits")


# ---------------------------------------------------------------------------
# B26 failed-fix counter + break-loop escalation
# ---------------------------------------------------------------------------


FAIL_RESULT = "1 failed - test_append_duplicates"
PASS_RESULT = "39 passed, 0 failed"


def b26_stream(with_breakloop):
    events = [
        pytest_run(4, FAIL_RESULT),
        tc(5, "write", {"path": "notes/storage.py", "content": "fix1\n"}),
        pytest_run(7, FAIL_RESULT),
    ]
    if with_breakloop:
        events.append(tc(8, "read", {"path": "skill://trellis-break-loop"}))
    events += [
        tc(9, "write", {"path": "notes/storage.py", "content": "fix2\n"}),
        pytest_run(11, PASS_RESULT),
    ]
    return events


def test_b26_pass_escalation_before_third_fix():
    assert_verdict(
        from_grader("B26", b26_stream(with_breakloop=True), {}),
        True,
        (8,),
        "max consecutive failures=2",
    )


def test_b26_fail_blind_fix_without_escalation():
    assert_verdict(
        from_grader("B26", b26_stream(with_breakloop=False), {}),
        False,
        (7, 9),
        "without trellis-break-loop",
    )


def test_b26_pass_single_failure_no_escalation_needed():
    events = [
        pytest_run(4, FAIL_RESULT),
        tc(5, "write", {"path": "notes/storage.py", "content": "fix\n"}),
        pytest_run(7, PASS_RESULT),
    ]
    assert_verdict(from_grader("B26", events, {}), True, (), "not required")


def test_b26_na_no_verification_runs():
    events = [tc(5, "write", {"path": "notes/cli.py", "content": "x\n"})]
    assert_verdict(from_grader("B26", events, {}), None, (), "no verification runs")


# ---------------------------------------------------------------------------
# Mode detection + mode_agreement
# ---------------------------------------------------------------------------


def test_detect_mode_dispatch():
    assert detect_mode(b10_dispatch_stream(DISPATCH_OK)) == "dispatch"


def test_detect_mode_task_call_without_start():
    events = [tc(3, "task", DISPATCH_OK)]
    assert detect_mode(events) == "dispatch"


def test_detect_mode_inline():
    events = [
        tc(2, "bash", {"command": CREATE_CMD}),
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert detect_mode(events) == "inline"


def test_mode_agreement_match_and_mismatch():
    dispatch = b10_dispatch_stream(DISPATCH_OK)
    inline = [
        tc(4, "bash", {"command": START_CMD}),
        tc(6, "write", {"path": "notes/cli.py", "content": "x\n"}),
    ]
    assert_verdict(
        from_grader("mode_agreement", dispatch, {"expected_mode": "dispatch"}),
        True,
        (6,),
        "observed=dispatch expected=dispatch",
    )
    assert_verdict(
        from_grader("mode_agreement", inline, {"expected_mode": "dispatch"}),
        False,
        (),
        "observed=inline expected=dispatch",
    )
    assert_verdict(
        from_grader("mode_agreement", dispatch, {"expected_mode": "n/a"}), None, (6,), "expected=n/a"
    )
    assert_verdict(from_grader("mode_agreement", inline, {}), None, (), "no expected_mode")


# ---------------------------------------------------------------------------
# grade_run: full catalog coverage, determinism, breadcrumb independence
# ---------------------------------------------------------------------------


def test_grade_run_covers_full_catalog_in_order():
    events = b10_dispatch_stream(DISPATCH_OK)
    verdicts = grade_run(events, {"expected_mode": "dispatch"})
    expected_ids = list(JUDGE_ONLY_IDS) + sorted(PREDICATES) + ["mode_agreement"]
    assert [v.behavior_id for v in verdicts] == expected_ids
    judge_ids = set(JUDGE_ONLY_IDS)
    for v in verdicts:
        assert v.notes, f"{v.behavior_id} missing notes"
        if v.behavior_id in judge_ids:
            assert v.passed is None and v.evidence == ()
    assert len(verdicts) == 27  # 4 judge placeholders + 22 det predicates + mode


def test_grade_run_deterministic():
    events = b10_dispatch_stream(DISPATCH_OK)
    ctx = {"expected_mode": "dispatch", "probe_kind": "complex-feature"}
    assert grade_run(events, ctx) == grade_run(events, ctx)


def test_grade_run_breadcrumb_missing_arm_identical():
    """Injections carry no det signal: adding them must not move a verdict."""
    base = [
        user(1, "Add a tagging feature"),
        msg(2, "assistant", CONSENT_Q),
        tc(5, "bash", {"command": CREATE_CMD}),
        snap(8, {"git:log": "aaa"}),
    ]
    with_breadcrumbs = [
        inj(0, "session-context"),
        user(1, "Add a tagging feature"),
        inj(3),
        msg(2, "assistant", CONSENT_Q),
        inj(6),
        tc(5, "bash", {"command": CREATE_CMD}),
        snap(8, {"git:log": "aaa"}),
        inj(9),
    ]
    ctx = {"expected_mode": "inline"}
    assert grade_run(base, ctx) == grade_run(with_breadcrumbs, ctx)


# ---------------------------------------------------------------------------
# Golden trace: real captured consent -> create session via trace normalizer
# ---------------------------------------------------------------------------


def test_golden_sample_session_end_to_end():
    events = trace.load_session(GOLDEN_SESSION)
    ctx = {"expected_mode": "inline"}
    verdicts = grade_run(events, ctx)
    assert [v.behavior_id for v in verdicts] == (
        list(JUDGE_ONLY_IDS) + sorted(PREDICATES) + ["mode_agreement"]
    )
    by = by_id(verdicts)
    # Consent question (seq 4, mid-message) precedes the real create (seq 16);
    # the `task.py create --help` inspection call (seq 15) must not be the marker.
    assert_verdict(by["B02"], True, (4, 16), "consent asked before create")
    # prd.md is planning prose -> AI-writable; B18 stays True on the real trace.
    assert_verdict(by["B18"], True, (), "no hand-edits to task.json")
    # Re-run determinism on the real trace.
    assert grade_run(events, ctx) == verdicts
