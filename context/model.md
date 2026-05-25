# model

> **Carry-in from ADR 0002 (added 2026-05-25 by the simulator config-yaml session):**
> `bearing_temp` is **not monotonic** in `degradation`. The ADR-0002 RPM coupling
> (`rpm = setpoint * (1 - degradation) + N(0, 5 + 15 * d)`) means RPM drops
> faster than the direct `degradation * 15` term rises, so bearing temp peaks
> somewhere in DEGRADING territory and falls again through FAILING and FAILED.
> Feature engineering for this model must not pre-suppose monotonicity in
> bearing temp. Recommended: use a rolling-window std of bearing_temp as the
> wear-trend feature (clean monotonic signal), and keep vibration_amp as the
> primary raw "wear" signal — it remains linear in degradation. See
> `docs/adr/0002-rpm-coupled-to-degradation.md` for the full justification.

## Purpose
Predicts P(pump fails within 48h) from 4 raw signals + 4 rolling features. Trained on synthetic data from the simulator in fast-forward.

## Current state
- [ ] Not started.
- Spec defined in `PLAN.md §2.3`.

## Interfaces (in / out)
- **In:** 8-feature vector (4 raw + 5-min rolling mean/std of vibration and bearing_temp).
- **Out:** scalar in [0, 1].
- **Artifacts:** `model/artifacts/model.pkl` (~200 KB), `model/artifacts/reference_distribution.json`.

## Acceptance criteria
- AUC ≥ 0.85 on held-out pumps.

## Open questions
- Lambda packaging: bundle pickle in deploy ZIP (default), Lambda Layer, or S3 cold-load? (HANDOFF.md §6 Q3 — default: bundle.)
- Feature versioning: how do we tag model + reference_distribution together so they can't desync?

## Related ADRs
None yet. Likely: feature engineering choices, model family rationale (HistGBT vs alternatives).
