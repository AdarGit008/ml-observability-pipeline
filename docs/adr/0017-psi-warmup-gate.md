# ADR 0017 — PSI Warmup Gate: Minimum-Sample Arming Threshold for Drift Alerts

- **Status:** Accepted (PO sign-off 2026-06-10; DeepSeek review folded 2026-06-10 — see §Addendum)
- **Date:** 2026-06-10
- **Deciders:** PO (Adar), Claude (architect), DeepSeek (reviewer — ADR 0011 §Addendum 2026-06-10)

## Principle (plain English)

**PSI on a cold window is noise, not news.** A pump that has only
reported for a few seconds has too few readings to populate the
reference's 10 bins; the Laplace prior dominates and max-PSI sails past
0.25 even when the pump is perfectly healthy. Arming an alert on that is
arming on the warm-up transient, not on drift. So PSI may only **arm an
alert** once the window holds enough samples for the metric to mean
something — `PSI_MIN_SAMPLES` = 150 (5 minutes at the 2 s tick). The
gate is on the **alert**, not the **computation**: `compute_psi` still
runs and still writes `latest_psi` every invocation, so the dashboard
shows PSI warming up; only the decision to page someone waits for warmth.

## Context

The first live apply (2026-06-07; `context/lambda_scorer.md` §Open
questions, memory `ml-obs-pipeline-live-apply-2026-06-07`) surfaced a
fleet-wide false-alert storm: on a `healthy` fleet, **9 of 14 pumps
fired `alert_flag: true` within minute 1**. The scores were all ≤ 0.02
(far below the 0.7 line) — the alerts were entirely PSI-driven:
`max(psi.values()) > 0.25` on sub-minute sample windows. The
edge-triggered SNS path (ADR 0012) then published fleet-wide on what was
purely a cold-start binning artifact.

Root cause is the interaction of three already-correct decisions:

- **ADR 0007** — PSI uses 10 equal-frequency reference bins with Laplace
  add-α smoothing (α = 1.0). With only a handful of readings, all actual
  mass clips into one or two bins and the α=1.0 prior (10 phantom
  observations spread across the bins) dominates the ratio, inflating
  PSI.
- **ADR 0008** — the operational reference reads STABLE on a healthy
  demo fleet only when the window is **full**; the carry-in measurements
  were all taken post-warm-up.
- **lambda_scorer handler** — computes PSI on *every* invocation (no tick
  cadence, by design — the cadence in `local_runtime` is an InfluxDB
  write-throttle, not a compute constraint). So the very first
  invocations, with 1–30 samples, feed `compute_psi` and the result arms
  the alert.

`local_runtime` never saw the storm because **it has no alert site** — it
writes PSI to InfluxDB for Grafana and stops. The arming logic
(`max(psi) > 0.25 OR score > 0.7`) lives only in `lambda_scorer`. That
asymmetry is the crux of where the fix belongs (see Decision).

This touches the drift surface (`shared.drift` / PSI) → the parity set
(DEV_NORMS §5 Tier 2b; ADR 0005). North star #6: any local/AWS
divergence in the drift signal is a bug or an ADR — hence this record.

The fix direction was pre-figured before the storm: `context/drift.md`
§Open questions already carried a "Warm-up gate" note ("a future polish
session could add an explicit `min_samples_for_psi` gate that returns
None until N samples accumulate"). This ADR is that session, with the
shape adjusted (a predicate, not a None-return — see Alternatives).

## Decision

1. **The warmup threshold and its predicate live in `shared.drift`.**
   - `PSI_MIN_SAMPLES: int = 150` — module constant.
   - `psi_is_armed(window) -> bool` — pure, dependency-free predicate
     returning `len(window) >= PSI_MIN_SAMPLES`. It is the single shared
     definition of "this window is warm enough to trust a PSI breach."

2. **`compute_psi` is unchanged.** Its return contract (a 4-key float
   dict, every invocation) is the parity-pinned boundary; touching it
   would ripple into the STATE-row `latest_psi` Map and the dashboards
   adapter JSON, and would conflate "not warm enough to alert" with
   `local_runtime`'s existing "not computed this tick" `None`. The gate
   is therefore decoupled from the computation.

3. **The gate is applied at the alert-arming site** in
   `lambda_scorer/handler.py`:
   `psi_breach = psi_alert_should_fire(window, psi)` (the shared
   composite — see §Addendum §1).
   `score_breach = score > 0.7` is **NOT** gated — a high failure
   probability is meaningful on a short window (the model tolerates it;
   `extract_features` degrades gracefully), and the observed storm was
   PSI-only (scores ≤ 0.02). `latest_psi` is still written to the STATE
   row on cold windows, so the dashboard shows the metric warming up.

4. **Threshold = 150 samples (5 minutes at the 2 s tick).** This is the
   existing `lambda_scorer.handler.WINDOW_SAMPLES` (the 5-minute scoring
   window) — an already-justified number rather than a new magic
   constant. At 150 samples across 10 bins there are ~15 observations per
   bin, so the α=1.0 Laplace prior is a ~6% correction, not the dominant
   term. On a full window a healthy fleet already reads STABLE (ADR
   0008), so the gate suppresses only the cold-start storm and never real
   drift, while leaving the bulk of even a 13-minute demo for genuine
   drift detection.

5. **Parity is satisfied by construction, not by a second call site.**
   `local_runtime` has no alert site, so there is no local/AWS PSI-value
   divergence to reconcile — `compute_psi` is byte-for-byte the same in
   both modes. The shared constant exists so the **future fleet-PSI
   EventBridge Lambda** (PLAN.md §2.7) arms on the same boundary as the
   per-pump scorer; a `test_structural_parity_psi_is_armed_loads_from_shared`
   guard pins that the handler imports the shared predicate rather than a
   vendored fork.

## Alternatives considered

### 1. Where the gate lives

**A. Shared predicate (`PSI_MIN_SAMPLES` + `psi_is_armed`) consumed at
the alert site (the decision).** Single source of truth for the warmup
boundary; `compute_psi` untouched; PSI values still flow to dashboards.
The future fleet-PSI Lambda consumes the same constant. Costs one new
structural-parity test.

**B. Gate inside `compute_psi` (return `None`/sentinel when cold).** One
chokepoint, impossible to bypass. Rejected: it changes the parity-pinned
return contract, ripples into the STATE-row `latest_psi` Map shape and
the dashboards adapter wire format (ADR 0014), and conflates "not warm"
with `local_runtime`'s existing "not computed this tick" `None` (ADR
0007 §5) — two different meanings collapsed onto one sentinel. It would
also suppress the dashboard's warming-up PSI, which is useful to see.

**C. Local constant in `lambda_scorer` only, no shared change.** Least
code today. Rejected: the future fleet-PSI Lambda would define its own
threshold and could silently diverge from the per-pump scorer — exactly
the local/AWS-divergence north star #6 forbids without an ADR. The whole
point of a parity project is that "how warm is warm enough" is decided
once.

### 2. The threshold value

**A. 150 samples / 5 min (the decision).** Reuses `WINDOW_SAMPLES`;
~15 obs/bin makes the Laplace prior a ~6% correction; well inside demo
length. Ties the justification to ADR 0007's binning + smoothing.

**B. 50 samples / ~1.7 min.** The ≥5-observations-per-bin rule of thumb
for a 10-bin histogram. Arms faster, defensible statistically, but not
tied to any existing codebase constant and leaves bins thin (prior ~17%
of mass). Reachable by changing one constant if a future demo wants
earlier arming.

**C. 1800 samples / full 1-hour window.** Most conservative — PSI only
arms once the full reference-length window is held. Rejected: PSI alerts
would never arm during a 13–30 minute demo, defeating the drift-detection
story the project exists to show.

**D. 30 samples / 1 min.** Matches the `local_runtime` PSI compute
cadence (`psi_period_ticks`). Rejected: this is the edge where the
2026-06-07 noise was observed; the prior still dominates (~25% of mass)
and the metric isn't trustworthy yet.

### 3. Gating PSI vs. gating both signals

**A. Gate only the PSI side; leave `score > 0.7` ungated (the
decision).** The storm was PSI-only. A model score is meaningful on a
short window in a way a 10-bin histogram is not, so there's no reason to
suppress a genuine high-failure-probability alert during warm-up.

**B. Gate the whole `alert_flag` until warm.** Simpler to state ("no
alerts for the first 5 minutes"). Rejected: it would blind the system to
a pump that is genuinely failing in its first 5 minutes of reporting —
a real signal the score path is designed to catch.

## Consequences

**Positive:**

- **The cold-start storm is closed at the source.** A healthy fleet
  produces zero PSI alerts during warm-up; once warm, ADR 0008's
  operational reference keeps healthy PSI STABLE, so still no false
  alerts. The SNS Always-Free 1,000-email/month envelope (ADR 0012) is
  no longer at risk of a fleet-wide minute-1 burn.
- **Parity is preserved trivially.** `compute_psi` is unchanged, so all
  structural-parity tests stay green by construction; the new predicate
  gets its own guard.
- **The dashboard still shows PSI warming up.** Gating the alert, not the
  value, means an operator watching Grafana sees the metric settle —
  useful signal, not a blank panel.
- **The future fleet-PSI Lambda inherits the boundary for free.** It
  imports `psi_is_armed`; it cannot diverge.
- **The score path is untouched** — a genuinely failing new pump still
  alerts on `score > 0.7` regardless of window depth.

**Negative:**

- **A real drift event in a pump's first 5 minutes of reporting is not
  PSI-alerted.** This is the deliberate trade: the only way to catch it
  via PSI would be to trust sub-warmup windows, which is precisely the
  noise this ADR removes. In practice a pump degrading that fast also
  crosses `score > 0.7`, which is ungated. At demo scale all pumps warm
  up in the first 5 minutes and scenarios inject drift later.
- **`PSI_MIN_SAMPLES` is a second window-length constant** alongside
  `WINDOW_SAMPLES` and `PSI_WINDOW_SAMPLES`. It happens to equal
  `WINDOW_SAMPLES` today but is conceptually distinct (arming floor vs.
  scoring window), so it's its own named constant in `shared.drift`
  rather than an import of the handler's value — keeping the drift
  module's import surface free of a `lambda_scorer` dependency.
- **The gate is sample-count, not wall-clock.** A pump with a gap in
  reporting warms by accumulated samples, not elapsed time. This matches
  how `compute_psi` already thinks about the window (it does no
  time-filtering) and is the honest definition for a metric computed over
  a sample window.

**Follow-ups:**

- Fleet-PSI EventBridge Lambda (PLAN.md §2.7) consumes `psi_is_armed`
  against its aggregated 5-minute fleet window — the shared constant is
  already in place for it.
- **150 is a deliberate conservative first cut** = the full sliding
  window. Re-evaluate after a week of live data; lowering toward ~50
  (alternative 2B, ≥5-per-bin) is a one-constant change if the live
  false-positive rate at 50 proves acceptable. The trade-off it buys
  down: a PSI-only drift in a pump's first 5 minutes is detected later
  (DeepSeek review §2).
- Demo-day rehearsal should confirm a `degrading` scenario still arms a
  PSI alert post-warm-up live (deferred item in `context/lambda_scorer.md`).

## References

- `context/lambda_scorer.md` §Open questions — "PSI warmup alert storm
  (NEW, 2026-06-07)", the open item this ADR closes.
- `context/drift.md` §Open questions — "Warm-up gate", the pre-figured
  direction this ADR realizes.
- ADR 0007 — PSI formula, 10 bins, Laplace α = 1.0 (the smoothing whose
  small-sample dominance this gate compensates for) and the per-tick
  cadence note.
- ADR 0008 — operational reference; healthy = STABLE on full windows
  (why a sample-count gate fully resolves the storm).
- ADR 0012 — edge-triggered SNS alerts; the publish path the storm
  fired through; thresholds (`max(psi) > 0.25 OR score > 0.7`).
- ADR 0005 — parity boundary; why the threshold's home is `shared/`.
- ADR 0009 — 4-key PSI surface (the `latest_psi` Map a §1-B return-change
  would have disturbed).
- Implementation: `shared/drift.py` (`PSI_MIN_SAMPLES`, `psi_is_armed`),
  `lambda_scorer/handler.py` (gated `psi_breach`).
- Tests: `lambda_scorer/tests/test_handler.py`
  (`test_structural_parity_psi_is_armed_loads_from_shared`,
  `test_psi_alert_gated_below_warmup`, `test_psi_alert_arms_when_warm`;
  the three PSI-breach SNS tests warmed to `PSI_MIN_SAMPLES`),
  `local_runtime/tests/test_shared_stubs.py`
  (`test_psi_is_armed_boundary`).
- Session log: `docs/sessions/2026-06-10-drift-psi-warmup-gate.md`.
- Live-apply finding: memory `ml-obs-pipeline-live-apply-2026-06-07`;
  `context/lambda_scorer.md` §Resource sizing (2026-06-07 measurements).


## Addendum 2026-06-10 — DeepSeek review dispositions

Source: `review_responses/2026-06-10-drift-psi-warmup-gate.md`
(deepseek-reasoner). Five points + observations; two drove code changes
(§1 + §3), one a test addition (§5), one a documentation strengthening
(§2), one validated (§4); the WINDOW_SAMPLES observation rejected with a
dependency-direction reason.

### §1 — Gate location: threshold not shared (Accepted — code landed)

DeepSeek: `PSI_MIN_SAMPLES` is shared but the *full* arming logic
(`psi_is_armed(window) AND max(psi) > PSI_ALERT_THRESHOLD`) lived in
`lambda_scorer`, with the 0.25 threshold defined there — so a future
fleet-PSI Lambda could import the constant yet pick a different
threshold, skip the gate, or re-implement it, falsifying north star #6.

**Decision:** colocate the whole decision. Added
`shared.drift.PSI_SIGNIFICANT_THRESHOLD = 0.25` + `psi_alert_should_fire(
window, psi, threshold=PSI_SIGNIFICANT_THRESHOLD)` (encodes warmup gate
AND threshold). `lambda_scorer/handler.py` now calls
`psi_breach = psi_alert_should_fire(window, psi)`, and its
`PSI_ALERT_THRESHOLD` is an alias of the shared constant. A sixth
structural-parity guard
(`test_structural_parity_psi_alert_should_fire_loads_from_shared`) pins
the load path. `psi_is_armed` survives as the primitive (its own guard +
unit test).

### §2 — Threshold = 150 conservative (Accepted as documentation)

DeepSeek: 150 = full window, so PSI-only drift in a pump's first 5 min is
detected late; consider 50 (≥5/bin). PO keeps 150 as the conservative
first cut; §Consequences/Follow-ups now records the planned re-evaluation
after live data and the one-constant path to 50. No code change.

### §3 — Ungated score path could storm (Accepted — code + test landed)

DeepSeek: "the storm was PSI-only" is an observation, not a proof the
score is immune on tiny windows; no test covers cold-window + high score.

**Decision:** the asymmetry is deliberate — a score is a per-sample model
output, not a distributional statistic needing bin population, so a high
P(failure) even on one reading is a legitimate signal we want surfaced.
Made it explicit: handler comment (§3 rationale) + a new test
`test_score_alert_not_gated_by_warmup` pinning that a cold window with
score > 0.7 DOES arm (`alert_type "high_failure_prob"`). The model's
out-of-distribution behaviour on tiny windows is a model-surface concern
(ADR 0006 / `context/model.md`), monitored post-deploy rather than
papered over with a second gate.

### §4 — Sample-count vs. wall-clock (Validated)

DeepSeek: sample count is the correct semantic for a fixed-size sliding
window; the gap-then-burst edge is covered by FIFO displacement + the
ungated score path. No change.

### §5 — Test honesty (Accepted — assertion added)

Warming the three PSI-breach SNS tests and the `score_fn` isolation
endorsed. Added the suggested `latest_psi`-is-written assertion to
`test_psi_alert_arms_when_warm` (proves the value persists even when the
alert fires) + a shared unit test `test_psi_alert_should_fire_composite`.

### Observation — derive `PSI_MIN_SAMPLES` from `WINDOW_SAMPLES` (Rejected)

DeepSeek suggested `PSI_MIN_SAMPLES = WINDOW_SAMPLES` for auto-consistency.
Rejected: `WINDOW_SAMPLES` lives in `lambda_scorer.handler`, and
`shared.drift` must not depend on `lambda_scorer` (it would invert the
parity dependency and break the "drift without the scorer" deployment
ADR 0007 §4 protects). They are conceptually distinct constants — the PSI
arming floor vs. the scoring window — that happen to share a value today;
`shared.drift` owns its own named constant. (Already noted in §Consequences
§Negative.)

### Test count after dispositions

Sandbox full suite (excl. `lambda_s3_batcher`, pyarrow-gated): **416
passed, 1 skipped** (was 413+1 pre-fold; +3 = composite unit test +
composite parity guard + ungated-score test; the §5 assertion extended an
existing test). All structural-parity guards green.
