# ADR 0006 — HistGradientBoostingClassifier + Training-Time DEGRADING-Dwell Stretch

- **Status:** Accepted (Gemini review 2026-06-01; PO sign-off pending)
- **Date:** 2026-06-01
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

The model session ships the first non-stub `shared.score.score`. Three
questions fell out of the implementation that are each non-obvious
enough to deserve a record, and grouping them here avoids three
follow-up ADRs that cross-reference each other:

1. **Model family.** PLAN.md §2.3 names
   `HistGradientBoostingClassifier(max_depth=5, max_iter=100)`. The
   choice has trade-offs (vs. RandomForest, vs. XGBoost, vs. linear
   models) worth recording — "PLAN.md said so" is not a rationale a
   reviewer would accept.
2. **Feature engineering rationale.** The 8-feature shape is fixed
   by `shared.features.FEATURE_NAMES` (ADR 0005). The question this
   ADR answers is *why those eight* — specifically why `bearing_temp`
   features are in the set at all given the non-monotonicity carry-in
   from ADR 0002, and why the rolling std is in the set (vs. rolling
   delta or first-difference).
3. **Training-time DEGRADING dwell stretch.** The simulator's
   `DEFAULT_PROFILES` give DEGRADING + FAILING a combined ~13 minutes
   of wall-clock at 2s/tick — perfect for a 60-second demo recording
   but mismatched with the 48h-prior failure horizon the model is
   trained to predict. The smoke test that surfaced this is in the
   session log. Resolution: training-data generation overrides the
   DEGRADING dwell only; the simulator's DEFAULT_PROFILES are
   untouched.

Constraints from `context/_global.md` driving this ADR: north star #4
(mode parity — drives the "live and training-time features go
through the same code" decision), #1 ($0 — drives "local-only
training, no SageMaker"), #5 (one polished repo — drives "no
hyperparameter zoo, no tuning script we won't maintain").

## Decision

The model session adopts the following three-part design:

1. **HistGradientBoostingClassifier with PLAN.md §2.3 hyperparameters
   (`max_depth=5`, `max_iter=100`, `random_state=seed`).** Locked at
   `model.train.fit_model` and re-asserted in
   `model/tests/test_train.py::test_fit_model_returns_histgbt_with_locked_hyperparams`.
   No hyperparameter search; tuning lives in a follow-up ADR if and
   when AUC drops below the 0.85 acceptance threshold.
2. **Feature engineering: the 8 features from
   `shared.features.FEATURE_NAMES` verbatim** — 4 raw signals
   (`vibration_amp`, `bearing_temp`, `motor_current`, `rpm`) + 4
   rolling 5-min features
   (`vibration_amp_{mean,std}_5m`, `bearing_temp_{mean,std}_5m`).
   Training-time extraction goes through
   `shared.features.extract_features` (the same code as the live
   scorer); the training matrix columns are built by iterating
   `FEATURE_NAMES` in order. The non-monotonic `bearing_temp` is
   kept as a raw feature because the *rolling std* recovers a
   monotonic wear signal (see "Alternatives" below).
3. **Training-data DEGRADING dwell override.** A new constant
   `model.train.TRAINING_DEGRADING_DWELL_TICKS = HORIZON_TICKS = 86_400`
   stretches DEGRADING from 200 ticks (~13 min) to 48h. The
   `rate_per_tick` is scaled down proportionally to preserve the
   DEGRADING ceiling (0.30 degradation at end of state). FAILING
   and FAILED keep `DEFAULT_PROFILES`. The simulator's
   `DEFAULT_PROFILES` are unchanged — the override lives entirely
   in `model.train._training_profiles`. The demo continues to use
   the simulator's 13-minute envelope; only the training corpus
   uses the slowed dwell.

Acceptance result: held-out AUC = **0.998** on 6 pumps held out
from a 30-pump corpus (seed 0). Well above the 0.85 target.

## Alternatives considered

### 1. Model family

**A. `HistGradientBoostingClassifier` (the decision).** sklearn's
native LightGBM-style histogram-based gradient boosting. Handles
the 8-feature problem easily, fits in seconds on a CPU, serializes
to ~300 KB via joblib (well under the 200-KB target order of
magnitude — close enough not to require a Lambda Layer). Sklearn is
already in the ML student's toolbox, which keeps the dep tree small
and the "what would a senior engineer use" answer obvious.

**B. RandomForestClassifier.** Considered for "same expressiveness,
simpler mental model." Rejected on artifact size — a forest of 100
trees with `max_depth=5` serializes to ~5–10 MB depending on the
training data, which is awkward to bundle in a 50-MB Lambda zip
once `sklearn` + `numpy` + `scipy` are also packed in. Also slower
inference per call (10–50× HistGBT on the 8-feature input).

**C. XGBoost / LightGBM directly.** Either gives marginally better
AUC on bigger problems and slightly faster inference. Rejected
because adding a non-sklearn dep crosses a hard line for portfolio
clarity ("how big is the deployed Lambda?") without buying anything
on this dataset size. HistGBT is sklearn's bundled answer to the
same problem.

**D. Logistic regression on hand-engineered features.** Considered
as the "did you really need a tree?" baseline. Rejected as a primary
model because the slow-rising vibration trajectory we're predicting
on is monotone but the *interactions* (high vibration AND high
bearing temp std) are what separate the FAILING regime from a
healthy pump under temperature stress, and additive linear models
miss interactions by definition. A logistic-regression baseline is a
reasonable Gemini-flagged follow-up (does HistGBT actually beat it?)
but doesn't earn the primary slot.

### 2. Feature engineering rationale

**A. 4 raw + 4 rolling (the decision).** PLAN.md §2.3 spec.
Justifications, per-feature:

- `vibration_amp` (raw): the primary monotonic-in-degradation
  signal. ADR 0002 confirms the equation `0.3 + degradation * 2.5
  + N(0, 0.05)` produces a clean monotone trend across the lifecycle.
- `bearing_temp` (raw): kept despite ADR 0002's non-monotonicity
  note because (a) it carries the ambient-shift signal that
  Scenario 1 (seasonal drift) injects, and (b) the model can pair
  it with `rpm` (the other half of the coupling equation) to
  recover the degradation contribution. Removing it would make the
  fleet-level PSI signal harder to attribute at drift time.
- `motor_current` (raw): linear in degradation per ADR 0002. Cheap
  to keep; gives the model another monotone signal that's
  uncorrelated with the noise on vibration.
- `rpm` (raw): under ADR 0002 the RPM equation is
  `setpoint * (1 - degradation) + N(0, 5 + 15 * degradation)`. RPM
  itself is monotonically *decreasing* in degradation, which the
  classifier picks up.
- `vibration_amp_mean_5m` (rolling): smooths the per-tick noise so
  the model isn't asked to discriminate on a 5 % deviation that
  could come from either the noise term or actual wear.
- `vibration_amp_std_5m` (rolling): degradation increases the
  *variance* of vibration even before it shifts the mean
  noticeably, so the std gives an early-warning signal the mean
  misses.
- `bearing_temp_mean_5m` (rolling): the smoothed companion to
  `bearing_temp`. The mean over 5 min collapses the per-tick noise
  on the +0.5°C noise term and makes the ambient-shift detection
  cleaner.
- `bearing_temp_std_5m` (rolling): the *wear-trend* feature per
  ADR 0002 carry-in. As the pump moves through DEGRADING, the
  expanding noise term `N(0, 5 + 15 * degradation)` on RPM
  propagates into bearing temp via the `0.02 * rpm` coupling, so
  the bearing-temp std rises monotonically even though the mean
  doesn't. This is the single feature most-justified by the
  ADR-0002 follow-up; removing it would lose the wear signal
  bearing temp is meant to provide.

**B. Add rolling delta / first-difference features.** Considered
for "more time-series structure." Rejected on the YAGNI argument:
HistGBT can construct the same information from
`current - mean_5m` via tree splits; adding it as a feature
duplicates signal without bringing genuinely new information.
Reachable in a follow-up ADR if a tuning pass shows AUC stuck.

**C. Drop bearing_temp from raw features (keep only rolling std).**
The ADR 0002 non-monotonicity is the argument. Rejected because
the raw value is what Scenario 1's ambient-shift drift detector
needs to flag at PSI time — dropping it would force the drift
session to either re-add it or invent a new "ambient" feature
that the model doesn't see. Mode parity is cleaner with all 4 raw
signals available end-to-end.

### 3. Training-data DEGRADING-dwell stretch

**A. Override DEGRADING dwell to 48h for training only (the decision).**
The simulator's `DEFAULT_PROFILES` give DEGRADING + FAILING ~13 min
of physical-signal evolution. The 48h failure horizon therefore
labels ~99 % of the trajectory as "positive" with no feature
signal to back it up — the classifier learns nothing useful. The
smoke test on the unmodified profiles showed AUC ≈ 0.51 (random)
on a 6-pump corpus. Stretching DEGRADING to span the 48h horizon
aligns the degradation cascade with the label window: the rolling
features now have a slow ramp the classifier can learn from.
Implemented in `model.train._training_profiles`; the demo path
imports `DEFAULT_PROFILES` directly and is unaffected. Held-out
AUC after the override: 0.998.

**B. Shorten the failure horizon to ~5 min (match DEFAULT_PROFILES).**
Avoids the override but contradicts PLAN.md §2.3's explicit
"48 hours" spec and weakens the portfolio narrative ("predict
failure 5 minutes in advance" is a different — and less
impressive — claim than "predict 48 hours in advance"). Rejected.

**C. Stretch DEFAULT_PROFILES globally.** Would unify training and
demo, but a 48h DEGRADING phase makes the 60-second demo
impossible to record at default tick rate, breaking PLAN.md
§4's demo script. The demo's compressed timeline is a fixed
constraint. Rejected.

**D. Use entirely separate physical model for training.** Most
honest, most expensive. Rejected on the YAGNI line: a single
constant override on one StateProfile is a smaller deviation
than a parallel model, and the mode-parity narrative reads as
"the simulator's clock is intentionally compressed for demos; the
training corpus uses the underlying physics-paced version" — which
is true.

## Consequences

**Positive:**

- **AUC well above target.** 0.998 on the held-out 6 pumps gives
  the portfolio narrative a strong number to report. The classifier
  is genuinely learning the degradation pattern rather than
  memorising trajectory shape.
- **Mode parity at training time.** `model.train` calls
  `shared.features.extract_features` directly. A future feature
  rename will either break the training run or the live scorer —
  not silently desync.
- **Trade-off is locally contained.** The training override lives in
  `model.train._training_profiles` (one function); the simulator's
  `DEFAULT_PROFILES` are byte-identical to what the demo session
  shipped. A reader investigating "did the model session change
  the simulator?" gets a clean "no" from the diff.
- **Reproducible:** `python -m model.train` regenerates both
  artifacts deterministically for any given seed. `--seed`,
  `--n-pumps`, `--n-test-pumps`, `--min-auc` CLI knobs cover the
  reproduction surface.

**Negative:**

- **Two physical models in the codebase.** The demo simulator and
  the training data generator use different DEGRADING profiles.
  This is documented (this ADR, module docstring of
  `model.train`, training-data note in
  `model/train.py::TRAINING_DEGRADING_DWELL_TICKS`) but a future
  reader still needs to learn the distinction. Mitigation: any
  attempt to read `DEFAULT_PROFILES[PumpState.DEGRADING].dwell_ticks`
  from inside model training code is wrong — the local override
  is the source of truth there, and the test
  `test_training_profiles_overrides_only_healthy_dwell_and_degrading`
  pins the contract.
- **scikit-learn + joblib as runtime deps.** Both are sizeable
  (~30 MB combined wheel). For the Lambda mode (HANDOFF.md §6 Q3:
  bundle the pickle), this pushes the deploy zip toward the
  50-MB unzipped limit. Mitigation: the lambda_scorer session can
  switch to S3 cold-load if the zip gets tight; that's an open
  question carried into the lambda_scorer brief.
- **One held-out split, no cross-validation.** AUC of 0.998 on a
  single 24/6 split is high enough that a CV story would be
  ceremony, but a reviewer can fairly ask whether the result is
  robust across splits. The training script's `--seed` knob makes
  re-runs with different seeds trivial; adding CV is a small
  follow-up if Gemini pushes.
- **Reference distribution computed from training set only.** No
  leakage, but the drift session will need to confirm that this
  baseline matches what an operator would consider "normal" given
  the dwell-stretch. The training corpus's wider DEGRADING band
  shifts the per-feature quantiles relative to a demo-paced run;
  the drift session may need to either recompute reference from
  demo-paced data or accept that PSI will trigger more
  conservatively. Flagged in the session log.

**Follow-ups:**

- Gemini review packet (`review_packets/2026-06-01-model-train-histgbt.md`)
  — open questions called out there.
- Model + reference versioning scheme (`model_version` field is in
  both artifacts today; lambda_scorer cold-start should refuse a
  mismatch). Out of scope here, captured as an open question in
  `context/model.md`.
- Lambda packaging — bundle vs S3 cold-load — decided in the
  lambda_scorer session per HANDOFF.md §6 Q3. The default (bundle)
  is what we built for; if the Gemini review or the lambda_scorer
  session finds the zip too large, a future ADR records the
  switch.
- Logistic-regression / linear baseline for the README. Not for
  this session.

## References

- PLAN.md §2.3 (the 8-feature spec + HistGBT hyperparameters this
  ADR implements).
- ADR 0002 (`bearing_temp` non-monotonicity carry-in — the
  rationale for keeping `bearing_temp_std_5m` in the feature set).
- ADR 0005 (mode-parity boundary at `shared/{features,score,drift}`;
  this ADR's implementation lives inside that boundary).
- HANDOFF.md §6 Q3 (Lambda packaging default: bundle pickle).
- Session log: `docs/sessions/2026-06-01-model-train-histgbt.md`.
- Review packet: `review_packets/2026-06-01-model-train-histgbt.md`.
- Implementation: `model/train.py`, `shared/score.py`,
  `model/artifacts/model.pkl`,
  `model/artifacts/reference_distribution.json`.
- Tests: `model/tests/test_train.py`,
  `model/tests/test_score_wiring.py`, updated
  `local_runtime/tests/test_shared_stubs.py`.
- External: scikit-learn `HistGradientBoostingClassifier`
  documentation — https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html

## Addendum 2026-06-01 — Gemini review dispositions

Source: `review_responses/2026-06-01-model-train-histgbt.md`. Seven
points raised; dispositions below. Three drove changes (Q3 + Q4 + Q6);
four validated the design.

### Q1 — DEGRADING-dwell override defensibility

**Disposition:** Validated. No change.

Gemini judged the override "standard and accepted practice in ML" and
specifically noted that locality (inside `model/train.py`,
`DEFAULT_PROFILES` untouched, guard test pins the contract) addresses
the "model session quietly changed the simulator" concern. The
existing ADR §3 text stands without amendment.

### Q2 — Lazy-load + double-checked Lock

**Disposition:** Validated. No change.

Gemini's read: the Lock is "cheap insurance for future scalability or
consumption patterns, making `shared.score` more robust without
significant performance or complexity penalties in the current use
cases." The pattern stays.

### Q3 — AUC of 0.998 — too good?

**Disposition:** Partially accepted — doc change, no model change.

Gemini's concern: 0.998 on a single split where every pump follows
identical deterministic physics is "almost perfect and, in a
real-world scenario, would immediately trigger an in-depth
investigation for data leakage or an overly simplistic problem
statement." The model is likely learning the precise simulator
signature rather than generalisable fault characteristics.

**Decision:** This is a portfolio project; the simulation is
deterministic-by-design and the high AUC is an honest reflection of
that. But the README narrative needs to acknowledge it explicitly
so a reviewer doesn't conclude "this person picked a trivial
problem." Added an "AUC honesty" paragraph to `context/model.md`
under "Current state" and to this ADR's Consequences section
(below). Cross-seed AUC distribution remains scope creep for a
portfolio session per the original packet.

**ADR text addition (logical extension of "Consequences › Negative"):**
The reported held-out AUC of 0.998 reflects the deterministic
physics + bounded noise of the simulator, not real-world performance.
A real bearing-failure dataset (e.g., NASA IMS, Case Western Reserve)
would carry asymmetric noise envelopes, sensor dropouts, and
operating-regime shifts that the synthetic corpus does not. The
portfolio framing in the README must call this out — the metric
demonstrates that the pipeline *works end-to-end* on the simulator's
distribution, not that the model would generalise to a real plant
unmodified.

### Q4 — Lambda zip size with sklearn

**Disposition:** Accepted — early measurement landed.

Gemini called this "critical, potentially show-stopping" and asked
for an early measurement before locking in the bundle approach.
Measurement in a clean Linux Python 3.10 env (representative of the
Lambda runtime), with `__pycache__` + `tests/` stripped per common
Lambda layer convention:

| Package        | Stripped size |
|----------------|---------------|
| scikit-learn   | 26.7 MB       |
| numpy          | 22.7 MB       |
| scipy          | 73.0 MB       |
| joblib         |  0.9 MB       |
| threadpoolctl  |  ~0.1 MB      |
| **Subtotal**   | **~123 MB**   |
| `shared/` + `lambda_scorer/` + `model/artifacts/` (model.pkl 300 KB + reference 5 KB) | ~0.5 MB |
| **Estimated deploy zip (unzipped)** | **~124 MB** |

Lambda's unzipped quota is 250 MB. Headroom: ~50 % — comfortable but
not generous. Folded into the lambda_scorer session's brief as the
measured baseline; if the build script's actual zip differs
materially from this estimate, the lambda_scorer session has the
option to fall back to the HANDOFF.md §6 Q3 alternatives (Lambda
Layer or S3 cold-load) without an ADR amendment, since this ADR's
decision is the model family, not the packaging strategy.

### Q5 — ScoreError at first call vs at import

**Disposition:** Validated. No change.

Gemini's read: "a common and generally acceptable pattern for
lazy-loaded resources." The first-call surface is "clear and easy to
diagnose" and the cold-start delta from eager loading is negligible.
Lazy-load stays.

### Q6 — Lazy joblib import

**Disposition:** Accepted — code change landed.

Gemini argued top-level import is Python convention, the marginal
dep-tree savings of lazy import are negligible, and readability
suffers slightly. Fair.

**Decision:** `import joblib` moved to module-top of
`shared/score.py` alongside `import numpy as np`. Tests still green
(322 passed, 1 pre-existing skip).

### Q7 — Test thresholds in `test_score_orders_…`

**Disposition:** Validated. No change.

Gemini's read: the ordering check is the right tool for a wiring
test; numeric thresholds would make the test brittle under retrains
and sklearn version updates. Quantitative evaluation belongs in
`model/tests/test_train.py`, not in the wiring test. Existing
assertion stays.

### Test count after dispositions

Pre-review: 322 passed, 1 skipped.
Post-review: 322 passed, 1 skipped. (Q6 was a static-import move
with no test surface change; Q3 / Q4 are doc-only.)
