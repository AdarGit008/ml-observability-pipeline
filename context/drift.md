# drift

## Purpose
Shared PSI (Population Stability Index) implementation. Used identically by `lambda_scorer` (per-pump, hot path), `local_runtime` (per-pump in local mode), and the EventBridge-scheduled fleet-PSI Lambda (every 5 minutes, future session).

This is the single most architecturally important shared module: if local mode and AWS mode disagree about drift, the project's mode-parity claim collapses.

## Current state
- ✅ **Shipped 2026-06-01.** Real PSI via `np.histogram` + Laplace add-α smoothing (α = 1.0). Lazy + module-cached reference load, with shape + feature_names + model/reference `model_version` validation.
- ✅ **Operational reference shipped 2026-06-02 (ADR 0008).** `_DEFAULT_REF_PATH` points at `model/artifacts/operational_reference_distribution.json`, built from `DEFAULT_PROFILES` HEALTHY-only data (5 pumps × 1800 post-warm-up ticks). The training-time `reference_distribution.json` is retired; `--reference-source training` regenerates a separately-named historical-comparison artifact. Raw-feature PSI now reads STABLE on healthy demo fleets (vs SIGNIFICANT under the training reference).
- ✅ **PSI surface shrunk to four raw features 2026-06-03 (ADR 0009).** `compute_psi` iterates `shared.features.PSI_FEATURE_NAMES` (the four raw signals) instead of `FEATURE_NAMES` (all eight). The four rolling features are scorer inputs only — their 149/150-overlap windows violate PSI's IID assumption and produced 0.10–0.40 autocorrelation noise on healthy fleets (ADR 0008 §Negative measurement). InfluxDB schema goes 17 → 13 fields per point. `load_reference` validates the on-disk reference's `feature_names` against `PSI_FEATURE_NAMES`; a pre-ADR-0009 reference (8-element list) is rejected with a clear DriftError pointing at the rebuild command.
- Reproducible: tests in `local_runtime/tests/test_shared_stubs.py` + `local_runtime/tests/test_drift_load.py` (incl. `test_demo_paced_healthy_psi_stable` regression guard — flipped to a hard four-key assertion + STABLE bound on every key) + `local_runtime/tests/test_features.py::test_psi_feature_names_is_subset_of_feature_names` (the asymmetry pin).
- ADR 0007 carries the formula, smoothing α, `reference=None` semantics, model/reference version match, and per-tick write cadence decisions. ADR 0008 carries the operational-vs-training reference-source split. ADR 0009 carries the PSI surface ≠ scorer feature set asymmetry.
- Gemini-approved 2026-06-03 on ADR 0009; ADR 0008 review still pending.

## Interfaces (in / out)
- **In:** Iterable of feature dicts spanning the rolling 1-hour PSI window (1800 dicts at 2s tick). Each dict carries all 8 `FEATURE_NAMES` keys (that's what `extract_features` produces); `compute_psi` looks up only the 4 `PSI_FEATURE_NAMES` keys. Reference distribution dict from `load_reference()` (default path `model/artifacts/operational_reference_distribution.json`).
- **Out:** Per-feature PSI scalar (`dict[str, float]` keyed by `shared.features.PSI_FEATURE_NAMES` — 4 keys per ADR 0009; was 8 pre-ADR-0009). Threshold classification (< 0.10 stable / 0.10–0.25 warning / > 0.25 significant) is the alerts layer's call and applies uniformly across all four surviving channels (no per-feature band branching).
- **Raises:** `shared.drift.DriftError` (sibling of `shared.score.ScoreError`) on missing/malformed reference, feature_names mismatch (incl. pre-ADR-0009 8-element shape), or model/reference `model_version` desync.

## Invariants
- Pure Python + `numpy` only on the hot path. `joblib` is lazy-imported inside the model/reference version-check branch — a "drift without sklearn" environment (e.g., a fleet-PSI Lambda layer that ships only the reference) is still importable.
- No `pandas` (Lambda cold-start cost).
- Deterministic for the same inputs across both runtimes — pinned by `test_compute_psi_identical_distribution_is_near_zero` against a synthetic reference.
- Handles zero-width bins (adjacent-equal `bin_edges`, the model session's nextafter-nudge case for near-constant features) without div-by-zero — pinned by `test_compute_psi_constant_bin_edges_no_div_by_zero`.
- **PSI surface ⊂ scorer input set, strict subset (ADR 0009).** `PSI_FEATURE_NAMES` lives in `shared/features.py` next to `FEATURE_NAMES`; any change that erases the asymmetry (or grows PSI past the four raw signals) has to update ADR 0009 and the structural test in `test_features.py`.

## Parameters
- **PSI surface:** four raw features per ADR 0009 — `vibration_amp`, `bearing_temp`, `motor_current`, `rpm`. Rolling features (`*_mean_5m`, `*_std_5m`) are scorer inputs only.
- **Bin count:** 10 equal-frequency — pinned by the reference distribution shipped by the model session.
- **Smoothing:** Laplace add-α with α = 1.0 (`shared.drift.LAPLACE_ALPHA`). ADR 0007 §2 trade-off.
- **PSI window:** 1-hour rolling per pump = 1800 samples at the default 2s tick (`LocalRuntimeConfig.psi_window_samples`).
- **Compute cadence:** every Nth tick, default N = 30 = once per minute at 2s tick (`LocalRuntimeConfig.psi_period_ticks`). ADR 0007 §5 resolves ADR 0005 §Addendum Q3.

## Open questions
- ~~**Autocorrelated-PSI threshold semantics.**~~ **CLOSED 2026-06-03 by ADR 0009.** The PLAN.md STABLE / WARNING / SIGNIFICANT bands are designed for IID samples; the four rolling features violated that assumption and produced 0.10–0.40 autocorrelation noise on healthy fleets. ADR 0009 dropped them from the PSI surface entirely (rolling features stay as scorer inputs where their autocorrelation is a feature, not a bug). Remediation option (b) from ADR 0008 §Follow-ups.
- **Dashboards session: four PSI panels, not eight (ADR 0009).** The dashboard panel set is `psi_vibration_amp`, `psi_bearing_temp`, `psi_motor_current`, `psi_rpm`. PLAN.md §2.7 bands apply uniformly to each. The InfluxDB schema (ADR 0005 §3 with the ADR 0009 amendment) is what panels query against.
- **Fleet-PSI EventBridge Lambda (PLAN.md §2.7).** Same `compute_psi`, aggregated 5-minute fleet window across all pumps. Out of scope here.
- **Warm-up gate.** On a fresh pump, the feature-history deque is short for the first 30 ticks. With the default cadence the first compute happens at tick 30 when the window is 30 samples deep — meaningful enough for demo. A future polish session could add an explicit `min_samples_for_psi` gate that returns None until N samples accumulate.

## Related ADRs
- **ADR 0009** — PSI surface ≠ scorer feature set. Drops the four rolling features from PSI (still scorer inputs). Closes the autocorrelated-PSI-threshold-semantics open question. **Accepted** 2026-06-03 (Gemini-approved 2026-06-03).
- **ADR 0008** — operational PSI reference (DEFAULT_PROFILES HEALTHY-only). Closes the ADR 0007 Reference-Validity carry-in. **Accepted** 2026-06-02 (Gemini review pending).
- **ADR 0007** — PSI implementation, Laplace α, `reference=None`, version match, per-tick cadence. **Accepted**. Formula / smoothing / cadence decisions hold under ADR 0008 and ADR 0009; only the reference baseline (ADR 0008) and the iterated feature set (ADR 0009) changed.
- ADR 0005 — parity boundary (`shared/{features,score,drift}`). Structural parity tests still green. §3's InfluxDB schema line carries an ADR 0009 amendment pointer (17 → 13 fields per point).
- ADR 0006 — training-time DEGRADING-dwell stretch. Stands as-is; the model corpus still uses the stretched DEGRADING. ADR 0008 is the architecturally-separate "reference uses DEFAULT_PROFILES" decision; ADR 0009 is the architecturally-separate "PSI surface ≠ scorer feature set" decision.
- ADR 0002 — simulator physics (RPM coupling, noise envelopes). Drives the demo-paced HEALTHY baseline that ADR 0008's operational reference is sampled from.
