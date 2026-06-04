# ADR 0007 — Real PSI Implementation, Laplace Smoothing, Reference-Load Semantics, Per-Tick Cadence

- **Status:** Accepted (Gemini review folded 2026-06-01; PO sign-off pending)
- **Date:** 2026-06-01
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

The drift session ships the first non-stub `shared.drift.compute_psi`.
Five interlocking design questions fell out of the implementation, each
non-obvious enough to deserve a record, and grouping them here avoids
five follow-up ADRs that cross-reference each other:

1. **PSI formula and binning.** PLAN.md §2.7 specifies the formula
   `PSI = Σ (a%-e%) · ln(a%/e%)` and the < 0.10 / 0.10–0.25 / > 0.25
   bands. `context/_interfaces.md` §"PSI parameters" pins 10
   equal-frequency bins, sliding 1-hour window. The model session
   built the per-feature `bin_edges` from training-time quantiles.
   What remains: the bin-membership mechanism (np.histogram vs.
   hand-rolled), out-of-range handling, and how Laplace smoothing is
   applied (count-side add-α vs. percentage-side ε floor).
2. **Laplace smoothing α.** Standard literature values range from
   tiny ε (epsilon-style, just avoiding div-by-zero) to Laplace's
   original α = 1.0 (one phantom observation per bin) to Jeffreys' 0.5.
   Each shapes how a low-sample-count actual distribution against the
   reference scores PSI.
3. **`reference=None` semantics.** ADR 0005 left the stub permissive
   (`reference=None` → return sentinel PSI). The real implementation
   needs to either raise on missing reference or fall back to lazy-
   loading from disk. The existing `service.py` call site
   (`compute_psi([features], reference=None)`) constrains the choice.
4. **Model/reference version match.** Both `model.pkl` and
   `reference_distribution.json` carry `model_version`. If they
   drift apart (one rebuilt without the other), the scorer and the
   drift signal would reference different model schemas — silent
   parity violation. Where the check lives and how it fails is open.
5. **Per-tick PSI write cadence.** ADR 0005 §Addendum 2026-05-29 Q3
   left this open: PLAN.md §2.5 prescribes per-tick PSI computation,
   but Gemini flagged the CPU and storage waste of writing PSI on
   every 2-second tick. ADR 0005 deferred the resolution to this
   session.

Constraints driving the decisions:

- `context/drift.md` invariants: pure Python + numpy, no pandas (Lambda
  cold-start cost).
- ADR 0005 mode-parity boundary: implementation in `shared/drift.py`;
  service.py and lambda_scorer import as peers; structural parity tests
  pin the load path.
- `context/_global.md` north star #4 (mode parity): the local and
  AWS modes must produce the same PSI for the same window.
- `context/_global.md` north star #1 ($0 cost): Lambda CPU minutes
  matter; we don't burn them on identical-by-design computations.

Carry-in from the model session (2026-06-01): the `reference_distribution.json`
was built from a 48h-stretched-DEGRADING training corpus, not the
13-minute demo-paced DEGRADING. The drift session is the natural place
to address (or document) the mismatch. See `context/model.md`
§"Reference distribution validity at demo time" and §"Open follow-ups
(carry into next session)" #4 in the model session log.

## Decision

The drift session adopts the following five-part design:

1. **PSI formula and binning.** Per-feature: bin the rolling window's
   feature column against the reference's `bin_edges` via
   `numpy.histogram`. Out-of-range values are `numpy.clip`-ed to the
   outermost edges (np.histogram would otherwise silently drop them,
   shrinking the actual total and skewing percentages). Laplace
   add-α smoothing applied to *counts* (not percentages — keeps the
   smoothing mathematically equivalent to a Bayesian Dirichlet prior).
   Pure numpy; no scipy.

2. **Laplace α = 1.0.** Standard Laplace prior — one phantom
   observation per bin. On a 1800-sample window (1 hour at 2 s/tick),
   an empty actual bin against a 10%-expected bin contributes ≈ 0.52
   to PSI — meaningful but not alarmist. Defined as the module
   constant `shared.drift.LAPLACE_ALPHA`.

3. **`reference=None` → lazy-load from disk.** The function reads
   `model/artifacts/reference_distribution.json` from a module-cached
   path on first call (same shape as `shared.score._load_classifier`).
   The existing `service.py` call site continues to work without
   changes. Loud `DriftError` if the file is missing or malformed —
   the alternative (silent zero-PSI fallback) would mask broken
   deployments. Tests that want to skip the disk path pass a
   synthetic `reference` dict directly.

   **Superseded by §Addendum 2026-06-01 Q2.** Gemini flagged the
   module-cache + `_reset_reference_cache` test helper as a debt
   signal. Refactored to make `load_reference(ref_path, model_path)`
   the single I/O entry point, called once at service init; the
   reference is then an explicit, required argument to
   `compute_psi`. See Addendum for the long form.

4. **Model/reference version-match check at first reference load.**
   If `model/artifacts/model.pkl` is also on disk, the `model_version`
   fields from both artifacts are compared. A mismatch raises
   `DriftError` (a sibling of `shared.score.ScoreError` —
   `RuntimeError` subclass, same posture). If `model.pkl` is absent
   (dev environment shipping only the reference), the check is
   skipped. `joblib` is lazy-imported inside the check branch so a
   "drift without sklearn" environment stays importable per the
   `context/drift.md` dependency invariants.

5. **PSI compute cadence: every N ticks, default N = 30 (≈ once per
   minute at 2 s tick).** Schema unchanged from ADR 0005 (PSI fields
   stay in `pump_telemetry`). Non-compute ticks emit `ScoredRow.psi
   = None`; `influx_writer.build_point` omits the `psi_*` fields
   entirely so InfluxDB stores nulls. Grafana's `last` aggregator
   surfaces the most recent computed value, giving a step-and-hold
   look rather than per-tick noise. `service.py` maintains a per-pump
   feature-history deque (`config.psi_window_samples` = 1800 at
   default tick) and a per-pump tick counter; PSI fires when
   `tick % psi_period_ticks == 0`.

Acceptance: 322 + 18 new tests passing post-implementation; 340 + 1
skipped passing post Gemini-review fixes. Structural parity tests
still green.

## Alternatives considered

### 1. PSI formula / binning

**A. `np.histogram` + count-side Laplace smoothing (the decision).**
Standard and well-understood. Clip-then-histogram correctly handles
out-of-range values without contaminating the actual total. Count-
side smoothing is the textbook Dirichlet prior interpretation.

**B. Hand-rolled `np.searchsorted`-based binning.** Slightly faster
because we skip the histogram dtype dispatch. Rejected on
maintainability: `np.histogram` is the documented public API and a
reviewer who later reads the file shouldn't have to verify our
hand-roll matches the numpy implementation.

**C. Percentage-side ε floor instead of count-side Laplace.** Common
in PSI literature: clamp `actual_pct` and `expected_pct` to `[ε, 1]`
before taking the log ratio. Equivalent for the div-by-zero
protection but loses the Bayesian interpretation. Rejected because
the count-side path is more honest about what we're doing.

**D. Drop-out-of-range values instead of clipping.** Streaming-data
distributions occasionally land outside the training-time min/max
(e.g., a transient spike). Dropping them shrinks the actual total
and inflates PSI in the rest of the bins — an artifact of the
binning, not real drift. Rejected: clipping treats the outlier as
"furthest bin" which is at least directionally honest.

### 2. Laplace smoothing α

**A. α = 1.0 (the decision).** Standard Laplace prior. Easy to
explain ("treat each bin as if we'd seen one prior observation").
Empty-but-expected-10% bin contributes ≈ 0.52 to PSI on 1800-sample
windows — meaningful, not alarmist.

**B. α = 0.5 (Jeffreys prior).** Slightly less aggressive smoothing,
marginally more sensitive to real drift. Defensible but less
common; "Laplace" and "α = 1.0" are widely-known synonyms in the
ML literature.

**C. α = 1e-4 (epsilon-style).** True smoothing barely registers;
this is really just div-by-zero protection. Empty bin contribution
dominated by `log(α)` magnitude — PSI can spike well above 0.25
from a single empty bin even on a healthy fleet. Rejected as
demo-fragile.

### 3. `reference=None` semantics

**A. Lazy-load from disk + module-cache (the decision).** Preserves
the existing `service.py` call site shape. Loud failure when the
reference is missing or malformed. Tests with synthetic references
pass an explicit dict.

**B. Raise loudly on `reference=None` always.** Forces callers to
load the JSON themselves. Cleaner separation of concerns but breaks
the existing call site and pushes file-path knowledge to two more
places (service.py + a future lambda_scorer).

**C. Silent zero-PSI fallback on `reference=None`.** Compatible with
the existing call site without any setup work. Rejected because it
masks broken deployments — a Lambda whose reference distribution
got dropped during packaging would silently report PSI = 0
everywhere, which would read as "fleet is perfectly stable" on the
dashboard.

(See §Addendum 2026-06-01 Q2 — the original A-vs-B-vs-C trade-off
was reopened by Gemini review and resolved by a refactor that
separates the I/O entry point from `compute_psi`.)

### 4. Model/reference version match

**A. Check inside `_load_reference`, raise `DriftError` on
mismatch (the decision).** Symmetric with `shared.score`'s
`ScoreError` — same posture, same exception family. Skipped when
`model.pkl` is absent so a drift-only environment is still
importable. `joblib` lazy-imported to keep the import surface
narrow.

**B. Check at module-import time.** Surface failures immediately
instead of at first `compute_psi` call. Rejected because it forces
`model.pkl` to be on disk at import time even for tests that don't
exercise the load path — same trade-off the model session made for
`shared.score` (Gemini Q5 of the 2026-06-01 review).

**C. Defer to the caller (`lambda_scorer.handler`).** The handler
could load both artifacts and compare versions itself. Rejected
because the drift module owns the reference; making the handler
own a cross-artifact invariant the drift module manages is poor
encapsulation.

### 5. Per-tick PSI write cadence

**A. Every Nth tick, default N = 30 ≈ once per minute (the
decision).** Schema unchanged per ADR 0005 §Addendum Q3 ("either
is compatible with this ADR's schema"). Service.py grows a feature-
history deque + tick counter. ScoredRow.psi becomes `Optional` so
non-compute ticks carry no PSI. Influx writer omits `psi_*` fields
when None — InfluxDB stores nulls, Grafana `last` aggregates pull
the most recent computed value.

**B. Every tick.** Faithful to PLAN.md §2.5 ("update PSI accumulator
on each tick"). Wastes Lambda CPU on identical-by-design
computations between cadence boundaries. Rejected on $0 north star
(north star #1).

**C. Separate `pump_drift` measurement on a tumbling 5-min
schedule.** Cleanest architecturally; PSI on its own retention
policy. Bigger lift: new InfluxDB measurement, dashboards session
has to learn the join. ADR 0005's `pump_telemetry` schema would
need amendment. Rejected as scope-creep for the drift session;
reachable via a follow-up ADR if a future scaling story warrants.

## Consequences

**Positive:**

- **PSI math is honest and well-understood.** The np.histogram +
  Laplace pipeline is the textbook implementation; a reviewer can
  read the file in 5 minutes and verify it matches the PLAN.md §2.7
  formula.
- **Mode parity is preserved.** Both local and AWS modes call the
  same `shared.drift.compute_psi`. Structural parity test
  (`test_structural_parity_compute_psi_loads_from_shared`) stays
  green. The lambda_scorer session can drop the function in as-is.
- **The existing service.py call site keeps working.** `reference=
  None` triggers lazy disk-load; no orchestration changes were
  needed beyond the cadence + feature-history work. (Updated by
  §Addendum Q2: post-refactor, service.py owns the load explicitly
  and `compute_psi` is a pure function.)
- **Broken deployments fail fast.** Missing reference → DriftError
  with the path. Malformed JSON → DriftError with the parse error.
  Version mismatch → DriftError naming both versions. No silent
  zero-PSI.
- **CPU and storage bounded.** PSI fires once per minute per pump
  rather than every 2 seconds — 30× reduction in PSI compute and
  storage. Demo dashboard updates feel responsive (1-minute cadence
  matches operator perception) without burning Lambda CPU on
  identical-by-design recomputes.

**Negative:**

- **Two physical models in `service.py`.** The 5-minute feature
  rolling window (`self._window`) and the 1-hour PSI feature
  history (`self._feature_history`) coexist. Both are needed
  (different time horizons, different sources of truth: telemetry
  vs. extracted features), but a future reader has to learn the
  distinction. Mitigated by the docstring at the top of `service.py`
  and the property `feature_history_size(pump_id)` exposed for the
  smoke step. See `context/local_runtime.md` §"State-management
  asymmetry between local and Lambda" for the cross-mode picture.
- **PSI on a 1-element window is degenerate but tolerated.** Service
  warm-up sees the first compute_psi call on a single-feature-dict
  window. With Laplace α = 1.0, the math is finite and the resulting
  PSI is high (one bin holds 100% of mass). The cadence default
  (N = 30) ensures the first compute_psi happens at tick 30 when
  the window has filled — but a test that sets `psi_period_ticks=1`
  will see high PSI on the first tick. Documented in the service
  docstring §"Warm-up policy."
- **Demo-paced PSI bias carry-in is REAL and SEVERE.** The
  reference was built from 48h-stretched DEGRADING. On demo-paced
  telemetry, the per-feature quantiles do not overlap cleanly —
  measured PSI 1.3–6.7 SIGNIFICANT on 7 of 8 features for HEALTHY
  fleets. The drift session ships the real implementation but does
  not recompute the reference; the carry-in is logged in the
  session §Verification with measurements on demo-paced traffic.
  **PO decided this ADR's option (c) "accept and document" is
  untenable;** option (a) — recompute reference from demo-paced
  HEALTHY data — is the recommended next step. Reachable as a
  follow-up without re-opening this ADR (the formula, smoothing,
  cadence decisions hold; only the reference baseline changes).
- **`joblib` lazy-import inside `_check_model_version_match`.** A
  catch-Exception block wraps the load to convert any joblib /
  pickle error into a `DriftError`. The error message names both
  paths so the operator can diagnose. (Updated by §Addendum Q4:
  except narrowed to specific exception types.)

**Follow-ups:**

- Gemini review packet
  (`review_packets/2026-06-01-drift-real-psi.md`). **Folded
  2026-06-01; see Addendum below.**
- Demo-paced reference recomputation (Reference-Validity carry-in).
  Decision deferred to the recording session per the PO call above.
- Lambda packaging — the lambda_scorer session needs to bundle
  `shared/drift.py` + `model/artifacts/reference_distribution.json`
  into the deploy zip. Reference is small (~5 KB) so this is
  mechanical. The model/reference version-check branch assumes both
  artifacts are in the zip; if a future ADR moves model.pkl to S3
  cold-load, the version-check branch needs the S3 path injected.
- Fleet-PSI EventBridge Lambda (PLAN.md §2.7). Uses the same
  `compute_psi` against an aggregated 5-minute fleet window. Out of
  scope here; the fleet aggregation logic is the EventBridge
  Lambda's territory.

## References

- PLAN.md §2.7 (PSI formula + thresholds — what this ADR implements).
- PLAN.md §2.5 ("update PSI accumulator on each tick" — the line ADR
  0005 §Addendum Q3 deferred and this ADR resolves to "every N
  ticks").
- ADR 0005 §Addendum Q3 (per-tick cadence open question — closed
  here).
- ADR 0006 §Negative ("Reference distribution computed from training
  set only" — the Reference-Validity carry-in).
- `context/drift.md` (pure-numpy invariant, 10-bin equal-frequency
  default).
- `context/_interfaces.md` §"PSI parameters" (10 bins, 1-hour
  rolling window).
- `context/model.md` §"Reference distribution validity at demo time"
  (the carry-in).
- `context/local_runtime.md` §"State-management asymmetry between
  local and Lambda" (the Q7 documentation).
- Session log: `docs/sessions/2026-06-01-drift-real-psi.md`.
- Implementation: `shared/drift.py`, `local_runtime/service.py`,
  `local_runtime/influx_writer.py`, `local_runtime/config.py`.
- Tests: `local_runtime/tests/test_shared_stubs.py` (rewritten PSI
  tests + Q6 golden-test comments),
  `local_runtime/tests/test_drift_load.py` (load path — uses
  explicit paths post Q2 refactor),
  `local_runtime/tests/test_service.py` (cadence),
  `local_runtime/tests/test_influx_writer.py` (psi=None handling),
  `local_runtime/tests/test_config.py` (PSI properties).
- Population Stability Index — original formulation:
  Karakoulas, "Empirical Validation of Retail Credit-Scoring
  Models," 2004; modern references in scikit-learn's drift-detection
  literature.

## Addendum 2026-06-01 — Gemini review dispositions

Source: `review_responses/2026-06-01-drift-real-psi.md`. Seven points
raised; dispositions below. Three drove code changes (Q2 + Q4 + Q7);
one drove a test-comment polish (Q6); three validated the design
(Q1 + Q3 + Q5).

### Q1 — Laplace α = 1.0 vs. Jeffreys α = 0.5 vs. ε-style

**Disposition:** Validated. No change.

Gemini's read: α is not load-bearing on a 1800-sample window; the
choice between Laplace and Jeffreys "is unlikely to dramatically
change the overall signal." Laplace's "one phantom observation per
bin" is "the correct Bayesian framing." α = 1.0 stays.

### Q2 — `reference=None` lazy-load semantics

**Disposition:** Accepted — refactor landed this session.

Gemini's point: the lazy disk-load as a side effect of
`reference=None` makes `compute_psi` impure; the module-level
`_ref_cache` plus the `_reset_reference_cache` test helper is a
"strong signal that the module-cached state makes testing harder
and less isolated." Function purity + explicit inputs > pragmatism.

**Decision:** Split the API:

- New `shared.drift.load_reference(ref_path, model_path) -> dict`
  is the **single I/O entry point**. Called once per process
  lifetime (`ScorerService.__init__` for the local subscriber;
  Lambda cold-start path for the future scorer). No module-level
  caching — the caller stores the returned dict.
- `shared.drift.compute_psi(window_features, reference)` is now a
  pure function. The `reference` argument is **required** (no
  default). No I/O, no module state.
- `ScorerService.__init__` accepts an optional `reference=` for
  test injection; falls back to `load_reference()` once at init.
- Removed `_ref_cache`, `_load_lock`, `_reset_reference_cache`.
- Tests that previously monkeypatched `_REF_PATH` / `_MODEL_PATH`
  now pass paths to `load_reference` directly via the new
  signature. The cache-reset test goes away (no cache to reset).

**Trade-off accepted:** the previous design preserved the existing
`compute_psi([features], reference=None)` call site at the cost of
shared module state. The refactor breaks that call shape but
restores function purity. Service.py owns the load path
explicitly; that's the right place for the I/O concern. The
parity boundary is unaffected — `compute_psi` is still the shared
function both modes import.

### Q3 — Per-tick PSI cadence

**Disposition:** Validated. No change.

Gemini's read: "null on non-compute ticks → Grafana `last`
aggregator surfaces the most recent computed value" is
semantically correct ("no value *was computed* at this precise
moment," not "the system failed"). Separate `pump_drift`
measurement remains a YAGNI follow-up. The tick-N start (not
tick-0) "is a good default, preventing misleading early signals."

### Q4 — Model/reference version-match check

**Disposition:** Accepted — narrowed exception + docstring landed.

Gemini's point: placing the check inside `load_reference` is
sensible, but `except Exception` is "too broad. It could mask
unrelated Python errors, making debugging difficult."
Lazy-importing `joblib` is endorsed.

**Decision:** Replaced `except Exception` in
`_check_model_version_match` with `except (OSError, EOFError,
pickle.UnpicklingError, ValueError, KeyError, AttributeError)` —
the specific failure modes for a corrupt/partial/wrong-pickle-version
model.pkl. RuntimeError-family deliberately not caught so
KeyboardInterrupt / MemoryError still propagate.

Added a "Why we skip when `model.pkl` is absent" section to the
function docstring, naming the drift-without-scorer deployment
pattern (fleet-PSI Lambda layer only needs the reference). New
test `test_load_reference_model_pkl_corrupt_raises_drifterror`
pins the new exception-narrowing.

### Q5 — Reference-Validity carry-in measurement

**Disposition:** Validated — option (a) recommendation confirmed.

Gemini: "If the system's baseline 'healthy' state already triggers
high drift values, the metric becomes unactionable due to constant
'false positives' or high noise, leading to alert fatigue."
Demo-paced HEALTHY is "the pragmatic and operationally sound
choice." Suggested naming the new artifact
`operational_reference_distribution.json` to make provenance clear.

**Decision:** Option (a) confirmed as the next-session direction.
Folded the "operational_" naming suggestion into the next-session
brief in `docs/sessions/2026-06-01-drift-real-psi.md` §"Open
follow-ups." No code change this session.

### Q6 — Golden tests for PSI value assertions

**Disposition:** Accepted — comment polish landed.

Gemini: "for a foundational metric, this forced re-validation is
often a feature, not a bug, ensuring that changes are deliberate
and fully understood." Suggested adding comments naming the tests
as "golden tests" so future maintainers understand the brittleness
is intentional.

**Decision:** Added a section to the
`local_runtime/tests/test_shared_stubs.py` module docstring naming
the PSI value tests as **golden tests** with analytically-derived
magnitudes. Each affected test docstring opens with `GOLDEN TEST:`
and explains the analytical derivation (e.g., why N=200, 20 per
bin gives PSI = 0 exactly under α = 1.0). No assertion changes.

### Q7 — Service.py's two-deque shape

**Disposition:** Validated with documentation.

Gemini: "two deques is a clear separation of concerns... the
asymmetry between the local runtime (in-memory deques) and the
Lambda hot path (rebuilding from DynamoDB) is a common and expected
consequence of designing for a serverless environment... not a code
smell."

**Decision:** Added a "State-management asymmetry between local
and Lambda (Gemini Q7, 2026-06-01)" section to
`context/local_runtime.md` with a per-state table documenting
where each piece of runtime state comes from in each mode (window
deque, feature-history deque, reference, model). The asymmetry is
preserved as an intentional adaptation; the documentation is for
the next reader.

### Test count after dispositions

Pre-Gemini-review: 340 passed, 1 skipped.
Post-Gemini-review:
- Q2 refactor removed `test_reset_reference_cache_clears_state`
  (no cache to reset).
- Q4 added `test_load_reference_model_pkl_corrupt_raises_drifterror`.
- Q6 / Q7 are docs only.

Net: 340 passed, 1 skipped. Structural parity tests still green
(`test_structural_parity_compute_psi_loads_from_shared` included).
