# Trellis workflow adherence evaluator

## Goal

Build an evaluator that measures how faithfully an AI coding model follows the
Trellis workflow (`.trellis/workflow.md` phases, gates, required steps) when
driven under a given harness setup, and produces comparable per-behavior
adherence reports across harness configurations.

User value: evidence-based answers to "does harness config X make the model
follow the Trellis workflow better than config Y?" — per behavior, with trace
evidence, not a single opaque number.

## Background (confirmed facts)

- Greenfield repo; local harness is Oh My Pi (`omp` v18.0.11) with the Trellis
  extension (`.omp/extensions/trellis/index.ts`) injecting four observable
  surfaces: session context, per-turn `<workflow-state>` breadcrumb, sub-agent
  task context, path-scoped spec injection (config: `spec_injection`).
- omp supports headless evaluation: `-p` non-interactive mode, `--mode=json`
  structured output, `--auto-approve`, `-e`/`--no-extensions` (extension arm),
  `--config` overlays (config-knob arms), `--system-prompt` variants,
  `--session-dir` trace capture, `--max-time` run bounding.
- 19 of 26 catalog behaviors (see `research/behavior-catalog.md`) are
  mechanically checkable from tool-call traces + `.trellis/` snapshots; only
  judgment calls (request classification, artifact substance, checklist-plan
  alignment, completion-claim integrity) need an LLM judge.

## Key decisions

- D1 (user): MVP harness scope = **omp only**, with a normalized internal
  trace-event schema so other platforms can be added later without grader/
  report redesign.
- D2: Grading = deterministic predicates first; LLM judge (redacted,
  arm-blind) only for judgment-call behaviors; judge agreement hand-validated
  on a 10-trace sample.
- D3: Stack = Python 3 package, stdlib + PyYAML; omp driven as subprocess via
  `-p` + `--continue` turn chaining (revisit `--mode=rpc` only if spike S1
  breaks interactivity).
- D4: Captures from omp session files, not a capture extension — measuring
  must not perturb the measured setup.

## Requirements

- R1 Probe suite: 11 probes across kinds `simple-question` (approve/reject),
  `complex-feature`, `bugfix`, `consent-reject`, `negative-control`,
  `flaky-bug` (repeated-debugging escalation); 2-3 paraphrases each; every
  catalog behavior covered by ≥1 probe.
- R2 Behavior catalog (`behaviors.yaml`): 26 behaviors from
  `research/behavior-catalog.md`, each with check class (det/judge) and
  evidence type.
- R3 Runner: fresh sandboxed fixture repo per run; omp driven turn-by-turn;
  full trace (tool calls, messages, injections) normalized to `events.jsonl`;
  per-turn filesystem-hash snapshots of `.trellis/**` + fixture code + git log.
- R4 User simulator: 4 deterministic policy state machines (approve_all,
  reject_task_creation, reject_first_then_approve, approve_with_changes).
- R5 Grader: deterministic predicates for all `det` behaviors; judge for
  `judge` behaviors on redacted transcripts.
- R6 Report: probe × behavior matrix per arm, per-behavior adherence rates,
  turn/token cost per phase, over-adherence score (negative controls),
  delegation-mode measure (R9), checklist-discipline metrics (per-item
  verification rate, lone-todo turns, done-lag), violations with event-seq
  evidence pointers.
- R7 Harness arms: `trellis-on` (baseline), `trellis-off` (`--no-extensions`),
  `no-spec-injection` (config overlay); identical probes/policies across arms.
- R8 Over-adherence measurement: negative-control probes detect process
  theater on trivial asks (planning ceremony spawned for a one-line question).
- R9 Delegation-mode measure (sub-agent spawn vs inline): observed execution
  mode per run (deterministic from trace: any Phase-2 sub-agent spawn =>
  dispatch, else inline) scored against the probe kind's expected mode;
  `mode_agreement` reported per probe and aggregate. Dispatch-protocol
  behaviors (B10-B12) graded within dispatch-observed runs; inline variants
  within inline-observed runs.

## Acceptance criteria

- [ ] `python -m evaluator.cli evaluate --arm trellis-on --probes simple-question`
      runs end-to-end in sandboxes and produces `report.md` without manual steps.
- [ ] Every behavior in `behaviors.yaml` is asserted by at least one probe
      (coverage script passes).
- [ ] Re-running the grader on the same trace produces identical `det`
      verdicts.
- [ ] All three arms run the full suite; report highlights per-behavior deltas
      between arms (e.g. `trellis-off` vs `trellis-on`).
- [ ] Judge agreement on the hand-labeled 10-trace sample is recorded before
      judge verdicts are trusted.
- [ ] One hand-verified violation per arm traces correctly to event-seq
      evidence in the stored trace.
- [ ] Report includes `mode_agreement` per probe and aggregate; one
      dispatch-observed and one inline-observed run hand-checked against the
      trace evidence.

## Out of scope

- Code-quality measurement of produced artifacts (completion criterion per
  probe is included only as context).
- Multi-platform drivers (Claude Code / Codex / ...) — enabled later by D1's
  normalized schema, not built now.
- Dashboard UI / CI integration; report is markdown.
- Improving Trellis or omp themselves; this tool only measures.

## Technical notes

- Blocking spike S1 (interactive consent observability under `-p`) must
  resolve before driver freeze; full spike list in `research/omp-driver-notes.md`.
- `runs/` is gitignored; evaluator never runs against this repo's own `.trellis`.
- Events.jsonl and probe YAML schemas are the versioned external contracts
  (see design.md).
