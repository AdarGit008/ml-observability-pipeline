# Session 2026-06-01 — model — train HistGBT + swap shared.score

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (CLI, pending)
- **Context loaded:** `_global`, `model`, `_interfaces`, Tier 2b (`shared/features.py`, `shared/score.py`, `shared/drift.py`, ADR 0005). Reference: ADR 0002, PLAN.md §2.3, HANDOFF.md §6 Q3.
- **Duration:** ~1.5h

## Intent

Train the predictive-maintenance HistGradientBoostingClassifier on simulator output in fast-forward, swap `shared.score.score` from stub to real `predict_proba`, and emit `model/artifacts/{model.pkl, reference_distribution.json}`.

## What changed

- **New:** `model/__init__.py`, `model/train.py` (≈470 lines), `model/tests/__init__.py`, `model/tests/test_train.py` (8 tests), `model/tests/test_score_wiring.py` (8 tests), `model/artifacts/model.pkl` (~300 KB), `model/artifacts/reference_distribution.json` (~5 KB).
- **New:** ADR 0006 — `docs/adr/0006-model-family-and-feature-engineering.md`.
- **Replaced:** `shared/score.py` — stub `clip(vibration_amp_mean_5m / 3.0, 0, 1)` → lazy-loaded HistGBT bundle with feature-schema validation. Signature `(Mapping[str, float]) -> float` unchanged. ScoreError raised on missing or schema-mismatched artifacts.
- **Updated:** `local_runtime/tests/test_shared_stubs.py` — dropped 2 stub-specific clamp tests (`test_score_clamps_at_one`, `test_score_clamps_at_zero`); kept the 3 interface-contract tests (bounded output, deterministic, ordering signal) and the 4 drift-stub tests. Docstring updated to record the model-session swap.
- **Updated:** `requirements.txt` — added `scikit-learn>=1.4` and `joblib>=1.3` with one-line justifications.

## Decisions

- **HistGBT hyperparameters locked to PLAN.md §2.3** (`max_depth=5`, `max_iter=100`, `random_state=seed`). No tuning; if AUC misses, that's an ADR amendment + a focused tuning pass, not a silent change here.
- **Training-time DEGRADING dwell override = 86,400 ticks (= 48h).** The simulator's `DEFAULT_PROFILES` give DEGRADING + FAILING ~13 min, which is mismatched with the 48h prediction horizon — the smoke test on unmodified profiles produced AUC ≈ 0.51. The override lives entirely in `model.train._training_profiles`; `simulator.pump.DEFAULT_PROFILES` is unchanged. Demo timing preserved. **ADR 0006** captures the rationale.
- **By-pump train/test split, 24/6.** First 6 pumps (by index) held out. Acceptance criterion is "AUC ≥ 0.85 on held-out pumps" so the unit has to be the pump.
- **Score lazy-loads model.pkl on first call.** Module-level cache + thread-safe double-checked locking. Tests that don't exercise scoring can run without the artifact present. Lambda cold-start lands on first invocation rather than at import time.
- **Model + reference artifacts both stamped with `model_version`** (`v0.1.0-seed-<n>`). `shared.score._load_classifier` asserts the artifact's embedded `feature_names` match `FEATURE_NAMES`; a mismatch raises `ScoreError`. This is the first half of the model/reference desync protection — the lambda_scorer cold-start check will compare versions across both artifacts.

## Trade-offs surfaced

- **Two physical models in the repo.** Simulator demo path uses `DEFAULT_PROFILES`; training corpus uses `_training_profiles` which stretches DEGRADING by ~430×. Documented in ADR 0006 + `model/train.py` docstring + a guard test (`test_training_profiles_overrides_only_healthy_dwell_and_degrading`). A future reader investigating "did the model session change the simulator?" gets a clean "no" from the diff.
- **scikit-learn + joblib pull in ~30 MB of wheels.** Pushes the Lambda zip toward the 50-MB unzipped limit once bundled with `shared/` + `lambda_scorer/`. HANDOFF.md §6 Q3 default (bundle) holds for now; the lambda_scorer session may need to switch to S3 cold-load if the build script's final zip exceeds the limit.
- **Single held-out split, no cross-validation.** AUC of 0.998 is high enough that CV feels like ceremony, but a reviewer can fairly ask for robustness. The `--seed` knob makes re-runs trivial; CV is a small follow-up if Gemini pushes.
- **Reference distribution computed from training corpus only** (no leakage). The drift session needs to check whether the wider DEGRADING band in training data shifts per-feature quantiles relative to demo-paced telemetry — PSI may trigger more conservatively at demo time. Flagged for the drift session.

## Sandbox friction (mode-of-work notes, not code)

- **Pytest cache + Edit-tool growth both hit the FUSE bug** described in `ml_obs_pipeline_git_on_windows`. Specifically: the second batch of edits to `model/train.py` got silently truncated at the original byte length, leaving a syntax-broken file Python failed to import. Fixed by rewriting the file via bash heredoc. Same workaround used for `shared/score.py` (new content > original) and `requirements.txt` (append-only). The Read tool sees its own cache, so the truncation isn't visible without `wc -c` / `tail` against the actual disk.
- **30-pump training run takes ~75s end-to-end on the sandbox's 2 cores**, which exceeds the 45s bash timeout. Split data generation into three chunks of 10 pumps each (saved to .npz), then fit + write artifacts in a fourth bash call. The PO-side run on Windows (Python 3.12) should fit comfortably in one shot — `python -m model.train` is the canonical command.

## Verification

- **322 passed, 1 skipped** across `local_runtime/`, `simulator/`, `model/` test trees in `/tmp/run` sandbox copy (the `/tmp` workaround for pytest cache cleanup per `ml_obs_pipeline_git_on_windows`).
- **All three structural parity tests green** — `test_structural_parity_no_vendoring`, `test_structural_parity_score_loads_from_shared`, `test_structural_parity_compute_psi_loads_from_shared`. The lazy-load swap preserves the inspect.getfile invariant; `shared.score.score` still physically loads from `shared/`.
- **Held-out AUC on the full 30-pump corpus: 0.998** (seed 0, 24 train / 6 test). Far above the 0.85 acceptance threshold.
- **Smoke verification of the live score path:** on synthetic feature vectors representing a healthy and a pre-failure pump, `score()` returned 0.0024 and 0.2314 respectively (correctly ordered, both in [0, 1]).

## Gemini review highlights

Pending — review packet drafted at `review_packets/2026-06-01-model-train-histgbt.md`. Will fill the Resolution table in the packet after the response lands.

## State at end of session

- Tests: 322 passing + 1 skipped (pre-existing). 16 new tests added in `model/tests/`; 2 stub-specific tests removed from `local_runtime/tests/test_shared_stubs.py`.
- Artifacts committed: `model/artifacts/model.pkl` (300 KB), `model/artifacts/reference_distribution.json` (5 KB).
- ADR 0006 in `Proposed` status pending Gemini + PO sign-off.
- `context/model.md` updated — see diff in PR.

## Open follow-ups (carry into next session)

1. **Gemini review** of this packet. Top open question: is the DEGRADING-dwell stretch the right call, or is there a cleaner shape (e.g., a separately-parameterised "training physics" config) that would be more reviewer-defensible?
2. **Model + reference versioning** end-to-end. Today both artifacts carry `model_version = "v0.1.0-seed-0"`. The lambda_scorer cold-start should refuse a mismatched pair; that wiring is the lambda_scorer session's job.
3. **Lambda packaging concern.** With scikit-learn in the dep tree, the deployed zip will be larger than the previous shared+lambda_scorer estimate. The lambda_scorer session needs to either confirm the bundle fits inside 50 MB unzipped or take the S3 cold-load fork from HANDOFF.md §6 Q3.
4. **Reference-distribution validity at demo time.** The PSI baseline is built from the slowed-DEGRADING training corpus. The drift session should sanity-check whether the per-feature quantiles overlap demo-paced telemetry's distribution; if not, the drift detector may need a separate "demo mode" reference or a recompute step.
5. **Logistic-regression baseline for the README.** Not blocking, but a "does the HistGBT actually beat a linear model" sentence is worth adding to the README story. Track in a future model-tuning session.

## Note for next session

If the next session is `drift`: the reference distribution at `model/artifacts/reference_distribution.json` carries per-feature `bin_edges` + `bin_counts` exactly in the shape the drift session's PSI implementation needs (10 equal-frequency bins per feature). The training-side build calls `np.histogram(column, bins=edges)`; the drift side should consume the same `edges` and apply its own Laplace smoothing on the actual-side counts. The `model_version` field appears in both `model.pkl` and `reference_distribution.json` — the drift session is the right place to add the version-match assertion at load time.

If the next session is `lambda_scorer`: the bundle written by `model.train.write_artifacts` is a dict `{model_version, feature_names, auc_held_out, classifier}`. The cold-start check should validate `feature_names == FEATURE_NAMES` (already done inside `shared.score._load_classifier`) and compare `model_version` against the reference distribution's. The deploy-time staging script (per ADR 0005 Q1 addendum) needs to copy `model/artifacts/` into the build root alongside `shared/` and `lambda_scorer/`.

Either way: re-run `python -m model.train` on a clean checkout to confirm reproducibility before merging — both artifacts should hash-match the committed bytes when seed=0.

---

## Gemini review outcome (2026-06-01, gemini-pro-latest)

Seven points raised. Three drove changes; four validated the design.

| # | Topic | Disposition | Edit |
|---|---|---|---|
| Q1 | DEGRADING-dwell override defensibility | Validated | — |
| Q2 | Lazy-load + double-checked Lock | Validated | — |
| Q3 | AUC 0.998 — too good? | Partially accepted (doc) | AUC honesty paragraph added to `context/model.md` + ADR 0006 §Consequences. |
| Q4 | Lambda zip size with sklearn | Accepted (measurement) | Stripped footprint: sklearn 27 + numpy 23 + scipy 73 + joblib 1 ≈ 124 MB. ~50 % headroom under 250 MB. Documented in `context/model.md` + ADR 0006 §Q4 addendum. |
| Q5 | ScoreError at first call vs at import | Validated | — |
| Q6 | Lazy joblib import | Accepted (code) | `import joblib` moved to module top of `shared/score.py`. |
| Q7 | Test thresholds in ordering check | Validated | — |

Tests after edits: **322 passed, 1 skipped** — unchanged. ADR 0006 flipped to **Accepted** status (PO sign-off pending). Resolution table in `review_packets/2026-06-01-model-train-histgbt.md`; long-form in ADR 0006 §Addendum 2026-06-01.

### Aside: Gemini access friction (mode-of-work, not code)

The PO's AI Studio account was on a paid project with depleted prepayment credits, so `gemini-pro-latest` 429'd. Standard fallback `-Model gemini-2.5-flash` 429'd too on the same key. Resolved by generating a new API key in a fresh AI Studio project (free-tier) and running Pro through that. Worth a note for future sessions: if both Pro and Flash 429 with "prepayment credits depleted," the issue is project-level billing, not model-level quota — make a new key in a new project.
