# Error Handling

> How errors are handled in this project.

---

## Overview

Two kinds of errors, never mixed:

1. **Fatal — raised as `RuntimeError`** at subprocess boundaries only: the omp binary is missing or a judge invocation exits nonzero. Messages name the failing binary and (for the judge) the remedy.
2. **Error-as-data** — anything a probe run can survive becomes a string field (`ProbeRun.error`, `JudgeVerdict.rationale`) or a stderr line, so one bad probe never discards the other results.

## Error Types

No custom exception classes. The fixed vocabulary:

- `RuntimeError`, always raised `from exc` (or as the leaf) so the cause chain survives:
  - `evaluator/driver.py:159` (`Driver.run_turn`): `f"omp binary not found: {self.omp_bin!r}"` wrapping the `FileNotFoundError`.
  - `evaluator/judge.py:307-310` (`_run_omp`): binary-not-found message plus a remedy hint (`inject runner=... / use --skip-judge offline`).
  - `evaluator/judge.py:312-314` (`_run_omp`): `f"omp judge failed (exit {proc.returncode}): {_clip(proc.stderr, 400)}"` — stderr clipped to 400 chars, never dumped raw.
- Error-as-data:
  - `report.ProbeRun.error: str = ""` (evaluator/report.py:78) holds `"ExcType: message"` strings for probes that died.
  - `JudgeVerdict(passed=None)` (evaluator/judge.py:321-327) marks an unparseable judge reply (n/a), with the clipped raw output in `.rationale`.

## Error Handling Patterns

**Subprocess hard bound (driver turns).** Every omp subprocess is launched with `start_new_session=True`; omp's own `--max-time` is the soft inner bound and `proc.communicate(timeout=self.per_turn_timeout)` (default 900.0, evaluator/driver.py:119) is the hard outer bound. On `subprocess.TimeoutExpired`: kill the whole process group with `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`, falling back to `proc.kill()` on `ProcessLookupError`/`PermissionError` (evaluator/driver.py:151-156), then re-`communicate()` to reap. Timeout is a result flag (`TurnResult.timed_out`, `exit_code=None`), not an exception — proven by `test_driver_enforces_per_turn_timeout` (tests/test_trace.py:460).

**Suite resilience.** `cmd_evaluate` wraps each `fut.result()` in `try/except Exception` with the comment "one failed probe must not kill the suite" (evaluator/cli.py:417-431). On failure it appends `(probe.id, f"{type(exc).__name__}: {exc}")` to `errors` AND a `report.ProbeRun` error row carrying the same string in `.error`, keeping the report matrix aligned with the probe list. `_errors_section` renders one `## Run errors` bullet per failed probe (evaluator/report.py:530-538); every error is also printed to stderr after the summary (evaluator/cli.py:464-465).

**Judge malformed output: one retry, then n/a.** `judge_transcript` parses each reply under `for _attempt in range(2)` — initial try plus exactly one retry (evaluator/judge.py:362). Still malformed → `JudgeVerdict(bid, None, f"judge output unparseable after retry: {_clip(raw, 200)}")` (evaluator/judge.py:373-375). `passed=None` maps to state `"n/a"` (`_state_of`, evaluator/report.py:97-102) and is excluded from every rate denominator (`attempted = ok + fail`, evaluator/report.py:309-312 and `_behavior_rates` evaluator/report.py:323). Tests: `test_judge_retries_once_on_malformed_output` (tests/test_cli.py:282), `test_na_excluded_from_rate_denominator` (tests/test_cli.py:348).

**Quiet degradation for evidence helpers.** Context collectors return empty values instead of raising: `_safe_yaml` → `None` on `(OSError, yaml.YAMLError)` (evaluator/cli.py:99-104), `_run_verify` → `False` on `TimeoutExpired` (evaluator/cli.py:188-189), `_sandbox_git_state` skips git calls that fail (evaluator/cli.py:205-206), `_implement_plan` → `""` on `OSError` (evaluator/cli.py:218-221). A missing artifact degrades one evidence field; it must not fail a probe.

## API Error Responses

No HTTP API; the CLI is the interface, so exit codes are the error contract (`cmd_evaluate`):

| exit | meaning | source |
|---|---|---|
| 0 | run completed, no probe errors | evaluator/cli.py:466 |
| 1 | run completed but ≥1 probe errored (`errors` non-empty) | evaluator/cli.py:466 |
| 2 | environment error before the run: missing arm overlay or no probes matched | evaluator/cli.py:379-386 |

Pre-run failures print `error: <message>` to stderr and return 2 before creating a run dir (evaluator/cli.py:379-386). Probe failures are listed after the summary as `  error [<probe-id>]: <ExcType>: <message>` (evaluator/cli.py:464-465). `main` returns the handler's int directly — no wrapping, no re-raising (evaluator/cli.py:685-688).

## Common Mistakes

**Wrong — let a probe exception propagate, or swallow it:**

```python
rows.append(fut.result())   # one bad probe kills the whole suite run
# or:
except Exception: pass      # probe vanishes from report; exit code says 0
```

**Correct — record and continue** (evaluator/cli.py:417-431). Both halves are required: the `errors` tuple list drives the exit code and stderr lines; the error `ProbeRun` row keeps the probe slot in report.md via `## Run errors`. A probe that dies before writing `events.jsonl` still shows up in the report — that is the deliverable, not a silent skip. Also: never raise from evidence helpers (see quiet degradation above) and never dump unclipped stderr — clip via `_clip` (evaluator/judge.py:104-108) before embedding in a message or rationale.
