"""Per-turn filesystem snapshots of the sandbox: {path: sha256}.

Taken after every assistant turn (design.md: snapshot ordering granularity is
per-turn -- exactly what every det predicate needs). The returned map covers
the whole sandbox tree minus noise, plus one pseudo-key:

- ``git:log`` -- sha256 of ``git log --oneline`` stdout in the sandbox; a value
  change between snapshots means a commit happened (grader B15 evidence).

Exclusions (noise, not model-mutated state): ``.git/``, ``__pycache__/``,
``.pytest_cache/``, ``*.pyc``, and any ``.runtime`` directory (time-driven
update-check markers; they change with wall time, not with model behavior).
Everything else -- ``.trellis/**``, fixture code, ``.omp/**`` -- is included
with repo-relative POSIX paths, so grader key-diffs over ``.trellis/**`` and
``.trellis/workspace/*journal*`` work as designed.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path, PurePosixPath

from evaluator.trace import _event

SNAPSHOT_GIT_LOG_KEY = "git:log"

_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv", "node_modules"})
_SKIP_SUFFIXES = (".pyc",)


def _skip(rel_path: str) -> bool:
    parts = PurePosixPath(rel_path).parts
    if any(part in _SKIP_DIRS for part in parts):
        return True
    if any(part == ".runtime" for part in parts):
        return True
    return rel_path.endswith(_SKIP_SUFFIXES)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_log(sandbox: Path) -> str | None:
    """``git log --oneline`` stdout, or None when git fails (no repo/commits)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(sandbox), "log", "--oneline"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def take_snapshot(sandbox) -> dict:
    """Hash every included file under ``sandbox`` (plus the ``git:log`` key)."""
    sandbox = Path(sandbox)
    hashes: dict = {}
    for path in sorted(sandbox.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(sandbox).as_posix()
        if _skip(rel):
            continue
        hashes[rel] = _sha256_file(path)
    git_log = _git_log(sandbox)
    if git_log is not None:
        hashes[SNAPSHOT_GIT_LOG_KEY] = hashlib.sha256(git_log.encode("utf-8")).hexdigest()
    return hashes


def snapshot_event(sandbox, *, ts: float | None = None, seq: int = 0) -> dict:
    """Snapshot as an events.jsonl line (kind=snapshot, role=system).

    ``ts``/``seq`` are filled by the driver's turn loop; standalone callers get
    wall-clock ts and seq=0.
    """
    return _event(
        seq=seq,
        ts=time.time() if ts is None else ts,
        kind="snapshot",
        role="system",
        snapshot=take_snapshot(sandbox),
    )
