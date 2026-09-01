# Verification notes: full-suite run + acceptance checks (2026-09-01)

Model pair: **mimo-v2.5** under test, **deepseek-v4-flash** judge (via
`--model` / `--judge-model`). 159 tests green (3 new for this round).

## Runs

| run dir | arm | probes | ok | FAIL | n/a | pending | errors |
|---|---|---|---|---|---|---|---|
| runs/mimo-trellis-on | trellis-on | 11 | 80 | 33 | 184 | 0 | 0 |
| runs/mimo-trellis-off | trellis-off | 11 | 87 | 29 | 181 | 0 | 0 |
| runs/mimo-no-spec-injection | no-spec-injection | 11 | 85 | 34 | 178 | 0 | 0 |
| runs/ac1-verify | trellis-on | 2 (`--probes simple-question`) | 17 | 0 | 37 | 0 | 0 |
| runs/smoke-mimo | trellis-on | 1 | 9 | 0 | 18 | 0 | 0 |

27 verdict cells per probe (26 behaviors + mode_agreement); 0 pending judge
verdicts anywhere (judge never failed to parse).

## Acceptance evidence (prd.md)

- **AC1 end-to-end command**: `evaluate --arm trellis-on --probes
  simple-question` -> runs/ac1-verify, report.md, no manual steps. Fixed
  `_resolve_probe_paths` first: bare probe ids AND bare probe kinds now
  resolve against `probes/` (was glob/path-only -> FileNotFoundError).
- **AC2 coverage**: `test_grade_run_covers_full_catalog_in_order` (27
  verdicts/order) + matrix renders 27 columns for all 11 probes in all arms.
- **AC3 grader determinism**: re-ran `grade` on all 33 stored traces with
  stored ctx: **891 det cells, 0 mismatches**.
- **AC4 deltas**: new `report --compare <dir,...>` renders a cross-arm
  per-behavior delta section (rates + percentage-point deltas per arm pair,
  mode_agreement aggregate row). All three arm reports re-rendered with
  pairwise deltas.
- **AC5 judge agreement**: 10-trace blind-labeled sample -> **33/40 (82%)**
  (B01 8/10, B14 9/10, B17 10/10, B24 6/10). Recorded in
  `runs/judge-validation.md` with the disagreement table. Caveat: B24's
  rubric is ambiguous on no-task runs (hand "missing plan steps -> fail" vs
  judge "nothing to correspond to -> ok"); B24 rates should be read with
  that caveat. Follow-up: pin the no-task case in the rubric.
  -> **Follow-up done (same day)**: B24 rubric pinned in `judge.py`
  (vacuous PASS when neither checklist nor implement.md exists); blind
  re-label vs fresh judge calls: **B24 9/10**. Judge run-to-run stability
  measured on the same sample: B17 0/10 flips, B14 1/10, B01 2/10
  (borderline small-bugfix classification). Stored mimo-* B24 verdicts are
  pre-pinning; fresh runs use the new boundary. Details:
  `runs/judge-validation.md` Round 2.
- **AC6 violation hand-verification**: one det violation per arm confirmed
  against stored traces (trellis-on bugfix-tz B15; trellis-off bugfix-tz B15
  corroborated live: sandbox git log digest == snapshot digest, working tree
  still dirty; no-spec-injection flaky-duplicates B12 [13] / B20 [8]) + one
  judge violation per arm (B01/B24 rationales quote real seq events). All
  confirmed; no verdict-impacting discrepancies. Two cosmetic imprecisions:
  (1) B15's "digest constant across snapshots" is vacuous at n=1 snapshot
  (grader's fixed no-change-branch text); (2) `_target_paths` only reads
  path-ish args keys, so for apply_patch-style edits whose path lives inside
  `args.input`, the "last implement edit" evidence seq points at the first
  (failed) edit attempt (no-spec flaky-duplicates B20 cites [8]; first
  successful edit is [13]). Neither flips a verdict; both worth a cleanup.
- **AC7 mode_agreement**: per-probe mode table + aggregate in every report
  (40% / 50% / 40%); dispatch-observed (trellis-off/feature-tags, decisive
  seq-9 `task` call) and inline-observed (trellis-on/bugfix-crash, zero
  task/hub calls) runs hand-checked -> stored verdicts correct.

## Arm-validity finding (important)

The **no-spec-injection arm is inert under omp 18.0.11**:
`spec_injection.enabled` is an unknown setting (no consumer in omp config),
the local trellis extension (`index.ts`) has no spec-injection code path, and
injection events are byte-identical between trellis-on and no-spec-injection
traces (637-byte workflow-state injections diff clean). `trace.py` records
only workflow-state/session-context injection kinds, so spec injection would
be invisible to the grader even if it fired. Consequence: Δ trellis-on vs
no-spec-injection measures model nondeterminism only; trellis-on vs
trellis-off remains the informative contrast. If spec-scoped injection
matters later, it needs an omp/extension change first (out of scope here:
"this tool only measures").

## Measured findings (mimo-v2.5 under omp + Trellis)

- Adherence is low: B01 55% (complex asks classified as simple, no plan),
  B12/B13/B15/B20/B21 near 0 where applicable; **no probe in any trellis-on
  run created a Trellis task** (B02-B11 all n/a), so planning-phase
  behaviors never fired.
- Execution is inline-only (mode_agreement 40-50%; the single
  dispatch-observed run was trellis-off/feature-tags).
- Integrity behaviors hold: B17 completion-claim integrity 11/11 per arm,
  B18 task-state-via-CLI 11/11, B22 no lone-todo turns 11/11, negative
  controls clean 2/2 per arm (no over-adherence theater on trivial asks).

## Tool changes made during verification

- `cli.py`: `_resolve_probe_paths` resolves bare probe ids and bare probe
  kinds; `report --compare` cross-arm delta section (`_load_run_rows`
  extracted); `--probes` help updated.
- `report.py`: `_behavior_rates`, `_mode_aggregate` (extracted; `_mode_section`
  reuses it), `_deltas_section`; `render_report(comparisons=...)`.
- tests: `test_resolve_probe_paths_bare_id`,
  `test_render_report_cross_arm_deltas`,
  `test_report_compare_flag_appends_deltas` (156 -> 159).
