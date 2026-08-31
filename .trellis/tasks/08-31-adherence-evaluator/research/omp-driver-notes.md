# Research: omp harness driver evidence

## omp CLI (v18.0.11) capabilities relevant to the runner

Verified via `omp --help` on this workstation:

| Need | Flag |
|------|------|
| Non-interactive turn | `-p, --print` (process prompt and exit) |
| Multi-turn continuation | `-c, --continue` / `--resume=<id>` |
| Structured output | `--mode=json` (text/json/rpc/rpc-ui) |
| Tool-call approval | `--auto-approve`, `--approval-mode=always-ask\|write\|yolo` |
| Harness arm: Trellis extension off | `--no-extensions` (explicit `-e` paths still load) |
| Harness arm: explicit extension | `-e .omp/extensions/trellis/index.ts` |
| Harness arm: config knobs | `--config=<overlay.yml>` (repeatable) |
| Harness arm: prompt variants | `--system-prompt`, `--append-system-prompt` |
| Trace capture | `--session-dir=<dir>` (session files saved there); `--no-session` to disable |
| Run bounding | `--max-time=<duration>` |
| Model selection | `--model=<value>` (fuzzy), `--thinking=<level>` |
| Skill filtering | `--no-skills`, `--skills=<glob>` |
| Working dir | `--cwd=<dir>` |

## Planned invocation shape

```
omp -p --mode=json --auto-approve --session-dir runs/<run>/sessions \
    --cwd <sandbox> --model <model> --max-time 10m "<probe prompt>"
# subsequent simulator turns:
omp -p --mode=json --auto-approve --continue --cwd <sandbox> "<user reply>"
```

## Spikes required before driver freeze (risk order)

- S1 (blocking): how do interactive consent/`ask` interactions surface in
  `-p` mode? Expected: model ends turn with a question → process exits →
  driver's user simulator replies via `--continue`. Must verify the model's
  consent question is observable in `--mode=json` output (or session file).
  If `ask`-tool-like blocking behavior exists under `--auto-approve`,
  fall back to `--mode=rpc` for a persistent session with request stream.
- S2: session file format under `--session-dir` — confirm JSONL contains
  messages, tool calls (name/args/result), and injected context blocks;
  needed by the trace normalizer.
- S3: are per-turn `<workflow-state>` breadcrumbs recorded in the session
  file? If not, `injection` events are reconstructed by the driver (text is
  deterministic — parsed from workflow.md by the extension).
- S4: extension discovery inside a fixture sandbox (`findProjectRoot` walks
  up from cwd; fixture carries its own `.omp/` copy) — confirm arm
  `trellis-off` really disables injection (`--no-extensions`).
- S5: wall-clock cost per probe session; size `--max-time` and suite
  parallelism accordingly.
- S6 (delegation-mode measure support): verify whether nested sub-agent tool
  calls are recorded in session files (predicate B11 needs the sub-agent's
  own tool calls; observed spawn/inline mode from the parent trace is
  unaffected). If nested calls are not traceable, B11 downgrades to
  judge-scope — recorded here, not silently dropped.

## User-simulator detection rule

Consent question = assistant turn that (a) ends with an interrogative and no
tool call, or (b) invokes the platform ask/clarification mechanism. Simulator
policy (approve_all / reject_task_creation / reject_first_then_approve /
approve_with_changes) resolves the reply. Policy state machines live with
probe YAML, not hard-coded.

## Spike outcomes (2026-08-31)

```yaml
# omp 18.0.11, model opencode-go/glm-5.3-flash --thinking low, sandbox
# /tmp/trellis-spike-sandbox (repo .trellis/ + .omp/ copies, git init, 40-line notes.py),
# sessions under /tmp/trellis-spike-sessions/<probe>/. All values observed, not inferred.
s1_interactivity: works            # -p --mode=json + --continue consent loop; no rpc fallback needed
s1_consent_field: >-
  stdout JSONL event stream: last {type: turn_end|message_end} event with
  message.role=assistant and message.content[].type=text carrying the question
  ("May I create a Trellis task for this and enter the planning phase?");
  no dedicated question/ask field; the ask tool (interactive-mode-only per
  omp --help) was never invoked in -p mode. Same text is the last assistant
  message in the session file.
s1_continue_context: true          # --continue resumes the session; --cwd works from a foreign shell cwd
s2_session_path_pattern: >-
  "<session-dir>/<ISO-8601-compact-ts>_<session-uuid>.jsonl" (e.g.
  2026-08-31T13-37-56-777Z_01a0580a-7a29-74c7-935b-0a0e76f0e3a7.jsonl);
  sibling directory "<same-basename>/" holds nested sub-agent transcripts
  (<name>.jsonl + <name>.md); each --continue appends to the same file.
s2_event_fields:
  - title{v, title, updatedAt}
  - session{version: 3, id, timestamp, cwd}
  - model_change{id, parentId, model, resolvedModelIsFallback}
  - thinking_level_change{thinkingLevel}
  - "message{id, parentId, timestamp, message:{role: user|assistant|toolResult,
    content: [text|thinking|toolCall{id, name, arguments, intent}],
    usage:{input, output, cacheRead, totalTokens, cost}, stopReason, api, provider, model}}"
  - "message role=toolResult adds {toolCallId, toolName, content, details, isError}"
  - "custom_message{customType: trellis-session-context | trellis-workflow-state |
    eager-task-prelude | async-result, content, display, attribution}"
  - "custom{customType: tool_execution_start{data:{toolCallId, toolName, startedAt,
    args?, intent}} | session_exit{data:{reason, kind}}}"
  - sub-agent session files add a session_init entry
s3_breadcrumbs_recorded: true
s3_caveat: >-
  custom_message customType=trellis-workflow-state is appended after EVERY user
  message (6/6 user turns with extensions on), containing the literal
  <workflow-state>[workflow-state:no_task]...</workflow-state> text plus
  <session-overview>. Assistant/tool-only turns carry none. customType=
  trellis-session-context is injected once per -p process start (repeats on
  every --continue). omp's own non-Trellis eager-task-prelude reminder also
  appears as a custom_message — normalizer must filter by customType.
s4_no_extensions_silent: true      # zero trellis-* custom_messages; only omp built-in eager-task-prelude remains
s5_trivial_seconds: 17.5           # one user turn, one tool round-trip, final answer (s5 run)
s6_nested_capture: separate_files
s6_default_tools: [read, bash, edit, write, grep, glob, task, hub, yield]
  # as recorded in session events: parent files show read/bash/edit/write/grep/glob/task/hub;
  # sub-agent file shows glob/bash/yield. omp --help full default set adds lsp, python,
  # notebook, inspect_image, browser, computer (disabled by default), todo, web_search,
  # ask (interactive-only).
```

### Evidence (reproducible commands + observed output)

Sandbox build (never touched this repo's .trellis):

```
mkdir -p /tmp/trellis-spike-sandbox
cp -r <repo>/.trellis <repo>/.omp /tmp/trellis-spike-sandbox/
cd /tmp/trellis-spike-sandbox && git init -q && git add -A && git commit -qm "init notes fixture"
# + trivial 40-line notes.py (argparse CLI: add/list), committed
```

Common flag suffix used by every run below: `--mode=json --auto-approve
--max-time 150 --model glm-5.3-flash --thinking low --no-title --session-dir <dir>`,
launched with shell cwd inside the sandbox (last run also verified `--cwd`).

- **S1a** feature request (54.3s wall): model classified the change trivial,
  skipped the consent gate, implemented, and ended with an interrogative:
  "...I didn't create a Trellis task for it — let me know if you'd like one
  anyway." — final assistant text, no tool call, process exited.
- **S1 consent gate** (13.9s): prompt "...tagging feature... ask me for
  consent before creating any Trellis task, and do not write any code until I
  answer." → single assistant turn, `stopReason: stop`, no tool call, stdout
  ends with `{"type":"message_end","message":{"role":"assistant",
  "content":[{"type":"text","text":"...May I create a Trellis task for this
  and enter the planning phase? ..."}]}}`.
- **S1 continue** (56.8s): `--continue "Yes — go ahead and create a Trellis
  task for the --verbose flag work anyway, and tell me what you changed in
  notes.py"` → model recalled the S1a exchange verbatim (recapped the 3 edits)
  and created `.trellis/tasks/08-31-notes-verbose-flag/` inside the sandbox
  copy. Consent-gate session continue: `--continue "Yes — create the Trellis
  task and plan it..."` → ran `python3 .trellis/scripts/task.py create
  "Notes tagging feature" --slug notes-tagging` (sandbox copy only) and wrote
  prd.md. Full consent→plan loop works under `-p` + `--continue`.
- **S1 `--cwd` + `--continue`** (8.8s): run from shell cwd `/tmp` with
  `--cwd /tmp/trellis-spike-sandbox --continue` → resumed the correct s6
  session (answered "40" with full context). Design's planned invocation
  shape confirmed as written — **no design.md edit required**.
- **S2/S3** session file (both S1a -p invocations appended to one file,
  41 lines): entry sequence per process start: `title, session, model_change,
  thinking_level_change, custom_message(trellis-session-context),
  custom_message(eager-task-prelude)`; per user turn: `message(user)` →
  `custom_message(trellis-workflow-state)` → `message(assistant, toolCall)` →
  `custom(tool_execution_start)` → `message(toolResult)` → ... →
  `custom(session_exit)`. Tool calls carry full args in
  `message.content[].toolCall.arguments`; results in the following
  `role=toolResult` message; no `tool_execution_end` entries persisted.
- **S4** `--no-extensions` (25.1s): session file contains only
  `customType:"eager-task-prelude"`; zero trellis-* entries;
  `grep -c trellis` = 3 hits are all the `/tmp/trellis-spike-sandbox` path
  substring in `cwd` fields. Model answered normally. Arm `trellis-off` is
  genuinely silent.
- **S5** trivial session (17.5s): one user turn, one grep tool call, final
  answer. Reference points: consent-only turn 13.9s; no-extensions trivial
  25.1s (2 tool calls); multi-tool work sessions 54-57s; sub-agent dispatch
  session 55.7s (sub-agent itself 24.8s). Per-turn TTFT ~6-8s dominates;
  `--max-time 150` never approached (expiry path unexercised).
- **S6** dispatch (55.7s): parent called `task` with
  `args:{context, tasks:[{name:"LineCounter", agent:"task", task:"..."}]}`;
  parent stream shows "Running agent LineCounter..." updates + `hub`
  op:wait polls, then `custom_message customType=async-result` carrying
  `<task-result id="LineCounter" agent="task" status="completed"
  duration="24.8s"><output>{"wc_l_count":40,...}</output></task-result>`.
  Sub-agent's own tool calls (glob, bash, yield) are NOT in the parent file —
  they are in the nested
  `.../s6/2026-08-31T13-46-44-335Z_01a05812-.../LineCounter.jsonl` (full
  session structure incl. `session_init`) plus `LineCounter.md` holding the
  final structured result. **B11 stays det-scope** (nested traceable); no
  downgrade to judge-scope.

Additional driver-relevant observations:

- Fresh-session isolation: after a session activated a task
  (`task.py create` → "Activated task for this session"), every NEW omp
  session still got `[workflow-state:no_task]` — session-scoped activation
  does not leak across sessions in the same sandbox.
- `--continue` resolves the latest session within `--session-dir` (observed
  cwd-scoped); driver should use a fresh per-probe session dir to avoid
  ambiguity.
- Consent-question detection rule above is confirmed usable: observed consent
  questions are final assistant text turns ending with an interrogative and no
  tool call; no platform ask mechanism fires in `-p` mode.
