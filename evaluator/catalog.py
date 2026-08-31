"""Behavior catalog loader for the Trellis workflow adherence evaluator.

Loads ``behaviors.yaml`` (1:1 with ``research/behavior-catalog.md``) and
exposes it as an id-indexed catalog plus a coverage helper used by the
probe-suite validation (every catalog behavior must be covered by >= 1 probe).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import yaml

#: behaviors.yaml lives at the repo root, one level above the package.
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "behaviors.yaml"

_CHECK_CLASSES = frozenset({"det", "judge", "sim", "det+judge"})
_MODE_TAGS = frozenset({"dispatch", "inline", "any", "n/a"})
_BEHAVIOR_ID_RE = re.compile(r"^B\d{2}$")


@dataclass(frozen=True)
class Behavior:
    """One catalog entry; shape is the frozen behaviors.yaml contract."""

    id: str
    phase: str
    behavior: str
    check: str
    evidence: str
    mode_tag: str


@dataclass(frozen=True)
class BehaviorCatalog:
    """Loaded behavior catalog indexed by behavior id."""

    behaviors: tuple[Behavior, ...]
    by_id: Mapping[str, Behavior] = field(repr=False)

    def uncovered_behavior_ids(
        self, covered: Iterable[Iterable[str]] | Iterable[str]
    ) -> list[str]:
        """Return sorted catalog ids not covered by any probe.

        ``covered`` is one expected-behavior list per probe (an iterable of
        iterables of behavior ids). A flat iterable of ids is also accepted.
        Unknown ids raise ``ValueError`` — a probe naming a nonexistent
        behavior is a probe bug, not a coverage gap.
        """
        covered_ids: set[str] = set()
        unknown: set[str] = set()
        for item in covered:
            if isinstance(item, str):
                entries: Iterable[str] = (item,)
            else:
                entries = item
            for behavior_id in entries:
                if behavior_id in self.by_id:
                    covered_ids.add(behavior_id)
                else:
                    unknown.add(behavior_id)
        if unknown:
            raise ValueError(f"unknown behavior ids in probe coverage: {sorted(unknown)}")
        return sorted(set(self.by_id) - covered_ids)


def load_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> BehaviorCatalog:
    """Load and validate the behavior catalog YAML at ``path``."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or "behaviors" not in raw:
        raise ValueError(f"{path}: expected a top-level 'behaviors' mapping list")
    entries = raw["behaviors"]
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'behaviors' must be a list")

    behaviors: list[Behavior] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{path}: behaviors[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where}: entry must be a mapping")
        missing = [key for key in ("id", "phase", "behavior", "check", "evidence", "mode_tag") if key not in entry]
        if missing:
            raise ValueError(f"{where}: missing keys {missing}")
        behavior = Behavior(
            id=entry["id"],
            phase=entry["phase"],
            behavior=entry["behavior"],
            check=entry["check"],
            evidence=entry["evidence"],
            mode_tag=entry["mode_tag"],
        )
        if not _BEHAVIOR_ID_RE.match(behavior.id):
            raise ValueError(f"{where}: id {behavior.id!r} does not match 'B<two digits>'")
        if behavior.id in seen:
            raise ValueError(f"{where}: duplicate behavior id {behavior.id}")
        seen.add(behavior.id)
        if not behavior.phase or not isinstance(behavior.phase, str):
            raise ValueError(f"{where}: phase must be a non-empty string")
        if behavior.check not in _CHECK_CLASSES:
            raise ValueError(f"{where}: check {behavior.check!r} not in {sorted(_CHECK_CLASSES)}")
        if behavior.mode_tag not in _MODE_TAGS:
            raise ValueError(f"{where}: mode_tag {behavior.mode_tag!r} not in {sorted(_MODE_TAGS)}")
        if not behavior.behavior or not behavior.evidence:
            raise ValueError(f"{where}: behavior and evidence must be non-empty")
        behaviors.append(behavior)

    return BehaviorCatalog(behaviors=tuple(behaviors), by_id={b.id: b for b in behaviors})
