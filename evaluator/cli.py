"""CLI orchestration for the Trellis workflow adherence evaluator.

Subcommands:

  evaluate  run probes end-to-end: sandbox -> omp (driver) -> trace ->
            grader -> judge -> report, all under ``runs/<run>/<probe>/``.
  grade     re-grade a stored events.jsonl (deterministic, no omp needed
            unless ``--judge`` adds LLM verdicts).
  report    re-render report.md from a stored run directory (no omp).

Arms (design.md "Harness arms"): ``trellis-on`` (no extra flags),
``trellis-off`` (``--no-extensions``), ``no-spec-injection`` (``--config``
overlay with ``spec_injection.enabled: false``).

Run layout::

    runs/<run>/run.json                  arm/model/date metadata
    runs/<run>/report.md                 rendered report
    runs/<run>/<probe>/sandbox/          fresh fixture copy
    runs/<run>/<probe>/session/          omp --session-dir
    runs/<run>/<probe>/events.jsonl      normalized trace
    runs/<run>/<probe>/ctx.json          grader ctx echo (re-grade input)
    runs/<run>/<probe>/verdicts.json     det + judge verdicts, usage, metrics
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import glob as _glob
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from evaluator import catalog as catalog_mod
from evaluator import driver, judge, probes as probes_mod, report, trace
from evaluator.grader import Verdict, grade_run
from evaluator.simulator import (
    APPROVE_REPLY,
    POLICY_REJECT_TASK_CREATION,
    REJECT_REPLY,
    UserSimulator,
)

__all__ = ["main"]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: omp flags per arm (design.md "Harness arms"); the no-spec-injection arm is
#: a --config overlay resolved relative to the repo root.
ARM_NO_SPEC_INJECTION = "no-spec-injection"
ARMS: dict[str, list[str]] = {
    "trellis-on": [],
    "trellis-off": ["--no-extensions"],
    ARM_NO_SPEC_INJECTION: ["--config", str(REPO_ROOT / "arms" / "no-spec-injection.yml")],
}

DEFAULT_PROBES_DIR = REPO_ROOT / "probes"
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "fixtures" / "repo-template"
VERIFY_TIMEOUT_S = 180
_GIT_TIMEOUT_S = 30
_PLAN_CLIP = 2000


# ---------------------------------------------------------------------------
# Probe loading (raw-YAML extras the frozen Probe contract does not carry)
# ---------------------------------------------------------------------------


def _resolve_probe_paths(spec: str) -> list[str]:
    """``all`` -> default probes dir; otherwise a glob or a file/dir path."""
    if spec == "all":
        return [str(DEFAULT_PROBES_DIR)]
    expanded = sorted(_glob.glob(spec))
    return expanded or [spec]


def _load_objection_lines(paths: list[str]) -> dict[str, dict[int, str]]:
    """Extract ``simulator.objection_lines`` from raw probe YAML.

    probes.py's loader drops unknown top-level keys (frozen Probe dataclass
    contract), so scripted simulator objections ride alongside and are wired
    into UserSimulator here. Keys are normalized to int turn indexes.
    """
    out: dict[str, dict[int, str]] = {}
    for path in _globexpand(paths):
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            continue
        sim = raw.get("simulator")
        lines = sim.get("objection_lines") if isinstance(sim, Mapping) else None
        if not isinstance(lines, Mapping) or "id" not in raw:
            continue
        out[str(raw["id"])] = {int(k): str(v) for k, v in lines.items()}
    return out


def _globexpand(paths: list[str]) -> list[str]:
    expanded: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            expanded.extend(
                str(c) for c in sorted(path.iterdir()) if c.suffix in (".yaml", ".yml")
            )
        else:
            expanded.append(str(p))
    return expanded


# ---------------------------------------------------------------------------
# Grader context + judge context assembly
# ---------------------------------------------------------------------------


def _turn_boundaries(events: list[dict]) -> list[dict]:
    """Simulator turn boundaries for B08: every user message after the probe
    prompt, classified by the simulator's scripted reply prefixes."""
    user_events = [
        e for e in events if e.get("kind") == "message" and e.get("role") == "user"
    ]
    out = []
    for ev in user_events[1:]:
        text = str(ev.get("text") or "")
        if text.startswith(APPROVE_REPLY):
            kind = "approval"
        elif text.startswith(REJECT_REPLY):
            kind = "rejection"
        else:
            kind = "answer"
        out.append({"seq": ev.get("seq"), "kind": kind})
    return out


def _grade_ctx(events: list[dict], probe, session_dir: Path) -> dict:
    ctx: dict[str, Any] = {
        "probe_kind": probe.kind,
        "expected_mode": probe.expected_mode,
        "user_rejected_task_creation": (
            probe.simulator_policy == POLICY_REJECT_TASK_CREATION
        ),
        "turn_boundaries": _turn_boundaries(events),
        "nested_events": trace.load_nested(session_dir),
    }
    return ctx


def _run_verify(probe, sandbox: Path) -> bool | None:
    """Run the probe's fixture_expectation.verify in the sandbox (B19)."""
    fe = probe.fixture_expectation or {}
    cmd = fe.get("verify")
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            str(cmd), shell=True, cwd=str(sandbox), capture_output=True,
            text=True, timeout=VERIFY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _sandbox_git_state(sandbox: Path) -> str:
    """Final sandbox git state (B17 judge evidence), compact."""
    parts = []
    for args, label in (
        (["log", "--oneline", "-5"], "log"),
        (["status", "--short"], "status"),
    ):
        try:
            proc = subprocess.run(
                ["git", "-C", str(sandbox), *args], capture_output=True,
                text=True, timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            body = " | ".join(proc.stdout.strip().splitlines()[:10])
            parts.append(f"{label}: {body}")
    return "\n".join(parts)


def _implement_plan(sandbox: Path) -> str:
    """The task's implement.md (B24 judge evidence), first task dir."""
    plans = sorted(sandbox.glob(".trellis/tasks/*/implement.md"))
    if not plans:
        return ""
    try:
        return plans[0].read_text(encoding="utf-8", errors="replace")[:_PLAN_CLIP]
    except OSError:
        return ""


def _spec_changes(events: list[dict]) -> list[str]:
    """Spec files whose snapshot digest changed across the run (B14)."""
    snaps = [
        e["snapshot"]
        for e in events
        if e.get("kind") == "snapshot" and isinstance(e.get("snapshot"), Mapping)
    ]
    if len(snaps) < 2:
        return []
    first, last = snaps[0], snaps[-1]
    keys = {k for k in set(first) | set(last) if k.startswith(".trellis/spec/")}
    return sorted(k for k in keys if first.get(k) != last.get(k))


def _todo_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return str(item.get("text") or item.get("content") or item.get("title") or item)
    return str(item)


def _judge_context(events: list[dict], probe, sandbox: Path) -> list[str]:
    """Run-context lines for the judge transcript (per-behavior evidence
    types from the catalog: probe kind for B01, spec changes for B14, git
    state for B17, checklist vs implement.md for B24)."""
    from evaluator.grader import _COMPLEX_KINDS, _todo_timeline

    lines = [f"probe_kind: {probe.kind}"]
    classification = "complex" if probe.kind in _COMPLEX_KINDS else "simple"
    lines.append(f"expected classification: {classification}")
    timeline = _todo_timeline(events)
    if timeline:
        items = [_todo_item_text(it) for it in timeline[-1][1].values()]
        lines.append("checklist items (final state): " + "; ".join(items))
    plan = _implement_plan(sandbox)
    if plan:
        lines.append("implement.md (task plan):")
        lines.append(plan)
    changed = _spec_changes(events)
    lines.append(
        "spec files changed during run: " + (", ".join(changed) if changed else "(none)")
    )
    git_state = _sandbox_git_state(sandbox)
    if git_state:
        lines.append("sandbox git state:")
        lines.append(git_state)
    return lines


# ---------------------------------------------------------------------------
# Per-probe pipeline
# ---------------------------------------------------------------------------


def _verdict_dict(v: Any) -> dict:
    return {
        "behavior_id": v.behavior_id,
        "passed": v.passed,
        "evidence": list(getattr(v, "evidence", ()) or ()),
        "notes": getattr(v, "notes", ""),
    }


def _judge_dict(v: Any) -> dict:
    return {"behavior_id": v.behavior_id, "passed": v.passed, "rationale": v.rationale}


def _verdicts_payload(row: report.ProbeRun) -> dict:
    return {
        "probe_id": row.probe_id,
        "kind": row.kind,
        "expected_mode": row.expected_mode,
        "det": [_verdict_dict(v) for v in row.det_verdicts],
        "judge": [_judge_dict(v) for v in row.judge_verdicts],
        "usage": dict(row.usage),
        "turns": row.turns,
        "phase_costs": {
            p: {"turns": b.get("turns", 0), "usage": dict(b.get("usage") or {})}
            for p, b in (row.phase_costs or {}).items()
        },
        "checklist": dict(row.checklist) if row.checklist else None,
    }


def _run_probe(
    probe,
    *,
    run_dir: Path,
    extra_flags: list[str],
    model: str | None,
    judge_model: str | None,
    judge_enabled: bool,
    objection_lines: dict[int, str] | None,
    catalog,
    template_dir: str,
) -> report.ProbeRun:
    """One probe end-to-end: sandbox -> driver -> trace -> grader -> judge."""
    probe_dir = run_dir / probe.id
    sandbox = driver.materialize_sandbox(template_dir, probe_dir / "sandbox")
    session_dir = probe_dir / "session"

    simulator = UserSimulator(probe.simulator_policy, objection_lines)
    drv = driver.Driver(
        sandbox, session_dir, max_time=probe.timeout, extra_flags=extra_flags,
        model=model,
    )
    session = drv.run_session(probe.prompt, simulator, max_turns=probe.max_turns)
    events = session.events
    trace.write_events(events, probe_dir / "events.jsonl")

    fixture_ok = _run_verify(probe, sandbox)
    ctx = _grade_ctx(events, probe, session_dir)
    if fixture_ok is not None:
        ctx["fixture_expectation_result"] = fixture_ok
    det = grade_run(events, ctx)

    judge_verdicts = []
    if judge_enabled:
        behaviors = [
            catalog.by_id[b] for b in judge.JUDGE_BEHAVIOR_IDS if b in catalog.by_id
        ]
        transcript = judge.build_transcript(
            events, context_lines=_judge_context(events, probe, sandbox)
        )
        judge_verdicts = judge.judge_transcript(transcript, behaviors, model=judge_model)

    row = report.ProbeRun(
        probe_id=probe.id,
        kind=probe.kind,
        expected_mode=probe.expected_mode,
        events_path=f"{probe.id}/events.jsonl",
        det_verdicts=tuple(det),
        judge_verdicts=tuple(judge_verdicts),
        usage=dict(session.total_usage()),
        turns=len(session.turns),
        phase_costs=report.cost_per_phase(events),
        checklist=report.checklist_metrics(events),
    )
    (probe_dir / "ctx.json").write_text(
        json.dumps(ctx, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (probe_dir / "verdicts.json").write_text(
        json.dumps(_verdicts_payload(row), indent=2) + "\n", encoding="utf-8"
    )
    return row


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_evaluate(args) -> int:
    catalog = catalog_mod.load_catalog()
    if args.arm == ARM_NO_SPEC_INJECTION and not Path(ARMS[args.arm][1]).exists():
        print(f"error: arm overlay missing: {ARMS[args.arm][1]}", file=sys.stderr)
        return 2
    probe_paths = _resolve_probe_paths(args.probes)
    probes = probes_mod.load_probes(probe_paths)
    if not probes:
        print(f"error: no probes matched {args.probes!r}", file=sys.stderr)
        return 2
    objection_lines = _load_objection_lines(probe_paths)

    if args.out:
        run_dir = Path(args.out)
    else:
        run_dir = REPO_ROOT / "runs" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    judge_enabled = not args.skip_judge
    errors: list[tuple[str, str]] = []
    rows: list[report.ProbeRun] = []

    def work(probe):
        return _run_probe(
            probe,
            run_dir=run_dir,
            extra_flags=ARMS[args.arm],
            model=args.model,
            judge_model=args.judge_model,
            judge_enabled=judge_enabled,
            objection_lines=objection_lines.get(probe.id),
            catalog=catalog,
            template_dir=args.template,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(work, p): p for p in probes}
        for fut in concurrent.futures.as_completed(futures):
            probe = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as exc:  # one failed probe must not kill the suite
                errors.append((probe.id, f"{type(exc).__name__}: {exc}"))
                rows.append(
                    report.ProbeRun(
                        probe_id=probe.id,
                        kind=probe.kind,
                        expected_mode=probe.expected_mode,
                        events_path=f"{probe.id}/events.jsonl",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    rows.sort(key=lambda r: r.probe_id)
    meta = {
        "run_name": run_dir.name,
        "arm": args.arm,
        "model": args.model or "",
        "judge_model": args.judge_model or "",
        "judge_ran": judge_enabled,
        "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
        "probes": [{"id": p.id, "kind": p.kind, "dir": p.id} for p in probes],
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    md = report.render_report(
        rows,
        run_name=meta["run_name"],
        arm=args.arm,
        model=args.model or "",
        date=meta["date"],
        run_dir=str(run_dir),
        behavior_text={b.id: b.behavior for b in catalog.behaviors},
    )
    (run_dir / "report.md").write_text(md, encoding="utf-8")

    counts = {"ok": 0, "FAIL": 0, "n/a": 0, "pending": 0}
    for row in rows:
        for cell in report.merge_verdicts(row.det_verdicts, row.judge_verdicts).values():
            counts[cell.state] = counts.get(cell.state, 0) + 1
    print(f"run: {run_dir}")
    print(f"report: {run_dir / 'report.md'}")
    print(
        f"verdicts: {counts['ok']} ok, {counts['FAIL']} FAIL, "
        f"{counts['n/a']} n/a, {counts['pending']} pending; "
        f"errors: {len(errors)}"
    )
    for probe_id, err in errors:
        print(f"  error [{probe_id}]: {err}", file=sys.stderr)
    return 1 if errors else 0


def cmd_grade(args) -> int:
    events = trace.read_events(args.trace)
    ctx: dict = {}
    if args.ctx:
        ctx = json.loads(Path(args.ctx).read_text(encoding="utf-8"))
    det = grade_run(events, ctx)

    judge_verdicts = []
    if args.judge:
        catalog = catalog_mod.load_catalog()
        behaviors = [
            catalog.by_id[b] for b in judge.JUDGE_BEHAVIOR_IDS if b in catalog.by_id
        ]
        context_lines = list(ctx.get("judge_context") or [])
        if not context_lines and ctx.get("probe_kind"):
            context_lines = [f"probe_kind: {ctx['probe_kind']}"]
        transcript = judge.build_transcript(events, context_lines=context_lines)
        judge_verdicts = judge.judge_transcript(transcript, behaviors, model=args.judge_model)

    out = Path(args.out) if args.out else Path(args.trace).resolve().parent / "verdicts.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "det": [_verdict_dict(v) for v in det],
        "judge": [_judge_dict(v) for v in judge_verdicts],
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    for v in list(det) + list(judge_verdicts):
        state = "ok" if v.passed is True else "FAIL" if v.passed is False else "n/a"
        notes = getattr(v, "notes", "") or getattr(v, "rationale", "")
        print(f"{v.behavior_id:14} {state:4} {notes}")
    print(f"verdicts written: {out}")
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    catalog = catalog_mod.load_catalog()
    rows: list[report.ProbeRun] = []
    for entry in meta.get("probes") or []:
        probe_dir = run_dir / entry["dir"]
        verdicts_path = probe_dir / "verdicts.json"
        if not verdicts_path.exists():
            rows.append(
                report.ProbeRun(
                    probe_id=entry["id"],
                    kind=entry.get("kind", ""),
                    expected_mode="",
                    events_path=f"{entry['dir']}/events.jsonl",
                    error="no verdicts.json stored (probe run failed?)",
                )
            )
            continue
        data = json.loads(verdicts_path.read_text(encoding="utf-8"))
        det = [
            Verdict(
                d["behavior_id"], d["passed"], tuple(d.get("evidence") or ()),
                d.get("notes", ""),
            )
            for d in data.get("det") or []
        ]
        jdg = [
            judge.JudgeVerdict(d["behavior_id"], d["passed"], d.get("rationale", ""))
            for d in data.get("judge") or []
        ]
        rows.append(
            report.ProbeRun(
                probe_id=data.get("probe_id", entry["id"]),
                kind=data.get("kind", entry.get("kind", "")),
                expected_mode=data.get("expected_mode", ""),
                events_path=f"{entry['dir']}/events.jsonl",
                det_verdicts=det,
                judge_verdicts=jdg,
                usage=data.get("usage") or {},
                turns=data.get("turns", 0),
                phase_costs=data.get("phase_costs") or {},
                checklist=data.get("checklist"),
            )
        )
    md = report.render_report(
        rows,
        run_name=meta.get("run_name", run_dir.name),
        arm=meta.get("arm", ""),
        model=meta.get("model", ""),
        date=meta.get("date", ""),
        run_dir=str(run_dir),
        behavior_text={b.id: b.behavior for b in catalog.behaviors},
    )
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    print(f"report written: {run_dir / 'report.md'}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluator.cli",
        description="Trellis workflow adherence evaluator: evaluate / grade / report.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser(
        "evaluate",
        help="run probes end-to-end: sandbox -> omp -> trace -> grader -> judge -> report",
        description=(
            "Materialize a fresh sandbox per probe, drive omp turn-by-turn, "
            "normalize the session to events.jsonl, grade deterministically, "
            "run the LLM judge, and render report.md."
        ),
    )
    p_eval.add_argument(
        "--arm", choices=sorted(ARMS), default="trellis-on",
        help="harness arm: trellis-on (baseline), trellis-off (--no-extensions), "
        "no-spec-injection (--config overlay with spec_injection disabled)",
    )
    p_eval.add_argument(
        "--probes", default="all",
        help='probe selection: "all" (probes/ dir), a glob pattern, or a path '
        "to a probe YAML file or directory",
    )
    p_eval.add_argument(
        "--jobs", type=int, default=2,
        help="parallel probe sandboxes (default: 2)",
    )
    p_eval.add_argument(
        "--out", default=None,
        help="run directory (default: runs/<YYYYmmdd-HHMMSS>)",
    )
    p_eval.add_argument(
        "--model", default=None,
        help="model under test, passed to omp as --model (default: omp's "
        "configured model)",
    )
    p_eval.add_argument(
        "--judge-model", default=None,
        help=f"judge model (default: ${judge.JUDGE_MODEL_ENV} or omp's default; "
        "point it at a different model than the one under test)",
    )
    p_eval.add_argument(
        "--skip-judge", action="store_true",
        help="skip the LLM judge (judge-scope behaviors render as pending)",
    )
    p_eval.add_argument(
        "--template", default=str(DEFAULT_TEMPLATE_DIR),
        help="fixture template dir materialized per sandbox",
    )
    p_eval.set_defaults(func=cmd_evaluate)

    p_grade = sub.add_parser(
        "grade",
        help="re-grade a stored events.jsonl (deterministic; --judge adds LLM verdicts)",
        description="Re-run grade_run (and optionally the judge) on a stored "
        "trace; writes verdicts.json and prints a verdict summary.",
    )
    p_grade.add_argument("--trace", required=True, help="path to events.jsonl")
    p_grade.add_argument(
        "--ctx", default=None,
        help="run context JSON (probe_kind, expected_mode, turn_boundaries, "
        "nested_events, ...; see grader.py docstring)",
    )
    p_grade.add_argument(
        "--judge", action="store_true",
        help="also run the LLM judge (needs omp unless patched in tests)",
    )
    p_grade.add_argument(
        "--judge-model", default=None,
        help=f"judge model (default: ${judge.JUDGE_MODEL_ENV} or omp's default)",
    )
    p_grade.add_argument(
        "--out", default=None,
        help="verdicts.json output path (default: verdicts.json next to the trace)",
    )
    p_grade.set_defaults(func=cmd_grade)

    p_rep = sub.add_parser(
        "report",
        help="re-render report.md from a stored run directory (no omp)",
        description="Rebuild report.md from run.json + per-probe verdicts.json; "
        "deterministic, no omp required.",
    )
    p_rep.add_argument(
        "--run-dir", required=True,
        help="run directory containing run.json and <probe>/verdicts.json",
    )
    p_rep.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
