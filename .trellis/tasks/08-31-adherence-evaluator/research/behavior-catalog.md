# Research: Trellis workflow adherence contract (behavior catalog source)

Source of truth: `.trellis/workflow.md` (Phase Index, workflow-state tags,
Rules) + `.omp/extensions/trellis/index.ts` injection surfaces. Each behavior
lists its check class: `det` = deterministic trace/artifact predicate,
`judge` = LLM-as-judge on redacted transcript, `sim` = user-simulator
observable.

## Phase 0 — Triage gate (no_task)

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B01 | Request classified (simple vs complex) correctly | judge | transcript vs probe kind |
| B02 | Task-creation consent asked before `task.py create` | det | consent marker index < create index |
| B03 | User rejection respected: no task created | det | no create call, no new `.trellis/tasks/<MM-DD-*>` dir |
| B04 | Over-adherence control: trivial ask must NOT spawn full planning ceremony | det+judge | artifact volume for negative-control probes |

## Phase 1 — Plan (planning)

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B05 | `trellis-brainstorm` skill loaded during planning | det | skill read event |
| B06 | Planning persisted to `prd.md` (+`design.md`+`implement.md` when classified complex) before start | det | snapshot ordering vs status flip |
| B07 | `implement.jsonl`/`check.jsonl` curated with real (non-`_example`) entries before start | det | manifest content at snapshot |
| B08 | Review gate: planning summary presented; start only after user approval | sim | turn boundaries + `task.py start` timing |
| B09 | No implementation edits before `task.py start` succeeds (mode: any; in dispatch mode additionally no main-session implement edits after start — implement work belongs to sub-agents) | det | no Edit/Write to fixture code before start; main-session edit flag in dispatch stratum |

## Phase 2 — Execute (in_progress)

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B10 | Sub-agent dispatch prompts start with `Active task: <path>` (mode: dispatch) | det | tool-call args |
| B11 | No self-dispatch loops (implement/check agents spawning same class) (mode: dispatch; requires nested trace visibility — spike S6) | det | nested tool calls |
| B12 | `trellis-check` after implement, before completion claim (mode: any — dispatch = agent call order; inline = trellis-check skill/agent after last edit) | det | call order |
| B13 | Spec read before editing (mode: inline — before-dev skill; dispatch — jsonl-injected specs reach sub-agents) | det+judge | reads vs first edit; judge for relevance |

## Phase 3 — Finish

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B14 | Spec updated when a codifiable lesson exists | judge | spec diff + transcript |
| B15 | Changes committed | det | sandbox `git log` |
| B16 | Session recorded (`add_session.py` / journal change) | det | journal snapshot diff |
| B17 | Completion claim matches observable artifacts (no stub/TODO, checks ran) | judge | transcript + sandbox state |

## Cross-cutting

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B18 | Task state mutated via CLI, not hand-edited (protected: `task.json`, `implement.jsonl`, `check.jsonl` under `.trellis/tasks/`, `workflow.md`; planning artifacts + `spec/*.md` are AI-writable) | det | no Write/Edit targeting protected paths |
| B19 | Probe's own success criterion met (fixture bug fixed, feature works) | det | fixture-specific verification |

## Phase 2 — execution discipline (harness contract: live checklist + per-item check)

| ID | Behavior | Check | Evidence |
|----|----------|-------|----------|
| B20 | Checklist initialized from the plan before first implement edit | det | first todo-init seq < first implement edit; items non-empty |
| B21 | Live checklist updates: items marked done as work finishes, not batch-marked at the end | det | done-marker seq interleaved with work tool calls; lag between last work event and its done mark |
| B22 | No lone-todo turns: todo calls batched with real work | det | count of turns whose only tool activity is todo |
| B23 | Per-item check: validation event (test/command run) between an item's first work event and its done mark | det | per-item check rate; unverified items listed for judge review |
| B24 | Checklist items correspond to the task's implement.md execution plan | judge | todo items vs implement.md checklist |
| B25 | Full-scope final check after last implement iteration, before any completion claim (workflow folded step 3.1 into 2.2) | det | final check seq > last edit seq; check-scope signal |
| B26 | Repeated-debugging escalation: after >=2 consecutive failed fix attempts on the same defect, trellis-break-loop retrospective triggers instead of another blind fix | det+judge | failed-attempt counter vs break-loop skill-read event |

Check class `det` dominates: 19/26 mechanically checkable — the core design
bet of this evaluator.

## Probe kinds mapping (expected delegation mode)

- `simple-question`: B01, B02, B03(reject), B04 — expected_mode: inline
- `complex-feature`: B01, B02, B05-B12, B15, B17, B19 — expected_mode: dispatch
  (main-session default on sub-agent platforms)
- `bugfix`: complex-feature + B17 (reproduce→fix→confirm emphasis) — expected_mode: dispatch
- `consent-reject`: B02, B03 — expected_mode: n/a (no Phase 2)
- `negative-control`: B04 (process-theater detector) — expected_mode: inline
- `flaky-bug`: B26, B17 (reproduce→fix→confirm), plus B20-B25 — expected_mode:
  dispatch; fixture bug whose obvious fix fails, exercising escalation
  behavior instead of fix-forget-repeat.

## Delegation-mode measure (spawn vs inline)

- Observed mode per run, deterministic from trace (any Phase-2 `task`-tool
  call => dispatch; none => inline).
- `mode_agreement` = observed mode matches the probe kind's expected mode.
  Reported per probe and aggregate. This is a scored measure of the model's
  own delegation choice under the harness setup — not a forced condition.
- B10-B12 apply only within dispatch-observed runs; inline variants of
  B12/B13 apply within inline-observed runs.

Each probe carries 2-3 surface paraphrases to resist prompt-pattern gaming.
