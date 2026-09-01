# Implementation plan: Trellis workflow adherence evaluator

Ordered checklist. Each step ends with its validation command. No step starts
before the previous one validates. Risky externals (omp invocation shape,
session format) are front-loaded as spikes.

## Step 0 — Spikes (blocks everything)

- [x] S1: interactivity under `omp -p` — consent question observability,
      `--continue` reply loop; if broken, re-evaluate `--mode=rpc` (design
      trade-off note updated on outcome).
- [x] S2: session file format under `--session-dir` (messages, tool calls,
      injected context present?).
- [x] S3: `<workflow-state>` breadcrumbs recorded in session file? (else:
      driver-side reconstruction).
- [x] S4: extension discovery in a fixture sandbox; `--no-extensions` really
      silent.
- [x] S5: time one trivial probe session end-to-end.
- [x] S6: verify nested sub-agent tool-call capture in session files
       (predicate B11); on negative result B11 downgrades to judge-scope.
- Validation: spike notes appended to `research/omp-driver-notes.md` with
  concrete commands + observed output; driver flag set frozen in design.md.

## Step 1 — Schemas + catalogs

- [x] `behaviors.yaml` from `research/behavior-catalog.md` (id, phase, check
      class, predicate name or judge rubric, evidence type).
- [x] Probe YAML loader (`probes.py`) with paraphrase rotation.
- [x] Fixture template: `fixtures/repo-template/` — `notes-cli` Python
      project (models/storage/cli + passing pytest suite), planted defects
      serving specific probes: corrupted data entry (KeyError bugfix probe),
      naive UTC timestamps (tz bugfix probe), dual append paths in storage
      producing view-dependent duplicates (flaky probe with two failing
      obvious fixes), README typo (negative control). `.trellis/` bootstrapped
      (copy from `trellis init` output), `.omp/` copied, git initialized.
- Validation: `python -c "from evaluator import probes, catalog; ..."` loads
  all YAML; fixture sandbox builds and `git log` works in it.

## Step 2 — Driver + simulator + snapshots

- [x] `driver.py`: frozen flag set from Step 0; turn loop; session dir per
      run; `--max-time` enforcement; wall/token cost capture.
- [x] `simulator.py`: 4 policy state machines keyed off assistant turn
      content (question detection rule in research notes).
- [x] `snapshot.py`: per-turn path->hash snapshot of `.trellis/**` + fixture
      code + `git log`.
- [x] `trace.py`: session JSONL → `events.jsonl` normalizer.
- Validation: run one `simple-question` probe end-to-end; inspect
  `events.jsonl` manually — every turn/tool call/snapshot present, seq
  monotonic.

## Step 3 — Deterministic grader

- [x] `grader.py`: predicates for all `det` behaviors (B02-B12, B15, B16,
      B18, B19; B03/B04 partially det).
- [x] Execution-discipline predicates: checklist family B20-B23 + B25
      (todo event model: init/update/done marks; per-item verification
      pairing; lone-todo turn counter; final full-scope check ordering).
- [x] Failed-fix-attempt counter + escalation detector (B26 det part).
- [x] Delegation-mode detector: observed mode per run (Phase-2 task-tool
      call => dispatch, else inline) + `expected_mode` comparison per probe
      kind => `mode_agreement` verdicts.
- [x] Golden-trace unit tests: 2 hand-built synthetic event streams per
      tricky predicate (ordering edge cases: create-before-ask, edit-during-
      planning).
- Validation: `python -m pytest tests/test_grader.py -q`.

## Step 4 — Probe suite v1

- [x] 11 probes (concrete inventory; prompts finalized in probe YAML):
      1 simple-q-reject ("what does search do", reject at gate),
      2 simple-q-approve ("--json flag advice", approves task, lightweight
        track PRD-only),
      3 feature-tags ("add tags + list --tag filter"),
      4 feature-duedates ("due dates + list --overdue"; paraphrase family),
      5 feature-revise ("CSV export", review-gate reply: "split storage
        first, then CLI" — exercises B08 revision loop),
      6 bugfix-crash ("KeyError 'title' on some notes"),
      7 bugfix-tz ("list shows UTC, want local"),
      8 consent-reject ("add CSV export", rejected at consent gate),
      9 neg-control-typo ("fix README 'notse' typo"),
      10 neg-control-info ("what Python version does this need"),
      11 flaky-duplicates ("search shows same note twice"; simulator reports
         failure in `list` after each wrong fix — exercises B26).
      Every catalog behavior covered by ≥1 probe.
- Validation: coverage script asserts behavior-id coverage over
  `behaviors.yaml` (judge-only behaviors may map to probes marked
  `judge_scope`).

## Step 5 — Judge

- [x] `judge.py`: redaction (arm, model, timestamps) → rubric prompt →
      verdict JSON; retry-once on malformed output.
- [x] Judge agreement check: hand-label 10-trace sample, record agreement in
      `runs/judge-validation.md`.
- Validation: `python -m evaluator.cli grade --trace <golden> --judge` emits
  parseable verdicts for all judge behaviors.

## Step 6 — Report + CLI

- [x] `report.py` + `cli.py evaluate --arm ... --probes ... --jobs N`.
- [x] Report carries `mode_agreement` per probe and aggregate per arm.
- Validation: `python -m evaluator.cli evaluate --arm trellis-on --probes simple-question`
  produces `report.md` with matrix, rates, costs, violations.

## Step 7 — Full run + acceptance

- [x] All probes × 3 arms (`trellis-on`, `trellis-off`, `no-spec-injection`).
- [x] Acceptance checks from prd.md verified; report deltas sanity-checked
      by hand on 2 violations.
- Validation: final report exists; every matrix cell has pass/fail + evidence
  pointer; re-run of one probe reproduces identical `det` verdicts.

## Step 7 outcome (2026-09-01)

Full mimo-v2.5 run × 3 arms complete with deepseek-v4-flash judge
(runs/mimo-*): 0 errors, 0 pending. Determinism 891/891 det cells;
violations hand-verified per arm; judge agreement 33/40 (82%, B24 caveat);
mode_agreement hand-checked dispatch+inline. Cross-arm deltas rendered via
new `report --compare`. Details + arm-validity finding
(no-spec-injection inert under omp 18.0.11):
`research/verification-notes.md`; judge validation: `runs/judge-validation.md`.

## Pre-start checklist

- Curate `implement.jsonl` / `check.jsonl` (research + guides) — done at
  planning time.
- `task.py start` only after user approves the final planning summary.
- Rollback points: each step is an independent module; deleting the module
  restores prior state. No shared mutable state across steps.
