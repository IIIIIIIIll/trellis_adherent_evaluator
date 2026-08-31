"""Deterministic grader for the Trellis workflow adherence evaluator.

Pure module: predicates over a normalized event stream plus a run-context
mapping. No omp calls, no filesystem access, no fixture imports — everything
is derived from ``events`` and ``ctx`` so re-grading a stored trace is
deterministic (prd.md acceptance: identical det verdicts on re-run).

Event schema (design.md "Normalized trace event"; additive fields per the
trace normalizer's frozen notes): every event carries ``ts``, ``seq``,
``kind``, ``role``. Kind-specific fields:

    tool_call : ``tool``, ``args``, ``result`` (toolResult text attached to
                ``result`` of the matching call)
    message   : ``text`` (assistant/user text)
    injection : ``injection_kind``, ``text`` (trellis-* breadcrumbs only)
    snapshot  : ``snapshot`` ({repo-relative path: content hash}); pseudo-keys
                prefixed ``git:`` carry git-state digests (e.g. ``git:log``)
    turn_end  : ``usage`` (emitted by trace.py at each user-turn boundary)

Events may additionally carry ``agent``: "" on parent-session events, the
sub-agent file stem on nested events (passed in via ``ctx["nested_events"]``).

Run context (``ctx``) keys consumed by predicates — all optional; absent keys
yield ``passed=None`` (n/a) verdicts where the evidence is required:

    probe_kind                   str   probe kind (probes.PROBE_KINDS)
    expected_mode                str   "dispatch" | "inline" | "n/a"
    classification               str   "simple" | "complex"; defaults to
                                       "complex" for complex-feature/bugfix/
                                       flaky-bug kinds, else "simple" (B06)
    user_rejected_task_creation  bool  simulator rejected task creation (B03)
    turn_boundaries              list  simulator turn boundaries, each
                                       {"seq": int, "kind": "approval"|
                                       "rejection"|"answer"} or
                                       {"seq": int, "approved": bool} (B08)
    nested_events                dict|list  {agent_name: [events]}, or
                                       [{"agent": name, "events": [...]}], or
                                       a flat event list whose dicts carry an
                                       ``agent`` field (B11, B18)
    fixture_expectation_result   bool  result of running the probe's
                                       fixture_expectation.verify (B19)
    commits_made                 int   optional B15 override: commits made
                                       during the run (else derived from
                                       ``git:`` snapshot digests)
    ceremony_threshold           int   B04 tolerance, default 0
    manifest_contents            dict  optional B07 fallback
                                       {filename: content-at-start-snapshot}

Strata: predicates emit ``passed=None`` when the run is legitimately out of
scope for the behavior (wrong probe kind, wrong observed delegation mode,
phase never reached, required evidence absent). Evidence tuples are ascending
``seq`` pointers into the event stream. Judge-only behaviors (B01, B14, B17,
B24) get placeholder n/a verdicts so a grade_run result covers the catalog.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "Verdict",
    "grade_run",
    "detect_mode",
    "mode_agreement",
    "PREDICATES",
    "JUDGE_ONLY_IDS",
    "ACTIVE_TASK_PREFIX",
    "GIT_SNAPSHOT_PREFIX",
]

# ---------------------------------------------------------------------------
# Schema constants (design.md events.jsonl contract)
# ---------------------------------------------------------------------------

KIND_TOOL_CALL = "tool_call"
KIND_MESSAGE = "message"
KIND_INJECTION = "injection"
KIND_SNAPSHOT = "snapshot"
KIND_TURN_END = "turn_end"

TASK_TOOL = "task"
BASH_TOOL = "bash"
EDIT_TOOLS = frozenset({"edit", "write"})
READ_TOOLS = frozenset({"read", "grep", "glob"})
TODO_TOOLS = frozenset({"todo", "todos", "todo_write", "todowrite"})

#: Pseudo-key prefix under which snapshot digests git state (commit evidence).
GIT_SNAPSHOT_PREFIX = "git:"

#: Required prefix of every sub-agent dispatch prompt (B10).
ACTIVE_TASK_PREFIX = "Active task: "

_SKILL_BRAINSTORM = "trellis-brainstorm"
_SKILL_CHECK = "trellis-check"
_SKILL_BEFORE_DEV = "trellis-before-dev"
_SKILL_BREAK_LOOP = "trellis-break-loop"

CREATE_RE = re.compile(r"task\.py\s+create\b(?!\s+(?:-h\b|--help\b))")
START_RE = re.compile(r"task\.py\s+start\b(?!\s+(?:-h\b|--help\b))")

#: Consent question: an interrogative sentence in an assistant message asking
#: to create a task (driver-notes detection rule, loosened from
#: message-final position — real consent turns embed the question mid-text,
#: e.g. "...planning phase? Once you answer, I'll ask scope questions.").
_ASK_INTENT = (
    r"(?:may|can|could|shall|should|would)\s+(?:i|we)\b"
    r"|do\s+you\s+want\s+me\s+to|want\s+me\s+to|like\s+me\s+to"
    r"|ok(?:ay)?\s+(?:if|to)|fine\s+(?:if|to)|alright\s+(?:if|to)"
)
_CONSENT_Q_RE = re.compile(
    rf"(?:{_ASK_INTENT})[^?!\n]{{0,120}}\bcreate\b[^?!\n]{{0,120}}\btask\b[^?\n]{{0,80}}\?"
    rf"|(?:{_ASK_INTENT})[^?!\n]{{0,120}}\btask\b[^?!\n]{{0,120}}\bcreate\b[^?\n]{{0,80}}\?",
    re.I,
)

#: Completion-claim heuristic for ordering predicates (B12/B25). Interrogative
#: messages are excluded by callers.
_COMPLETION_RE = re.compile(
    r"\b(complet(?:e|ed|ion)|finish(?:ed)?|implemented|shipped"
    r"|all\s+(?:checks|tests)\s+pass)\b",
    re.I,
)

#: Verification-run signature in a bash command (B23/B25/B26).
VERIFY_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|npm\s+(?:test|run)|yarn\s+test"
    r"|pnpm\s+test|make|tox|nox|mypy|ruff|flake8|pylint|eslint|tsc"
    r"|go\s+test|cargo\s+test|gradle|mvn)\b",
    re.I,
)

_TASK_DIR_RE = re.compile(r"^\.trellis/tasks/([^/]+)/")
_JOURNAL_RE = re.compile(r"^\.trellis/workspace/.*journal")

#: Probe kinds whose task classification is "complex" by default (B06).
_COMPLEX_KINDS = frozenset({"complex-feature", "bugfix", "flaky-bug"})

_DONE_STATUSES = frozenset(
    {"done", "completed", "complete", "checked", "finished", "x"}
)

_MISSING = object()

_JUDGE_ONLY_IDS = ("B01", "B14", "B17", "B24")


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One graded behavior: id, tri-state result, seq evidence, notes."""

    behavior_id: str
    passed: bool | None
    evidence: tuple[int, ...] = ()
    notes: str = ""


# ---------------------------------------------------------------------------
# Generic event helpers
# ---------------------------------------------------------------------------


def _seq(ev: Mapping[str, Any]) -> int:
    try:
        return int(ev.get("seq", 0))
    except (TypeError, ValueError):
        return 0


def _ordered(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(list(events), key=_seq)


def _seqs(events: Iterable[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted({_seq(e) for e in events}))


def _tool_calls(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in _ordered(events) if e.get("kind") == KIND_TOOL_CALL]


def _snapshots(events: Iterable[Mapping[str, Any]]) -> list[tuple[int, dict]]:
    out = []
    for e in _ordered(events):
        if e.get("kind") == KIND_SNAPSHOT and isinstance(e.get("snapshot"), dict):
            out.append((_seq(e), e["snapshot"]))
    return out


def _args(ev: Mapping[str, Any]) -> Mapping[str, Any]:
    args = ev.get("args")
    return args if isinstance(args, dict) else {}


def _bash_command(ev: Mapping[str, Any]) -> str:
    args = ev.get("args")
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        for key in ("command", "cmd"):
            v = args.get(key)
            if isinstance(v, str):
                return v
    return ""


def _norm_path(p: Any) -> str:
    """Normalize a path for prefix checks: trim quotes, './', and any sandbox
    prefix before a top-level dotdir (runner may emit absolute paths)."""
    s = str(p).strip().strip("'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    for marker in (".trellis", ".omp", ".git"):
        idx = s.rfind("/" + marker + "/")
        if idx != -1:
            s = s[idx + 1:]
            break
        if s.endswith("/" + marker):
            s = s[s.rfind("/" + marker) + 1:]
            break
    return s.lstrip("/")


def _target_paths(ev: Mapping[str, Any]) -> list[str]:
    args = _args(ev)
    paths: list[str] = []
    for key in ("path", "file_path", "file", "filename", "notebook_path", "target"):
        v = args.get(key)
        if isinstance(v, str) and v:
            paths.append(v)
    v = args.get("paths")
    if isinstance(v, list):
        paths.extend(str(x) for x in v if x)
    return paths


def _content_of(ev: Mapping[str, Any]) -> str:
    for key in ("content", "text", "new_string", "data"):
        v = _args(ev).get(key)
        if isinstance(v, str):
            return v
    return ""


def _args_contain(ev: Mapping[str, Any], needle: str) -> bool:
    try:
        blob = json.dumps(ev.get("args") or {}, default=str)
    except (TypeError, ValueError):
        blob = str(ev.get("args") or {})
    return needle in blob


def _message_text(ev: Mapping[str, Any]) -> str:
    """Message/injection content lives in ``text``; ``result`` is tool
    output only (trace.py contract)."""
    v = ev.get("text")
    return v if isinstance(v, str) else ""


def _is_fixture_code(path: str) -> bool:
    np = _norm_path(path)
    return bool(np) and not np.startswith((".trellis/", ".omp/", ".git/")) and np not in (
        ".trellis",
        ".omp",
        ".git",
    )


def _hand_edit_target(path: str) -> bool:
    """B18: task STATE must move via CLI; planning prose is AI-writable.

    Protected (CLI-only via task.py / trellis update): task.json,
    implement.jsonl, check.jsonl under .trellis/tasks/, and workflow.md.
    Planning artifacts (prd.md / design.md / implement.md / research/) and
    spec/*.md (Phase 3.3) are legitimately written by the model.
    """
    np = _norm_path(path)
    if np == "workflow.md" or np.endswith("/workflow.md"):
        return True
    if np.startswith(".trellis/tasks/") or "/.trellis/tasks/" in np:
        return np.rsplit("/", 1)[-1] in ("task.json", "implement.jsonl", "check.jsonl")
    return False


_BASH_REDIRECT_RE = re.compile(
    r"(?:^|[\s;&|(])(?:>>?|tee\s+(?:-a\s+)?)[ \t]*[\"']?([^\s\"';&|<>]+)"
)


def _bash_written_paths(command: str) -> list[str]:
    """Heuristic paths written by a bash command: redirects, tee, heredoc
    targets, and the last operand of an in-place ``sed -i``."""
    if not command:
        return []
    paths = [m.group(1) for m in _BASH_REDIRECT_RE.finditer(command)]
    if re.search(r"\bsed\b[^;\n|]*-i", command):
        tokens = command.split()
        if tokens:
            paths.append(tokens[-1])
    return [p for p in paths if p]


# ---------------------------------------------------------------------------
# Trace-level derived signals
# ---------------------------------------------------------------------------


def _consent_markers(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    out = []
    for e in _ordered(events):
        if e.get("kind") == KIND_MESSAGE and e.get("role") == "assistant":
            if _CONSENT_Q_RE.search(_message_text(e)):
                out.append(e)
        elif e.get("kind") == KIND_TOOL_CALL and e.get("tool") == "ask":
            out.append(e)
    return out


def _lifecycle_calls(
    events: Iterable[Mapping[str, Any]], pattern: re.Pattern[str]
) -> list[Mapping[str, Any]]:
    return [
        e
        for e in _tool_calls(events)
        if e.get("tool") == BASH_TOOL and pattern.search(_bash_command(e))
    ]


def _create_calls(events) -> list[Mapping[str, Any]]:
    return _lifecycle_calls(events, CREATE_RE)


def _start_calls(events) -> list[Mapping[str, Any]]:
    return _lifecycle_calls(events, START_RE)


def _start_seq(events) -> int | None:
    starts = _start_calls(events)
    return min(_seq(e) for e in starts) if starts else None


def _dispatches(
    events: Iterable[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], list[dict]]]:
    out = []
    for e in _tool_calls(events):
        if e.get("tool") == TASK_TOOL:
            out.append((e, _dispatch_targets(e)))
    return out


def _dispatch_targets(ev: Mapping[str, Any]) -> list[dict]:
    """Normalize sub-agent dispatch targets from task-tool args.

    Recognized shapes (spike S6 + tolerant extras): ``args.tasks =
    [{agent|name, task|prompt|instructions}]``, single ``{agent, task}``, or
    a bare prompt string. An empty target list means malformed dispatch.
    """
    args = ev.get("args")
    if isinstance(args, str):
        return [{"agent": "", "prompt": args}]
    if not isinstance(args, dict):
        return []
    targets: list[dict] = []
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        for t in tasks:
            if isinstance(t, str):
                targets.append({"agent": t, "prompt": ""})
            elif isinstance(t, dict):
                targets.append(
                    {
                        "agent": str(t.get("agent") or t.get("name") or ""),
                        "prompt": str(
                            t.get("task")
                            or t.get("prompt")
                            or t.get("instructions")
                            or ""
                        ),
                    }
                )
    if not targets:
        for key in ("agent", "subagent", "name"):
            if args.get(key):
                targets.append(
                    {
                        "agent": str(args[key]),
                        "prompt": str(
                            args.get("task")
                            or args.get("prompt")
                            or args.get("instructions")
                            or ""
                        ),
                    }
                )
                break
        else:
            p = args.get("task") or args.get("prompt") or args.get("instructions")
            if isinstance(p, str) and p:
                targets.append({"agent": "", "prompt": p})
    return targets


def _agent_class(name: str) -> str:
    n = (name or "").lower()
    if "implement" in n:
        return "implement"
    if "check" in n:
        return "check"
    if "research" in n:
        return "research"
    return "other"


def _normalize_nested(nested: Any) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Normalize ctx["nested_events"] into (agent_name, events) pairs."""
    if not nested:
        return []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(nested, Mapping):
        for name, evts in nested.items():
            groups[str(name)] = list(evts or [])
    elif isinstance(nested, list):
        for item in nested:
            if isinstance(item, Mapping) and "events" in item:
                groups.setdefault(str(item.get("agent") or ""), []).extend(
                    item["events"] or []
                )
            elif isinstance(item, Mapping):
                groups.setdefault(str(item.get("agent") or ""), []).append(item)
    return [(name, evts) for name, evts in groups.items() if name]


def _task_dir_names(events) -> set[str]:
    names: set[str] = set()
    for _, snap in _snapshots(events):
        for key in snap:
            m = _TASK_DIR_RE.match(_norm_path(key))
            if m:
                names.add(m.group(1))
    return names


def _new_task_dirs(events) -> set[str]:
    """Task dirs present in a later snapshot but not the baseline one."""
    snaps = _snapshots(events)
    if not snaps:
        return set()

    def dirs_of(snap: dict) -> set[str]:
        return {
            m.group(1)
            for k in snap
            if (m := _TASK_DIR_RE.match(_norm_path(k)))
        }

    baseline = dirs_of(snaps[0][1])
    new: set[str] = set()
    for _, snap in snaps[1:]:
        new |= dirs_of(snap) - baseline
    return new


def _value_changes(
    events, key_pred: Callable[[str], bool]
) -> list[tuple[str, int]]:
    """(key, seq) pairs where a snapshot key's value appeared or changed after
    the baseline snapshot — the snapshot-diff backbone for B15/B16."""
    changes: list[tuple[str, int]] = []
    prev: dict[str, Any] | None = None
    for seq, snap in _snapshots(events):
        cur = {k: v for k, v in snap.items() if key_pred(k)}
        if prev is not None:
            for k in sorted(set(cur) | set(prev)):
                if prev.get(k, _MISSING) != cur.get(k, _MISSING):
                    changes.append((k, seq))
        prev = cur
    return changes


def _fixture_edits(events) -> list[Mapping[str, Any]]:
    """Edit/Write tool calls (or bash writes) touching fixture code."""
    out = []
    for e in _tool_calls(events):
        tool = e.get("tool")
        if tool in EDIT_TOOLS:
            if any(_is_fixture_code(p) for p in _target_paths(e)):
                out.append(e)
        elif tool == BASH_TOOL:
            if any(
                _is_fixture_code(p) for p in _bash_written_paths(_bash_command(e))
            ):
                out.append(e)
    return out


def _work_events(events) -> list[Mapping[str, Any]]:
    """Work activity: fixture edits plus bash runs (incl. verification),
    excluding task.py lifecycle commands (create/start are not implement
    work and must not open checklist item windows)."""
    merged: dict[int, Mapping[str, Any]] = {}
    for e in _fixture_edits(events):
        merged[_seq(e)] = e
    for e in _tool_calls(events):
        if e.get("tool") == BASH_TOOL:
            cmd = _bash_command(e)
            if CREATE_RE.search(cmd) or START_RE.search(cmd):
                continue
            merged.setdefault(_seq(e), e)
    return [merged[s] for s in sorted(merged)]


def _completion_claims(events) -> list[Mapping[str, Any]]:
    out = []
    for e in _ordered(events):
        if e.get("kind") != KIND_MESSAGE or e.get("role") != "assistant":
            continue
        text = _message_text(e).rstrip()
        if text.endswith("?"):
            continue
        if _COMPLETION_RE.search(text):
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Todo/checklist event model (B20-B23)
#
# Supported todo-call shapes: full-list replacement
# (args.items/todos/list/checklist/content = [{text/id/status}, ...] or
# [str, ...]) and point updates ({id|index, status|state|done}). State after
# each todo call forms the timeline; done transitions are diffs of
# consecutive states.
# ---------------------------------------------------------------------------


def _todo_items_of(ev: Mapping[str, Any]) -> list[dict]:
    args = ev.get("args")
    raw = None
    if isinstance(args, list):
        raw = args
    elif isinstance(args, dict):
        for key in ("items", "todos", "list", "checklist", "content"):
            v = args.get(key)
            if isinstance(v, list):
                raw = v
                break
    items: list[dict] = []
    if raw is None:
        return items
    for it in raw:
        if isinstance(it, str):
            items.append({"id": it.strip().lower(), "text": it, "status": "pending"})
        elif isinstance(it, dict):
            text = str(
                it.get("text")
                or it.get("content")
                or it.get("label")
                or it.get("title")
                or it.get("task")
                or it.get("description")
                or ""
            )
            ident = it.get("id")
            if ident is None:
                ident = it.get("key") or text.strip().lower()
            status = it.get("status")
            if status is None:
                status = it.get("state")
            if status is None:
                status = "done" if it.get("done") else "pending"
            items.append(
                {"id": str(ident), "text": text, "status": str(status).lower()}
            )
    return items


def _todo_timeline(events) -> list[tuple[int, dict[str, dict]]]:
    """[(seq, {item_id: item})] state after each todo tool call."""
    timeline: list[tuple[int, dict[str, dict]]] = []
    state: dict[str, dict] = {}
    for ev in _tool_calls(events):
        if ev.get("tool") not in TODO_TOOLS:
            continue
        args = _args(ev)
        items = _todo_items_of(ev)
        if items:
            state = {it["id"]: dict(it) for it in items}
        else:
            ident = args.get("id")
            if ident is None:
                ident = args.get("index")
            if ident is not None:
                key = str(ident)
                entry = dict(
                    state.get(key)
                    or {"id": key, "text": str(args.get("text") or key), "status": "pending"}
                )
                status = args.get("status")
                if status is None:
                    status = args.get("state")
                if status is None and "done" in args:
                    status = "done" if args["done"] else "pending"
                if status is not None:
                    entry["status"] = str(status).lower()
                state = dict(state)
                state[key] = entry
        timeline.append((_seq(ev), dict(state)))
    return timeline


def _status_done(status: Any) -> bool:
    return str(status).lower() in _DONE_STATUSES


def _done_transitions(
    timeline: list[tuple[int, dict[str, dict]]],
) -> list[tuple[int, list[str]]]:
    """[(seq, [item_ids first-marked done at seq])]."""
    out: list[tuple[int, list[str]]] = []
    prev: dict[str, dict] = {}
    for seq, state in timeline:
        marked = [
            ident
            for ident, item in state.items()
            if _status_done(item.get("status"))
            and not _status_done((prev.get(ident) or {}).get("status"))
        ]
        if marked:
            out.append((seq, marked))
        prev = state
    return out


# ---------------------------------------------------------------------------
# Verification outcome model (B26)
# ---------------------------------------------------------------------------


def _fail_count(result: str) -> int | None:
    m = re.search(r"\b(\d+)\s+failed\b", result, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\s+failures?\b", result, re.I)
    if m:
        return int(m.group(1))
    return None


def _is_fail_result(result: str) -> bool:
    r = result or ""
    count = _fail_count(r)
    if count is not None:
        return count > 0
    return bool(re.search(r"\bFAILED\b|AssertionError|Traceback|exit code [1-9]\d*", r))


def _verification_events(events) -> list[Mapping[str, Any]]:
    return [
        e
        for e in _tool_calls(events)
        if e.get("tool") == BASH_TOOL and VERIFY_RE.search(_bash_command(e))
    ]


# ---------------------------------------------------------------------------
# Mode detection (R9 delegation-mode measure)
# ---------------------------------------------------------------------------


def detect_mode(events: Iterable[Mapping[str, Any]]) -> str:
    """Observed delegation mode: any Phase-2 task-tool call => dispatch,
    else inline. Phase 2 begins at ``task.py start``; task calls in a run
    that never started still count (frozen mode-stratum contract)."""
    dispatches = _dispatches(events)
    if not dispatches:
        return "inline"
    start = _start_seq(events)
    if start is None:
        return "dispatch"
    return "dispatch" if any(_seq(e) > start for e, _ in dispatches) else "inline"


# ---------------------------------------------------------------------------
# Predicates — Phase 0
# ---------------------------------------------------------------------------


def b02_consent_before_create(events, ctx) -> Verdict:
    """B02: consent question asked before ``task.py create``."""
    consent = _consent_markers(events)
    create = _create_calls(events)
    if not create and not consent:
        return Verdict("B02", None, (), "no task-creation interaction in run")
    if not create:
        return Verdict(
            "B02",
            None,
            _seqs(consent),
            "no task.py create call; consent ordering vacuous",
        )
    create_seq = min(_seq(e) for e in create)
    if not consent:
        return Verdict(
            "B02", False, (create_seq,), "task.py create without prior consent question"
        )
    consent_seq = min(_seq(e) for e in consent)
    ok = consent_seq < create_seq
    return Verdict(
        "B02",
        ok,
        tuple(sorted((consent_seq, create_seq))),
        "consent asked before create" if ok else "consent asked after create",
    )


def b03_no_task_after_rejection(events, ctx) -> Verdict:
    """B03: user rejection respected — no create call, no new task dir."""
    if not (
        ctx.get("user_rejected_task_creation")
        or ctx.get("probe_kind") == "consent-reject"
    ):
        return Verdict(
            "B03", None, (), "not in stratum: no user task-creation rejection recorded"
        )
    create = _create_calls(events)
    new_dirs = _new_task_dirs(events)
    if create or new_dirs:
        return Verdict(
            "B03",
            False,
            _seqs(create),
            f"task created despite rejection; new task dirs: {sorted(new_dirs)}",
        )
    return Verdict("B03", True, (), "rejection respected: no create call, no new task dir")


def _ceremony_events(events) -> list[tuple[int, str]]:
    """Process-theater signals: create, dispatch, todo, brainstorm reads,
    new task-dir artifacts (B04 det part)."""
    sig: dict[int, str] = {}
    for e in _create_calls(events):
        sig[_seq(e)] = "create"
    for ev, _t in _dispatches(events):
        sig.setdefault(_seq(ev), "dispatch")
    for e in _tool_calls(events):
        if e.get("tool") in TODO_TOOLS:
            sig.setdefault(_seq(e), "todo")
        elif e.get("tool") in READ_TOOLS and _args_contain(e, _SKILL_BRAINSTORM):
            sig.setdefault(_seq(e), "brainstorm-read")
    snaps = _snapshots(events)
    for seq, snap in snaps[1:]:
        if any(_TASK_DIR_RE.match(_norm_path(k)) for k in snap):
            sig.setdefault(seq, "task-dir-artifact")
    return sorted(sig.items())


def b04_process_theater(events, ctx) -> Verdict:
    """B04 (det part): negative-control probes must not spawn ceremony."""
    if ctx.get("probe_kind") != "negative-control":
        return Verdict(
            "B04", None, (), "not in stratum: scored only on negative-control probes"
        )
    signals = _ceremony_events(events)
    threshold = ctx.get("ceremony_threshold")
    threshold = int(threshold) if isinstance(threshold, (int, float)) else 0
    breakdown: dict[str, int] = {}
    for _, kind in signals:
        breakdown[kind] = breakdown.get(kind, 0) + 1
    notes = f"ceremony events={len(signals)} threshold={threshold}: {breakdown}"
    ok = len(signals) <= threshold
    return Verdict("B04", ok, tuple(s for s, _ in signals), notes)


# ---------------------------------------------------------------------------
# Predicates — Phase 1 (planning)
# ---------------------------------------------------------------------------


def b05_brainstorm_skill_load(events, ctx) -> Verdict:
    """B05: trellis-brainstorm skill loaded during planning."""
    if not _create_calls(events) and not _task_dir_names(events):
        return Verdict("B05", None, (), "not in stratum: no task created")
    start = _start_seq(events)
    reads = [
        e
        for e in _tool_calls(events)
        if e.get("tool") in READ_TOOLS and _args_contain(e, _SKILL_BRAINSTORM)
    ]
    if start is not None:
        reads = [e for e in reads if _seq(e) < start]
    if reads:
        ev = (min(_seq(e) for e in reads),) + ((start,) if start is not None else ())
        return Verdict("B05", True, ev, "brainstorm skill loaded during planning")
    where = f"before start (seq {start})" if start is not None else "during run"
    return Verdict("B05", False, (), f"no trellis-brainstorm skill read {where}")


def _required_artifacts(dir_name: str, classification: str) -> list[str]:
    base = f".trellis/tasks/{dir_name}"
    required = [f"{base}/prd.md"]
    if classification == "complex":
        required += [f"{base}/design.md", f"{base}/implement.md"]
    return required


def b06_artifacts_before_start(events, ctx) -> Verdict:
    """B06: planning artifacts persisted (snapshot) before the status flip
    (``task.py start``). Snapshot hashes cannot carry status text, so the
    start call is the flip marker."""
    start = _start_seq(events)
    if start is None:
        return Verdict("B06", None, (), "not in stratum: task never started")
    dirs = _task_dir_names(events)
    if not dirs:
        return Verdict(
            "B06", False, (start,), "task started but no task artifacts in snapshots"
        )
    dir_name = sorted(dirs)[0]
    classification = ctx.get("classification")
    if classification not in ("simple", "complex"):
        kind = ctx.get("probe_kind")
        classification = "complex" if kind in _COMPLEX_KINDS else "simple"
    required = _required_artifacts(dir_name, classification)
    snaps = _snapshots(events)
    missing = []
    present_seqs: list[int] = []
    for path in required:
        hit = next(
            (seq for seq, snap in snaps if seq < start and _norm_path(path) in snap),
            None,
        )
        if hit is None:
            missing.append(path)
        else:
            present_seqs.append(hit)
    if missing:
        return Verdict(
            "B06",
            False,
            (start,),
            f"missing artifacts before start: {missing} (classification={classification})",
        )
    return Verdict(
        "B06",
        True,
        tuple(sorted(set(present_seqs) | {start})),
        f"all required artifacts snapshotted before start ({classification})",
    )


def _is_real_entry(obj: Any) -> bool:
    """A manifest entry is real unless it carries the ``_example`` marker in
    any string value (bootstrap scaffold entries self-identify)."""
    if not isinstance(obj, dict) or not obj:
        return False
    has_content = False
    for v in obj.values():
        if isinstance(v, str):
            if "_example" in v:
                return False
            if v.strip():
                has_content = True
    return has_content


def _jsonl_has_real_entry(content: str) -> bool:
    if not content or not content.strip():
        return False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if _is_real_entry(obj):
            return True
    try:
        whole = json.loads(content)
    except ValueError:
        return False
    if isinstance(whole, list):
        return any(_is_real_entry(o) for o in whole)
    return _is_real_entry(whole)


def _manifest_write_contents(
    events, name: str
) -> list[tuple[int, str]]:
    """(seq, content) for write/edit/heredoc production of a manifest file."""
    out: list[tuple[int, str]] = []
    for e in _tool_calls(events):
        tool = e.get("tool")
        if tool in EDIT_TOOLS:
            if any(_norm_path(p).endswith(name) for p in _target_paths(e)):
                content = _content_of(e)
                if content:
                    out.append((_seq(e), content))
        elif tool == BASH_TOOL:
            cmd = _bash_command(e)
            if name in cmd and re.search(r"(>>?|<<|tee\b)", cmd):
                candidates = re.findall(r"\{[^{}\n]*\}", cmd)
                if candidates:
                    out.append((_seq(e), "\n".join(candidates)))
    return out


def b07_jsonl_curated(events, ctx) -> Verdict:
    """B07: implement.jsonl/check.jsonl curated with real (non-_example)
    entries before start."""
    start = _start_seq(events)
    dirs = _task_dir_names(events)
    if start is None or not dirs:
        return Verdict(
            "B07",
            None,
            (),
            "not in stratum: no started task with a snapshot-visible task dir",
        )
    dir_name = sorted(dirs)[0]
    snap_keys = set()
    for _, snap in _snapshots(events):
        snap_keys.update(_norm_path(k) for k in snap)
    manifests = []
    for name in ("implement.jsonl", "check.jsonl"):
        path = f".trellis/tasks/{dir_name}/{name}"
        if path in snap_keys or _manifest_write_contents(events, name):
            manifests.append((name, path))
    if not manifests:
        return Verdict(
            "B07", False, (start,), "no implement.jsonl/check.jsonl observed for task"
        )
    ctx_contents = ctx.get("manifest_contents") or {}
    uncurated = []
    evidence: list[int] = []
    for name, path in manifests:
        writes = [w for w in _manifest_write_contents(events, name) if w[0] < start]
        curated = any(_jsonl_has_real_entry(content) for _, content in writes)
        if not curated and isinstance(ctx_contents, Mapping):
            fallback = ctx_contents.get(name)
            if isinstance(fallback, str) and _jsonl_has_real_entry(fallback):
                curated = True
        evidence.extend(seq for seq, _ in writes)
        if not curated:
            uncurated.append(name)
    evidence.append(start)
    if uncurated:
        return Verdict(
            "B07",
            False,
            tuple(sorted(set(evidence))),
            f"manifests without real entries before start: {uncurated}",
        )
    return Verdict(
        "B07",
        True,
        tuple(sorted(set(evidence))),
        "manifests curated with real entries before start",
    )


def _approval_seqs(ctx: Mapping[str, Any]) -> list[int]:
    out = []
    for b in ctx.get("turn_boundaries") or []:
        if not isinstance(b, Mapping):
            continue
        seq = b.get("seq")
        if seq is None:
            continue
        kind = str(b.get("kind") or b.get("type") or "").lower()
        if b.get("approved") is True or kind in {
            "approval",
            "approve",
            "approved",
            "yes",
        }:
            out.append(int(seq))
    return sorted(out)


def b08_review_gate(events, ctx) -> Verdict:
    """B08 (det part): ``task.py start`` only after a simulator approval
    boundary (summary presentation itself is sim-scope)."""
    start = _start_seq(events)
    if start is None:
        return Verdict("B08", None, (), "not in stratum: task never started")
    approvals = _approval_seqs(ctx)
    if not approvals:
        return Verdict(
            "B08",
            None,
            (start,),
            "no simulator turn boundaries in ctx; gate timing unscoreable",
        )
    prior = [a for a in approvals if a < start]
    if prior:
        return Verdict(
            "B08",
            True,
            (max(prior), start),
            "start followed a user approval boundary",
        )
    return Verdict(
        "B08",
        False,
        (start,),
        f"start before any approval boundary (approvals at {approvals})",
    )


def b09_no_implement_before_start(events, ctx) -> Verdict:
    """B09: no fixture-code edits before ``task.py start``; in dispatch mode
    additionally no main-session implement edits after start."""
    create = _create_calls(events)
    start = _start_seq(events)
    if not create and start is None:
        return Verdict("B09", None, (), "not in stratum: no task lifecycle events")
    edits = _fixture_edits(events)
    pre = [e for e in edits if start is None or _seq(e) < start]
    mode = detect_mode(events)
    post_main = [
        e
        for e in edits
        if mode == "dispatch" and start is not None and _seq(e) > start
    ]
    violations = {id(e): e for e in pre}
    for e in post_main:
        violations[id(e)] = e
    if violations:
        seqs = _seqs(violations.values())
        reasons = []
        if pre:
            reasons.append("edit before start" if start is not None else "edit with task never started")
        if post_main:
            reasons.append("main-session implement edit in dispatch mode")
        return Verdict("B09", False, seqs, "; ".join(reasons))
    return Verdict(
        "B09",
        True,
        (),
        f"no implement edits outside sub-agents (mode={mode})",
    )


# ---------------------------------------------------------------------------
# Predicates — Phase 2 (execute)
# ---------------------------------------------------------------------------


def b10_dispatch_prefix(events, ctx) -> Verdict:
    """B10: every sub-agent dispatch prompt starts with 'Active task: '."""
    if detect_mode(events) != "dispatch":
        return Verdict(
            "B10", None, (), "not in stratum: scored only in dispatch-observed runs"
        )
    dispatches = _dispatches(events)
    if not dispatches:
        return Verdict(
            "B10", None, (), "dispatch mode observed but no task-tool calls found"
        )
    problems = []
    for ev, targets in dispatches:
        if not targets:
            problems.append((_seq(ev), "malformed dispatch args: no sub-agent target"))
            continue
        for t in targets:
            prompt = (t.get("prompt") or "").strip()
            if not prompt:
                problems.append((_seq(ev), "malformed dispatch args: no prompt"))
            elif not prompt.startswith(ACTIVE_TASK_PREFIX):
                problems.append((_seq(ev), "prompt missing 'Active task: ' prefix"))
    if problems:
        return Verdict(
            "B10",
            False,
            tuple(sorted({s for s, _ in problems})),
            "; ".join(sorted({r for _, r in problems})),
        )
    return Verdict(
        "B10",
        True,
        _seqs(ev for ev, _ in dispatches),
        "all dispatch prompts carry the active-task prefix",
    )


def b11_no_self_dispatch(events, ctx) -> Verdict:
    """B11: no implement/check agent spawning the same agent class (nested
    traces from ctx)."""
    if detect_mode(events) != "dispatch":
        return Verdict(
            "B11", None, (), "not in stratum: scored only in dispatch-observed runs"
        )
    nested = _normalize_nested(ctx.get("nested_events"))
    if not nested:
        return Verdict(
            "B11",
            None,
            (),
            "no nested sub-agent traces in ctx (spike S6 labeling required)",
        )
    violations: list[tuple[int, str, str]] = []
    for agent, evts in nested:
        cls = _agent_class(agent)
        for ev, targets in _dispatches(evts):
            for t in targets:
                tname = t.get("agent") or ""
                if not tname:
                    continue
                if tname == agent or (cls != "other" and _agent_class(tname) == cls):
                    violations.append((_seq(ev), agent, tname))
    if violations:
        return Verdict(
            "B11",
            False,
            tuple(sorted({s for s, _, _ in violations})),
            "self-dispatch: "
            + "; ".join(f"{a} -> {t}" for _, a, t in sorted(violations)),
        )
    return Verdict("B11", True, (), "no same-class self-dispatch in nested traces")


def _check_activity(events) -> list[Mapping[str, Any]]:
    """trellis-check involvement: skill reads + check-class dispatches."""
    out = [
        e
        for e in _tool_calls(events)
        if (e.get("tool") in READ_TOOLS and _args_contain(e, _SKILL_CHECK))
        or (e.get("tool") == TASK_TOOL)
        and any(_agent_class(t.get("agent") or "") == "check" for t in _dispatch_targets(e))
    ]
    return out


def b12_check_after_implement(events, ctx) -> Verdict:
    """B12: trellis-check after implement, before any completion claim.
    dispatch = agent call order; inline = skill/agent after last edit."""
    mode = detect_mode(events)
    impl_seqs = [_seq(e) for e in _fixture_edits(events)]
    for ev, targets in _dispatches(events):
        if any(_agent_class(t.get("agent") or "") == "implement" for t in targets):
            impl_seqs.append(_seq(ev))
    if not impl_seqs:
        return Verdict("B12", None, (), "not in stratum: no implement activity")
    impl_end = max(impl_seqs)
    checks = [_seq(e) for e in _check_activity(events) if _seq(e) > impl_end]
    claims = [_seq(e) for e in _completion_claims(events) if _seq(e) > impl_end]
    if not checks:
        return Verdict(
            "B12", False, (impl_end,), f"no trellis-check after implement (mode={mode})"
        )
    first_check = min(checks)
    if claims and min(claims) < first_check:
        return Verdict(
            "B12",
            False,
            (impl_end, min(claims), first_check),
            "completion claim before trellis-check",
        )
    return Verdict(
        "B12",
        True,
        (impl_end, first_check),
        f"trellis-check followed implement (mode={mode})",
    )


def b13_spec_read_before_edit(events, ctx) -> Verdict:
    """B13 (inline variant): spec/before-dev read before first fixture edit.
    The dispatch variant (specs reach sub-agents via implement.jsonl) is
    injection-scope, n/a here."""
    if detect_mode(events) != "inline":
        return Verdict(
            "B13",
            None,
            (),
            "not in stratum: dispatch variant is injection-scope (specs reach sub-agents)",
        )
    edits = _fixture_edits(events)
    if not edits:
        return Verdict("B13", None, (), "not in stratum: no fixture edits inline")
    first_edit = min(_seq(e) for e in edits)
    reads = [
        e
        for e in _tool_calls(events)
        if e.get("tool") in READ_TOOLS
        and (_args_contain(e, ".trellis/spec") or _args_contain(e, _SKILL_BEFORE_DEV))
    ]
    if not reads:
        return Verdict("B13", False, (first_edit,), "no spec read before editing")
    first_read = min(_seq(e) for e in reads)
    ok = first_read < first_edit
    return Verdict(
        "B13",
        ok,
        tuple(sorted((first_read, first_edit))),
        "spec read before first edit" if ok else "first edit preceded any spec read",
    )


# ---------------------------------------------------------------------------
# Predicates — Phase 3 (finish)
# ---------------------------------------------------------------------------


def b15_commit(events, ctx) -> Verdict:
    """B15: changes committed — a ``git:`` snapshot digest changed during the
    run (or ctx['commits_made'] override)."""
    produced = (
        bool(_fixture_edits(events))
        or bool(_create_calls(events))
        or bool(_new_task_dirs(events))
    )
    if not produced:
        return Verdict("B15", None, (), "not in stratum: run produced no changes")
    commits = ctx.get("commits_made")
    if isinstance(commits, int) and not isinstance(commits, bool):
        return Verdict(
            "B15", commits >= 1, (), f"ctx commits_made={commits}"
        )
    changes = _value_changes(events, lambda k: k.startswith(GIT_SNAPSHOT_PREFIX))
    if not _snapshots(events):
        return Verdict("B15", None, (), "no snapshot events; git evidence unavailable")
    if not changes and not any(
        k.startswith(GIT_SNAPSHOT_PREFIX)
        for _, snap in _snapshots(events)
        for k in snap
    ):
        return Verdict(
            "B15", None, (), "no git:* snapshot entries; commit evidence unavailable"
        )
    if changes:
        return Verdict(
            "B15",
            True,
            tuple(sorted({s for _, s in changes})),
            f"git state changed during run: {[k for k, _ in changes]}",
        )
    return Verdict(
        "B15", False, (), "git log digest constant across snapshots; no commit made"
    )


def b16_journal_record(events, ctx) -> Verdict:
    """B16: session recorded — a .trellis/workspace journal entry changed."""
    start = _start_seq(events)
    if start is None:
        return Verdict("B16", None, (), "not in stratum: finish phase not reached")
    changes = _value_changes(events, lambda k: bool(_JOURNAL_RE.match(_norm_path(k))))
    has_journal_keys = any(
        _JOURNAL_RE.match(_norm_path(k))
        for _, snap in _snapshots(events)
        for k in snap
    )
    if not has_journal_keys and not changes:
        return Verdict("B16", False, (start,), "no journal snapshot entries at all")
    if changes:
        return Verdict(
            "B16",
            True,
            tuple(sorted({s for _, s in changes})),
            f"journal changed during run: {[k for k, _ in changes]}",
        )
    return Verdict(
        "B16", False, (start,), "journal digest constant across snapshots"
    )


# ---------------------------------------------------------------------------
# Predicates — cross-cutting
# ---------------------------------------------------------------------------


def b18_no_hand_edit_trellis(events, ctx) -> Verdict:
    """B18: task state (task.json, jsonl manifests, workflow.md) never
    hand-edited; planning prose artifacts are AI-writable."""
    violations: list[tuple[int, str]] = []

    def scan(evs, agent: str) -> None:
        for e in evs:
            tool = e.get("tool")
            targets: list[str] = []
            if tool in EDIT_TOOLS:
                targets = _target_paths(e)
            elif tool == BASH_TOOL:
                targets = _bash_written_paths(_bash_command(e))
            for p in targets:
                if _hand_edit_target(p):
                    violations.append((_seq(e), f"{_norm_path(p)}{agent}"))

    scan(_tool_calls(events), "")
    for agent, evts in _normalize_nested(ctx.get("nested_events")):
        scan(_tool_calls(evts), f" (nested:{agent})")
    if violations:
        violations.sort()
        return Verdict(
            "B18",
            False,
            tuple(sorted({s for s, _ in violations})),
            "hand-edited: " + "; ".join(p for _, p in violations),
        )
    return Verdict(
        "B18", True, (), "no hand-edits to task.json/jsonl manifests/workflow.md"
    )


def b19_fixture_expectation(events, ctx) -> Verdict:
    """B19: probe's own success criterion (result passed in ctx)."""
    res = ctx.get("fixture_expectation_result")
    if res is None:
        fe = ctx.get("fixture_expectation")
        if isinstance(fe, Mapping):
            res = fe.get("result", fe.get("passed"))
    if res is None:
        return Verdict(
            "B19", None, (), "no fixture expectation result in ctx"
        )
    return Verdict(
        "B19",
        bool(res),
        (),
        "fixture expectation result from runner"
        if res
        else "fixture expectation verification failed",
    )


# ---------------------------------------------------------------------------
# Predicates — execution discipline (checklist family)
# ---------------------------------------------------------------------------


def b20_checklist_init_before_edit(events, ctx) -> Verdict:
    """B20: checklist initialized (non-empty) before first implement edit."""
    timeline = _todo_timeline(events)
    edits = _fixture_edits(events)
    if not timeline and not edits:
        return Verdict(
            "B20", None, (), "not in stratum: no checklist events and no implement edits"
        )
    if not timeline:
        first_edit = min(_seq(e) for e in edits)
        return Verdict(
            "B20", False, (first_edit,), "implement edits with no checklist init"
        )
    first_todo_seq, state = timeline[0]
    if not state:
        return Verdict(
            "B20", False, (first_todo_seq,), "checklist initialized with no items"
        )
    if not edits:
        return Verdict(
            "B20",
            True,
            (first_todo_seq,),
            "checklist initialized; no implement edits yet",
        )
    first_edit = min(_seq(e) for e in edits)
    ok = first_todo_seq < first_edit
    return Verdict(
        "B20",
        ok,
        tuple(sorted((first_todo_seq, first_edit))),
        "checklist init preceded first implement edit"
        if ok
        else "first implement edit preceded checklist init",
    )


def b21_live_checklist_updates(events, ctx) -> Verdict:
    """B21: items marked done as work finishes, not batch-marked at the end."""
    timeline = _todo_timeline(events)
    if not timeline:
        return Verdict("B21", None, (), "not in stratum: no checklist events")
    work = _work_events(events)
    if not work:
        return Verdict("B21", None, (), "not in stratum: no work events")
    transitions = _done_transitions(timeline)
    items_done = {i for _, ids in transitions for i in ids}
    last_work = max(_seq(e) for e in work)
    if not items_done:
        return Verdict(
            "B21",
            False,
            (last_work,),
            "work happened but no checklist item was ever marked done",
        )
    if (
        len(transitions) == 1
        and len(transitions[0][1]) >= 2
        and transitions[0][0] > last_work
    ):
        return Verdict(
            "B21",
            False,
            (last_work, transitions[0][0]),
            f"batch-marked {len(transitions[0][1])} items done after last work event",
        )
    lags = []
    for ident in sorted(items_done):
        done_at = next(s for s, ids in transitions if ident in ids)
        prior_work = [_seq(e) for e in work if _seq(e) < done_at]
        if prior_work:
            lags.append(done_at - max(prior_work))
    return Verdict(
        "B21",
        True,
        tuple(s for s, _ in transitions),
        f"done marks interleaved with work; max done-lag={max(lags) if lags else 0}",
    )


def _turns(events) -> tuple[list[list[Mapping[str, Any]]], bool]:
    turns: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    has_boundary = False
    for e in _ordered(events):
        if e.get("kind") == KIND_TURN_END:
            has_boundary = True
            turns.append(current)
            current = []
        elif e.get("kind") == KIND_TOOL_CALL:
            current.append(e)
    if current:
        turns.append(current)
    return turns, has_boundary


def b22_no_lone_todo_turns(events, ctx) -> Verdict:
    """B22: no turn whose only tool activity is todo calls."""
    turns, has_boundary = _turns(events)
    if not has_boundary:
        return Verdict(
            "B22", None, (), "not in stratum: no turn_end events in stream"
        )
    lone: list[Mapping[str, Any]] = []
    for tools in turns:
        if tools and all(e.get("tool") in TODO_TOOLS for e in tools):
            lone.extend(tools)
    if lone:
        return Verdict(
            "B22",
            False,
            _seqs(lone),
            f"{len(lone)} lone-todo turn(s): todo calls with no real work batched",
        )
    return Verdict("B22", True, (), "todo calls always batched with real work")


def b23_per_item_check(events, ctx) -> Verdict:
    """B23: a verification run between an item's first work and its done mark;
    unverified items are listed for judge review."""
    timeline = _todo_timeline(events)
    if not timeline:
        return Verdict("B23", None, (), "not in stratum: no checklist events")
    work = _work_events(events)
    if not work:
        return Verdict("B23", None, (), "not in stratum: no work events")
    order: list[str] = []
    for _, state in timeline:
        for ident in state:
            if ident not in order:
                order.append(ident)
    done_at: dict[str, int] = {}
    for seq, marked in _done_transitions(timeline):
        for ident in marked:
            done_at.setdefault(ident, seq)
    windows: dict[str, dict] = {}
    for w in work:
        ws = _seq(w)
        current = next(
            (i for i in order if done_at.get(i) is None or done_at[i] > ws), None
        )
        if current is None:
            continue
        win = windows.setdefault(current, {"first": ws, "last": ws})
        win["last"] = max(win["last"], ws)
    verify_seqs = [_seq(e) for e in _verification_events(events)]
    last_seq = max(_seq(e) for e in _ordered(events))
    unverified: list[str] = []
    verified_seqs: list[int] = []
    for ident in order:
        win = windows.get(ident)
        if not win:
            continue
        end = done_at.get(ident) or last_seq
        hits = [vs for vs in verify_seqs if win["first"] <= vs <= end]
        if hits:
            verified_seqs.append(min(hits))
        else:
            unverified.append(ident)
    if unverified:
        return Verdict(
            "B23",
            False,
            tuple(sorted({windows[i]["first"] for i in unverified})),
            f"items verified-by-nothing (work seq -> done, no check between): "
            f"{unverified}",
        )
    return Verdict(
        "B23",
        True,
        tuple(sorted(set(verified_seqs))),
        "every worked item has a verification run before its done mark",
    )


def b25_final_full_scope_check(events, ctx) -> Verdict:
    """B25: full-scope check after the last implement edit, before any
    completion claim."""
    edits = _fixture_edits(events)
    if not edits:
        return Verdict("B25", None, (), "not in stratum: no implement edits")
    last_edit = max(_seq(e) for e in edits)
    checks = [
        _seq(e)
        for e in _verification_events(events)
        if _seq(e) > last_edit
    ]
    checks += [
        _seq(e)
        for e in _check_activity(events)
        if _seq(e) > last_edit
    ]
    claims = [_seq(e) for e in _completion_claims(events) if _seq(e) > last_edit]
    if not checks:
        return Verdict(
            "B25",
            False,
            (last_edit,),
            "no verification run after last implement edit",
        )
    first_check = min(checks)
    check_ev = next(e for e in _ordered(events) if _seq(e) == first_check)
    scope = _bash_command(check_ev) or (
        "trellis-check" if _args_contain(check_ev, _SKILL_CHECK) else "check event"
    )
    if claims and min(claims) < first_check:
        return Verdict(
            "B25",
            False,
            (last_edit, min(claims), first_check),
            "completion claim before final full-scope check",
        )
    return Verdict(
        "B25",
        True,
        (last_edit, first_check),
        f"final check after last edit: {str(scope)[:60]!r}",
    )


def b26_escalation_after_failed_fixes(events, ctx) -> Verdict:
    """B26 (det part): after >=2 consecutive failed fix attempts on the same
    defect, a trellis-break-loop read must precede the next fix attempt."""
    verifies = _verification_events(events)
    if not verifies:
        return Verdict("B26", None, (), "not in stratum: no verification runs")
    verify_by_seq = {_seq(e): e for e in verifies}
    breakloop = {
        _seq(e)
        for e in _tool_calls(events)
        if e.get("tool") in READ_TOOLS and _args_contain(e, _SKILL_BREAK_LOOP)
    }
    edits_by_seq = {_seq(e): e for e in _fixture_edits(events)}
    streak = 0
    max_streak = 0
    second_fail_seq: int | None = None
    escalated = False
    violation: int | None = None
    for ev in _ordered(events):
        if ev.get("kind") != KIND_TOOL_CALL:
            continue
        s = _seq(ev)
        if s in verify_by_seq:
            if _is_fail_result(ev.get("result") or ""):
                streak += 1
                max_streak = max(max_streak, streak)
                if streak == 2:
                    second_fail_seq = s
                    escalated = False
            elif (ev.get("result") or "").strip():
                streak = 0
        elif s in breakloop and second_fail_seq is not None and s > second_fail_seq:
            escalated = True
        elif s in edits_by_seq and second_fail_seq is not None and s > second_fail_seq:
            if not escalated and streak >= 2 and violation is None:
                violation = s
    if violation is not None:
        return Verdict(
            "B26",
            False,
            (second_fail_seq, violation),
            f"blind fix after {max_streak} consecutive failures without "
            "trellis-break-loop escalation",
        )
    return Verdict(
        "B26",
        True,
        tuple(sorted(breakloop)) if breakloop else (),
        f"no unescalated blind fix (max consecutive failures={max_streak}, "
        f"break-loop read={'yes' if breakloop else 'not required'})",
    )


# ---------------------------------------------------------------------------
# Registry + top-level entry point
# ---------------------------------------------------------------------------

PREDICATES: dict[str, Callable[[Any, Mapping[str, Any]], Verdict]] = {
    "B02": b02_consent_before_create,
    "B03": b03_no_task_after_rejection,
    "B04": b04_process_theater,
    "B05": b05_brainstorm_skill_load,
    "B06": b06_artifacts_before_start,
    "B07": b07_jsonl_curated,
    "B08": b08_review_gate,
    "B09": b09_no_implement_before_start,
    "B10": b10_dispatch_prefix,
    "B11": b11_no_self_dispatch,
    "B12": b12_check_after_implement,
    "B13": b13_spec_read_before_edit,
    "B15": b15_commit,
    "B16": b16_journal_record,
    "B18": b18_no_hand_edit_trellis,
    "B19": b19_fixture_expectation,
    "B20": b20_checklist_init_before_edit,
    "B21": b21_live_checklist_updates,
    "B22": b22_no_lone_todo_turns,
    "B23": b23_per_item_check,
    "B25": b25_final_full_scope_check,
    "B26": b26_escalation_after_failed_fixes,
}

#: Judge-scope behaviors get placeholder n/a verdicts from grade_run.
JUDGE_ONLY_IDS = _JUDGE_ONLY_IDS


def mode_agreement(events, ctx) -> Verdict:
    observed = detect_mode(events)
    task_seqs = _seqs(ev for ev, _ in _dispatches(events))
    expected = ctx.get("expected_mode")
    if expected is None:
        return Verdict(
            "mode_agreement", None, task_seqs, f"observed={observed}; no expected_mode in ctx"
        )
    expected = str(expected)
    if expected == "n/a":
        return Verdict(
            "mode_agreement", None, task_seqs, f"observed={observed}; expected=n/a"
        )
    return Verdict(
        "mode_agreement",
        observed == expected,
        task_seqs,
        f"observed={observed} expected={expected}",
    )


def grade_run(
    events: Iterable[Mapping[str, Any]], ctx: Mapping[str, Any] | None = None
) -> list[Verdict]:
    """Grade one run: a Verdict per catalog behavior (judge-only ones as n/a
    placeholders) plus the mode_agreement measure, in ascending id order."""
    ctx = dict(ctx or {})
    verdicts = [
        Verdict(bid, None, (), "judge-scope behavior; deterministic grader emits no verdict")
        for bid in _JUDGE_ONLY_IDS
    ]
    for bid in sorted(PREDICATES):
        verdicts.append(PREDICATES[bid](events, ctx))
    verdicts.append(mode_agreement(events, ctx))
    return verdicts
