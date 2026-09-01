# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

The evaluator's quality bar is **deterministic, offline, fixture-pinned**:

- Full suite: `python -m pytest -q tests` — 159 tests, ~4.5s (tests/test_cli.py,
  test_grader.py, test_trace.py); all green before any change lands.
- Core invariant: identical inputs produce identical artifacts — re-grading
  a stored trace reproduces stored verdicts; re-rendering reproduces a
  byte-identical `report.md` (tests/test_cli.py `test_report_rerender_deterministic`).
- Real-capture golden fixtures, not synthetic data: schema and grading tests
  pin a real omp consent-loop session (tests/fixtures/sample_session.jsonl,
  spike S1c), validating frozen schemas against session-file reality.

## Forbidden Patterns

- Network-dependent tests. The omp turn loop and LLM judge are always
  monkeypatched offline: the `patched_pipeline` fixture (tests/test_cli.py:95)
  swaps `driver.Driver.run_session` and `judge.judge_transcript` for canned
  versions; driver tests use `FAKE_OMP_LOOP`/`FAKE_OMP_SLEEP` (tests/test_trace.py
  `test_driver_enforces_max_turns`, `test_driver_enforces_per_turn_timeout`).
- Reordering or filtering `grade_run` output. The verdict list is the full
  catalog in fixed order — judge placeholders first (`_JUDGE_ONLY_IDS` =
  B01/B14/B17/B24, evaluator/grader.py:147), then `sorted(PREDICATES)`, then
  `mode_agreement`. Never `verdicts.sort(key=...)` (the Wrong example in
  evaluator-contracts.md, "events.jsonl schema").
- Renaming or repurposing events.jsonl fields. Schema evolution is additive
  only; tests/test_trace.py `test_normalize_real_session_schema` pins the
  exact `EVENT_KEYS` set and 1-based `seq` monotonicity, so any rename or
  key removal fails the suite.
- Mutating tests/fixtures/sample_session.jsonl. Tests pin exact content:
  tests/test_trace.py `test_tool_call_result_pairing` asserts 9 tool_call /
  9 toolResult pairs, `test_fixture_is_real_consent_loop_capture` pins
  customType counts; tests/test_grader.py pins exact verdicts and evidence
  seqs against the same fixture.
- Wall-clock, RNG, or dict-iteration-order dependence in grading or
  rendering paths (predicate iteration is `sorted(PREDICATES)` for a reason).

## Required Patterns

- Golden-fixture testing for schema work: normalize the real capture via
  `trace.load_session(FIXTURE)` and assert on the real output
  (tests/test_trace.py `test_normalize_real_session_schema`).
- New grader behaviors must register in `PREDICATES` (evaluator/grader.py:1619)
  and survive the catalog-order test — tests/test_grader.py
  `test_grade_run_covers_full_catalog_in_order` expects exactly
  `list(JUDGE_ONLY_IDS) + sorted(PREDICATES) + ["mode_agreement"]`
  (27 verdicts: 4 judge placeholders + 22 deterministic + mode_agreement).
- Determinism acceptance for any grading/report change:
  tests/test_grader.py `test_grade_run_deterministic` (same events + ctx
  produce equal verdict lists), tests/test_cli.py `test_grade_rerun_deterministic`
  (re-grading stored events.jsonl reproduces stored verdicts.json),
  `test_report_rerender_deterministic`.
- New events.jsonl fields extend `EVENT_KEYS` (tests/test_trace.py) plus a
  fixture-backed assertion; update design.md and announce to grader/report
  consumers (evaluator-contracts.md).

## Testing Requirements

- Scope to `tests/`: `python -m pytest -q tests`. Bare `pytest -q` from the
  repo root collects gitignored runs/**/sandbox copies and fails collection
  (2026-09-01: 80 errors) — never drop the path.
- Determinism is the acceptance gate: repeat grading equality,
  byte-identical report re-render, full-catalog coverage in order.
- Offline end-to-end stays offline: tests/test_cli.py
  `test_evaluate_end_to_end` runs the whole pipeline (sandbox → trace →
  grader → judge → report) via `patched_pipeline`; keep new stages network-free.
- Trace I/O round-trip safety: tests/test_trace.py `test_write_and_read_events_roundtrip`
  and `test_orphan_tool_result_dropped_and_no_empty_turn_end` guard write/read
  and malformed-input handling.

## Code Review Checklist

- Read .trellis/spec/backend/evaluator-contracts.md before touching driver,
  trace, grader, judge, or probe schema — it owns the omp invocation
  contract, the events.jsonl schema, and grading semantics.
- Full suite green: `python -m pytest -q tests` (159 passed expected); no
  new skips to force it.
- Changed `grade_run` or report output → determinism tests updated and
  passing, not just adjusted expectations.
- Added a behavior → catalog-order test updated; judge-scope behaviors go in
  `_JUDGE_ONLY_IDS` (placeholder n/a verdict, evaluator/grader.py:1676),
  not a predicate.
- Schema changes are additive only; breaking events.jsonl changes need
  design.md update + consumer announcement.
