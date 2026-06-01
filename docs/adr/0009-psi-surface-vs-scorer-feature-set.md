# ADR 0009 — PSI Surface ≠ Scorer Feature Set

- **Status:** Accepted (PO sign-off 2026-06-03; Gemini approved 2026-06-03)
- **Date:** 2026-06-03
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Principle (plain English)

**Two feature lists, two purposes.** The model and the drift detector
care about different statistical properties of the same telemetry.

The *scorer* needs every feature that helps it predict failure — and
that includes the four rolling features (5-minute mean and std of
vibration and bearing temperature) because they smooth out per-tick
noise and let the model see the slow ramp that precedes a bearing
fault. Rolling features are aggregates over a window: the
autocorrelation in consecutive aggregates is a *feature* for the
classifier (temporal denoising), not a bug.

The *drift detector* needs samples that satisfy PSI's IID assumption
— the load-bearing assumption behind its < 0.10 / 0.10–0.25 / > 0.25
threshold semantics. Consecutive 1800-sample windows over rolling
features share 149 of every 150 underlying readings; PSI on them
produces 0.10–0.40 autocorrelation noise on perfectly healthy
fleets. The same property that makes rolling features useful for
prediction (smoothing across overlapping windows) makes them
useless for PSI (broken IID across consecutive aggregates).

So the scorer keeps consuming all 8 features (`FEATURE_NAMES`) and
the drift detector iterates only the 4 raw signals
(`PSI_FEATURE_NAMES`). Two related but distinct contracts, one
file (`shared/features.py`), pinned by a strict-subset structural
test. A future engineer trying to pass `FEATURE_NAMES` into
`compute_psi` — or worse, "harmonize" the two lists — runs into
this principle before they get to the implementation.

The rest of this ADR is the historical context that surfaced the
decision, the alternatives considered, and the mechanics of how the
asymmetry is locked across code, artifact emission, and tests.

## Context

ADR 0008's 2026-06-02 measurement and Gemini's headline Q1 endorsement
(`review_responses/2026-06-02-model-operational-reference.md`)
established that per-pump PSI on the four rolling features
(`vibration_amp_mean_5m`, `vibration_amp_std_5m`,
`bearing_temp_mean_5m`, `bearing_temp_std_5m`) is autocorrelation-
bounded above the 0.10 STABLE band on healthy fleets — worst-case PSI
~0.39 across 10 test seeds; ~10 % of (feature, seed) combinations
firing SIGNIFICANT on healthy pumps. The root cause is structural:
consecutive rolling-window samples share 149 of 150 underlying
readings, so the samples PSI compares are NOT IID. Increasing the
reference pump count from 5 → 50 moved worst-case PSI by < 0.05 — the
floor is structural, not sample-size-bounded.

PSI's IID assumption is the load-bearing assumption behind its
threshold semantics. Computing PSI on samples that violate IID
produces values that look like drift signal but are autocorrelation
noise — operationally indistinguishable from genuine distribution
shift on a Grafana panel. ADR 0008 §Negative carried this as an open
follow-up (`context/drift.md` §"Open questions" →
autocorrelated-PSI-threshold-semantics).

The brief for the 2026-06-03 session listed three remediation options
(carried forward from ADR 0008's follow-up):

1. **Lift the bands for rolling features** (e.g., 0.25 / 0.50 / 1.00).
   Asks the operator to remember two threshold sets. Demo dashboards
   would need conditional bands per panel. Doesn't fix the
   noise-vs-signal problem — it just widens the noise tolerance.
2. **Collapse rolling features into raw features for PSI purposes**
   (drop the four rolling-PSI fields; rely on raw-features PSI for
   drift detection). The scorer keeps consuming all 8 features as
   input. Clean asymmetry; one threshold set.
3. **Replace PSI for rolling features with a moving-average-band
   metric.** New code, new threshold semantics, additional Grafana
   panel logic. Largest architectural footprint; smallest payoff
   given that raw-feature PSI is a leading indicator for any drift
   the rolling features would also signal (rolling features are
   derived from raw features — drift in raw propagates to rolling
   with a 5-minute lag).

This ADR records the decision and the architectural principle behind
it. The principle survives even if a future session swaps in
remediation (3) — the asymmetry between scorer input and PSI surface
stays.

Constraints driving the decision (from `context/_global.md`):

- **#4 mode parity:** the PSI computation is shared between local
  and AWS modes (ADR 0005). Whatever subset PSI iterates over must
  be a single source of truth that both modes import.
- **#5 one polished repo:** PSI panels on the demo dashboard should
  read STABLE on healthy pumps without "but ignore those four
  channels" caveats.

## Decision

Adopt **remediation option 2 from ADR 0008's follow-up**: drop rolling
features from the PSI surface. Lock the asymmetry between scorer
input and PSI surface as the architectural principle.

Concretely:

1. **New constant `PSI_FEATURE_NAMES`** in `shared/features.py`:

   ```python
   PSI_FEATURE_NAMES: tuple[str, ...] = (
       "vibration_amp",
       "bearing_temp",
       "motor_current",
       "rpm",
   )
   ```

   Lives next to `FEATURE_NAMES` so the two feature sets read
   symmetrically and a future addition has to touch the obvious
   file. Self-documenting via proximity.

2. **`shared.drift.compute_psi` iterates `PSI_FEATURE_NAMES`**, not
   `FEATURE_NAMES`. The reference dict's `features` map covers
   exactly the four raw features; the returned PSI dict carries
   exactly those four keys.

3. **`shared.drift.load_reference` validates `feature_names` against
   `PSI_FEATURE_NAMES`** (not `FEATURE_NAMES`). The on-disk reference
   JSON now embeds a 4-element `feature_names` list. A reference that
   carries the old 8-element list fails load with a clear DriftError
   pointing at the rebuild command.

4. **`model.train.compute_reference_distribution` bins only
   `PSI_FEATURE_NAMES` columns**, and `write_artifacts` emits the
   reference JSON with the 4-element `feature_names` list. The X
   matrix still carries all 8 columns (the model is trained on all
   8); the reference distribution is sliced to the PSI surface at
   write time.

5. **`local_runtime/influx_writer.py::build_point` emits only four
   `psi_<feature>` fields**, prefixed with the names in
   `PSI_FEATURE_NAMES`. InfluxDB schema goes 17 → 13 fields per point
   on compute ticks; non-compute ticks stay at 9 fields (psi_*
   omitted entirely, unchanged).

6. **The asymmetry is pinned by tests.** A new test verifies
   `set(PSI_FEATURE_NAMES) ⊂ set(FEATURE_NAMES)` and pins
   `PSI_FEATURE_NAMES = ("vibration_amp", "bearing_temp",
   "motor_current", "rpm")` so a "let me add a feature to PSI"
   regression has to update this ADR.

The scorer continues to consume all 8 features. `shared.score.score`
and `shared.features.extract_features` are unchanged. The 8-element
`FEATURE_NAMES` stays in place as the scorer input contract; the new
4-element `PSI_FEATURE_NAMES` is the drift surface contract. Two
related but distinct feature lists, serving two related but distinct
purposes.

## Alternatives considered

### 1. Remediation strategy

**A. Drop rolling features from PSI (the decision).** Cleanest
asymmetry; smallest code surface; one threshold set across all PSI
channels; rolling features stay as model inputs where their
autocorrelation is a feature (temporal smoothing for prediction),
not a bug (broken IID for drift detection). PSI panel count goes
from 8 to 4, which simplifies the dashboards session's panel set
without losing operational signal — rolling features echo raw
drift with a 5-minute lag, so the four raw-feature PSI channels are
strictly leading indicators.

**B. Lift bands for rolling features (e.g., 0.25 / 0.50 / 1.00).**
Keeps the eight-channel PSI surface intact but asks operators to
remember two threshold sets. Grafana panels would need conditional
band rendering. Doesn't address the noise-vs-signal problem — it
widens noise tolerance, which means real drift on rolling features
takes longer to detect. Cost (operator cognitive load, panel
complexity) > benefit (eight PSI series of which four are noisy).

**C. Make rolling features IID via downsampling + extended
simulation.** Subsample one rolling reading per window
(non-overlapping) and extend `OPERATIONAL_REFERENCE_TICKS_PER_PUMP`
~15× to keep bin counts healthy. Costs ~30 s of training-time wall
clock and an extra structural change (PSI window sample rate
diverges from the live tick rate). Buys back rolling-feature PSI
that is ~80 % redundant with raw-feature PSI (rolling features are
derived from raw features; raw drift propagates to rolling). Rejected
as ceremony for a near-redundant signal. If a future session
discovers a drift pattern that the rolling features catch but the
raw features miss, this option is re-openable without breaking the
asymmetry — `PSI_FEATURE_NAMES` is the configuration point.

**D. Replace PSI with a moving-average-band metric for rolling
features.** New compute path, new threshold semantics, new panel
type. Largest architectural footprint. Same redundancy argument as
(C). Rejected on cost-benefit.

### 2. Where `PSI_FEATURE_NAMES` lives

**A. `shared/features.py` (the decision).** Next to `FEATURE_NAMES`,
in the same parity-boundary file. Two feature lists read
symmetrically; a future "add a feature to PSI" change has to touch
the obvious place. Self-documenting via proximity; one file holds
both contracts.

**B. `shared/drift.py`.** Closer to the only consumer (`compute_psi`).
Cleaner separation (`features.py` doesn't know about drift concerns)
but spreads the canonical feature lists across two files. Future
maintainer has to know to look in two places. Rejected: the
proximity argument wins.

### 3. ADR shape

**A. New ADR 0009 (the decision).** The "PSI surface ≠ scorer input
set" architectural principle is structurally distinct from ADR 0008's
"operational reference source ≠ training matrix" decision. Two clean
ADRs > one mixed-purpose one. Same logic that produced ADR 0006 vs
ADR 0008 last session.

**B. Amend ADR 0008.** Folds the asymmetry into 0008's "consequences"
section. Smaller doc surface but conflates two unrelated decisions
(reference-source separation vs. PSI-surface scope). Rejected on
clarity grounds.

## Consequences

**Positive:**

- **PSI panels on the demo dashboard read STABLE on healthy fleets
  without rolling-feature noise.** Expected: 4/4 raw PSI < 0.10 on
  the same seed-42 test pump that ADR 0008 measured. The four
  rolling-feature panels disappear from the schema; future
  dashboards build against the surviving four.
- **One threshold set across the entire PSI surface.** PLAN.md §2.7
  bands (< 0.10 / 0.10–0.25 / > 0.25) apply uniformly to every
  channel; no per-feature branching in alerts.
- **Parity boundary is sharper, not smaller.** `compute_psi`'s
  scope is now an explicit, pinned-by-test subset of `FEATURE_NAMES`.
  A future "let me harmonize these" PR has to update this ADR.
- **Test count goes UP, not down.** The four-key dict assertion
  replaces the previous eight-key assertion AND adds a new
  asymmetry-pin test. The rolling-feature soft bound from
  `test_demo_paced_healthy_psi_stable` is retired and replaced with
  a hard "rolling features are not in the PSI dict" assertion via
  the dict-key check.
- **Scorer is untouched.** `extract_features` still produces 8
  features; `score` still consumes 8; `FEATURE_NAMES` still has 8
  members. The model continues to use rolling features as temporal
  smoothers — their value as prediction inputs is preserved.
- **InfluxDB schema cost reduction is small but real.** 17 → 13
  fields per point on compute ticks (PSI cadence is every Nth tick,
  default N = 30 — once per minute). At 15 pumps × 30 readings/min
  this is 4 fewer floats × 15 writes/min = 60 fewer fields/min of
  storage, which matters approximately not at all locally but is a
  free win.

**Negative:**

- **The on-disk reference distribution shape changes.** Reference
  JSONs produced by `model.train` before this ADR carry an 8-element
  `feature_names` list and an 8-key `features` map. After this ADR
  they carry 4 and 4. `load_reference` rejects the old shape with a
  `DriftError`. Any committed reference artifact must be regenerated
  via `model.train`. The sandbox-runtime reference regenerated here
  is the 12-pump variant per ADR 0008; PO regenerates at 30 pumps
  natively for production (Item 6 from the session brief — separate
  follow-up).
- **The InfluxDB schema changes within a deployment.** The four
  retired `psi_*` field names (`psi_vibration_amp_mean_5m`,
  `psi_vibration_amp_std_5m`, `psi_bearing_temp_mean_5m`,
  `psi_bearing_temp_std_5m`) simply stop being written. Historical
  rows in InfluxDB that carry the old fields are unaffected;
  Grafana queries against those field names go from "last value =
  recent reading" to "last value = old reading", which is the right
  behavior. The dashboards session (not yet started) wires its
  panels against the four surviving names from day one — no UI
  rework.
- **ADR 0005 §3 (InfluxDB schema) needs an amendment pointer.**
  §3's "17 fields per point including 8 `psi_*`" line now reads "13
  fields per point including 4 `psi_*` (see ADR 0009)". Added as a
  one-line refinement in §3, not a full rewrite — ADR 0005's
  shared-package + subscriber-topology decisions stand untouched.
- **Anyone who wants rolling-feature drift detection later has to
  re-open this ADR.** Intentional: the asymmetry should be a
  load-bearing decision, not a default someone overrides without
  thought. The `PSI_FEATURE_NAMES` constant is the natural
  configuration point for re-opening — it's literally one tuple to
  extend, and the structural test enforces an ADR update on extension.

**Follow-ups:**

- **Dashboards session wires four PSI panels, not eight** (Gemini's
  Q from `review_responses/2026-06-02-model-operational-reference.md`
  follow-up). The four panel set is `psi_vibration_amp`,
  `psi_bearing_temp`, `psi_motor_current`, `psi_rpm`. No conditional
  band logic needed; PLAN.md §2.7 bands apply uniformly. Carried
  in `context/drift.md` §Open questions → closed by this ADR; the
  dashboards-session pickup is a new bullet.
- **PO regenerates production artifacts at 30 pumps natively** after
  this ADR lands (Item 6 from the 2026-06-03 session brief). Same
  `v0.1.0-seed-0` tag. New-shape reference (4-element
  `feature_names`) ships from day one.
- **Operational reference pump count bump (5 → 15)** is an
  independent decision from this ADR (Item 3 from the session
  brief). Carried as a separate follow-up; not blocking this ADR.

## References

- ADR 0005 §3 — InfluxDB schema (the 17-fields-per-point line gets
  a §3 refinement pointer to this ADR).
- ADR 0007 — PSI formula + Laplace + per-tick cadence. Unchanged by
  this ADR; the only change is which feature set the formula
  iterates.
- ADR 0008 — operational PSI reference source-separated from
  training. The autocorrelation measurement in §Negative is the
  direct motivation for this ADR; the open follow-up flagged there
  (`autocorrelated-PSI-threshold-semantics`) is closed by this ADR
  choosing remediation option 2.
- `review_packets/2026-06-02-model-operational-reference.md` §Q1 —
  the headline question that produced Gemini's endorsement of
  dropping rolling features from PSI.
- `review_responses/2026-06-02-model-operational-reference.md` Q1 —
  Gemini's endorsement of the asymmetry approach.
- `context/drift.md` §"Open questions" → autocorrelated-PSI threshold
  semantics — the open question this ADR closes.
- `docs/sessions/2026-06-02-model-operational-reference.md`
  §"Rolling-features autocorrelation finding" — the 10-seed
  measurement that surfaced the structural floor.
- Implementation: `shared/features.py` (`PSI_FEATURE_NAMES`
  constant), `shared/drift.py` (`compute_psi` + `load_reference`
  iterate / validate against `PSI_FEATURE_NAMES`), `model/train.py`
  (`compute_reference_distribution` + `write_artifacts` emit
  4-feature reference), `local_runtime/influx_writer.py`
  (`build_point` emits 4 `psi_*` fields).
- Tests: `model/tests/test_train.py` (compute_reference_distribution
  shape update; write_artifacts default still operational path),
  `local_runtime/tests/test_drift_load.py`
  (`test_demo_paced_healthy_psi_stable` flips rolling-features soft
  bound into a hard "rolling features are not in PSI output" check),
  `local_runtime/tests/test_shared_stubs.py` (synthetic-reference
  tests iterate `PSI_FEATURE_NAMES`),
  `local_runtime/tests/test_influx_writer.py` (13-field point;
  4-key psi block), new `test_psi_feature_names_is_subset_of_feature_names`
  in `local_runtime/tests/test_features.py` pins the asymmetry.
- Session log: `docs/sessions/2026-06-03-drift-psi-surface-cleanup.md`.
- Review packet: `review_packets/2026-06-03-drift-psi-surface-cleanup.md`.
