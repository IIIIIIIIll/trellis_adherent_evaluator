# Logging Guidelines

> How logging is done in this project.

---

## Overview

This repo has **no logging framework** — no `import logging` anywhere in `evaluator/`. This is deliberate: the evaluator must be deterministic and its output must be artifacts, not log lines.

- **Libraries print nothing.** `evaluator/driver.py`, `grader.py`, `judge.py`, `report.py`, `trace.py`, `simulator.py`, `snapshot.py` contain no `print()` calls and emit no diagnostics. They communicate via return values and exceptions.
- **The CLI boundary prints.** All `print()` calls in the package live in `evaluator/cli.py` inside the `cmd_*` functions.
- **Structured artifacts are the record.** A run leaves files under `runs/<run>/` (layout documented in the `evaluator/cli.py` module docstring); there is nothing else to reconstruct.

## Log Levels

No levels exist. The CLI uses two channels plus exit codes (see `cmd_evaluate`, `cmd_grade`, `cmd_report` in `evaluator/cli.py`):

| Channel | Content | Example |
|---|---|---|
| stdout | result summary lines | `print(f"run: {run_dir}")`, `print(f"report: {run_dir / 'report.md'}")` (cli.py:457-462), verdict table (cli.py:499), `report written:` (cli.py:575) |
| stderr | per-probe / precondition errors | `print(f"error: no probes matched {args.probes!r}", file=sys.stderr)` (cli.py:385), `print(f"  error [{probe_id}]: {err}", file=sys.stderr)` (cli.py:465) |

Exit codes carry the level semantics: `0` ok, `1` run had probe errors, `2` usage/precondition failure (cli.py:466, 381-386).

## Structured Logging

The "log" is the artifact set under `runs/<run>/` (writers are in `evaluator/cli.py`):

| Artifact | Written by | Content |
|---|---|---|
| `<probe>/events.jsonl` | `_run_probe` (cli.py:333, via `trace.write_events`) | normalized trace, one event per line, `seq` monotonic from 1 (driver.py:187-188 passes `seq_start=len(events)+1`; trace.py increments per event) |
| `<probe>/ctx.json` | `_run_probe` (cli.py:363) | grader ctx echo — the exact re-grade input |
| `<probe>/verdicts.json` | `_run_probe` (cli.py:366); `cmd_grade` rewrites it next to the trace (cli.py:488-494) | det + judge verdicts, usage, metrics |
| `run.json` | `cmd_evaluate` (cli.py:440) | arm/model/date metadata, probe list |
| `report.md` | `cmd_evaluate` (cli.py:451) / `cmd_report` (cli.py:574) | rendered report |
| `<probe>/session/` | `driver.Driver.__init__` (driver.py:123) | raw omp `--session-dir`; input, not output |

Every events.jsonl event carries the full key set (`evaluator/trace.py` module docstring — the frozen events.jsonl contract); graders index evidence by `seq` (`evaluator/grader.py` `_seq`, judge transcript pointers `judge.py:161`).

## What to Log

Do not add logging. Record via artifacts:

- New observable runtime facts belong on the event schema (additively, per the frozen contract in `evaluator/trace.py`) or on the grader ctx (`_grade_ctx`, cli.py:164-174) so they flow into `events.jsonl` / `ctx.json`.
- Progress/errors during `evaluate` are already captured as `ProbeRun(error=...)` rows (cli.py:424-427) and land in `verdicts.json` + the stderr summary.
- The only wall-clock reads allowed are at evaluate time: run-dir naming and the `date` field in `run.json` (cli.py:392, 437). `report.render_report` takes `date` as a parameter — "no wall-clock reads; `date` is caller-supplied (stored in run.json at evaluate time) so re-renders are byte-identical" (report.py:554-555, module docstring report.py:2-5). Enforced by `tests/test_cli.py::test_report_rerender_deterministic`; grader determinism by `tests/test_grader.py::test_grade_run_deterministic`.

## What NOT to Log

- **No `print()`/`logging` inside `evaluator/` library code.** Surface diagnostics by raising (the CLI turns exceptions into `ProbeRun(error=...)` rows, cli.py:424-427) or by writing artifacts. New modules follow the same rule.
- **No ad-hoc parsing of omp session files.** `evaluator/trace.py` is the single parser ("platform-specific quarantine layer... OWNER of the events.jsonl contract", trace.py:1-4). Read raw sessions only through it (`trace.load_nested`, used once at cli.py:172). Wrong: grepping/globbing session JSONL for content elsewhere — `driver.py` only *locates* the newest `*.jsonl` to pass `--continue` (driver.py:222-224), it never interprets contents.
- **No timestamps in derived outputs.** Re-renders and re-grades must be byte-identical; anything derived from `datetime.now()` outside `cmd_evaluate` breaks that contract.
