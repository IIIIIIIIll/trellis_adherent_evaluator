# Design: Trellis workflow adherence evaluator

## Architecture

Python 3 package (stdlib + PyYAML), no web UI. Drives `omp` as a subprocess.

```
evaluator/
  cli.py          # orchestration: evaluate / grade / report subcommands
  driver.py       # omp subprocess control (turn loop, session dir, arm flags)
  simulator.py    # user-simulator state machines (policy per probe)
  sandbox.py      # fixture repo builder per run (fresh copy, git init)
  snapshot.py     # .trellis filesystem snapshots (path -> hash) over time
  trace.py        # normalize omp session JSONL -> events.jsonl (internal schema)
  catalog.py      # loads behaviors.yaml
  probes.py       # loads probe YAML, picks paraphrase
  grader.py       # deterministic predicates over events + snapshots
  judge.py        # LLM judge via `omp -p --mode=json` on redacted transcript
  report.py       # markdown report: matrix + rates + costs + violations
behaviors.yaml    # behavior catalog (from research/behavior-catalog.md)
probes/*.yaml     # probe definitions
fixtures/repo-template/  # small project: few source files, planted bug, .trellis + .omp bootstrapped
runs/             # gitignored: one dir per run (sandbox copy, sessions, events, report)
```

Fixture is a `notes-cli` Python project whose planted defects each serve a
specific probe: corrupted data entry (KeyError bugfix), naive UTC timestamps
(tz bugfix), dual append paths (flaky duplicates probe — two obvious fixes
each fail in a different view), README typo (negative control). Full
11-probe inventory in implement.md Step 4.

Boundaries: `driver` knows omp only; `grader` knows the event schema only;
platform specifics are quarantined in `trace.py` (normalizer). Adding a
platform later = new normalizer + driver flags, zero grader/report changes.

## Data contracts

### Data sources

- Scored data is **generated fresh per evaluation run** (probe → omp session
  in a sandbox → session JSONL + snapshots → events.jsonl → grader). Past
  sessions are never the scored corpus: no controlled ground truth
  (expected mode, expected behaviors, simulator branches) and confounded
  harness versions make historical adherence scores meaningless.
- Run artifacts are retained under `runs/<run>/` so re-grading and judge
  re-scoring never require a re-run.
- Historical logs have three supporting roles only: (1) spike-S2 calibration
  corpus for the session-file normalizer, (2) messy real transcripts in the
  judge-validation sample, (3) one-time violation-taxonomy skim of
  `.trellis/workspace` journals to prioritize probe kinds.
- Out of scope (possible later extension): an offline "audit mode" scoring
  an arbitrary historical session file — only ground-truth-free behaviors
  (B10, B18, B20-B23) would be checkable; excluded to keep score semantics
  clean.

### Normalized trace event (events.jsonl, one JSON object per line)

```json
{"ts": 1234.5, "seq": 42, "kind": "tool_call|message|injection|snapshot|turn_end",
 "role": "assistant|user|simulator|system",
 "tool": "bash|read|edit|write|task|ask|...", "args": {}, "result": "",
 "injection_kind": "workflow-state|session-context|spec|task-context",
 "snapshot": {".trellis/tasks/x/prd.md": "h1", "git:log": "h3"},
 "text": "", "agent": "", "usage": {}}
```

Ordering (`seq`) is the grader's backbone: predicates are index comparisons
over this stream. Snapshots are taken after every assistant turn.

Additive fields (schema-owner update after normalizing real spike sessions;
every event always carries all keys, empty defaults):

- `text` — content of `message` and `injection` events (assistant/user text,
  injected breadcrumb text). `result` remains tool-output only on `tool_call`
  events (attached from the matching `toolResult` by toolCallId).
- `agent` — `""` on parent-session events; nested sub-agent events (from
  `trace.load_nested`, keyed `{agent_name: [events]}`) are labeled with the
  agent file stem. Merging nested events into the parent stream is the
  grader's decision.
- `usage` — `{}` except on `turn_end`: summed per-turn assistant usage
  `{input, output, cacheRead, cacheWrite, totalTokens, reasoningTokens, cost}`
  from session entries (turn/token cost source for reports).
- `turn_end` is emitted by the normalizer at each user-turn boundary (before
  the next user message, and at end of stream when the trailing turn had
  assistant activity); role `assistant`.
- Injections come only from `trellis-*` custom_message customTypes
  (`trellis-workflow-state` → `workflow-state`, `trellis-session-context` →
  `session-context`). omp's `eager-task-prelude` is dropped; omp's
  `async-result` (sub-agent result delivery) is a `message` event with role
  `system`, not an injection.
- Snapshot keys are repo-relative POSIX paths → sha256; `git:log` is a
  pseudo-key holding sha256 of `git log --oneline` stdout (value change across
  snapshots = a commit happened). `.git/`, `__pycache__/`, `.pytest_cache/`,
  `*.pyc`, and `.runtime/**` (time-driven marker noise) are excluded.

### Probe YAML

```yaml
id: complex-feature-auth-endpoint
kind: complex-feature            # maps to expected behavior set
prompt: "..."                    # + paraphrases: [ ... ]
simulator_policy: approve_all    # approve_all | reject_task_creation |
                                 # reject_first_then_approve | approve_with_changes
fixture_expectation:             # probe's own success criterion (B19)
  verify: "pytest tests/test_auth.py -q passes"
max_turns: 12
timeout: 900
```

### Arm definition

```yaml
id: trellis-on
omp_flags: []                    # extension discovered from fixture .omp/
id: trellis-off
omp_flags: ["--no-extensions"]
id: no-spec-injection
config_overlay: "arms/no-spec-injection.yml"   # spec_injection.enabled: false
id: workflow-prompt-arm
omp_flags: ["--append-system-prompt", "arms/workflow-restatement.txt"]
```

## Harness arms (MVP)

1. `trellis-on` — full default setup (baseline).
2. `trellis-off` — `--no-extensions`: measures how much of adherence survives
   without per-turn breadcrumb + injections (the harness's causal effect).
3. `no-spec-injection` — config overlay: isolates path-scoped spec injection.
All arms share probe suite and simulator policies; report diffs per behavior.

## Delegation-mode measure (sub-agent spawn vs inline)

- The model's own choice to spawn sub-agents or work inline — under the
  harness setup under test — is itself a testable measure, not a config knob.
- Observed mode per run is deterministic from the trace: any `task`-tool call
  during Phase 2 => dispatch; none => inline. No judge involved.
- Each probe kind declares an expected mode (probe YAML `expected_mode`):
  complex-feature/bugfix => dispatch (main-session default on sub-agent
  platforms), simple-question/negative-control => inline (no dispatch
  ceremony), consent-reject => n/a (no Phase 2).
- Headline metric `mode_agreement` = observed mode matches expected mode;
  reported per probe and aggregate per arm. Inline processing where dispatch
  was expected (and vice versa) is a scored disagreement with trace
  evidence — never a silent pass.
- When dispatch is observed, protocol behaviors B10-B12 apply within that
  run; when inline is observed, inline variants (B09/B12/B13 inline forms)
  apply. Behaviors carry mode tags so graders score the right variant.

## Grading pipeline

1. Run probe → sandbox copy built from `fixtures/repo-template`, omp driven
   turn-by-turn, simulator answers per policy, snapshots + session capture.
2. Normalize session JSONL → `events.jsonl`.
3. Deterministic predicates (catalog `det` entries) → pass/fail + evidence
   pointers (seq ranges, file hashes).
4. Judge pass (`judge` entries): redacted transcript (model identity, arm
   stripped) + rubric → verdict JSON {behavior_id, verdict, rationale}.
   Same completion path as runner (`omp -p --mode=json`) for MVP.
5. Report: probe × behavior matrix per arm; per-behavior adherence rate =
   passed/attempted across probes where the behavior is in scope; turn/token
   cost per phase; over-adherence score from negative controls; violation
   list linking to event seq in the stored trace.

## Key trade-offs

- `-p` + `--continue` chaining over rpc mode: simpler, per-turn process spawn
  is slower but sessions are minutes not hours; revisit only if S1 spike
  shows lost interactivity.
- Session-file parsing over a capture extension: zero interference with the
  thing being measured (a capture extension would itself be a harness change).
- Filesystem-hash snapshots instead of inotify: ordering granularity is
  per-turn, which is exactly what every `det` predicate needs.
- Judge uses the same omp path (no extra provider dependency); judge model is
  a CLI flag, default different from the model under test.

## Risks / open technical items

- S1..S6 spikes in `research/omp-driver-notes.md`; S1 (interactivity under
  `-p`) is blocking for driver freeze, scheduled first in implement.md.
  S6 covers nested sub-agent session capture (predicate B11 needs the
  sub-agent's own tool calls); if nested calls are not traceable, B11
  downgrades to judge-scope (documented, not silently dropped).
- Judge validity: hand-label a 10-trace sample once, measure judge agreement
  before trusting judge behaviors (guard against rubric-gaming).
- Sandbox leakage: every run gets a fresh fixture copy + `--session-dir`
  inside `runs/<run>/`; evaluator never runs against this repo's own `.trellis`.

## Operational

- `runs/` gitignored; reports written to `runs/<run>/report.md` and copied to
  `reports/<arm>-<ts>.md` for comparison runs.
- Suite parallelism = one process per probe sandbox (CPU-bound omp subprocess
  count via `--jobs`, default 2) — model API rate limits are the real cap.
- Rollback: greenfield; deleting a module is the rollback. External surface
  to keep stable: events.jsonl schema + probe YAML schema (versioned fields).
