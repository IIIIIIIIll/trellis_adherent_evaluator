# Backend Development Guidelines

> Conventions for the Trellis workflow adherence evaluator (`evaluator/` Python package).

---

## Overview

This project is a single-purpose Python 3 tool (stdlib + PyYAML) that drives
omp headlessly to measure how faithfully AI models follow the Trellis
workflow. The docs below describe the code as it actually is — every rule
cites real files and symbols.

---

## Guidelines Index

| Guide | Description |
|-------|-------------|
| [Evaluator Contracts](./evaluator-contracts.md) | Executable contracts: omp invocation shape, events.jsonl schema, grading semantics, arm validity, fixture defect map |
| [Directory Structure](./directory-structure.md) | Where things go: `evaluator/` module map, probes, arms, fixture template, tests, runs output |
| [Error Handling](./error-handling.md) | Actionable RuntimeError patterns, subprocess bounds, per-probe suite resilience, judge retry semantics, exit codes |
| [Logging Guidelines](./logging-guidelines.md) | No logging framework by design: CLI-boundary prints + structured artifacts are the record |
| [Quality Guidelines](./quality-guidelines.md) | Test bar (golden fixtures, determinism acceptance, catalog coverage invariant), offline pipeline pattern, review checklist |

---

## Read before touching

- `driver.py`, `trace.py`, `grader.py`, `judge.py`, or any probe schema →
  read [Evaluator Contracts](./evaluator-contracts.md) first; schema
  evolution is additive only.
- `report.py` / anything rendering → determinism is a contract: same inputs,
  byte-identical output, no wall-clock reads.

---

**Language**: All documentation should be written in **English**.
