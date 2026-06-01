# model

> **Carry-in from ADR 0002 (added 2026-05-25 by the simulator config-yaml session):**
> `bearing_temp` is **not monotonic** in `degradation`. The ADR-0002 RPM coupling
> (`rpm = setpoint * (1 - degradation) + N(0, 5 + 15 * d)`) means RPM drops
> faster than the direct `degradation * 15` term rises, so bearing temp peaks
> somewhere in DEGRADING territory and falls again through FAILING and FAILED.
> Feature engineering for this model must not pre-suppose monotonicity in
> bearing temp. The shipped model (2026-06-01) addresses this by including
> `bearing_temp_std_5m` as the wear-trend feature — the noise envelope
> grows monotonically through DEGRADING via the `0.02 * rpm` coupling
> even when the mean wobbles. `vibration_amp` stays the primary
> monotonic "wear" signal. See `docs/adr/0002-rpm-coupled-to-degradation.md`
> and ADR 0006 §"Feature engineering rationale".

## Purpose
Predicts P(pump fails within 48h) from 4 raw signals + 4 rolling features. Trained on synthetic data from the simulator in fast-forward.

## Current state
- ✅ Shipped 2026-06-01. Held-out AUC = **0.997** on 3 pumps held out from a 12-pump corpus (sandbox-runtime regen 2026-06-02 and again 2026-06-03 after ADR 0009); 0.998 originally on 6/30 split. AUC well above 0.85 acceptance.
- Reproducible: `python -m model.train` (~30 s on a sandbox at 12 pumps, ~75 s natively at 30 pumps; `--reference-source operational` is the default per ADR 0008).
- Artifacts committed: `model/artifacts/model.pkl` (~290 KB), `model/artifacts/operational_reference_distribution.json` (~2.2 KB after ADR 0009 — half the pre-ADR-0009 size because the surface shrank from 8 to 4 features). The old `reference_distribution.json` is retired (PO removes via `git rm`).
- ADR 0006 carries the model family / feature engineering / DEGRADING-dwell-stretch rationale (still stands).
- ADR 0008 carries the operational reference source-separation (DEFAULT_PROFILES HEALTHY-only, 5 pumps × 1800 post-warm-up ticks).
- ADR 0009 carries the PSI surface ≠ scorer feature set asymmetry: model bundle's `feature_names` is `FEATURE_NAMES` (8); reference JSON's `feature_names` is `PSI_FEATURE_NAMES` (4). `compute_reference_distribution` slices the 8-column X matrix down to the 4-column PSI surface before binning.
- Gemini review 2026-06-01 cleared the model design; 3 of 7 points drove changes (Q3 doc note, Q4 footprint measurement, Q6 joblib import position). Disposition table in `review_packets/2026-06-01-model-train-histgbt.md`; long-form in ADR 0006 §Addendum 2026-06-01.
- Gemini-approved 2026-06-03 on ADR 0009; ADR 0008 review still pending.

## AUC honesty (per Gemini Q3, 2026-06-01)
The 0.998 held-out AUC is a property of the **simulator**, not a forecast of real-world performance. The synthetic corpus has deterministic physics + bounded Gaussian noise + identical degradation trajectories across pumps; a HistGBT will separate the DEGRADING-phase signature near-perfectly on data shaped like that. A real bearing-failure dataset (NASA IMS, Case Western Reserve, plant SCADA history) would have asymmetric noise envelopes, sensor dropouts, operating-regime shifts, and survivor bias the synthetic corpus does not. README framing must call this out — the metric proves the **pipeline works end-to-end** on the simulator's distribution, not that the model would generalise unmodified to a real plant.

## Lambda deploy footprint (measured 2026-06-01, per Gemini Q4)
Stripped (no `__pycache__`, no `tests/`) sizes from a clean Linux Python 3.10 env:

| Component | Size |
|---|---|
| scikit-learn | 26.7 MB |
| numpy | 22.7 MB |
| scipy | 73.0 MB |
| joblib | 0.9 MB |
| threadpoolctl | ~0.1 MB |
| `shared/` + `lambda_scorer/` + `model/artifacts/` | ~0.5 MB |
| **Estimated unzipped deploy** | **~124 MB** |

Lambda's unzipped limit is 250 MB. ~50 % headroom. Bundling (HANDOFF.md §6 Q3 default) stays viable for the lambda_scorer session; Lambda Layer or S3 cold-load remain available without re-opening this ADR if the build script's actual zip diverges.

## Interfaces (in / out)
- **In:** 8-feature dict matching `shared.features.FEATURE_NAMES` (4 raw + 5-min rolling mean/std of vibration and bearing_temp).
- **Out:** scalar in [0, 1] via `shared.score.score(features)`.
- **Artifacts:**
  - `model/artifacts/model.pkl` — joblib bundle dict `{model_version, feature_names, auc_held_out, classifier}`. `feature_names` is `FEATURE_NAMES` (8 — the scorer input contract).
  - `model/artifacts/operational_reference_distribution.json` (ADR 0008 + ADR 0009) — per-PSI-feature equal-frequency 10-bin histograms with `bin_edges` + `bin_counts`, plus the same `model_version` so a desync is detectable. Built from `DEFAULT_PROFILES` HEALTHY-only data (5 pumps × 1800 post-warm-up ticks = 9 000 samples). `feature_names` and the `features` map cover `PSI_FEATURE_NAMES` (4 entries per ADR 0009 — rolling features are scorer inputs only, never PSI surface members). Consumed by `shared.drift.load_reference` + `compute_psi`.
  - `model/artifacts/training_reference_distribution.json` (optional, ADR 0008) — produced when `--reference-source training` is invoked; built from the model training matrix (pre-ADR-0008 behaviour). Carries the same 4-feature surface (ADR 0009) so the load API is identical. Not consumed by `shared.drift` at load time; reachable via `load_reference(ref_path=...)` for historical-comparison runs.

## Acceptance criteria
- AUC ≥ 0.85 on held-out pumps. **Met:** 0.997–0.998 (seed 0). The `--min-auc` CLI flag wires the threshold into the training script's exit code so a regression fails the run loudly. (See "AUC honesty" above for what this number does and does not mean.)

## Training data contract
- 30 simulated pumps × variable per-pump duration (each pump short-circuits at its FAILED transition; MAX_TICKS_PER_PUMP cap = 300 000 = ~6.9 days). Sandbox-runtime regen 2026-06-02 and 2026-06-03 used 12 pumps (bash 45 s cap); PO regenerates at 30 pumps natively on Windows. Same `v0.1.0-seed-0` tag either way.
- Sampling cadence: 1 row per 30 ticks (≈ 1 row/min). Total ≈ 230 k rows; train ≈ 188 k, held-out ≈ 44 k.
- Label: `y = 1` iff pump reaches `PumpState.FAILED` within next 86 400 ticks (= 48 h at 2 s/tick); else 0.
- Training-time **DEGRADING dwell override** = 86 400 ticks (vs. `DEFAULT_PROFILES`' 200). Justification + locality in ADR 0006 §3. Simulator demo path unchanged.
- Determinism: `--seed` controls both `np.random.default_rng` (dwell sampling) and per-pump `random.Random(per_pump_seed)`. Same seed → identical artifacts.

## Open questions
- **Feature versioning.** Both artifacts carry `model_version = "v0.1.0-seed-<n>"` today. Lambda_scorer cold-start should refuse a mismatched (model, reference) pair — wiring is the lambda_scorer session's job. Pre-1.0 the version tag is just the seed; once we have git access from training we'll move to a git-short-sha + date scheme.
- ~~**Reference distribution validity at demo time.**~~ **CLOSED 2026-06-02 by ADR 0008.** Drift session 2026-06-01 measured the carry-in (PSI 1.3–6.7 SIGNIFICANT on 7/8 features for healthy demo fleets under the training reference); ADR 0008 separates the reference source from the model corpus and ships an operational reference built from `DEFAULT_PROFILES` HEALTHY-only data. Raw-feature PSI now reads STABLE.
- ~~**Autocorrelated rolling-feature PSI.**~~ **CLOSED 2026-06-03 by ADR 0009.** Rolling features dropped from the PSI surface entirely; they remain scorer inputs where their autocorrelation is a feature (temporal smoothing), not a bug (broken IID for drift detection).
- **Logistic-regression / linear baseline for the README.** Not blocking, but a "does the HistGBT actually beat a linear model" sentence would strengthen the portfolio story per Gemini Q1. Track in a future model-tuning session.

## Related ADRs
- ADR 0005 — parity boundary (`shared/{features,score,drift}`). `shared.score.score` is rewritten inside the boundary; structural parity tests still pass. §3 InfluxDB schema line carries an ADR 0009 amendment (17 → 13 fields per point).
- ADR 0006 — model family + feature engineering + training-time DEGRADING-dwell stretch. **Accepted** (Gemini review folded; PO sign-off pending). Still stands; ADR 0008 + ADR 0009 are architecturally-separate decisions.
- ADR 0008 — operational PSI reference source-separated from the training corpus (DEFAULT_PROFILES HEALTHY-only). **Accepted** 2026-06-02 (Gemini review pending).
- ADR 0009 — PSI surface ≠ scorer feature set (drops the 4 rolling features from PSI). **Accepted** 2026-06-03 (Gemini-approved 2026-06-03). Closes the autocorrelated-PSI carry-in from ADR 0008.
- ADR 0002 — bearing_temp non-monotonicity carry-in. Honoured by `bearing_temp_std_5m` as the wear feature.
