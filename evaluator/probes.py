"""Probe suite loader for the Trellis workflow adherence evaluator.

Probe YAML files (one probe per file, schema frozen in design.md) are loaded
into :class:`Probe` values. Each probe carries 2+ surface paraphrases; the
active surface is picked by stable-seed rotation so a run seed always yields
the same prompt per probe without any RNG.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

#: Probe kinds (design.md / behavior-catalog.md probe-kind mapping).
PROBE_KINDS = frozenset(
    {
        "simple-question",
        "complex-feature",
        "bugfix",
        "consent-reject",
        "negative-control",
        "flaky-bug",
    }
)

_SIMULATOR_POLICIES = frozenset(
    {
        "approve_all",
        "reject_task_creation",
        "reject_first_then_approve",
        "approve_with_changes",
    }
)
_EXPECTED_MODES = frozenset({"dispatch", "inline", "n/a"})

#: Kind-level expected delegation mode (behavior-catalog.md, R9). Probes may
#: override per file with an explicit ``expected_mode``.
KIND_EXPECTED_MODE = {
    "simple-question": "inline",
    "negative-control": "inline",
    "consent-reject": "n/a",
    "complex-feature": "dispatch",
    "bugfix": "dispatch",
    "flaky-bug": "dispatch",
}

DEFAULT_MAX_TURNS = 12
DEFAULT_TIMEOUT = 900

_BEHAVIOR_ID_RE = re.compile(r"^B\d{2}$")

_PROBE_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class Probe:
    """One probe definition; field set is the frozen probe YAML contract."""

    id: str
    kind: str
    prompt: str
    paraphrases: tuple[str, ...]
    simulator_policy: str
    expected_mode: str
    expected_behaviors: tuple[str, ...]
    fixture_expectation: Mapping[str, Any] | None
    max_turns: int
    timeout: int

    def prompt_variants(self) -> tuple[str, ...]:
        """All surface forms: canonical prompt first, then paraphrases."""
        return (self.prompt, *self.paraphrases)

    def prompt_for(self, seed: int) -> str:
        """Paraphrase picked by ``seed`` — stable for a given (probe, seed)."""
        variants = self.prompt_variants()
        return variants[seed % len(variants)]


def _parse_probe(path: Path, raw: Any) -> Probe:
    where = str(path)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a single probe mapping (one probe per file)")
    required = ("id", "kind", "prompt", "simulator_policy", "expected_behaviors")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"{where}: missing keys {missing}")

    probe_id = raw["id"]
    if not isinstance(probe_id, str) or not probe_id.strip():
        raise ValueError(f"{where}: id must be a non-empty string")
    kind = raw["kind"]
    if kind not in PROBE_KINDS:
        raise ValueError(f"{where}: kind {kind!r} not in {sorted(PROBE_KINDS)}")
    prompt = raw["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{where}: prompt must be a non-empty string")

    paraphrases_raw = raw.get("paraphrases", [])
    if not isinstance(paraphrases_raw, list) or any(
        not isinstance(p, str) or not p.strip() for p in paraphrases_raw
    ):
        raise ValueError(f"{where}: paraphrases must be a list of non-empty strings")
    paraphrases = tuple(paraphrases_raw)

    simulator_policy = raw["simulator_policy"]
    if simulator_policy not in _SIMULATOR_POLICIES:
        raise ValueError(
            f"{where}: simulator_policy {simulator_policy!r} not in {sorted(_SIMULATOR_POLICIES)}"
        )

    expected_mode = raw.get("expected_mode") or KIND_EXPECTED_MODE[kind]
    if expected_mode not in _EXPECTED_MODES:
        raise ValueError(f"{where}: expected_mode {expected_mode!r} not in {sorted(_EXPECTED_MODES)}")

    expected_raw = raw["expected_behaviors"]
    if not isinstance(expected_raw, list) or not expected_raw:
        raise ValueError(f"{where}: expected_behaviors must be a non-empty list")
    bad = [b for b in expected_raw if not isinstance(b, str) or not _BEHAVIOR_ID_RE.match(b)]
    if bad:
        raise ValueError(f"{where}: expected_behaviors entries must be 'B<two digits>' ids, got {bad}")
    expected_behaviors = tuple(expected_raw)

    fixture_expectation = raw.get("fixture_expectation")
    if fixture_expectation is not None and not isinstance(fixture_expectation, Mapping):
        raise ValueError(f"{where}: fixture_expectation must be a mapping when present")

    max_turns = raw.get("max_turns", DEFAULT_MAX_TURNS)
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(max_turns, int) or max_turns <= 0:
        raise ValueError(f"{where}: max_turns must be a positive integer")
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"{where}: timeout must be a positive integer")

    return Probe(
        id=probe_id,
        kind=kind,
        prompt=prompt,
        paraphrases=paraphrases,
        simulator_policy=simulator_policy,
        expected_mode=expected_mode,
        expected_behaviors=expected_behaviors,
        fixture_expectation=dict(fixture_expectation) if fixture_expectation is not None else None,
        max_turns=max_turns,
        timeout=timeout,
    )


def _probe_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    files: list[Path] = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            files.extend(
                child
                for child in sorted(path.iterdir())
                if child.is_file() and child.suffix in _PROBE_SUFFIXES
            )
        else:
            files.append(path)
    return files


def load_probes(
    paths: str | Path | Iterable[str | Path], *, seed: int | None = None
) -> list[Probe]:
    """Load probes from YAML files (a path, or an iterable of files/dirs).

    Directories are expanded to their sorted ``*.yaml``/``*.yml`` children.
    When ``seed`` is given, the active surface per probe is chosen by stable
    seed rotation: the rotated variant becomes ``prompt`` and the remaining
    variants move to ``paraphrases`` (no variant is lost). Duplicate probe
    ids raise ``ValueError``.
    """
    probes: list[Probe] = []
    seen: set[str] = set()
    for file in _probe_files(paths):
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        probe = _parse_probe(file, raw)
        if probe.id in seen:
            raise ValueError(f"{file}: duplicate probe id {probe.id!r}")
        seen.add(probe.id)
        if seed is not None:
            chosen = probe.prompt_for(seed)
            variants = [v for v in probe.prompt_variants() if v != chosen]
            probe = dataclasses.replace(probe, prompt=chosen, paraphrases=tuple(variants))
        probes.append(probe)
    return probes
