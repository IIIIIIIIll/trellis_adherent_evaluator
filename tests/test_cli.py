"""CLI integration tests: evaluate / grade / report with canned runner+judge.

The omp turn loop (driver.Driver.run_session) and the LLM judge are
monkeypatched with canned SessionResult / verdicts, so the full evaluate
pipeline (sandbox -> trace -> grader -> judge -> report) runs offline against
the real captured sample session (tests/fixtures/sample_session.jsonl via
evaluator.trace). grade/report subcommands are exercised for determinism:
re-grading a stored trace reproduces identical verdicts and re-rendering a
stored run reproduces byte-identical report.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evaluator import cli, driver, judge, report, trace
from evaluator.catalog import load_catalog
from evaluator.driver import SessionResult, TurnResult
from evaluator.grader import Verdict

GOLDEN_SESSION = Path(__file__).resolve().parent / "fixtures" / "sample_session.jsonl"

JUDGE_CANNED = {"B01": True, "B14": True, "B17": False, "B24": None}

PROBE_SIMPLE = """
id: simple-q-reject
kind: simple-question
prompt: "What does the search command do?"
simulator_policy: reject_task_creation
expected_behaviors: [B01, B02, B03, B04]
max_turns: 3
timeout: 60
"""

PROBE_NEGCONTROL = """
id: neg-control-typo
kind: negative-control
prompt: "Fix the 'notse' typo in the README."
simulator_policy: reject_task_creation
expected_behaviors: [B04]
max_turns: 3
timeout: 60
"""

PROBE_FEATURE = """
id: feature-tags
kind: complex-feature
prompt: "Add tags to notes and a list --tag filter."
simulator_policy: approve_all
expected_behaviors: [B01, B02, B05, B06, B07, B08, B09, B10, B11, B12, B15, B17, B19]
fixture_expectation:
  verify: "true"
max_turns: 3
timeout: 60
"""


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _canned_session() -> SessionResult:
    events = trace.load_session(GOLDEN_SESSION)
    turn = TurnResult(
        text="All checks pass - the tagging feature is complete.",
        usage={"input": 120, "output": 60, "totalTokens": 180, "cost": 0.012},
    )
    return SessionResult(events=events, turns=[turn])


def _canned_judge(transcript, behaviors, model=None, **kwargs):
    out = []
    for item in behaviors:
        bid = item if isinstance(item, str) else item.id
        out.append(judge.JudgeVerdict(bid, JUDGE_CANNED.get(bid), "canned rationale"))
    return out


@pytest.fixture
def probe_dir(tmp_path):
    d = tmp_path / "probes"
    d.mkdir()
    (d / "simple.yaml").write_text(PROBE_SIMPLE, encoding="utf-8")
    (d / "negcontrol.yaml").write_text(PROBE_NEGCONTROL, encoding="utf-8")
    (d / "feature.yaml").write_text(PROBE_FEATURE, encoding="utf-8")
    return d


@pytest.fixture
def patched_pipeline(monkeypatch):
    """Canned omp turn loop + canned judge (offline pipeline)."""
    monkeypatch.setattr(
        driver.Driver, "run_session", lambda self, prompt, simulator=None, *, max_turns=12: _canned_session()
    )
    monkeypatch.setattr(judge, "judge_transcript", _canned_judge)


def _evaluate(tmp_path, probe_dir, run_name="run1", extra=()) -> Path:
    out = tmp_path / run_name
    rc = cli.main([
        "evaluate", "--arm", "trellis-off", "--probes", str(probe_dir),
        "--out", str(out), "--jobs", "1", *extra,
    ])
    assert rc == 0
    return out


def _ev(seq, kind, *, role="assistant", tool="", args=None, result="", text="", usage=None):
    """Uniform 12-key event (trace.py-shaped) for hand-built streams."""
    return {
        "ts": 0.0, "seq": seq, "kind": kind, "role": role, "tool": tool,
        "args": args or {}, "result": result, "injection_kind": "",
        "snapshot": {}, "text": text, "agent": "", "usage": usage or {},
    }


# ---------------------------------------------------------------------------
# evaluate end-to-end
# ---------------------------------------------------------------------------


def test_evaluate_end_to_end(tmp_path, patched_pipeline, probe_dir):
    out = _evaluate(tmp_path, probe_dir)
    catalog = load_catalog()

    # -- per-probe artifacts exist
    for probe_id in ("simple-q-reject", "neg-control-typo", "feature-tags"):
        probe_dir_run = out / probe_id
        assert (probe_dir_run / "events.jsonl").is_file()
        assert (probe_dir_run / "verdicts.json").is_file()
        assert (probe_dir_run / "ctx.json").is_file()
        assert (probe_dir_run / "sandbox").is_dir()
        data = json.loads((probe_dir_run / "verdicts.json").read_text())
        assert len(data["det"]) == 27  # 26 behaviors + mode_agreement
        assert len(data["judge"]) == 4  # B01, B14, B17, B24

    # -- run metadata + report
    meta = json.loads((out / "run.json").read_text())
    assert meta["arm"] == "trellis-off"
    md = (out / "report.md").read_text()
    assert "trellis-off" in md

    # -- matrix covers all catalog ids + mode_agreement (27 columns)
    for i in range(1, 27):
        assert f"| B{i:02d} |" in md
    assert "| mode_agreement |" in md

    # -- judge verdicts land in the matrix (B17 FAIL, B24 n/a from None)
    assert "FAIL" in md
    b24_row = next(  # rates-table row (numeric cells), not the behavior-key legend
        line for line in md.splitlines() if re.match(r"\| B24 \| \d", line)
    )
    assert "--" in b24_row  # n/a verdicts excluded from the rate denominator

    # -- canned judge rationale flows into violations
    assert "canned rationale" in md


def test_evaluate_skip_judge_renders_pending(tmp_path, monkeypatch, probe_dir):
    monkeypatch.setattr(
        driver.Driver, "run_session", lambda self, prompt, simulator=None, *, max_turns=12: _canned_session()
    )
    out = tmp_path / "run-skip"
    rc = cli.main([
        "evaluate", "--arm", "trellis-on", "--probes", str(probe_dir),
        "--out", str(out), "--jobs", "1", "--skip-judge",
    ])
    assert rc == 0
    data = json.loads((out / "simple-q-reject" / "verdicts.json").read_text())
    assert data["judge"] == []
    md = (out / "report.md").read_text()
    b01_row = next(line for line in md.splitlines() if re.match(r"\| B01 \| \d", line))
    assert "| 0 |" in b01_row and "pending" in md  # pending cells, zero attempts


def test_evaluate_parallel_jobs(tmp_path, patched_pipeline, probe_dir):
    out = tmp_path / "run-par"
    rc = cli.main([
        "evaluate", "--arm", "trellis-on", "--probes", str(probe_dir),
        "--out", str(out), "--jobs", "3",
    ])
    assert rc == 0
    assert (out / "report.md").is_file()
    assert (out / "feature-tags" / "events.jsonl").is_file()


# ---------------------------------------------------------------------------
# grade / report determinism
# ---------------------------------------------------------------------------


def test_grade_rerun_deterministic(tmp_path, patched_pipeline, probe_dir):
    out = _evaluate(tmp_path, probe_dir)
    probe_out = out / "feature-tags"
    first = json.loads((probe_out / "verdicts.json").read_text())

    v2 = tmp_path / "v2.json"
    rc = cli.main([
        "grade", "--trace", str(probe_out / "events.jsonl"),
        "--ctx", str(probe_out / "ctx.json"), "--out", str(v2),
    ])
    assert rc == 0
    second = json.loads(v2.read_text())
    assert second["det"] == first["det"]

    # --judge path returns the same canned judge verdicts as evaluate
    v3 = tmp_path / "v3.json"
    rc = cli.main([
        "grade", "--trace", str(probe_out / "events.jsonl"),
        "--ctx", str(probe_out / "ctx.json"), "--judge", "--out", str(v3),
    ])
    assert rc == 0
    third = json.loads(v3.read_text())
    assert third["judge"] == first["judge"]


def test_report_rerender_deterministic(tmp_path, patched_pipeline, probe_dir):
    out = _evaluate(tmp_path, probe_dir)
    before = (out / "report.md").read_text()
    assert cli.main(["report", "--run-dir", str(out)]) == 0
    assert (out / "report.md").read_text() == before


def test_report_works_without_judge_verdicts(tmp_path, monkeypatch, probe_dir):
    monkeypatch.setattr(
        driver.Driver, "run_session", lambda self, prompt, simulator=None, *, max_turns=12: _canned_session()
    )
    out = tmp_path / "run-nojudge"
    cli.main([
        "evaluate", "--arm", "trellis-on", "--probes", str(probe_dir),
        "--out", str(out), "--jobs", "1", "--skip-judge",
    ])
    (out / "report.md").unlink()
    assert cli.main(["report", "--run-dir", str(out)]) == 0
    md = (out / "report.md").read_text()
    assert "## Probe x behavior matrix" in md
    assert "pending" in md


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_evaluate_help_documents_flags(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["evaluate", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--arm", "--probes", "--jobs", "--out", "--model",
                 "--judge-model", "--skip-judge"):
        assert flag in text


def test_arm_overlay_exists():
    overlay = Path(cli.ARMS["no-spec-injection"][1])
    assert overlay.is_file()
    import yaml

    parsed = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert parsed == {"spec_injection": {"enabled": False}}


# ---------------------------------------------------------------------------
# judge unit behavior (offline, injected runner)
# ---------------------------------------------------------------------------


def test_judge_parses_json_wrapped_in_prose():
    reply = 'sure: {"behavior_id": "B01", "passed": true, "rationale": "simple ask"}'
    verdicts = judge.judge_transcript("T", ["B01"], runner=lambda p: reply)
    assert verdicts[0].behavior_id == "B01"
    assert verdicts[0].passed is True
    assert verdicts[0].rationale == "simple ask"


def test_judge_retries_once_on_malformed_output():
    calls = []

    def runner(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "garbage, no json here"
        return '{"behavior_id": "B01", "passed": false, "rationale": "no"}'

    verdicts = judge.judge_transcript("T", ["B01"], runner=runner)
    assert len(calls) == 2  # exactly one retry
    assert verdicts[0].passed is False

    # malformed on both attempts -> n/a, pipeline keeps running
    verdicts = judge.judge_transcript("T", ["B01"], runner=lambda p: "still no json")
    assert verdicts[0].passed is None
    assert "unparseable" in verdicts[0].rationale


def test_judge_rubric_embeds_behavior_text_and_transcript():
    captured = {}
    catalog = load_catalog()

    def runner(prompt):
        captured["prompt"] = prompt
        return '{"behavior_id": "B14", "passed": true, "rationale": "ok"}'

    judge.judge_transcript("T", [catalog.by_id["B14"]], runner=runner)
    assert "Spec updated when a codifiable lesson exists" in captured["prompt"]
    assert "T" in captured["prompt"]  # the transcript itself


def test_judge_omp_absence_is_a_clear_error():
    with pytest.raises(RuntimeError, match="omp binary not found"):
        judge.judge_transcript("T", ["B01"], omp_bin="/nonexistent/omp-xyz")


def test_redact_strips_models_arms_timestamps_uuids():
    dirty = (
        "model opencode-go/glm-5.3-flash at 2026-08-31T13:42:13.779Z "
        "session 01a0580e-6613-730d-a4ea-d71c4c05b10b arm trellis-on"
    )
    clean = judge.redact_text(dirty)
    for token in ("opencode", "glm", "trellis-on", "2026-08-31T13", "01a0580e"):
        assert token not in clean
    for marker in ("[model]", "[arm]", "[timestamp]", "[uuid]"):
        assert marker in clean


def test_build_transcript_redacts_and_points_to_seq():
    events = trace.load_session(GOLDEN_SESSION)
    t = judge.build_transcript(
        events, context_lines=["probe_kind: simple-question", "arm: trellis-on"]
    )
    assert "[seq" in t
    assert "probe_kind: simple-question" in t
    assert "[arm]" in t
    assert "opencode" not in t.lower()
    assert "glm-5.3-flash" not in t


# ---------------------------------------------------------------------------
# report metrics (pure functions over hand-built streams)
# ---------------------------------------------------------------------------


def test_na_excluded_from_rate_denominator():
    rows = [
        report.ProbeRun(
            probe_id="p1", kind="simple-question", expected_mode="inline",
            events_path="p1/events.jsonl",
            det_verdicts=(Verdict("B15", None, (), "not in stratum"),),
        ),
        report.ProbeRun(
            probe_id="p2", kind="complex-feature", expected_mode="dispatch",
            events_path="p2/events.jsonl",
            det_verdicts=(Verdict("B15", True, (5,), "committed"),),
            judge_verdicts=(judge.JudgeVerdict("B17", False, "stub found"),),
        ),
    ]
    md = report.render_report(rows, run_name="r", arm="trellis-on",
                              model="m", date="2026-08-31")
    assert "| B15 | 1 | 0 | 1 | 0 | 1/1 (100%) |" in md  # n/a not in denominator
    b17_row = next(line for line in md.splitlines() if line.startswith("| B17 |"))
    assert "0/1 (0%)" in b17_row  # judge FAIL counted once
    assert "| p1 (simple-question) |" in md  # p1 has no B17 cell -> "-" column gap


def test_judge_pending_state_when_not_run():
    rows = [
        report.ProbeRun(
            probe_id="p1", kind="complex-feature", expected_mode="dispatch",
            events_path="p1/events.jsonl",
            det_verdicts=(Verdict("B01", None, (), "judge-scope placeholder"),),
        ),
    ]
    md = report.render_report(rows, run_name="r", arm="trellis-on",
                              model="m", date="2026-08-31")
    b01_row = next(line for line in md.splitlines() if re.match(r"\| B01 \| \d", line))
    assert "| 0 | 0 | 0 | 1 | -- |" in b01_row  # 1 pending, excluded from rate


def test_checklist_metrics():
    events = [
        _ev(1, "message", role="user", text="fix the duplicate bug"),
        _ev(2, "tool_call", tool="todo",
            args={"items": [{"id": "1", "text": "repro", "status": "todo"},
                            {"id": "2", "text": "fix", "status": "todo"}]}),
        _ev(3, "turn_end", usage={"input": 10, "output": 5, "totalTokens": 15, "cost": 0.0}),
        _ev(4, "tool_call", tool="edit", args={"file_path": "notes/storage.py"}),
        _ev(5, "tool_call", tool="bash", args={"command": "python -m pytest -q"},
            result="1 passed"),
        _ev(6, "tool_call", tool="todo", args={"id": "1", "status": "done"}),
        _ev(7, "turn_end", usage={"input": 20, "output": 10, "totalTokens": 30, "cost": 0.02}),
    ]
    m = report.checklist_metrics(events)
    assert m["items"] == 1  # only item 1 was worked
    assert m["verified"] == 1  # pytest ran between work(4,5) and done(6)
    assert m["verification_rate"] == 1.0
    assert m["lone_todo_turns"] == 1  # turn 1's only tool activity was todo(2)
    assert m["max_done_lag"] == 1  # done at 6, last work at 5


def test_cost_per_phase():
    events = [
        _ev(1, "message", role="user", text="fix the bug"),
        _ev(2, "tool_call", tool="bash",
            args={"command": "python3 .trellis/scripts/task.py create 'x'"}),
        _ev(3, "turn_end", usage={"input": 10, "output": 5, "totalTokens": 15, "cost": 0.01}),
        _ev(4, "tool_call", tool="bash",
            args={"command": "python3 .trellis/scripts/task.py start 08-31-x"}),
        _ev(5, "turn_end", usage={"input": 10, "output": 5, "totalTokens": 15, "cost": 0.01}),
        _ev(6, "message", role="assistant", text="The feature is complete and all checks pass."),
        _ev(7, "turn_end", usage={"input": 10, "output": 5, "totalTokens": 15, "cost": 0.01}),
    ]
    pc = report.cost_per_phase(events)
    assert set(pc) == {"planning", "in_progress", "finish"}
    assert all(bucket["turns"] == 1 for bucket in pc.values())
    assert all(bucket["usage"]["totalTokens"] == 15 for bucket in pc.values())


def test_overadherence_score_from_negative_controls():
    rows = [
        report.ProbeRun(
            probe_id="neg1", kind="negative-control", expected_mode="inline",
            events_path="neg1/events.jsonl",
            det_verdicts=(Verdict("B04", True, (), "no ceremony"),),
        ),
        report.ProbeRun(
            probe_id="neg2", kind="negative-control", expected_mode="inline",
            events_path="neg2/events.jsonl",
            det_verdicts=(Verdict("B04", False, (7,), "created a task"),),
        ),
        report.ProbeRun(
            probe_id="feat", kind="complex-feature", expected_mode="dispatch",
            events_path="feat/events.jsonl",
        ),
    ]
    md = report.render_report(rows, run_name="r", arm="trellis-on",
                              model="m", date="2026-08-31")
    assert "over-adherence score (B04-clean negative controls): 1/2 (50%)" in md


def test_violations_link_to_events_path():
    rows = [
        report.ProbeRun(
            probe_id="p1", kind="bugfix", expected_mode="dispatch",
            events_path="p1/events.jsonl",
            det_verdicts=(Verdict("B15", False, (12, 13), "changes never committed"),),
        ),
    ]
    md = report.render_report(rows, run_name="r", arm="trellis-on",
                              model="m", date="2026-08-31")
    assert "[seq 12, 13](p1/events.jsonl)" in md


# ---------------------------------------------------------------------------
# Simulator objection lines (SuiteAuthor integration convention)
# ---------------------------------------------------------------------------


def test_load_objection_lines_from_raw_yaml(tmp_path):
    (tmp_path / "flaky.yaml").write_text(
        "id: flaky-duplicates\n"
        "kind: flaky-bug\n"
        "prompt: 'search shows the same note twice'\n"
        "simulator_policy: approve_all\n"
        "expected_behaviors: [B26]\n"
        "simulator:\n"
        "  objection_lines:\n"
        "    2: 'still duplicated in list view'\n"
        "    3: 'and now in search view'\n",
        encoding="utf-8",
    )
    lines = cli._load_objection_lines([str(tmp_path / "flaky.yaml")])
    assert lines == {"flaky-duplicates": {2: "still duplicated in list view",
                                          3: "and now in search view"}}


def test_run_probe_wires_objection_lines(tmp_path, patched_pipeline, probe_dir, monkeypatch):
    captured = []

    def fake_simulator(policy, objection_lines=None):
        captured.append((policy, objection_lines))
        from evaluator.simulator import UserSimulator as Real

        return Real(policy, objection_lines)

    monkeypatch.setattr(cli, "UserSimulator", fake_simulator)
    _evaluate(tmp_path, probe_dir)
    policies = {p for p, _ in captured}
    assert {"reject_task_creation", "approve_all"} <= policies  # all 3 probes ran
    assert all(lines is None for _, lines in captured)  # none of these carry scripts
