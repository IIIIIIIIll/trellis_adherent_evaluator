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


## Session 3: Pin B24 no-task boundary, re-validate judge agreement

**Date**: 2026-09-01
**Task**: Pin B24 no-task boundary, re-validate judge agreement
**Branch**: `main`

### Summary

Session summary was not supplied.

### Main Changes

- B24 judge rubric pins the zero-artifacts case: no checklist AND no implement.md = vacuous pass (absence scored by B01/B02/B06/B20)
- Re-validation on same 10 traces: B24 blind agreement 6/10 -> 9/10; residual 1/10 = judge leniency on improvised-but-true-to-work checklists
- Measured judge run-to-run stability: B17 0/10 flips, B14 1/10, B01 2/10 (borderline small-bugfix classification on trellis-off traces)
- Stored runs/mimo-* B24 verdicts are pre-pinning vintage; fresh runs use the new boundary

### Git Commits

| Hash | Message |
|------|---------|
| `a533cfa` | (see git log) |

### Testing

- [OK] python3 -m pytest tests -q -> 159 passed

### Status

[OK] **Completed**
