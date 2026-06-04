# Gemini Review Packet — drift session: real PSI implementation

- **Date:** 2026-06-01
- **Session log:** `docs/sessions/2026-06-01-drift-real-psi.md`
- **ADR:** `docs/adr/0007-psi-implementation-and-cadence.md`
- **Test count:** 322 → 340 passed pre-review; 340 → 340 post-review-fixes (the Q2 refactor moved 1 cache-reset test out and renamed 1, net 0). 1 skipped, unchanged.

## What changed

Replaced the `shared.drift` sentinel stub with the real PSI
implementation specified in PLAN.md §2.7: per-feature `np.histogram`
against the training-time `reference_distribution.json` bin edges,
Laplace add-α smoothing (α = 1.0), summed
Σ (a%-e%) · ln(a%/e%) contributions. Loud `DriftError` on
missing/malformed reference and on model/reference `model_version`
mismatch.

`local_runtime/service.py` grew a per-pump rolling feature-history
deque (1800 samples at default tick = 1 hour) and a per-pump tick
counter; PSI fires on every Nth tick (default 30 = once per minute at
2s tick) per ADR 0007's resolution of ADR 0005 §Addendum Q3. Non-compute
ticks emit `ScoredRow.psi = None`; `influx_writer.build_point` omits
the `psi_*` fields entirely on those ticks so InfluxDB stores nulls.

Five decisions landed in ADR 0007: formula/binning, Laplace α,
`reference=None` semantics, model/reference version match, per-tick
cadence.

## Files changed

- `shared/drift.py` (rewrite: stub → real PSI; +DriftError; lazy
  reference load with validation).
- `local_runtime/config.py` (+PSI_WINDOW_SECONDS, +PSI_COMPUTE_EVERY_SECONDS,
  +psi_window_samples, +psi_period_ticks).
- `local_runtime/service.py` (+feature_history deque, +tick counter,
  +psi_period_ticks ctor parameter, +feature_history_size accessor).
- `local_runtime/influx_writer.py` (ScoredRow.psi typing → Optional;
  build_point skips psi_* on None).
- `local_runtime/tests/test_shared_stubs.py` (rewrote drift tests).
- `local_runtime/tests/test_drift_load.py` (new — load-path failure
  modes).
- `local_runtime/tests/test_service.py` (cadence tests).
- `local_runtime/tests/test_influx_writer.py` (psi=None handling).
- `local_runtime/tests/test_config.py` (PSI property tests).
- `docs/adr/0007-psi-implementation-and-cadence.md` (new).
- `docs/sessions/2026-06-01-drift-real-psi.md` (this session).
- `context/drift.md` (status: shipped + Reference-Validity finding).

## Constraints reminder for Gemini

- `context/_global.md` north star #4: mode parity. The shared module is
  the boundary between local and AWS. Implementation must not have any
  local-only or AWS-only branches.
- `context/_global.md` north star #1: $0 ceiling. PSI compute every 2s
  per pump × 15 pumps × 24h × 30 days would burn Lambda CPU for
  identical-by-design recomputes. The every-Nth-tick decision is
  defensive on this.
- `context/drift.md` invariants: pure Python + numpy; no pandas. (joblib
  is lazy-imported only inside the model/reference version-check
  branch, not on the hot path.)
- ADR 0005's structural parity tests must remain green —
  `test_structural_parity_compute_psi_loads_from_shared` named
  explicitly in the brief.

## Questions for Gemini

### Q1 — Laplace α = 1.0 vs. Jeffreys α = 0.5 vs. ε-style

We picked α = 1.0 (standard Laplace prior). Alternatives in ADR 0007
§Alternatives. Push us on:

- Are we sensitive to α in a way that matters here? E.g., would
  α = 0.5 make the warning band [0.10, 0.25] crossings noisier on
  short windows?
- Is "one phantom observation per bin" the right Bayesian framing for
  the PSI use case, or is the count-side Laplace really just
  div-by-zero protection in disguise?

### Q2 — `reference=None` lazy-load semantics

`compute_psi` reads `model/artifacts/reference_distribution.json` from
a module-cached path when `reference=None`. We picked this over
"raise loudly" to preserve the existing `service.py` call site.
Trade-offs in ADR 0007 §3.

- Is the lazy disk-load surprising as a side effect of "no caller
  passed a reference"? Would you prefer `compute_psi` to require an
  explicit reference and have a separate `load_reference()` helper?
- The reference is module-cached. Tests that exercise the load path
  call `_reset_reference_cache()` between tests. Is the test-only
  helper a smell, or appropriately minimal?

### Q3 — Per-tick PSI cadence

We compute PSI every `psi_period_ticks` (default 30 = once per
minute) and emit `psi=None` on non-compute ticks; the writer omits
`psi_*` fields entirely so InfluxDB stores nulls. ADR 0005's
addendum disposed Q3 as "either is compatible with this ADR's
schema."

- Is "null on non-compute ticks → Grafana `last` aggregator surfaces
  the most recent computed value" the right user-experience story?
  A reviewer might argue "the data point should always have all
  fields; missing fields suggest broken telemetry."
- We considered (a) every tick, (b) every Nth tick, (c) separate
  `pump_drift` measurement. (c) was rejected as scope creep. Worth
  reopening?
- Per-pump tick counter starts at 0 and increments before the modulo
  check, so the first compute happens at tick N (not tick 0). This
  avoids a meaningless 1-sample PSI on warm-up. Is this the right
  default, or should warm-up wait until the feature-history deque
  has at least M samples?

### Q4 — Model/reference version-match check inside `_load_reference`

We compare `model_version` between `reference_distribution.json` and
`model.pkl` on first reference load, raising `DriftError` on
mismatch. `joblib` is lazy-imported inside the check. If
`model.pkl` is absent, the check is skipped (dev environment).

- Is the cross-artifact check the right place to live? `shared.score`
  could equally well do the symmetric check on its side. Our
  reasoning: the drift module already loads the reference, so
  comparing one more field is cheap; the score module would have to
  read the reference JSON it otherwise doesn't need.
- Lazy-importing `joblib` inside the check branch keeps the drift
  module's dependency surface narrow per `context/drift.md`. Is the
  noqa comment + the catch-Exception → DriftError pattern reasonable,
  or is the broad except too loose?

### Q5 — Reference-Validity carry-in measurement

Demo-paced healthy traffic produces PSI 1.3–6.7 on 7 of 8 features
against the training-time reference (full table in session log
§"Reference-Validity carry-in — MEASUREMENT"). ADR 0007's default
"accept and document" is rejected by the measurement. We recommend
option (a): recompute the reference from demo-paced healthy data in a
follow-up session.

- Is "demo-paced healthy" the right baseline for the reference, or
  is the training-time baseline philosophically correct (the model
  was trained on it) and we should be adjusting thresholds rather
  than recomputing the reference?
- The session log shows option (b) "dual references" as an
  alternative. Are there architectural reasons to prefer (b) over
  (a) for a portfolio project?

### Q6 — Test magnitudes are pinned analytically rather than via
deterministic seeds

The shifted-distribution test pins `[0.10, 0.25)` for a 28%-in-top-bin
shift (computed PSI ~ 0.21). The magnitudes are derived analytically
in ADR 0007 — not from a seeded random draw.

- Is the analytical pinning brittle in a way that a `np.random` seed
  + tolerance assertion wouldn't be? Argument for: tests fail loudly
  if Laplace α changes. Argument against: changing the formula in a
  way that *is* correct will fail the test for the right reason but
  the reviewer has to redo the math.

### Q7 — Service.py's two-deque shape

`ScorerService` now holds two per-pump rolling windows: the
5-minute telemetry window (`self._window`, drives rolling stats in
features) and the 1-hour feature history (`self._feature_history`,
drives PSI). Both are local-only; the Lambda hot path rebuilds both
from DynamoDB. Is this asymmetry a code-smell, or the right
separation of concerns (telemetry for stats, features for drift)?

## Resolution

Source: `review_responses/2026-06-01-drift-real-psi.md`. Seven points
raised; dispositions below. Three drove code changes (Q2 + Q4 + Q7);
one drove a test-comment polish (Q6); three validated the design
(Q1 + Q3 + Q5). Long-form record in ADR 0007 §Addendum 2026-06-01.

| # | Disposition | Action |
|---|-------------|--------|
| Q1 | Accepted — no change | α = 1.0 holds. Gemini confirmed Laplace's interpretation is appropriate and α is not load-bearing on a 1800-sample window. |
| Q2 | Accepted — refactor landed | `compute_psi(window, reference)` — reference required, no default. New `shared.drift.load_reference(ref_path, model_path)` is the single I/O entry point. Removed `_ref_cache`, `_load_lock`, `_reset_reference_cache`. `ScorerService.__init__` accepts an optional `reference=` for injection; falls back to `load_reference()` once at init. Tests that previously monkeypatched `_REF_PATH` / `_MODEL_PATH` now pass paths to `load_reference` directly. |
| Q3 | Accepted — no change | Every-Nth-tick cadence with `psi=None` on non-compute ticks holds. Gemini confirmed "null = not computed at this instant" is the semantically correct read; `pump_drift` separate measurement remains a YAGNI follow-up. |
| Q4 | Accepted — refactor landed | Replaced `except Exception` in `_check_model_version_match` with `except (OSError, EOFError, pickle.UnpicklingError, ValueError, KeyError, AttributeError)` — the specific failure modes for a corrupt/partial/wrong-version pickle. Added a "Why we skip when `model.pkl` is absent" docstring section explaining the drift-without-scorer deployment pattern. New test `test_load_reference_model_pkl_corrupt_raises_drifterror`. |
| Q5 | Accepted — option (a) confirmed for next session | Gemini concurred that the training-time baseline produces operationally unactionable signals; demo-paced HEALTHY is the right reference baseline. Suggested naming the new artifact `operational_reference_distribution.json` to make provenance clear — folded into the next-session brief. |
| Q6 | Accepted — comment polish landed | Added a module-level docstring section calling out the PSI value tests as **golden tests** with analytically-derived magnitudes; each affected test docstring opens with `GOLDEN TEST:` and explains the derivation. No assertion changes. |
| Q7 | Accepted — documentation landed | Added a "State-management asymmetry between local and Lambda (Gemini Q7, 2026-06-01)" section to `context/local_runtime.md` with the per-state table (window source, reference source, model source). The asymmetry is preserved as intentional adaptation, not flagged as a smell — preserving Gemini's reasoning. |

### Test count after dispositions

Pre-review: 340 passed, 1 skipped.
Post-review:
- Q2 refactor removed `test_reset_reference_cache_clears_state` (no cache to reset).
- Q4 added `test_load_reference_model_pkl_corrupt_raises_drifterror`.
- Q6 / Q7 are docs only.

Net change: 0. 340 passed, 1 skipped after the Q2 + Q4 work.
