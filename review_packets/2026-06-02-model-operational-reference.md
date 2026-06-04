# Gemini Review Packet — 2026-06-02 — model: operational PSI reference (ADR 0008)

## Component
`model.train` (new helpers + CLI flag); `shared.drift` (default reference path); `model/artifacts/` (artifact name change).

## Session
`docs/sessions/2026-06-02-model-operational-reference.md`

## ADR
`docs/adr/0008-operational-reference-source-separation.md`

## Summary

This session closes the Reference-Validity carry-in from ADR 0007 §"Demo-paced PSI bias carry-in is REAL and SEVERE." The drift session (2026-06-01) measured PSI 1.3–6.7 SIGNIFICANT on 7/8 features for healthy demo fleets against the committed training-time reference; PO + Gemini Q5 of the drift review confirmed option (a) — recompute reference from demo-paced HEALTHY data, name it `operational_reference_distribution.json`.

Implementation: `model.train` grows `_operational_profiles()` (returns `DEFAULT_PROFILES` verbatim) + `_generate_operational_samples(n_pumps=5, ticks_per_pump=1800)` + a `--reference-source {operational, training}` CLI flag defaulting to `operational`. `shared.drift._DEFAULT_REF_PATH` updated; `compute_psi` / `load_reference` signatures unchanged; structural-parity tests green. ADR 0008 records the four-part decision. ADR 0006 unchanged (training-data dwell-stretch decision still stands).

Measurement after rebuild — single HEALTHY pump (`P-99`, seed 42), 1800 post-warm-up ticks, against the committed 5-pump operational reference:

| Feature                  | PSI before | PSI after | Band after | Δ        |
|--------------------------|-----------:|----------:|------------|----------|
| vibration_amp            |     1.4143 |    0.0034 | STABLE     | ~415×    |
| bearing_temp             |     1.3020 |    0.0038 | STABLE     | ~340×    |
| motor_current            |     6.7091 |    0.0067 | STABLE     | ~1000×   |
| rpm                      |     1.7358 |    0.0033 | STABLE     | ~520×    |
| vibration_amp_mean_5m    |     1.8103 |    0.1755 | WARNING    | ~10×     |
| vibration_amp_std_5m     |     0.0625 |    0.2255 | WARNING    | regressed|
| bearing_temp_mean_5m     |     2.0123 |    0.0576 | STABLE     | ~35×     |
| bearing_temp_std_5m      |     0.3432 |    0.1151 | WARNING    | ~3×      |

All four **raw features** now STABLE — massive improvement. Three of four **rolling features** WARNING; **zero SIGNIFICANT firings**. The rolling-features WARNING is structural — see Q1 below.

## Questions for Gemini

### Q1 — Rolling-features PSI is autocorrelation-bounded above the STABLE band

**The finding.** The 1800-sample PSI window contains 1650 consecutive 150-tick rolling-stat values that share 149/150 readings between neighbours — the samples are NOT IID. A single-pump 1800-tick window traces an autocorrelated walk through a narrow region of the steady-state rolling-stats distribution; the multi-pump reference covers a wider region by mixing pump-noise instances. PSI then reads "test pump's region narrower than reference's" as a distribution shift.

Across 10 test seeds against the committed 5-pump operational reference, worst-case PSI on rolling features:

| seed | worst PSI | n features ≥ 0.10 | n features ≥ 0.25 |
|------|-----------|-------------------|-------------------|
|  42  | 0.2255    | 3                 | 0                 |
| 100  | 0.2810    | 4                 | 2                 |
| 200  | 0.3596    | 4                 | 2                 |
| 300  | 0.3219    | 4                 | 1                 |
| 400  | 0.3856    | 3                 | 2                 |
| 500  | 0.3331    | 2                 | 1                 |
| 600  | 0.2461    | 3                 | 0                 |
| 700  | 0.2103    | 3                 | 0                 |
| 800  | 0.1297    | 1                 | 0                 |
| 900  | 0.1783    | 3                 | 0                 |

About 10% of (feature, seed) combinations fire SIGNIFICANT on healthy pumps. Raw features ALL clear < 0.10 across all 10 seeds.

**Increasing reference pump count doesn't fix it.** Tested 5 → 15 → 30 → 50 reference pumps with five test seeds; worst-case PSI moved 0.39 → 0.46 → 0.37 → 0.36. Diminishing returns; structural floor ~0.35. More reference pumps slightly *widens* the bin range, which keeps the single-pump test window in fewer bins.

**This session's regression test compromise.** `test_demo_paced_healthy_psi_stable` pins:
- Raw features: PSI < 0.10 STABLE (strict). Strong regression guard with ~415–1000× headroom.
- Rolling features: PSI < 0.5 soft bound (catches catastrophic regressions; doesn't claim the structurally-unachievable < 0.10).

**Three remediation options on the table for a follow-up session** (logged in `context/drift.md` §"Open questions"):
1. **Lift the PSI bands for rolling features** (e.g., < 0.25 STABLE / 0.25–0.50 WARNING / > 0.50 SIGNIFICANT) so the dashboard semantics stay meaningful under autocorrelation.
2. **Drop the rolling features from the PSI surface entirely.** The drift detection signal comes from the raw features; the rolling features were added to the scorer's input but maybe shouldn't be in PSI's input.
3. **Replace PSI for rolling features with a moving-average-band metric** (e.g., "rolling 5-min mean inside historic ±2σ band"). Different math, but operationally equivalent for autocorrelated features.

**Gemini, please weigh in on:**
- Is the structural finding correct? Are we missing a fix that would let rolling-feature PSI cleanly clear < 0.10 against a multi-pump reference?
- Of the three remediation options, is one obviously the right call, or is this a "needs experimentation in a follow-up session" punt?
- Is the regression test's raw-strict / rolling-soft split a reasonable interim posture, or should we tighten the rolling bound further (or relax it)?

### Q2 — Single-reference vs dual-reference architecture

**Decision in ADR 0008 §1.** Single-reference per Gemini Q5 of the 2026-06-01 review: "dual references add significant architectural and operational complexity without providing a compelling advantage for a portfolio project." `--reference-source=training` produces a separately-named `training_reference_distribution.json` artifact for historical comparison, but it's not consumed by `shared.drift` at load time.

**Gemini sanity-check:** is the single-reference choice still right given the rolling-features finding above? Could a dual-reference scheme (operational baseline for raw features, training baseline for rolling features?) be the cleaner architectural fix to Q1?

### Q3 — Operational sample: 5 pumps × 1800 ticks vs alternatives

**PO call in the session brief plan-step.** Floor for stable equal-frequency quantiles is ~2 000 samples (200/bin × 10 bins); 5 × 1800 = 9 000 clears that with ~4.5× margin. Five pumps average per-pump noise instances; single-pump would bake the reference into one trajectory's noise realisation.

The Q1 measurement shows N doesn't move the rolling-features floor much. **Is 5 pumps the right N, or should we go higher (15 = demo fleet size) to make a future debugging session's "matches what the fleet emits" claim more direct, even though it doesn't help PSI?**

### Q4 — Warm-up skip in `_generate_operational_samples`

The drift session's measurement harness included the `WINDOW_TICKS = 150` warm-up (1800 ticks from tick 0). This session skips warm-up in both the operational reference generation AND the regression test, so both sides describe steady-state distributions.

**Rationale:** the live runtime's PSI window in steady state is fully post-warm-up (warm-up samples roll off after the first hour); matching the reference to steady-state is the operationally honest baseline. Cold-start PSI on the first 30–1800 ticks will spike but that's expected and the dashboard's `last` aggregator (ADR 0007 §5) handles it cleanly.

**Gemini check:** is "reference describes steady-state, cold-start PSI is expected to be noisy" the right semantic, or should the operational reference include warm-up so cold-start and steady-state both compare against the same baseline?

### Q5 — Two profile dicts in `model/train.py`

ADR 0008 §1 locks in `_training_profiles` (stretched DEGRADING) and `_operational_profiles` (DEFAULT_PROFILES verbatim) as two distinct, intentionally asymmetric helpers. The asymmetry is the core of ADR 0008. Tests pin both shapes.

**Gemini check:** is the asymmetry sufficiently visible to a future reader, or does the model session's `model/train.py` need a more prominent "TWO PROFILE DICTS, BY DESIGN" banner near the top? (Currently in the module docstring, but easy to miss in a > 700-line file.)

### Q6 — Sandbox-runtime corpus (12 pumps) committed; PO regenerates at 30

The committed `model.pkl` was generated in the sandbox with `--n-pumps 12 --n-test-pumps 3` because the bash 45 s per-call timeout blocks the full 30-pump run (which takes ~80 s of training-data generation). AUC stays at 0.997 (well above 0.85). Same `v0.1.0-seed-0` version tag. PO regenerates at 30 pumps natively on Windows.

**Gemini check:** is "sandbox-runtime artifact ships as proof-of-correctness; PO regenerates production artifact at full corpus natively" an acceptable workflow, or should the session log call this out more prominently as a "do not consume this committed model.pkl in production" warning?

### Q7 — Old `reference_distribution.json` not deleted in this session

The FUSE mount blocks `rm` on existing files (per `[[ml-obs-pipeline-git-on-windows]]`). The session log's commit-message draft includes `git rm model/artifacts/reference_distribution.json` for the PO to run natively.

**Gemini check:** is this a hard blocker (the artifact must be deleted before the ADR is "shipped") or is the "PO removes via `git rm` in the same commit" workflow acceptable?

## Disposition workflow

PO + Gemini answers feed into ADR 0008's §Addendum. Code/test changes (if any) land in a follow-up session pinned to the disposition table. The rolling-features autocorrelation finding (Q1) is the most likely to drive a follow-up session.
