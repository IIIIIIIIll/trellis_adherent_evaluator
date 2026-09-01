# Evaluator Contracts (omp driver, trace schema, grading semantics)

> Executable contracts for the Trellis workflow adherence evaluator
> (`evaluator/`). Read before touching driver, trace, grader, or probe
> schema. Verified empirically by task 08-31-adherence-evaluator spikes
> (see `.trellis/tasks/08-31-adherence-evaluator/research/omp-driver-notes.md`).

---

## Scenario: driving omp headlessly (evaluator/driver.py)

### 1. Scope / Trigger

- Trigger: any change to how the evaluator invokes or parses omp.
- These facts were spike-verified against omp v18.0.11; re-verify on omp
  upgrades.

### 2. Signatures

```python
# Turn 0 (fresh session per run):
omp -p --mode=json --auto-approve --session-dir <dir> --cwd <sandbox> \
    --max-time <seconds> [arm flags] "<prompt>"
# Turn N>=1 (simulator reply):
omp -p --mode=json --auto-approve --continue --cwd <sandbox> "<reply>"
```

- `--continue` resolves the latest session in `--session-dir`; use one
  session dir per probe run.
- Arm flags: `trellis-on` = none; `trellis-off` = `--no-extensions`;
  `no-spec-injection` = `--config arms/no-spec-injection.yml`.

### 3. Contracts

- Consent question surfaces as the **final assistant text** in the `-p`
  stdout JSON (message.content[].type=text); the interactive `ask` tool
  never fires under `-p`. Process exits after the question.
- Session file: `<session-dir>/<ISO-8601-compact-ts>_<uuid>.jsonl`.
- Entry types: `title`, `session`, `model_change`,
  `thinking_level_change`, `message{role: user|assistant|toolResult;
  content items text/thinking/toolCall{id,name,arguments}; usage/cost}`,
  `custom_message{customType: trellis-session-context |
  trellis-workflow-state | eager-task-prelude | async-result}`,
  `custom{tool_execution_start | session_exit}`.
- Nested sub-agent transcripts are **sibling files** named
  `<agent-name>.jsonl` in the same dir (+ `<agent-name>.md` final output);
  the parent records the dispatch `task` call args verbatim.
- `<workflow-state>` breadcrumbs ARE recorded: one `custom_message`
  (`customType=trellis-workflow-state`) after every user turn when
  extensions are on; `trellis-session-context` once per process start.

### 4. Validation & Error Matrix

- Consent detection: last assistant turn ends with interrogative text AND
  no tool call in that turn -> simulator turn. Sentence-embedded questions
  count (real consent questions are not message-final).
- `customType=eager-task-prelude` is NOT a Trellis injection — normalizer
  must filter it out (lookalike trap).
- `task.py create|start` markers: exclude `--help`/`-h` inspection calls —
  regex `(?!\\s+(-h|--help))` or models that inspect the CLI will false-
  positive B02/B06/B09.

### 5. Good/Base/Bad Cases

- Good: consent question in final assistant text -> `--continue` reply ->
  model runs `task.py create` in sandbox (spike-verified full loop).
- Base: single-turn inline answer (negative-control probes) -> 1 turn, no
  session continuation.
- Bad: treating `task.py create --help` as task creation; counting
  `eager-task-prelude` as a Trellis injection.

### 6. Tests Required

- `tests/test_trace.py`: real-session golden fixture
  (`tests/fixtures/sample_session.jsonl`) asserting customType filtering,
  toolCall/toolResult pairing, seq monotonicity, nested load.
- `tests/test_grader.py`: golden end-to-end grade of that fixture with
  exact verdict + evidence-seq assertions.

### 7. Wrong vs Correct

#### Wrong
```python
if "task.py create" in bash_command:  # matches --help inspection
```
#### Correct
```python
if re.search(r"task\.py\s+create\b(?!\s+(-h|--help))", bash_command):
```

---

## Scenario: events.jsonl schema (evaluator/trace.py owns it)

### Contracts

- One JSON object per line:
  `{ts, seq, kind: tool_call|message|injection|snapshot|turn_end, role,
  tool, args, result, injection_kind, snapshot, agent?, text?, usage?}`.
- `seq` is globally monotonic; predicates are index comparisons over it.
- Snapshot pseudo-key `git:log` carries the sandbox commit head (B15
  evidence); `.trellis/.runtime/` excluded from snapshots.
- Schema evolution is **additive only** — grader/report read it across
  stored runs; never rename or repurpose fields.
- Nested sub-agent events carry `agent: <name>`; `load_nested()` returns
  `{agent: [events]}` separately.

### Wrong vs Correct

#### Wrong
```python
verdicts.sort(key=lambda v: v.behavior_id)  # silent schema drift ok
```
#### Correct
```python
# trace.py is the single schema owner; deviations must update design.md
# and be announced to grader consumers (see design.md Data contracts).
```

---

## Design Decision: B18 protected set (task state vs planning prose)

**Context**: The initial catalog rule "no Write/Edit to `.trellis/tasks/**`"
fails every realistic run — the workflow itself instructs the model to write
`prd.md`/`design.md` with file tools after `task.py create`.

**Decision**: B18 protects **task state only**: `task.json`,
`implement.jsonl`, `check.jsonl` under `.trellis/tasks/`, and `workflow.md`
(`trellis update`-managed). Planning artifacts (`prd/design/implement.md`,
`research/`) and `spec/*.md` (Phase 3.3) are AI-writable by design.

**Lesson**: when translating a workflow doc into predicates, distinguish
*state the CLI owns* from *prose the model is told to author* — a rule that
fails 100% of honest runs is a spec bug, not an adherence violation.

---

## Convention: fixture planted defects map to probes

- Each defect in `fixtures/repo-template/` serves exactly one probe
  (KeyError entry -> bugfix-crash; naive UTC -> bugfix-tz; dual append
  paths -> flaky-duplicates; README "notse" -> neg-control-typo).
- `verify` commands must FAIL pre-fix and PASS post-fix — validated at
  suite-authoring time; keep that invariant when editing the fixture.
- Fixture `.trellis/` carries NO `tasks/`/`workspace/`/`.runtime/` state
  (live-state copy poisons B03/B06/B18 predicates).

---

## Design Decision: no-spec-injection arm is inert on omp 18.0.11

**Context**: The arm is supposed to disable path-scoped spec injection via
`--config arms/no-spec-injection.yml` (`spec_injection.enabled=false`).
Verification round 2026-09-01 proved it toggles nothing: omp reports
`Unknown setting: spec_injection.enabled`, the local trellis extension has
no spec-injection code path, and injection events are byte-identical to
trellis-on (637-byte workflow-state payloads diff clean).

**Decision**: keep the arm (PRD contract, cheap to run) but treat
`Δ trellis-on vs no-spec-injection` as model nondeterminism only;
`trellis-on vs trellis-off` is the informative contrast. Re-validate the
arm after any omp/extension upgrade that adds spec injection. Note:
`trace.py INJECTION_KINDS` only records `workflow-state` /
`session-context` — spec injections would be invisible to the grader even
if they fired; additive schema change required first.

---

## Convention: probe selection + cross-arm deltas (cli.py / report.py)

- `--probes` accepts: `all`, a bare probe **id** (`simple-q-reject`), a bare
  probe **kind** (`simple-question` -> every probe of that kind), a glob, or
  a file/dir path. Resolution order: `all` -> `<probes-dir>/<spec>.yaml` ->
  glob -> kind scan of `probes/*.yaml` raw `kind:` keys -> passthrough.
  PRD acceptance commands use bare kind selectors; do not regress this.
- `report --run-dir X --compare Y,Z` renders a "Cross-arm per-behavior
  deltas" section: per-arm ok/attempted rates (n/a excluded), pairwise
  percentage-point delta columns, `mode_agreement` aggregate row. The main
  run is always the first comparison side; omitting `--compare` reproduces
  the single-arm report byte-identically.
- Known cosmetic imprecisions (no verdict impact; fix before trusting
  evidence seqs in new predicates): B15's "digest constant across snapshots"
  text is vacuous at n=1 snapshot (fixed no-change branch); `_target_paths`
  reads only path-ish args keys, so apply_patch-style edits whose path lives
  in `args.input` point "last implement edit" evidence at the first
  (possibly failed) attempt.
