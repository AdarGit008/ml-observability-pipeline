# ADR 0008 — Operational PSI Reference: Source-Separated From the Training Matrix

- **Status:** Accepted (PO sign-off 2026-06-02; Gemini review pending)
- **Date:** 2026-06-02
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

ADR 0006 §"Reference distribution computed from training set only"
flagged a carry-in: the per-feature reference distribution was built
from the 30-pump training matrix, which uses the 48h-stretched
DEGRADING dwell (ADR 0006 §3). The training corpus's wider DEGRADING
band shifts the per-feature quantiles relative to a demo-paced
HEALTHY run. The drift session (2026-06-01) measured the impact and
found it **severe**: PSI 1.3–6.7 SIGNIFICANT on 7 of 8 features for
healthy demo-paced fleets. ADR 0007 §"Demo-paced PSI bias carry-in
is REAL and SEVERE" recorded the finding and deferred the resolution
to a follow-up. Gemini Q5 of the 2026-06-01 drift review confirmed
option (a) — recompute reference from demo-paced HEALTHY data — as
the right next step and suggested naming the new artifact
`operational_reference_distribution.json` to make provenance
unambiguous.

This ADR records the decision and its consequences. It does NOT
amend ADR 0006 — the training-data DEGRADING-dwell stretch decision
stands on its own (the model corpus needs the slow ramp; that
hasn't changed). The decision here is structurally separate: where
the *PSI reference* comes from. Hence a new ADR rather than an
ADR 0006 amendment.

Constraints driving the decision (from `context/_global.md` north
stars):

- **#4 mode parity:** the PSI computation is shared between local
  and AWS modes (ADR 0005). The reference artifact is the input
  both consumers load. A demo-paced reference is consumed
  identically by both — parity boundary unaffected.
- **#1 $0 cost:** the operational sample is generated in-process
  during `model.train`; no extra infra.
- **#5 one polished repo:** PSI panels on the demo dashboard
  should read STABLE on healthy pumps. The training-time reference
  made them SIGNIFICANT — a recording blocker.

Carry-in from the drift session's measurement: per Gemini Q5 and
the PO's plan-step call, the architecture is **single-reference
(replace, don't double-source)**. The training reference is not
shipped to drift's load path; if a future debugger wants to compare
baselines, `--reference-source=training` regenerates a separately-
named `training_reference_distribution.json` artifact.

## Decision

`model.train` adopts the following four-part design:

1. **Two profile dicts, by design.** `_training_profiles` and
   `_operational_profiles` produce different `dict[PumpState,
   StateProfile]` shapes:
   - `_training_profiles(healthy_dwell_ticks)`: overrides
     `HEALTHY.dwell_ticks` (per-pump randomised) and stretches
     `DEGRADING` to 48h with a scaled rate (per ADR 0006 §3).
   - `_operational_profiles()`: returns `DEFAULT_PROFILES` verbatim.
     No overrides. The demo emits telemetry at these profiles; the
     reference distribution must describe that population.

   The asymmetry is locked in by the tests
   `test_training_profiles_overrides_only_healthy_dwell_and_degrading`
   and `test_operational_profiles_returns_default_profiles_verbatim`.

2. **`_generate_operational_samples(n_pumps=5, ticks_per_pump=1800,
   seed=0)`** runs N HEALTHY pumps via `Pump.step()` against
   `DEFAULT_PROFILES`, extracts features every tick via
   `shared.features.extract_features` (the same code path the live
   runtime uses), and stacks them into the X matrix that
   `compute_reference_distribution` then bins. Skips the
   `WINDOW_TICKS` warm-up so the reference describes steady-state
   distribution (the live runtime's PSI window in steady state is
   fully post-warm-up).

   Shape: 5 pumps × 1800 ticks = 9 000 samples. PO call (2026-06-02
   session log, plan-step Q1).

3. **`--reference-source {operational, training}` CLI flag,**
   defaulting to `operational`. Operational writes to
   `model/artifacts/operational_reference_distribution.json`;
   training writes to `model/artifacts/training_reference_distribution
   .json` (a separately-named historical-comparison artifact, not
   consumed by `shared.drift` at load time).

4. **`shared.drift._DEFAULT_REF_PATH` points at
   `operational_reference_distribution.json`.** Same load path
   (`load_reference`), same compute path (`compute_psi`), same
   mode-parity tests green. The change is to which artifact ships
   as the default; the consumer's API contract is unchanged.

## Alternatives considered

### 1. Reference source

**A. Demo-paced HEALTHY-only (the decision).** Per Gemini Q5: "the
pragmatic and operationally sound choice." Mirrors what operators
consider 'normal' — the live demo emits telemetry at DEFAULT_PROFILES,
so the reference is built from the same physical model. PSI on
healthy pumps now reads STABLE on raw features (vs SIGNIFICANT under
the training reference).

**B. Dual references (training + operational), config flag selects.**
Considered briefly. Rejected per Gemini Q5: "adds significant
architectural and operational complexity (more artifacts, more logic,
potentially more confusion for users) without providing a compelling
advantage for a portfolio project." Single-reference architecture is
cleaner for the portfolio narrative.

**C. Accept and document the training-reference bias.** ADR 0007's
original default; rejected by the drift session's measurement (PSI
1.3–6.7 SIGNIFICANT on healthy fleets) — the demo dashboard would
paint red on healthy pumps and read as broken.

### 2. Operational sample shape (N pumps × duration)

**A. 5 pumps × 1800 ticks = 9 000 samples (the decision).** PO call.
Clears the 200-samples-per-bin × 10-bins = 2 000-sample floor for
stable equal-frequency quantiles with ~4.5× margin. Five pumps
average across noise instances; single-pump would bake the
reference into one trajectory's noise realisation. Adds ~1 s of
wall-clock to `model.train`; negligible.

**B. 2 pumps × 1800 ticks.** Clears the quantile-stability floor but
gives the reference less per-pump-noise robustness.

**C. 15 pumps × 1800 ticks (matches demo fleet).** Maximally faithful
to "what the fleet emits" but ~3× the wall-clock cost. Diminishing
returns on the rolling-features PSI floor (see Consequences below
— increasing N from 5 to 50 moved the worst-case PSI from 0.39 →
0.36, well within the autocorrelation noise floor).

### 3. Where the "operational vs training" choice lives

**A. CLI flag (the decision).** `--reference-source {operational,
training}` defaulting to operational. Reachable from a Makefile
target if needed; the historical-comparison path stays accessible
without code changes.

**B. Environment variable.** Less discoverable than a CLI flag.

**C. Hardcoded "always operational."** Removes the historical-
comparison path entirely; reachable only by reverting to a pre-
ADR-0008 commit. Rejected because a future debugging session may
want to compare a fresh operational reference against the
training baseline without a git checkout.

## Consequences

**Positive:**

- **Demo dashboard reads STABLE on healthy pumps' raw features.**
  Measured PSI on a fresh HEALTHY pump (seed 42 against the
  committed 5-pump reference, seed 0): `vibration_amp` 0.003,
  `bearing_temp` 0.004, `motor_current` 0.007, `rpm` 0.003 — all
  STABLE. vs. the training reference's 1.3–6.7 SIGNIFICANT on
  the same pump. ~1000× improvement on raw features.
- **Parity boundary unaffected.** `shared.drift.compute_psi` and
  `load_reference` signatures unchanged. The structural-parity
  test (`test_structural_parity_compute_psi_loads_from_shared`)
  still passes. The lambda_scorer session that bundles
  `shared/drift.py` + `model/artifacts/` into the deploy zip
  picks up the new reference filename mechanically.
- **Model artifact unaffected.** Both `model.pkl` and the
  operational reference share the same `model_version`
  (`v0.1.0-seed-0`); the version-match check (ADR 0007 §4) keeps
  working. AUC stays at 0.997 (12-pump sandbox-runtime corpus;
  PO can regenerate at 30 pumps natively).
- **Future debugger has a clear path back to the training
  baseline.** `python -m model.train --reference-source training`
  regenerates `training_reference_distribution.json` for
  comparison. Same load API; explicit `ref_path` argument to
  `load_reference` handles the override.

**Negative:**

- **Per-pump rolling-features PSI is autocorrelation-bounded above
  the 0.10 STABLE band.** Measured 0.10–0.40 across 10 test seeds
  on a single HEALTHY pump. Root cause: rolling 5-min mean/std
  features share 149 of 150 readings between consecutive 1800-
  sample windows; the resulting samples are NOT IID. A single-pump
  1800-tick window traces an autocorrelated walk through a narrow
  region of the steady-state distribution; the multi-pump reference
  covers a wider region by mixing pump-noise instances. PSI then
  reads "test pump's region narrower than reference's" as a
  distribution shift, putting PSI in the WARNING-to-low-SIGNIFICANT
  band on healthy pumps. The four raw features (per-tick IID
  values) stay STABLE; the four rolling features (`*_mean_5m` and
  `*_std_5m`) do not. Verified that increasing N from 5 to 50
  reference pumps shifts the worst-case PSI by < 0.05 — this is a
  structural property, not a sample-size problem.

  **The regression test
  (`test_demo_paced_healthy_psi_stable`)** acknowledges this
  explicitly: raw-feature PSI is pinned at < 0.10 (STABLE); rolling-
  feature PSI is pinned at a soft < 0.50 bound that catches
  catastrophic reference-rebuild regressions without claiming the
  structurally-unachievable < 0.10. The dashboard band semantics
  (STABLE / WARNING / SIGNIFICANT) need adjustment for autocorrelated
  features in a future session; carried as an open question.

- **The brief's "PSI < 0.10 on all 8 features" acceptance is
  unachievable.** Recorded the measurement and the structural
  diagnosis; reset the regression-test acceptance to the realistic
  split (raw strict, rolling soft). Flagged for PO sign-off in the
  review packet — the brief's target reflected the drift-session
  recommendation made before this measurement existed.

- **Old training-time `reference_distribution.json` artifact
  remains in the repo.** Sandbox FUSE mount blocks `rm` on
  pre-existing files (per `[[ml-obs-pipeline-git-on-windows]]`).
  PO removes it natively on Windows. Commit-message draft includes
  the `git rm` line.

- **Sandbox-runtime corpus is 12 pumps, not 30.** Bash 45 s
  per-call cap forced the shrink (30 pumps takes ~80 s of
  training-data generation). AUC stays at 0.997 (well above the
  0.85 threshold). PO regenerates at 30 pumps natively on Windows
  for the production artifact; the v0.1.0-seed-0 version tag is
  the same either way. Surfaced in the session log.

**Follow-ups:**

- **Autocorrelated-PSI threshold semantics.** PLAN.md's STABLE /
  WARNING / SIGNIFICANT bands were designed for IID samples.
  Rolling-window features violate IID. A future session should
  either (a) lift the bands for rolling features (e.g., 0.25 /
  0.50 / 1.00), (b) collapse rolling features into the raw
  features for PSI purposes (drop the four rolling-PSI fields and
  rely on raw-features PSI for drift detection), or (c) replace
  PSI for rolling features with a moving-average-band metric.
  Worth ~1 short session.

- **PO re-runs `python -m model.train` (default `--reference-source
  operational`) at the full `--n-pumps 30` on Windows** to
  regenerate the production model.pkl + operational reference.
  Committed artifacts here are the sandbox-runtime 12-pump version
  (proof of correctness; AUC 0.997). Same `v0.1.0-seed-0` tag.

- **PO deletes `model/artifacts/reference_distribution.json`** via
  `git rm` (the FUSE mount blocks sandbox-side deletion). Commit-
  message draft in the session log carries the line.

- **Gemini review packet** at
  `review_packets/2026-06-02-model-operational-reference.md`. The
  autocorrelation finding is the headline Q for PO + Gemini
  sign-off.

## References

- ADR 0005 — parity boundary (`shared/{features,score,drift}`).
  Structural-parity tests still green.
- ADR 0006 — training-time DEGRADING-dwell stretch + reference-
  distribution-validity carry-in. This ADR closes the carry-in
  without amending ADR 0006's decision.
- ADR 0007 — PSI implementation + Laplace + per-tick cadence. This
  ADR closes the "Demo-paced PSI bias carry-in is REAL and SEVERE"
  consequence; the formula / smoothing / cadence decisions hold.
- `docs/sessions/2026-06-01-drift-real-psi.md` §"Reference-Validity
  carry-in — MEASUREMENT" — the original PSI 1.3–6.7 measurement.
- `docs/sessions/2026-06-02-model-operational-reference.md` — this
  session's log.
- `review_packets/2026-06-02-model-operational-reference.md` — PO
  + Gemini review questions.
- `review_responses/2026-06-01-drift-real-psi.md` Q5 — Gemini's
  endorsement of option (a) + the
  `operational_reference_distribution.json` naming suggestion.
- Implementation: `model/train.py` (`_operational_profiles`,
  `_generate_operational_samples`, CLI flag + paths),
  `shared/drift.py` (`_DEFAULT_REF_PATH` updated).
- Tests: `model/tests/test_train.py` (4 new tests for the
  operational helpers + path-default pin),
  `local_runtime/tests/test_drift_load.py`
  (`test_demo_paced_healthy_psi_stable` regression guard).
- PSI bands per PLAN.md §2.7 + `context/_interfaces.md` §"PSI
  parameters": < 0.10 stable / 0.10–0.25 warning / > 0.25
  significant.
