# 2026-06-01 — drift: real PSI implementation + per-tick cadence

## Component
`shared.drift` (parity boundary); `local_runtime.service` (consumer); `local_runtime.influx_writer` (consumer); `local_runtime.config` (constants/properties).

## Intent
Ship real `shared.drift.compute_psi` per PLAN.md §2.7 — replace the
sentinel stub with the binned, Laplace-smoothed PSI computation against
the training-time `reference_distribution.json`. Resolve the per-tick
write cadence carried over from ADR 0005 §Addendum Q3. Surface and
measure the demo-paced PSI bias carried in from ADR 0006 §"Reference
distribution validity at demo time."

## What changed

### Code
- `shared/drift.py` — full rewrite. Stub removed; real implementation
  per PLAN.md §2.7: `np.histogram` against per-feature `bin_edges` →
  Laplace add-α smoothing on counts → percentage normalization →
  Σ (a%-e%) · ln(a%/e%). Module-cached lazy reference load with
  validation (shape, feature_names, model/reference version match).
  New `DriftError` (sibling of `ScoreError`). `joblib` lazy-imported
  inside the version-check branch to keep the drift module's
  dependency surface minimal per `context/drift.md` invariants.
  `_reset_reference_cache()` test-only helper.
- `local_runtime/config.py` — two new module constants
  (`PSI_WINDOW_SECONDS = 3600.0`, `PSI_COMPUTE_EVERY_SECONDS = 60.0`)
  + two new derived properties on `LocalRuntimeConfig`
  (`psi_window_samples`, `psi_period_ticks`). Mirrors the existing
  `FEATURE_WINDOW_SECONDS` / `window_samples` pattern.
- `local_runtime/service.py` — `ScorerService` grew a per-pump
  feature-history deque (1800 samples at default tick) and a per-pump
  tick counter. PSI fires on `tick % psi_period_ticks == 0`; other
  ticks emit `psi=None`. New `psi_period_ticks` ctor parameter (tests
  inject 1 for every-tick PSI). New `feature_history_size(pump_id)`
  inspector for smoke checks.
- `local_runtime/influx_writer.py` — `ScoredRow.psi` typing widened
  to `Optional[Mapping[str, float]]`. `build_point` skips `psi_*`
  fields entirely when `row.psi is None`, so InfluxDB stores nulls
  and Grafana's `last` aggregator surfaces the most recent computed
  value (no snap-to-zero between computes).

### Tests
- `local_runtime/tests/test_shared_stubs.py` — removed
  `test_compute_psi_values_are_floats`,
  `test_compute_psi_accepts_empty_window`,
  `test_compute_psi_sentinels_span_warning_and_stable` (stub-pinning).
  Added `test_compute_psi_identical_distribution_is_near_zero`,
  `test_compute_psi_shifted_distribution_crosses_warning_then_significant`,
  `test_compute_psi_constant_bin_edges_no_div_by_zero` (value-driven).
  Kept `test_compute_psi_returns_dict_with_all_feature_keys` (interface
  invariant). Score-side tests unchanged.
- `local_runtime/tests/test_drift_load.py` — **new file**. Pins the
  reference-load failure paths: missing file, malformed JSON, missing
  `features` key, `feature_names` mismatch, `model.pkl` absent (skip),
  `model_version` mismatch (raise), version match (pass),
  `reference=None` lazy-load works, cache reset clears state.
- `local_runtime/tests/test_service.py` — updated
  `test_service_handle_psi_dict_present_when_compute_fires` to inject
  `psi_period_ticks=1`. Added 4 new cadence tests:
  `test_service_handle_psi_is_none_on_non_compute_ticks`,
  `test_service_handle_psi_period_per_pump_isolated`,
  `test_service_feature_history_size_tracks_per_pump`,
  `test_service_psi_period_ticks_default_from_config`.
- `local_runtime/tests/test_influx_writer.py` — added
  `test_build_point_psi_none_omits_psi_fields` and
  `test_build_point_field_count_without_psi`. The 17-field count test
  is renamed `test_build_point_field_count_with_psi` to disambiguate
  from the new 9-field non-compute-tick case. Test helper `_row`
  switched to a sentinel default so callers can pass `psi=None`
  explicitly.
- `local_runtime/tests/test_config.py` — added
  `test_psi_window_samples_derived_from_tick`,
  `test_psi_period_ticks_derived_from_tick`,
  `test_psi_window_and_period_at_non_default_tick`. The existing
  `test_unknown_top_level_key_raises` and `test_missing_top_level_key_raises`
  still pass on the substring match.

### Documentation
- `docs/adr/0007-psi-implementation-and-cadence.md` — **new ADR**.
  Five decisions: formula/binning, Laplace α = 1.0, `reference=None`
  semantics, model/reference version match, per-tick cadence.
- `context/drift.md` — status moved from "Not started" → "Shipped
  2026-06-01"; ADR 0007 referenced; the Reference-Validity finding
  surfaced in §"Open questions."
- `review_packets/2026-06-01-drift-real-psi.md` — review packet.

## Trade-offs surfaced

1. **`reference=None` → lazy-load vs. raise loudly.** Picked lazy-
   load with module-cache so the existing `service.py` call site
   keeps working without orchestration changes. Loud `DriftError` on
   missing/malformed reference — silent zero-PSI fallback would mask
   broken deployments. Tests inject synthetic references via the
   positional `reference=` parameter.
2. **Laplace α = 1.0 vs. ε-style.** Picked Laplace because it gives
   the smoothing a Bayesian interpretation ("one phantom observation
   per bin") and because demo-fragility under ε-style smoothing is
   real — a single empty bin against a 10%-expected bin would push
   PSI well past 0.25 even on a healthy fleet.
3. **Per-tick PSI cadence.** Picked "every Nth tick" (default 30 =
   once per minute at 2s tick) over both "every tick" (CPU waste) and
   "separate measurement" (schema churn). Schema unchanged per
   ADR 0005's "either is compatible." Non-compute ticks carry `psi=
   None` and the InfluxDB writer omits the fields entirely.
4. **Service.py scope.** Brief allowed splitting service.py off into
   a follow-up; included this session because the DoD "measurements
   on demo-paced telemetry for the Reference-Validity carry-in"
   becomes uncomputable until service.py picks up the new cadence
   shape. +50 LOC; tests grew by 4 in test_service.py.
5. **PSI on a 1-element window is degenerate but tolerated.** Service
   warm-up sees the first call with a single feature dict (under the
   default cadence the first call doesn't fire until tick 30 anyway,
   but tests that override to 1 will see this). With Laplace α = 1.0
   the math is finite; the resulting PSI is high (one bin holds 100%
   of mass). Documented in service.py docstring.

## Reference-Validity carry-in — MEASUREMENT

The ADR 0006 §"Reference distribution validity at demo time" carry-in
is real and **severe**.

**Method.** Generated 1800 ticks of synthetic HEALTHY-state telemetry
(degradation = 0) using the ADR 0002 noise model with seed 0:
- `vibration_amp ~ 0.3 + N(0, 0.05)`
- `motor_current ~ 3.5 + N(0, 0.1)`
- `rpm ~ 1800 + N(0, 5)`
- `bearing_temp ~ 22 + 0.02·rpm + N(0, 0.5)`

Ran the full feature pipeline (`extract_features` with 5-minute
rolling stats) tick-by-tick, then computed PSI on the 1800-sample
feature history against the committed training-time reference.

**Result.**

| Feature                  | PSI    | Band        |
|--------------------------|--------|-------------|
| vibration_amp            | 1.4143 | SIGNIFICANT |
| bearing_temp             | 1.3020 | SIGNIFICANT |
| motor_current            | 6.7091 | SIGNIFICANT |
| rpm                      | 1.7358 | SIGNIFICANT |
| vibration_amp_mean_5m    | 1.8103 | SIGNIFICANT |
| vibration_amp_std_5m     | 0.0625 | STABLE      |
| bearing_temp_mean_5m     | 2.0123 | SIGNIFICANT |
| bearing_temp_std_5m      | 0.3432 | SIGNIFICANT |

**7 of 8 features fire SIGNIFICANT** on a healthy fleet at demo pace.
Only `vibration_amp_std_5m` lands in STABLE (the noise envelope
matches between training and demo because the per-tick noise term is
the same).

**Interpretation.** The training-time DEGRADING-dwell stretch (ADR
0006 §3 "Training-data DEGRADING dwell override") shifted the
per-feature quantiles: the training corpus spent ~99% of its samples
inside a slow DEGRADING ramp, so the equal-frequency bins span the
full degradation range. Demo-paced HEALTHY traffic stays in a tiny
sub-region of the bin span — concentrating ~100% of actuals in bins
1–5 of vibration_amp, ~100% in the bin corresponding to motor_current
~ 3.5, etc. The PSI formula then reads this as "the entire
distribution shifted from uniform to concentrated" — which it did,
but only because the training distribution was stretched for model
purposes, not because real drift is happening.

**ADR 0007's default decision was option (c) "accept and document."
The magnitude of the measurement makes (c) untenable** — every demo
run would paint the dashboard red on healthy pumps. The carry-in must
be resolved before the recording session. Options on the table for
PO sign-off:

(a) **Recompute `reference_distribution.json` from demo-paced healthy
    telemetry.** Cleanest fix; the model session's `--training-dwell`
    knob already separates training and reference; a new `model.train
    --reference-source demo` switch would build the reference from
    `DEFAULT_PROFILES`-paced HEALTHY data. Cost: one follow-up session
    + a model.train CLI flag + a session log. Doesn't touch the
    parity boundary.

(b) **Dual references.** Ship `reference_distribution.training.json`
    and `reference_distribution.demo.json`; `compute_psi` picks
    based on a config flag. Cleaner separation of concerns but
    doubles the reference-shipping surface and introduces a new
    "which reference?" question every consumer has to answer.

(c) Accept and document (ADR 0007 default) — **rejected** by the
    measurement above.

**Recommendation:** option (a). The training-time dwell-stretch was
a model-quality fix; the *reference* baseline is what an operator
considers "normal." Operators see the demo-paced pump, not the
48h-stretched corpus. Build the reference from demo-paced HEALTHY
data and the parity claim becomes honest.

Tracked in `context/drift.md` §"Open questions" → "Reference-Validity
post-measurement decision."

## Verification

- `pytest -p no:cacheprovider --tb=short -q` from the `/tmp` workspace
  copy (per `[[ml-obs-pipeline-git-on-windows]]` FUSE workaround):
  **340 passed, 1 skipped, 9 warnings in 14.50s.**
- Baseline pre-session: 322 passed + 1 skipped (per ADR 0006 §Test
  count). Delta: +18 net tests. -3 stub-pinning removed, +21 new
  (3 value-driven PSI tests, 9 load-path tests, 4 service cadence
  tests, 2 influx null-PSI tests, 3 config PSI-property tests).
- Brief's named-explicit test
  `test_structural_parity_compute_psi_loads_from_shared`: **PASS**.
  All 5 mode-parity tests pass.
- Demo-paced measurement above is the Reference-Validity carry-in's
  empirical answer.

## Open follow-ups (carry into next session)

1. **Reference-Validity post-measurement decision.** Recommendation
   is option (a) — recompute reference from demo-paced healthy data.
   Awaiting PO sign-off. Until resolved, the demo dashboard will
   show PSI fields red on healthy pumps; this is a recording blocker.
2. **Fleet-PSI EventBridge Lambda** (PLAN.md §2.7). Same `compute_psi`,
   aggregates a 5-minute fleet window across all pumps. Out of scope
   here.
3. **lambda_scorer session needs to bundle `shared/drift.py` +
   `model/artifacts/reference_distribution.json`** in the deploy zip.
   Reference is ~5 KB; mechanical. The model/reference version-check
   branch assumes both files are in the zip — if a future ADR moves
   `model.pkl` to S3 cold-load, the version-check needs the S3 path
   injected.
4. **PSI on the warm-up window.** When the feature-history deque has
   < 30 samples (early demo), the first compute_psi call produces
   high PSI just from the single-bin concentration. Cadence default
   (compute fires at tick 30, when window is 30 samples deep) makes
   this nearly unreachable in practice; a future polish session
   could add a `min_samples_for_psi` warm-up gate that returns None
   until the window has at least N samples.

## Workflow notes (for the next session)

- The FUSE truncation bug bit twice during this session — once on
  `Write` of the new `shared/drift.py` (truncated to ~73 lines), once
  on `Edit` of `test_shared_stubs.py` (truncated mid-statement).
  Workaround per `[[ml-obs-pipeline-git-on-windows]]`: rewrite via
  `cat > file <<'EOF' ... EOF` bash heredoc. Documented in the
  existing memory; no update needed.
- The committed `model/artifacts/reference_distribution.json` was
  silently truncated to 4660 bytes on disk (mid-value at line 216:
  `3.7`). Read tool showed the un-truncated content but bash and
  pytest hit the corruption. Resolution this session: regenerated the
  file via Python in bash with the known content. **The model session
  likely shipped a truncated file via the same FUSE bug — worth
  spot-checking the other committed artifacts (`model.pkl`) in a
  future session.**

## Commit message draft (for PO to run from Windows)

```
drift: real PSI per PLAN.md s2.7 + every-Nth-tick cadence

Replace shared.drift stub with binned, Laplace-smoothed PSI against
the training-time reference distribution. service.py grows a per-pump
1-hour feature-history deque + per-pump tick counter; PSI fires on
every Nth tick (default 30 = once per minute at 2s tick) and
non-compute ticks emit ScoredRow.psi=None so the InfluxDB writer
omits the psi_* fields. Five decisions land in ADR 0007: formula,
Laplace alpha = 1.0, reference=None semantics, model/reference
version match, per-tick cadence.

Demo-paced Reference-Validity carry-in measurement included:
PSI fires SIGNIFICANT on 7 of 8 features for healthy pumps because
the training corpus used a stretched DEGRADING dwell. ADR 0007's
default "accept and document" rejected; recommend recomputing the
reference from demo-paced healthy data in a follow-up session.

340 passed, 1 skipped (was 322 + 1).

Refs: ADR 0007.
```
