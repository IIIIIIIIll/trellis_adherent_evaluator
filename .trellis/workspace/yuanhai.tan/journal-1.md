# Journal - yuanhai.tan (Part 1)

> AI development session journal
> Started: 2026-08-31

---



## Session 1: Build Trellis workflow adherence evaluator

**Date**: 2026-08-31
**Task**: Build Trellis workflow adherence evaluator
**Branch**: `main`

### Summary

Planned+built evaluator: 26-behavior catalog, 11-probe suite x 3 harness arms, omp headless driver (spike-verified), events.jsonl normalizer, 22 det predicates + judge + report/CLI; 156 tests green; smoke+verify runs passed; spec: evaluator-contracts.md; B18 narrowed to task state. Full mimo-v2.5 run held pending user review.

### Git Commits

| Hash | Message |
|------|---------|
| `bfc63df` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Verify adherence evaluator with mimo-v2.5 + deepseek-v4-flash

**Date**: 2026-09-01
**Task**: Verify adherence evaluator with mimo-v2.5 + deepseek-v4-flash
**Branch**: `main`

### Summary

Session summary was not supplied.

### Main Changes

- Full 11-probe x 3-arm run with mimo-v2.5 under test and deepseek-v4-flash judge: 0 errors, 0 pending verdicts
- Acceptance evidence: determinism 891/891 det cells; violations hand-verified per arm; judge agreement 33/40 (82%); mode_agreement dispatch+inline hand-checked
- Fixed --probes bare id/kind selection; added report --compare cross-arm delta section (+3 tests)
- Finding: no-spec-injection arm inert under omp 18.0.11 (spec_injection has no consumer); trellis-on vs trellis-off is the informative contrast

### Git Commits

| Hash | Message |
|------|---------|
| `3636f4c` | (see git log) |

### Testing

- [OK] python3 -m pytest tests -q -> 159 passed

### Status

[OK] **Completed**
