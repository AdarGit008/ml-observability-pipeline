# drift

## Purpose
Shared PSI (Population Stability Index) implementation. Used identically by `lambda_scorer` (per-pump, hot path) and `local_runtime` (per-pump in local mode) and by the EventBridge-scheduled fleet-PSI Lambda (every 5 minutes).

This is the single most architecturally important shared module: if local mode and AWS mode disagree about drift, the project's mode-parity claim collapses.

## Current state
- [ ] Not started.
- Spec defined in `PLAN.md §2.7`.

## Interfaces (in / out)
- **In:** Window of recent readings + reference distribution (from `model/artifacts/reference_distribution.json`).
- **Out:** Per-feature PSI scalar. Threshold classification: stable | warning | significant.

## Invariants
- Pure Python + `numpy` only. No `pandas` (Lambda cold-start cost).
- Deterministic for the same inputs across both runtimes — testable.
- Handles empty bins (Laplace smoothing, epsilon floor) without div-by-zero.

## Open questions
- Bin count: 10 equal-frequency is the default. Confirm with the synthetic data once generated.
- Smoothing parameter: epsilon vs Laplace add-α. (See `_interfaces.md` PSI parameters.)
- Window edge handling: hourly tumbling vs sliding. Default: sliding (more responsive to scenario 1).

## Related ADRs
None yet. Likely: `0002-drift-metric-psi.md` (already planned in `PLAN.md §1`).
