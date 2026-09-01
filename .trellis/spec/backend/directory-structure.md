# Directory Structure

> How backend code is organized in this project.

---

## Overview

Single-package Python repo: all logic lives in `evaluator/` (Python 3.13, stdlib + PyYAML only — `evaluator/__init__.py` re-exports only the `catalog` and `probes` symbols). The package drives omp as a subprocess and grades Trellis-workflow adherence. Two root data files sit one level above the package: `behaviors.yaml` (loaded via `catalog.DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "behaviors.yaml"`) and the `probes/` YAML suite (schema owned by `evaluator/probes.py`). Module ownership is strict:

- `evaluator/trace.py` owns the events.jsonl schema — `evaluator/snapshot.py` imports its `_event` constructor rather than building event dicts itself.
- `evaluator/driver.py` owns the model-under-test omp subprocess; `evaluator/judge.py` has its own omp path (`judge._run_omp`); `evaluator/simulator.py` and `evaluator/report.py` are pure (no omp, no I/O).
- `evaluator/grader.py` owns the todo/work event model; `evaluator/report.py` imports its helpers (`_todo_timeline`, `_work_events`, ...) so rendered metrics cannot drift from the verdicts it renders (see the import block in `evaluator/report.py`).
- `evaluator/cli.py` orchestrates the per-probe pipeline (`_run_probe`: sandbox -> driver -> trace -> grader -> judge -> report).

Frozen data contracts are documented in `.trellis/spec/backend/evaluator-contracts.md`.

---

## Directory Layout

```
.
├── evaluator/                # the package: loaders, driver, grader, judge, report
├── probes/                   # 11 probe definitions (*.yaml)
├── arms/                     # harness-arm omp --config overlays (*.yml)
├── fixtures/repo-template/   # sandbox fixture, materialized fresh per run
├── tests/                    # pytest suite; fixtures/ golden session
├── runs/                     # run artifacts — gitignored (.gitignore), never commit
├── behaviors.yaml            # behavior catalog, frozen entry shape
└── .trellis/spec/backend/    # contract + convention docs
```

---

## Module Organization

| Module | Responsibility | Key symbols |
|---|---|---|
| `evaluator/cli.py` | Orchestration; `evaluate`/`grade`/`report` subcommands | `ARMS`, `_run_probe`, `cmd_evaluate`, `_resolve_probe_paths`, `REPO_ROOT` |
| `evaluator/driver.py` | omp subprocess control: sandbox materialization + turn loop | `BASE_FLAGS` (`["-p", "--mode=json", "--auto-approve"]`), `Driver.run_session`, `materialize_sandbox`, `TurnResult`/`SessionResult` |
| `evaluator/simulator.py` | User-simulator policy state machines (pure) | `UserSimulator.reply`, `is_consent_question`, `POLICY_APPROVE_ALL` ... `POLICY_APPROVE_WITH_CHANGES` |
| `evaluator/snapshot.py` | Per-turn `{path: sha256}` sandbox snapshots + `git:log` pseudo-key | `take_snapshot`, `snapshot_event`, `SNAPSHOT_GIT_LOG_KEY` |
| `evaluator/trace.py` | omp session JSONL -> internal events.jsonl schema | `parse_session`, `normalize_entries`, `read_events`, `write_events`, `load_nested`, `INJECTION_KINDS` |
| `evaluator/grader.py` | Deterministic predicates, one `Verdict` per catalog behavior | `grade_run`, `PREDICATES`, `Verdict`, `mode_agreement`, `detect_mode`, `JUDGE_ONLY_IDS` |
| `evaluator/judge.py` | LLM judge for judgment-call behaviors (B01/B14/B17/B24) | `RUBRICS`, `judge_transcript`, `redact_text`, `build_transcript`, `JudgeVerdict`, `JUDGE_MODEL_ENV` |
| `evaluator/report.py` | I/O-free deterministic markdown rendering | `render_report`, `ProbeRun`, `Cell`, `merge_verdicts`, `checklist_metrics` |
| `evaluator/catalog.py` | Loads `behaviors.yaml` | `load_catalog`, `Behavior`, `BehaviorCatalog.uncovered_behavior_ids` |
| `evaluator/probes.py` | Probe loader; owns the probe YAML schema contract | `Probe`, `load_probes`, `PROBE_KINDS`, `_parse_probe`, `_PROBE_SUFFIXES` |

Non-package trees:

- `probes/*.yaml` — 11 files (`simple-q-approve.yaml`, `bugfix-crash.yaml`, `flaky-duplicates.yaml`, `neg-control-typo.yaml`, ...), one scenario each: `id`, `kind`, `prompt`, `paraphrases`, `simulator_policy`, `expected_mode`, `expected_behaviors`, `fixture_expectation.verify`, `max_turns`, `timeout` (see `probes/simple-q-approve.yaml`). Default dir: `cli.DEFAULT_PROBES_DIR = REPO_ROOT / "probes"`.
- `arms/` — harness-arm overlays mapped to omp flags by `cli.ARMS`; `arms/no-spec-injection.yml` is a `--config` YAML setting `spec_injection.enabled: false`.
- `fixtures/repo-template/` — the sandbox fixture: `notes/` CLI app (`notes/storage.py`, `notes/cli.py`) with planted defects targeted by the bugfix/flaky probes (e.g. `probes/bugfix-crash.yaml`: "the third entry in data/notes.json has no "title" key"), its own `tests/`, `data/notes.json`, and `.trellis/`/`.omp/` copies; materialized fresh per probe by `driver.materialize_sandbox` (default `cli.DEFAULT_TEMPLATE_DIR`).
- `tests/` — pytest suite (`test_trace.py`, `test_grader.py`, `test_cli.py`) plus `tests/fixtures/sample_session.jsonl`, the real omp session captured in spike S1c, loaded as `GOLDEN_SESSION` (`test_cli.py`, `test_grader.py`) / `FIXTURE` (`test_trace.py`).
- `runs/<run>/<probe>/` — output root: per probe `sandbox/`, `session/`, `events.jsonl`, `ctx.json`, `verdicts.json`; `run.json` + `report.md` at run level (layout documented in the `evaluator/cli.py` module docstring). `runs/` is in `.gitignore`.

Where new things go:

- New deterministic check -> predicate fn + `PREDICATES` registry entry in `evaluator/grader.py`; judge-scope ids additionally need a `RUBRICS` entry in `evaluator/judge.py` and a slot in `grader.JUDGE_ONLY_IDS`.
- New scenario -> one YAML in `probes/` + a catalog entry in `behaviors.yaml` (coverage check: `BehaviorCatalog.uncovered_behavior_ids`).
- New arm -> `arms/<name>.yml` overlay + `ARMS` entry in `evaluator/cli.py`.

---

## Naming Conventions

- Modules: single-purpose snake_case files in `evaluator/`; private helpers underscore-prefixed (`cli._run_probe`, `probes._parse_probe`); public surface declared in `__all__` (e.g. `evaluator/report.py`).
- Contracts: frozen dataclasses — `grader.Verdict`, `judge.JudgeVerdict`, `report.ProbeRun`/`Cell`, `catalog.Behavior`, `probes.Probe` (all `@dataclass(frozen=True)`).
- Probes: kebab-case filename equals the `id` (`probes/simple-q-approve.yaml` -> `id: simple-q-approve`).
- Tests: `tests/test_<module>.py` mirrors `evaluator/<module>.py`.
- Runs: timestamped `runs/<YYYYmmdd-HHMMSS>/` (default in `cmd_evaluate`, `evaluator/cli.py`).

---

## Examples

- `evaluator/trace.py` — reference for the single-owner pattern: schema constants up top, one normalization path, validated against real captured data (`tests/test_trace.py` runs on `tests/fixtures/sample_session.jsonl`).
- `evaluator/report.py` — pure-render pattern; determinism pinned by the byte-identical re-render checks in `tests/test_cli.py`.
- `tests/test_grader.py::test_golden_sample_session_end_to_end` — integration pin grading the real session through `evaluator.trace`.
