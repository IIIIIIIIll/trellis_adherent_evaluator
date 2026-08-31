"""omp subprocess control: sandbox materialization + turn-loop driver.

Frozen invocation shape (research/omp-driver-notes.md spike outcomes):

    turn 1:   omp -p --mode=json --auto-approve --session-dir <dir> --cwd <sandbox>
              --max-time <t> [<arm flags>] <prompt>
    turn n>1: same + --continue          (omp appends to the same session file)

Spike-verified facts this module relies on:
- consent questions surface as the turn's final assistant text ending with
  ``?`` with no tool call in the turn (stdout ``message_end`` events; S1);
- ``--continue`` resolves the latest session within ``--session-dir`` and
  appends to the same file (S1/S2) -- hence a fresh per-probe session dir;
- session files are named ``<ISO-ts>_<uuid>.jsonl`` directly under the
  session dir; nested sub-agent transcripts live in a sibling directory
  (handled by evaluator.trace.load_nested, S6);
- per-turn cost comes from assistant-message ``usage`` fields in the session
  file (S2), read here as a per-turn delta over newly appended entries.

The driver owns no grading logic; it composes trace normalization (per-turn
chunks) + snapshot events into a single seq-monotonic event stream.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from evaluator import trace
from evaluator.simulator import is_consent_question
from evaluator.snapshot import snapshot_event

#: Frozen base flags for every omp invocation (turn 1 and continuations).
BASE_FLAGS = ["-p", "--mode=json", "--auto-approve"]


# ------------------------------------------------------------------ sandbox

def materialize_sandbox(template_dir, sandbox_dir) -> Path:
    """Materialize a fresh run sandbox by copying the fixture template and
    ensuring a clean git baseline: if the template carries ``.git`` it is
    reset to HEAD; otherwise a fresh repo with an "Initial commit" is
    created (B15 commit evidence needs history in every sandbox).

    Fails if ``sandbox_dir`` already exists -- sandboxes are fresh per run.
    """
    template_dir = Path(template_dir)
    sandbox_dir = Path(sandbox_dir)
    if sandbox_dir.exists():
        raise FileExistsError(f"sandbox already exists: {sandbox_dir}")
    sandbox_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_dir, sandbox_dir, symlinks=True)
    if (sandbox_dir / ".git").exists():
        _run_quiet(["git", "-C", str(sandbox_dir), "reset", "--hard", "--quiet"])
        _run_quiet(["git", "-C", str(sandbox_dir), "clean", "-fdq"])
    else:
        _run_quiet(["git", "-C", str(sandbox_dir), "init", "-q", "-b", "main"])
        _run_quiet(["git", "-C", str(sandbox_dir), "add", "-A"])
        _run_quiet([
            "git", "-C", str(sandbox_dir),
            "-c", "user.name=evaluator", "-c", "user.email=evaluator@local",
            "commit", "-q", "-m", "Initial commit",
        ])
    return sandbox_dir


def _run_quiet(cmd: list) -> None:
    subprocess.run(cmd, capture_output=True, check=False)


# -------------------------------------------------------------- turn results

@dataclass
class TurnResult:
    """Outcome of one omp invocation (one assistant turn)."""

    text: str = ""                      # final assistant text of the turn
    has_tool_calls: bool = False
    tool_calls: list = field(default_factory=list)   # [{"name": str, "args": dict}]
    usage: dict = field(default_factory=dict)        # per-turn delta (session usage fields)
    is_consent_question: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    duration_s: float = 0.0
    session_file: Path | None = None
    entries: list = field(default_factory=list)      # session entries appended this turn


@dataclass
class SessionResult:
    """Full multi-turn session: seq-monotonic events + per-turn results."""

    events: list = field(default_factory=list)
    turns: list = field(default_factory=list)

    def total_usage(self) -> dict:
        total: dict = {}
        for turn in self.turns:
            total = trace.add_usage(total, turn.usage)
        return total


# -------------------------------------------------------------------- driver

class Driver:
    """Drives omp turn-by-turn against a sandbox and captures the run.

    The driver knows omp only (design.md boundary): session-format specifics
    are delegated to evaluator.trace, snapshots to evaluator.snapshot.
    """

    def __init__(self, sandbox, session_dir, *, max_time: str | int = 600,
                 per_turn_timeout: float = 900.0, extra_flags: list | None = None,
                 model: str | None = None, omp_bin: str = "omp"):
        self.sandbox = Path(sandbox).resolve()
        self.session_dir = Path(session_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.max_time = max_time
        self.per_turn_timeout = per_turn_timeout
        self.extra_flags = list(extra_flags or [])
        self.model = model
        self.omp_bin = omp_bin
        self._session_file: Path | None = None
        self._entries_seen = 0

    # -- public API

    def run_turn(self, prompt: str, *, continue_session: bool = False) -> TurnResult:
        """Run one omp invocation and capture its observable outcome."""
        cmd = self._build_cmd(prompt, continue_session)
        start = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stdout = ""
        try:
            # start_new_session: --max-time is omp's own bound; this is the
            # hard outer bound, killing the whole process group on expiry.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
            try:
                stdout, _ = proc.communicate(timeout=self.per_turn_timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                stdout, _ = proc.communicate()
        except FileNotFoundError as exc:
            raise RuntimeError(f"omp binary not found: {self.omp_bin!r}") from exc
        duration = time.monotonic() - start

        result = _turn_from_stdout(stdout)
        result.entries = self._read_new_entries()
        result.usage = trace.summarize_usage(result.entries)
        result.is_consent_question = is_consent_question(result.text, result.has_tool_calls)
        result.timed_out = timed_out
        result.exit_code = exit_code
        result.duration_s = duration
        result.session_file = self._session_file
        return result

    def run_session(self, initial_prompt: str, simulator=None, *,
                    max_turns: int = 12) -> SessionResult:
        """Turn loop: run turns, consult the simulator after each, normalize
        the session delta, and take a filesystem snapshot after every turn.

        Ends when the simulator returns None (no reply -- includes
        non-question turns), or after max_turns.
        """
        events: list = []
        turns: list = []
        prompt = initial_prompt
        for turn_index in range(max_turns):
            result = self.run_turn(prompt, continue_session=turn_index > 0)
            turns.append(result)
            events.extend(trace.normalize_entries(
                result.entries, seq_start=len(events) + 1))
            events.append(snapshot_event(self.sandbox, seq=len(events) + 1))
            reply = None
            if simulator is not None:
                reply = simulator.reply(
                    result.text, turn_index, has_tool_calls=result.has_tool_calls)
            if reply is None:
                break
            prompt = reply
        return SessionResult(events=events, turns=turns)

    # -- internals

    def _build_cmd(self, prompt: str, continue_session: bool) -> list:
        cmd = [
            self.omp_bin, *BASE_FLAGS,
            "--session-dir", str(self.session_dir),
            "--cwd", str(self.sandbox),
            "--max-time", str(self.max_time),
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_flags
        if continue_session:
            cmd += ["--continue"]
        cmd.append(prompt)
        return cmd

    def _read_new_entries(self) -> list:
        """Session entries appended since the last read (delta per turn).

        --continue resolves the latest session within --session-dir (spike:
        fresh per-probe session dir keeps this unambiguous), so the newest
        ``*.jsonl`` after turn 1 is the session file for all turns.
        """
        if self._session_file is None:
            candidates = sorted(self.session_dir.glob("*.jsonl"),
                                key=lambda p: p.stat().st_mtime)
            if not candidates:
                return []
            self._session_file = candidates[-1]
        entries = trace.parse_session(self._session_file)
        new_entries = entries[self._entries_seen:]
        self._entries_seen = len(entries)
        return new_entries


def _turn_from_stdout(stdout: str) -> TurnResult:
    """Extract the assistant turn from ``--mode=json`` stdout (spike S1: a
    JSONL event stream whose ``message_end`` events carry the message payload;
    no dedicated ask/question field in -p mode). Non-JSON lines are skipped.
    """
    texts: list = []
    tool_calls: list = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif item.get("type") == "toolCall":
                args = item.get("arguments")
                if not isinstance(args, dict):
                    try:
                        args = json.loads(item.get("partialArgs") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                tool_calls.append({"name": item.get("name", ""), "args": args})
    return TurnResult(
        text="\n\n".join(t for t in texts if t),
        has_tool_calls=bool(tool_calls),
        tool_calls=tool_calls,
    )
