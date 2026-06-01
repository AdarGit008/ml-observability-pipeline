# Review Packet 2026-06-01 — model — train HistGBT + swap shared.score

> Paste this entire file into Gemini via:
> `.\scripts\gemini_review.ps1 -Slug 2026-06-01-model-train-histgbt`
> (PowerShell from repo root) or `./scripts/gemini_review.sh 2026-06-01-model-train-histgbt` (bash).

## Role for Gemini

You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)

1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. **Mode parity between local and AWS demo paths.** (Especially load-bearing here — this session edits the parity-bounded `shared/` package.)
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

This session ships the first real (non-stub) `shared.score.score`. A new `model/train.py` drives `Pump.step()` in fast-forward across 30 simulated pumps, extracts the 8 features via `shared.features.extract_features` (mode-parity preserving), labels each sample with a 48h-failure horizon, fits a `HistGradientBoostingClassifier(max_depth=5, max_iter=100)`, and writes `model.pkl` + `reference_distribution.json`. `shared/score.py` is swapped to a lazy-load wrapper that calls `predict_proba` on the bundled classifier; the function signature `(Mapping[str, float]) -> float` is preserved so the three `test_structural_parity_*` tests (ADR 0005) stay green. Held-out AUC = 0.998 on 6 pumps held out from the 30-pump corpus. ADR 0006 captures the model-family + feature-engineering + training-time-DEGRADING-dwell-stretch rationale. The simulator's `DEFAULT_PROFILES` are untouched — the dwell stretch lives entirely in `model.train._training_profiles`.

## Files changed

- **New:** `model/__init__.py`, `model/train.py`, `model/tests/__init__.py`, `model/tests/test_train.py`, `model/tests/test_score_wiring.py`.
- **New artifacts (committed):** `model/artifacts/model.pkl` (~300 KB), `model/artifacts/reference_distribution.json` (~5 KB).
- **New:** `docs/adr/0006-model-family-and-feature-engineering.md`.
- **Replaced (full rewrite):** `shared/score.py` — stub `clip(vibration_amp_mean_5m / 3.0, 0, 1)` → lazy-loaded HistGBT bundle with schema validation. Lines: 44 → 147.
- **Updated:** `local_runtime/tests/test_shared_stubs.py` — removed two stub-specific clamp tests; kept the interface-contract tests. The drift-side tests are unchanged (drift is still a stub).
- **Updated:** `requirements.txt` — added `scikit-learn>=1.4` and `joblib>=1.3` with one-line justifications.
- **Updated:** `context/model.md` — status moved from "not started" to "shipped, AUC 0.998 on held-out 6 pumps." Two open questions added.

## Key diffs

### shared/score.py — the swap

```python
# OLD (stub):
def score(features: Mapping[str, float]) -> float:
    raw = features["vibration_amp_mean_5m"] / 3.0
    return max(0.0, min(1.0, raw))

# NEW (lazy-loaded real model):
def score(features: Mapping[str, float]) -> float:
    clf = _load_classifier()  # cached after first call; thread-safe
    X = np.array([[features[name] for name in FEATURE_NAMES]], dtype=np.float64)
    proba = clf.predict_proba(X)
    return float(proba[0, 1])
```

`_load_classifier` validates the artifact at `<repo>/model/artifacts/model.pkl`, asserts `bundle["feature_names"] == FEATURE_NAMES`, raises `ScoreError` (subclass of `RuntimeError`) on missing/mismatched artifacts.

### model/train.py — the training-time DEGRADING override

```python
TRAINING_DEGRADING_DWELL_TICKS: int = HORIZON_TICKS  # 86_400 = 48h at 2s/tick

def _training_profiles(healthy_dwell_ticks: int) -> dict[PumpState, StateProfile]:
    profiles = dict(DEFAULT_PROFILES)
    # HEALTHY.dwell randomised per pump
    profiles[PumpState.HEALTHY] = StateProfile(
        rate_per_tick=0.0, ceiling=0.05, dwell_ticks=healthy_dwell_ticks,
    )
    # DEGRADING dwell stretched 200 → 86,400 ticks; rate scaled to preserve ceiling
    new_rate = (0.30 - 0.05) / TRAINING_DEGRADING_DWELL_TICKS
    profiles[PumpState.DEGRADING] = StateProfile(
        rate_per_tick=new_rate, ceiling=0.30,
        dwell_ticks=TRAINING_DEGRADING_DWELL_TICKS,
    )
    # FAILING and FAILED untouched (DEFAULT_PROFILES).
    return profiles
```

### local_runtime/tests/test_shared_stubs.py — what was removed

The two `test_score_clamps_at_*` tests pinned the stub's `clip(vibration_amp_mean_5m / 3.0, 0, 1)` math. The file docstring originally said "When the model + drift sessions land real implementations, these tests update with the actual semantics but the interface contracts stay" — so this is the anticipated workflow. The 3 interface-contract tests (`bounded`, `deterministic`, `increases_with_vibration_mean`) and the 4 PSI-stub tests are unchanged.

## Specific questions for Gemini

1. **Is the training-time DEGRADING-dwell override (ADR 0006 §3) the right call, or is there a cleaner shape?** Alternatives considered in the ADR: shorten the horizon (rejected — breaks PLAN.md §2.3 spec), stretch `DEFAULT_PROFILES` globally (rejected — breaks 60s demo), use a separate physics module (rejected on YAGNI). Specifically: does "the simulator's clock is intentionally compressed for demos; training uses physics-paced version" read as a defensible trade-off in a portfolio, or as "model session quietly changed the simulator's behavior"? The override is locally contained (one function, two profile overrides) and a guard test pins the contract.

2. **Is lazy-load + double-checked locking in `shared/score.py` overengineered for the Lambda case?** Lambda containers are single-threaded per invocation today, so the `Lock` is dead weight there. Local-runtime is async-single-loop, also single-threaded. The Lock is defensive against a hypothetical future ProcessPoolExecutor fan-out (Gemini Q2 of the 2026-05-29 review touched on this). Should it stay as cheap insurance, or get removed?

3. **AUC of 0.998 — is that too good?** On a 30-pump corpus where every pump's failure trajectory follows the same physical model, the classifier may be memorising the trajectory shape rather than learning generalisable features. The split is by pump (not by time), so leakage isn't from temporal proximity. Possible failure modes: (a) the random.Random(per_pump_seed) noise is too similar across pumps; (b) the deterministic dwell sampling per seed gives the test pumps trajectories the train pumps have seen near-copies of. Should this session add a cross-seed AUC distribution check, or is that scope creep for a portfolio project?

4. **Bundling 300 KB classifier + 5 KB JSON in the deploy zip + scikit-learn (~25 MB unzipped) — is the Lambda zip still going to fit under the 250 MB unzipped quota?** HANDOFF.md §6 Q3 picked "bundle" as the default. The lambda_scorer session will land the `scripts/build_lambda.ps1` staging step from ADR 0005 §Q1 Addendum. Should this session pre-empt the size question by computing the zipped sklearn footprint, or defer to the lambda_scorer session?

5. **Feature-schema mismatch raises `ScoreError` at load time, but only when `score()` is first called.** A misconfigured Lambda (wrong pickle in the deploy package) would pass the cold-start init step and only fail on the first message. Is that acceptable — first-message-failure is cheap to detect and explicit — or should the check fire eagerly at module import?

6. **`shared.score._load_classifier` imports `joblib` lazily inside the function body** so callers who never invoke `score()` don't need joblib installed. Adds branchy code for a marginal dep-tree benefit. Worth keeping, or just import at module top?

7. **`model/tests/test_score_wiring.py::test_score_orders_healthy_below_pre_failure`** uses two hand-crafted feature dicts. The "healthy" one scores 0.002 and the "pre-failure" one scores 0.23. The test only asserts `pre_fail > healthy`, not absolute thresholds — should it assert tighter bands (e.g., `healthy < 0.1`, `pre_fail > 0.5`) to catch a regression where the model trends but doesn't separate well, or is the ordering check sufficient?

## What I'm NOT looking for in this review

- **Style / formatting** — line lengths, naming, etc.
- **Test coverage gaps unrelated to the scoring contract** — drift / PSI tests are owned by the drift session.
- **PLAN.md `n_pumps=30` and `30 days` literal interpretation** — addressed in the ADR. The 30 pumps stays; the 30 days is honoured via MAX_TICKS_PER_PUMP cap + per-pump short-circuit at failure_tick (sessions just don't simulate ticks past the failed transition because they add no signal).
- **DynamoDB schema** — still open per HANDOFF.md §6 Q5; not this session's territory.

## Resolution (filled 2026-06-01 after Gemini response)

Full long-form dispositions in ADR 0006 §"Addendum 2026-06-01 — Gemini review dispositions". Summary:

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. DEGRADING-dwell override defensibility | Validated | Gemini: "standard and accepted practice in ML"; locality (one function, guard test) addresses the "silent change" concern. No edit. |
| 2. Lazy-load + double-checked Lock | Validated | "Cheap insurance for future scalability or consumption patterns." Keep. |
| 3. AUC 0.998 — too good? | Partially accepted | Cross-seed CV would be scope creep, but Gemini's "almost-perfect, in real-world would trigger investigation" framing must surface in the docs. Added an **AUC honesty** paragraph to `context/model.md` and to ADR 0006 §Consequences (Negative). No model change. |
| 4. Lambda zip size with sklearn | Accepted | Early measurement landed (sklearn 27 + numpy 23 + scipy 73 + joblib 1 ≈ 124 MB unzipped). ~50 % headroom under 250 MB. Folded into context/model.md and ADR 0006 §Q4 addendum. Bundle stays the lambda_scorer default; alternatives reachable without re-opening this ADR. |
| 5. ScoreError at first call vs at import | Validated | "Common and generally acceptable" for lazy-loaded resources. Lazy-load stays. |
| 6. Lazy joblib import | Accepted | `import joblib` moved to module top in `shared/score.py` alongside `import numpy as np`. Tests still 322 passing. |
| 7. Test thresholds in `test_score_orders_…` | Validated | Ordering check is the right tool for a wiring test; thresholds would be brittle under retrains. Quantitative evaluation lives in `test_train.py`. Keep. |

### Test count after dispositions

Pre-review: **322 passed, 1 skipped**.
Post-review: **322 passed, 1 skipped**. (Q6 = static import move with no test surface change; Q3 / Q4 are doc-only.)
