"""Markdown report rendering for the Trellis workflow adherence evaluator.

Pure functions: no omp calls, no filesystem I/O. ``render_report`` is
deterministic -- the same run artifacts always render byte-identical
markdown, so ``cli.py report --run-dir`` re-renders are stable.

Inputs are per-probe :class:`ProbeRun` rows assembled by the CLI (evaluate
builds them in memory; ``report --run-dir`` rebuilds them from stored
verdicts.json). Metrics that need the event stream (checklist discipline,
cost per phase) are computed at row-build time via :func:`checklist_metrics`
/ :func:`cost_per_phase` and carried on the row, keeping rendering I/O-free.

Cell states: ``ok`` (passed), ``FAIL`` (failed; evidence seqs appended),
``n/a`` (verdict None -- out of stratum; excluded from all denominators),
``pending`` (judge-scope behavior with no judge verdict, e.g. --skip-judge).
"""

from __future__ import annotations

import json
import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from evaluator import trace

# The grader owns the todo/work event model and the lifecycle regexes; the
# report reuses its helpers so the metrics it prints cannot drift from the
# verdicts it renders (these are private-by-convention, single-owner).
from evaluator.grader import (  # noqa: F401
    CREATE_RE,
    START_RE,
    TODO_TOOLS,
    _COMPLETION_RE,
    _done_transitions,
    _ordered,
    _seq,
    _todo_timeline,
    _turns,
    _verification_events,
    _work_events,
)

__all__ = [
    "Cell",
    "ProbeRun",
    "checklist_metrics",
    "cost_per_phase",
    "merge_verdicts",
    "render_report",
]

_STATE_OK, _STATE_FAIL, _STATE_NA, _STATE_PENDING = "ok", "FAIL", "n/a", "pending"
_PHASES = ("no_task", "planning", "in_progress", "finish")
_MODE_ID = "mode_agreement"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeRun:
    """One graded probe run: everything the report needs, no I/O attached."""

    probe_id: str
    kind: str
    expected_mode: str
    events_path: str                  # link target relative to the report
    det_verdicts: Sequence[Any] = ()  # grader.Verdict (incl. mode_agreement)
    judge_verdicts: Sequence[Any] = ()  # judge.JudgeVerdict; empty = not run
    usage: Mapping[str, Any] = field(default_factory=dict)
    turns: int = 0
    phase_costs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    checklist: Mapping[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class Cell:
    """One merged matrix cell for a behavior in one probe run."""

    behavior_id: str
    state: str  # ok | FAIL | n/a | pending
    passed: bool | None
    evidence: tuple[int, ...] = ()
    notes: str = ""


# ---------------------------------------------------------------------------
# Verdict merging
# ---------------------------------------------------------------------------


def _state_of(passed: bool | None) -> str:
    if passed is True:
        return _STATE_OK
    if passed is False:
        return _STATE_FAIL
    return _STATE_NA


def merge_verdicts(
    det_verdicts: Iterable[Any],
    judge_verdicts: Iterable[Any] = (),
    judge_ids: Iterable[str] = ("B01", "B14", "B17", "B24"),
) -> dict[str, Cell]:
    """Merge grade_run verdicts with judge verdicts by behavior_id.

    Judge-scope ids take their verdict only from ``judge_verdicts`` (the det
    list carries placeholders for them by contract); a judge-scope id with no
    judge verdict renders ``pending``. ``passed=None`` renders ``n/a`` and is
    excluded from every rate denominator.
    """
    judge_map = {v.behavior_id: v for v in judge_verdicts or []}
    cells: dict[str, Cell] = {}
    for v in det_verdicts or ():
        bid = v.behavior_id
        if bid in set(judge_ids):
            jv = judge_map.pop(bid, None)
            if jv is None:
                cells[bid] = Cell(bid, _STATE_PENDING, None, (), "judge did not run")
            else:
                cells[bid] = Cell(bid, _state_of(jv.passed), jv.passed, (), jv.rationale)
        else:
            cells[bid] = Cell(
                bid, _state_of(v.passed), v.passed, tuple(v.evidence or ()), v.notes
            )
    for bid, jv in judge_map.items():  # judge verdicts beyond the det list
        cells[bid] = Cell(bid, _state_of(jv.passed), jv.passed, (), jv.rationale)
    return cells


def _row_cells(row: ProbeRun) -> dict[str, Cell]:
    return merge_verdicts(row.det_verdicts, row.judge_verdicts)


# ---------------------------------------------------------------------------
# Metrics computed from the event stream (at row-build time)
# ---------------------------------------------------------------------------


def checklist_metrics(events: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Checklist-discipline metrics for one run, mirroring grader B21/B22/B23
    (whose helpers are reused so report numbers cannot drift from verdicts).

    Returns None when the run has no checklist events.
    """
    timeline = _todo_timeline(events)
    if not timeline:
        return None
    work = _work_events(events)

    # Per-item verification (the b23 window model): attribute each work event
    # to the first item not yet done, then require a verification run between
    # the item's first work event and its done mark.
    order: list[str] = []
    for _, state in timeline:
        for ident in state:
            if ident not in order:
                order.append(ident)
    done_at: dict[str, int] = {}
    for seq, marked in _done_transitions(timeline):
        for ident in marked:
            done_at.setdefault(ident, seq)
    windows: dict[str, int] = {}
    for w in work:
        ws = _seq(w)
        current = next(
            (i for i in order if done_at.get(i) is None or done_at[i] > ws), None
        )
        if current is None:
            continue
        windows.setdefault(current, ws)
    verify_seqs = [_seq(e) for e in _verification_events(events)]
    last_seq = max(_seq(e) for e in _ordered(events))
    verified = sum(
        1
        for ident, first in windows.items()
        if any(first <= vs <= (done_at.get(ident) or last_seq) for vs in verify_seqs)
    )
    total = len(windows)

    # Lone-todo turns (b22): turns whose only tool activity is todo calls.
    turns, _has_boundary = _turns(events)
    lone = sum(
        1 for tools in turns if tools and all(e.get("tool") in TODO_TOOLS for e in tools)
    )

    # Done lag (b21): distance from the last work event to the done mark.
    lags = []
    for ident, dseq in done_at.items():
        prior = [_seq(e) for e in work if _seq(e) < dseq]
        if prior:
            lags.append(dseq - max(prior))

    return {
        "items": total,
        "verified": verified,
        "verification_rate": (verified / total) if total else None,
        "lone_todo_turns": lone,
        "max_done_lag": max(lags) if lags else None,
    }


def _bash_command(ev: Mapping[str, Any]) -> str:
    args = ev.get("args")
    if isinstance(args, Mapping):
        for key in ("command", "cmd"):
            v = args.get(key)
            if isinstance(v, str):
                return v
    return ""


def _event_text(ev: Mapping[str, Any]) -> str:
    v = ev.get("text")
    return v if isinstance(v, str) else ""


def cost_per_phase(events: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Turn/token cost per workflow phase, attributed per turn.

    Phase transitions follow the lifecycle markers the grader keys on:
    ``task.py create`` -> planning, ``task.py start`` -> in_progress, the
    first non-interrogative completion claim -> finish. Each ``turn_end``'s
    usage is attributed to the phase active when it fired.
    """
    acc = {p: {"turns": 0, "usage": {}} for p in _PHASES}
    phase = "no_task"
    for ev in _ordered(events):
        kind = ev.get("kind")
        if kind == "tool_call":
            blob = _bash_command(ev)
            if not blob:
                args = ev.get("args")
                blob = (
                    json.dumps(args, sort_keys=True, ensure_ascii=False)
                    if isinstance(args, Mapping)
                    else ""
                )
            if CREATE_RE.search(blob):
                phase = "planning"
            elif START_RE.search(blob):
                phase = "in_progress"
        elif kind == "message" and ev.get("role") == "assistant":
            text = _event_text(ev).rstrip()
            if text and not text.endswith("?") and _COMPLETION_RE.search(text):
                phase = "finish"
        elif kind == "turn_end":
            bucket = acc[phase]
            bucket["turns"] += 1
            bucket["usage"] = trace.add_usage(bucket["usage"], ev.get("usage") or {})
    return {p: b for p, b in acc.items() if b["turns"] or b["usage"]}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _md(text: str) -> str:
    """Escape a notes string for a markdown table cell / bullet."""
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _matrix_cell(cell: Cell) -> str:
    if cell.state == _STATE_FAIL and cell.evidence:
        return f"FAIL({','.join(str(s) for s in cell.evidence)})"
    return cell.state


def _matrix_section(rows: list[ProbeRun], behavior_text: Mapping[str, str] | None) -> list[str]:
    cells_per_row = [_row_cells(r) for r in rows]
    cols = sorted({b for cells in cells_per_row for b in cells if b != _MODE_ID})
    if any(_MODE_ID in cells for cells in cells_per_row):
        cols.append(_MODE_ID)
    lines = ["## Probe x behavior matrix", ""]
    lines.append("| probe | " + " | ".join(cols) + " |")
    lines.append("|---" * (len(cols) + 1) + "|")
    for row, cells in zip(rows, cells_per_row):
        vals = [_matrix_cell(cells[b]) if b in cells else "-" for b in cols]
        lines.append(f"| {row.probe_id} ({row.kind}) | " + " | ".join(vals) + " |")
    lines.append("")
    if behavior_text:
        lines += ["Behavior key:", "", "| id | behavior |", "|---|---|"]
        for b in cols:
            if b == _MODE_ID:
                lines.append(
                    f"| {_MODE_ID} | observed delegation mode matches the "
                    "probe kind's expected mode |"
                )
            else:
                lines.append(f"| {b} | {_md(behavior_text.get(b, ''))} |")
        lines.append("")
    return lines


def _rates_section(rows: list[ProbeRun]) -> list[str]:
    cells_per_row = [_row_cells(r) for r in rows]
    ids = sorted({b for cells in cells_per_row for b in cells if b != _MODE_ID})
    lines = ["## Per-behavior adherence rates", "", "| behavior | ok | fail | n/a | pending | rate |", "|---|---|---|---|---|---|"]
    for b in ids:
        states = [cells[b].state for cells in cells_per_row if b in cells]
        ok, fail = states.count(_STATE_OK), states.count(_STATE_FAIL)
        na, pending = states.count(_STATE_NA), states.count(_STATE_PENDING)
        attempted = ok + fail
        rate = f"{ok}/{attempted} ({100 * ok / attempted:.0f}%)" if attempted else "--"
        lines.append(f"| {b} | {ok} | {fail} | {na} | {pending} | {rate} |")
    lines += ["", "n/a and pending verdicts are excluded from the rate denominator.", ""]
    return lines


def _behavior_rates(rows: list[ProbeRun]) -> dict[str, tuple[int, int]]:
    """behavior_id -> (ok, attempted) with mode_agreement excluded."""
    cells_per_row = [_row_cells(r) for r in rows]
    ids = sorted({b for cells in cells_per_row for b in cells if b != _MODE_ID})
    out: dict[str, tuple[int, int]] = {}
    for b in ids:
        states = [cells[b].state for cells in cells_per_row if b in cells]
        attempted = states.count(_STATE_OK) + states.count(_STATE_FAIL)
        out[b] = (states.count(_STATE_OK), attempted)
    return out


def _mode_aggregate(rows: list[ProbeRun]) -> tuple[int, int] | None:
    """(agreed, scored) mode_agreement aggregate, or None if unscoreable."""
    scored = agreed = 0
    for row in rows:
        cell = _row_cells(row).get(_MODE_ID)
        if cell is None or cell.passed is None:
            continue
        _, expected = _parse_mode_notes(cell.notes)
        if expected in (None, "", "n/a") and row.expected_mode in (None, "", "n/a"):
            continue
        scored += 1
        agreed += 1 if cell.passed else 0
    return (agreed, scored) if scored else None


def _deltas_section(
    comparisons: Sequence[tuple[str, list[ProbeRun]]],
) -> list[str]:
    """Cross-arm per-behavior deltas (PRD acceptance: report highlights
    per-behavior deltas between arms). Rates are per-arm ok/attempted;
    delta columns cover every arm pair, in percentage points."""
    if len(comparisons) < 2:
        return []
    rates = {label: _behavior_rates(rows) for label, rows in comparisons}
    ids = sorted({b for r in rates.values() for b in r})
    pairs = list(itertools.combinations(range(len(comparisons)), 2))

    def _fmt(ok: int, attempted: int) -> str:
        return f"{ok}/{attempted} ({100 * ok / attempted:.0f}%)" if attempted else "--"

    def _delta(a: tuple[int, int] | None, b: tuple[int, int] | None) -> str:
        if not a or not b or not a[1] or not b[1]:
            return "--"
        pp = round(100 * a[0] / a[1] - 100 * b[0] / b[1])
        return f"{pp:+d}pp"

    lines = [
        "## Cross-arm per-behavior deltas",
        "",
        "Per-arm ok/attempted (n/a excluded from denominators); "
        "Δ columns give percentage-point rate differences between arm pairs "
        "(-- when either side has no attempted verdicts).",
        "",
    ]
    header = (
        "| behavior | "
        + " | ".join(label for label, _ in comparisons)
        + " | "
        + " | ".join(f"Δ {comparisons[i][0]} vs {comparisons[j][0]}" for i, j in pairs)
        + " |"
    )
    lines.append(header)
    lines.append("|" + "---|" * (1 + len(comparisons) + len(pairs)))
    for b in ids:
        cells = [_fmt(*rates[label].get(b, (0, 0))) for label, _ in comparisons]
        deltas = [
            _delta(rates[comparisons[i][0]].get(b), rates[comparisons[j][0]].get(b))
            for i, j in pairs
        ]
        lines.append("| " + b + " | " + " | ".join(cells + deltas) + " |")
    aggs = {label: _mode_aggregate(rows) for label, rows in comparisons}
    mode_cells = [_fmt(*agg) if agg else "--" for agg in (aggs[l] for l, _ in comparisons)]
    mode_deltas = [
        _delta(aggs[comparisons[i][0]], aggs[comparisons[j][0]]) for i, j in pairs
    ]
    lines.append("| " + _MODE_ID + " | " + " | ".join(mode_cells + mode_deltas) + " |")
    lines.append("")
    return lines


def _parse_mode_notes(notes: str) -> tuple[str, str | None]:
    m = re.search(r"observed=(\S+)", notes or "")
    observed = m.group(1).rstrip(";,") if m else ""
    m = re.search(r"expected=(\S+)", notes or "")
    expected = m.group(1).rstrip(";,") if m else None
    return observed, expected


def _mode_section(rows: list[ProbeRun]) -> list[str]:
    lines = ["## Delegation mode", "", "| probe | expected | observed | verdict |", "|---|---|---|---|"]
    scored = agreed = 0
    for row in rows:
        cell = _row_cells(row).get(_MODE_ID)
        if cell is None:
            continue
        observed, expected = _parse_mode_notes(cell.notes)
        if expected is None:
            expected = row.expected_mode
        lines.append(
            f"| {row.probe_id} | {expected or 'n/a'} | {observed or '?'} | {cell.state} |"
        )
    lines.append("")
    agg = _mode_aggregate(rows)
    if agg:
        lines.append(
            f"aggregate mode_agreement: {agg[0]}/{agg[1]} ({100 * agg[0] / agg[1]:.0f}%)"
        )
    else:
        lines.append("aggregate mode_agreement: no scoreable probes (all n/a)")
    lines.append("")
    return lines


def _overadherence_section(rows: list[ProbeRun]) -> list[str]:
    lines = ["## Over-adherence (negative controls)", ""]
    negative = [r for r in rows if r.kind == "negative-control"]
    if not negative:
        lines += ["No negative-control probes in this run.", ""]
        return lines
    lines += ["| probe | B04 | ceremony signals |", "|---|---|---|"]
    scored = clean = 0
    for row in negative:
        cell = _row_cells(row).get("B04")
        state = cell.state if cell else _STATE_PENDING
        lines.append(f"| {row.probe_id} | {state} | {_md(cell.notes if cell else '')} |")
        if cell is not None and cell.passed is not None:
            scored += 1
            clean += 1 if cell.passed else 0
    lines.append("")
    score = (
        f"{clean}/{scored} ({100 * clean / scored:.0f}%)"
        if scored
        else "no scoreable negative controls"
    )
    lines.append(f"over-adherence score (B04-clean negative controls): {score}")
    lines.append("")
    return lines


def _checklist_section(rows: list[ProbeRun]) -> list[str]:
    lines = ["## Checklist discipline", ""]
    rows_with = [r for r in rows if r.checklist]
    if not rows_with:
        lines += ["No checklist activity in this run (no todo events).", ""]
        return lines
    lines += [
        "| probe | items (worked) | verified | per-item verification | lone-todo turns | max done-lag (seq) |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows_with:
        m = row.checklist
        rate = m.get("verification_rate")
        rate_s = f"{100 * rate:.0f}%" if isinstance(rate, (int, float)) else "--"
        lag = m.get("max_done_lag")
        lines.append(
            f"| {row.probe_id} | {m.get('items', 0)} | {m.get('verified', 0)} | {rate_s} "
            f"| {m.get('lone_todo_turns', 0)} | {lag if lag is not None else '--'} |"
        )
    lines.append("")
    return lines


def _usage_cell(usage: Mapping[str, Any]) -> str:
    return (
        f"{usage.get('input', 0)} | {usage.get('output', 0)} "
        f"| {usage.get('totalTokens', 0)} | {float(usage.get('cost', 0) or 0):.4f}"
    )


def _cost_section(rows: list[ProbeRun]) -> list[str]:
    lines = ["## Cost per phase (turns / tokens)", ""]
    lines += ["| probe | turns | input | output | total tokens | cost |", "|---|---|---|---|---|---|"]
    agg_phase: dict[str, dict[str, Any]] = {}
    for row in rows:
        usage = row.usage or {}
        lines.append(f"| {row.probe_id} | {row.turns} | {_usage_cell(usage)} |")
        for phase, bucket in (row.phase_costs or {}).items():
            agg = agg_phase.setdefault(phase, {"turns": 0, "usage": {}})
            agg["turns"] += bucket.get("turns", 0)
            agg["usage"] = trace.add_usage(agg["usage"], bucket.get("usage") or {})
    lines += ["", "By workflow phase (aggregated across probes):", ""]
    lines += ["| phase | turns | input | output | total tokens | cost |", "|---|---|---|---|---|---|"]
    for phase in _PHASES:
        agg = agg_phase.get(phase)
        if not agg:
            continue
        lines.append(f"| {phase} | {agg['turns']} | {_usage_cell(agg['usage'])} |")
    lines.append("")
    return lines


def _violations_section(rows: list[ProbeRun]) -> list[str]:
    lines = ["## Violations", ""]
    any_violation = False
    for row in rows:
        cells = _row_cells(row)
        for bid in sorted(cells):
            cell = cells[bid]
            if cell.state != _STATE_FAIL:
                continue
            any_violation = True
            entry = f"- **{bid} -- {row.probe_id}**: {_md(cell.notes)}"
            if cell.evidence:
                seqs = ", ".join(str(s) for s in cell.evidence)
                entry += f" -- evidence [seq {seqs}]({row.events_path})"
            lines.append(entry)
    if not any_violation:
        lines.append("No failed verdicts.")
    lines.append("")
    return lines


def _errors_section(rows: list[ProbeRun]) -> list[str]:
    failed = [r for r in rows if r.error]
    if not failed:
        return []
    lines = ["## Run errors", ""]
    for r in failed:
        lines.append(f"- {r.probe_id}: {_md(r.error)}")
    lines.append("")
    return lines


def render_report(
    rows: Iterable[ProbeRun],
    *,
    run_name: str,
    arm: str,
    model: str,
    date: str,
    run_dir: str = "",
    behavior_text: Mapping[str, str] | None = None,
    comparisons: Sequence[tuple[str, list[ProbeRun]]] | None = None,
) -> str:
    """Render the full markdown report for one arm's run.

    Deterministic: no wall-clock reads; ``date`` is caller-supplied (stored
    in run.json at evaluate time) so re-renders are byte-identical.
    """
    rows = list(rows)
    lines = ["# Trellis workflow adherence report", ""]
    lines.append(f"- arm: **{arm}**")
    lines.append(f"- model: {model or '(omp default)'}")
    lines.append(f"- date: {date}")
    lines.append(f"- run dir: {run_dir or run_name}")
    lines.append(f"- probes: {len(rows)}")
    lines.append("")
    lines += _matrix_section(rows, behavior_text)
    lines += _rates_section(rows)
    lines += _deltas_section(list(comparisons or []))
    lines += _mode_section(rows)
    lines += _overadherence_section(rows)
    lines += _checklist_section(rows)
    lines += _cost_section(rows)
    lines += _violations_section(rows)
    lines += _errors_section(rows)
    return "\n".join(lines).rstrip() + "\n"
