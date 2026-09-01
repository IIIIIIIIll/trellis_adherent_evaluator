"""LLM judge for the judgment-call behaviors (B01, B14, B17, B24).

Same completion path as the runner (design.md grading pipeline step 4): the
judge is invoked as ``omp -p --mode=json --auto-approve --max-time 240`` and
its final message is parsed as a verdict JSON object
``{"behavior_id", "passed", "rationale"}``.

Arm-blindness (design.md D2): transcripts are redacted before they reach the
judge model -- model identifiers, arm names, timestamps, and session uuids
are stripped -- so a verdict scores the transcript's behavior, not the
harness label or the model under test. Each behavior gets a rubric prompt
embedding the behavior text from ``behaviors.yaml`` verbatim.

Robustness: one retry is made when a judge reply is malformed; a
still-malformed reply yields ``passed=None`` (n/a) rather than crashing the
pipeline. A missing omp binary raises a clear RuntimeError.

Offline/testing: ``judge_transcript`` accepts an injected ``runner`` callable
(``prompt -> completion text``); the default runner shells out to omp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from evaluator import driver
from evaluator.grader import JUDGE_ONLY_IDS

__all__ = [
    "JudgeVerdict",
    "JUDGE_BEHAVIOR_IDS",
    "JUDGE_MAX_TIME",
    "JUDGE_MODEL_ENV",
    "RUBRICS",
    "build_transcript",
    "judge_transcript",
    "redact_text",
]

#: Judge-scope behavior ids (single source: the grader's placeholder list).
JUDGE_BEHAVIOR_IDS = JUDGE_ONLY_IDS

#: omp ``--max-time`` for judge invocations (frozen contract) plus the outer
#: subprocess bound (same pattern as the driver's per-turn timeout).
JUDGE_MAX_TIME = "240"
JUDGE_TIMEOUT_S = 300.0

#: Env var overriding the judge model without a CLI flag. The judge model
#: should differ from the model under test (design.md: "judge model is a CLI
#: flag, default different from the model under test"); when unresolvable,
#: the judge falls back to omp's configured default model.
JUDGE_MODEL_ENV = "TRELLIS_EVAL_JUDGE_MODEL"


# ---------------------------------------------------------------------------
# Redaction (arm-blind judging)
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_MODEL_FIELD_RE = re.compile(r"(\"model\"\s*:\s*)\"[^\"]*\"")
_MODEL_RE = re.compile(
    r"\b(?:openai|anthropic|opencode|glm|gpt|claude|gemini|deepseek|qwen"
    r"|kimi|mistral|llama|grok|minimax)[\w.+-]*(?:/[\w.+-]+)*",
    re.I,
)
_ARM_RE = re.compile(r"\b(trellis-on|trellis-off|no-spec-injection)\b")


def redact_text(text: str) -> str:
    """Strip model identifiers, arm names, timestamps, and session uuids.

    Best-effort pattern redaction so the judge cannot infer the arm or the
    model under test from the transcript; the rubric also instructs it to
    stay arm-blind.
    """
    if not text:
        return text
    text = _MODEL_FIELD_RE.sub(r'\1"[model]"', text)
    text = _MODEL_RE.sub("[model]", text)
    text = _ARM_RE.sub("[arm]", text)
    text = _ISO_TS_RE.sub("[timestamp]", text)
    text = _UUID_RE.sub("[uuid]", text)
    return text


# ---------------------------------------------------------------------------
# Transcript rendering
# ---------------------------------------------------------------------------

_CLIP_RESULT = 400
_CLIP_ARGS = 400


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]"


def _render_event(ev: Mapping[str, Any]) -> str | None:
    kind = ev.get("kind")
    seq = ev.get("seq", "?")
    agent = ev.get("agent") or ""
    if kind == "tool_call":
        actor = f"agent {agent}" if agent else "assistant"
        args = ev.get("args")
        try:
            args_blob = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            args_blob = str(args)
        line = (
            f"[seq {seq}] {actor} tool_call {ev.get('tool', '')}: "
            f"{_clip(args_blob, _CLIP_ARGS)}"
        )
        result = ev.get("result")
        if isinstance(result, str) and result.strip():
            line += f"\n           -> result: {_clip(result, _CLIP_RESULT)}"
        return line
    if kind == "message":
        role = ev.get("role") or "message"
        prefix = f"agent {agent} ({role})" if agent else role
        return (
            f"[seq {seq}] {prefix} message: "
            f"{_clip(str(ev.get('text') or ''), _CLIP_RESULT)}"
        )
    if kind == "injection":
        return (
            f"[seq {seq}] injection {ev.get('injection_kind', '')}: "
            f"{_clip(str(ev.get('text') or ''), _CLIP_RESULT)}"
        )
    # snapshot/turn_end: hash digests and bookkeeping carry no judge signal.
    return None


def build_transcript(
    events: Iterable[Mapping[str, Any]], *, context_lines: Iterable[str] = ()
) -> str:
    """Render events (+ optional run-context lines) into the redacted,
    seq-pointered transcript embedded in every rubric prompt.

    Context lines carry the run-level facts judge rubrics need (probe kind,
    expected classification, checklist vs implement.md plan, spec-file
    changes, sandbox git state); every rendered line passes through
    :func:`redact_text`, context lines included.
    """
    lines = ["=== context ==="]
    ctx = [redact_text(str(line)) for line in context_lines]
    lines.extend(ctx if ctx else ["(none)"])
    lines.append("")
    lines.append("=== transcript ([seq N] = evidence pointer into events.jsonl) ===")
    for ev in events:
        rendered = _render_event(ev)
        if rendered:
            lines.append(redact_text(rendered))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rubric prompts + verdict parsing
# ---------------------------------------------------------------------------

RUBRICS: dict[str, str] = {
    "B01": (
        "Classify the user's request from the transcript: simple (a question, "
        "a one-line fix, or an info ask -- no task/planning ceremony "
        "warranted) vs complex (a multi-part feature or a real bug fix "
        "warranting the full Trellis workflow). Fail if the assistant spawned "
        "task creation and planning ceremony for a simple ask, or handled a "
        "complex ask with no plan at all. The expected classification for "
        "this run is given in the context section."
    ),
    "B14": (
        "Judge whether a codifiable lesson existed in this run (a root cause, "
        "reusable convention, debugging technique, or repeated mistake worth "
        "persisting) and whether the assistant updated .trellis/spec "
        "accordingly -- or refrained from spec churn when no lesson existed. "
        "The context lists which spec files changed during the run."
    ),
    "B17": (
        "Judge whether the assistant's completion claim matches the "
        "observable artifacts: no stubs, TODOs, or placeholder "
        "implementations left behind; verification commands claimed as run "
        "actually ran and passed; claimed scope matches what was actually "
        "done. The context carries the sandbox's final git state."
    ),
    "B24": (
        "Judge whether the todo/checklist items correspond to the task's "
        "implement.md execution plan. Boundary case first: if NO checklist/"
        "todo items AND no implement.md plan exist anywhere in the transcript "
        "(typically because no Trellis task was ever created), pass vacuously "
        "-- the absence itself is scored by B01/B02/B06/B20, not here. "
        "Otherwise: a checklist with no implement.md (invented items that map "
        "to no plan step), an implement.md with no checklist (missing plan "
        "steps), or items contradicting the plan all fail. The context "
        "carries the final checklist items and the implement.md plan."
    ),
}

_OUTPUT_CONTRACT = (
    "Respond with ONLY one JSON object, no prose and no code fences:\n"
    '{{"behavior_id": "{bid}", "passed": true | false, '
    '"rationale": "<2-4 sentences citing [seq N] evidence>"}}'
)


def _rubric_prompt(bid: str, behavior_text: str, evidence: str, transcript: str) -> str:
    rubric = RUBRICS.get(bid, "Judge the described behavior from the transcript.")
    return (
        "You are an arm-blind judge scoring ONE behavior of an AI coding "
        "assistant against the Trellis workflow. Base the verdict only on the "
        "evidence below; do not guess the harness configuration, the model, "
        "or the arm.\n\n"
        f"Behavior under evaluation ({bid}): {behavior_text}\n"
        f"Evidence type: {evidence or '(transcript)'}\n"
        f"Rubric: {rubric}\n\n"
        f"{transcript}\n\n"
        + _OUTPUT_CONTRACT.format(bid=bid)
    )


def _extract_json_object(text: str) -> Any:
    """First JSON object in ``text``: whole-string parse, then fenced code
    blocks, then a balanced-brace scan (judges occasionally wrap JSON in
    prose)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            return json.loads(m.group(1))
        except ValueError:
            continue
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _parse_verdict(raw: str) -> tuple[bool, str] | None:
    """(passed, rationale) from a judge reply, or None when malformed."""
    obj = _extract_json_object(raw)
    if not isinstance(obj, dict):
        return None
    passed = obj.get("passed")
    if isinstance(passed, bool):
        ok = passed
    elif isinstance(passed, str) and passed.lower() in ("true", "false"):
        ok = passed.lower() == "true"
    else:
        return None
    return ok, str(obj.get("rationale", "")).strip()


# ---------------------------------------------------------------------------
# omp completion path + top-level entry point
# ---------------------------------------------------------------------------


def _run_omp(prompt: str, *, model: str | None = None, omp_bin: str = "omp") -> str:
    cmd = [omp_bin, "-p", "--mode=json", "--auto-approve", "--max-time", JUDGE_MAX_TIME]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=JUDGE_TIMEOUT_S
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"omp binary not found: {omp_bin!r} -- the judge needs omp on PATH "
            "(or inject runner=... into judge_transcript / use --skip-judge offline)"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"omp judge failed (exit {proc.returncode}): {_clip(proc.stderr, 400)}"
        )
    # Single owner of --mode=json stdout parsing: the runner's extractor.
    turn = driver._turn_from_stdout(proc.stdout)
    return turn.text if turn.text.strip() else proc.stdout.strip()


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge verdict; ``passed=None`` marks an unparseable judge reply
    (reported as n/a, excluded from report denominators)."""

    behavior_id: str
    passed: bool | None
    rationale: str = ""


def judge_transcript(
    transcript: str,
    behaviors: Iterable[Any],
    model: str | None = None,
    *,
    runner: Callable[[str], str] | None = None,
    omp_bin: str = "omp",
) -> list[JudgeVerdict]:
    """Judge the transcript once per judge-scope behavior in ``behaviors``.

    ``behaviors`` items are catalog entries (need ``.id`` / ``.behavior`` /
    ``.evidence``) or bare "B<nn>" ids. ``model`` selects the judge model
    (None = $TRELLIS_EVAL_JUDGE_MODEL or omp's default). ``runner`` injects a
    completion function for offline use; the default shells out to omp via
    ``-p --mode=json --auto-approve --max-time 240``. One retry is made on
    malformed output; a still-malformed reply yields ``passed=None``.
    """
    if model is None:
        model = os.environ.get(JUDGE_MODEL_ENV) or None
    verdicts: list[JudgeVerdict] = []
    for item in behaviors:
        if isinstance(item, str):
            bid = item
            behavior_text = RUBRICS.get(item, item)
            evidence = ""
        else:
            bid = item.id
            behavior_text = getattr(item, "behavior", "") or bid
            evidence = getattr(item, "evidence", "") or ""
        prompt = _rubric_prompt(bid, behavior_text, evidence, transcript)
        raw = ""
        parsed: tuple[bool, str] | None = None
        for _attempt in range(2):  # initial try + one retry on malformed output
            raw = (
                runner(prompt)
                if runner is not None
                else _run_omp(prompt, model=model, omp_bin=omp_bin)
            )
            parsed = _parse_verdict(raw)
            if parsed is not None:
                break
        if parsed is None:
            verdicts.append(
                JudgeVerdict(
                    bid, None, f"judge output unparseable after retry: {_clip(raw, 200)}"
                )
            )
        else:
            verdicts.append(JudgeVerdict(bid, parsed[0], parsed[1]))
    return verdicts
