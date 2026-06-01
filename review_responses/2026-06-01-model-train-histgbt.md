Here's my adversarial-but-fair review of Review Packet 2026-06-01:

## Overall Impression

This is a well-structured and thoroughly documented change, which is commendable for a portfolio project. The use of ADRs (0006 especially) to capture rationale and trade-offs is excellent, and the explicit handling of `shared/score.py`'s transition from stub to real implementation demonstrates a strong understanding of project constraints and architectural intentions (mode parity). The committed artifacts align with the "$0 AWS cost" north star.

However, as an adversarial reviewer, I've dug into potential weaknesses and assumptions.

---

## Review Points

### 1. Is the training-time DEGRADING-dwell override (ADR 0006 §3) the right call, or is there a cleaner shape?

**Verdict: Defensible and well-justified.**

The ADR 0006 provides a robust rationale for this decision, explicitly addressing alternatives and linking back to `PLAN.md` constraints (horizon, demo experience). Your approach of modifying the simulator's parameters *solely for training data generation* within `model/train.py` via `_training_profiles` is a standard and accepted practice in ML. It allows for the generation of sufficient positive samples without distorting the simulator's behavior in the demo/runtime path, thus preserving "mode parity" for the *feature extraction* and *scoring* components.

The concern about "model session quietly changed the simulator's behavior" is mitigated by:
1.  Explicit documentation in ADR 0006.
2.  The change being localized to `model/train.py`, not `shared/simulator.py`.
3.  The `DEFAULT_PROFILES` remaining untouched for general simulation.
4.  The mention of a "guard test [that] pins the contract" for `DEFAULT_PROFILES`.

This is a smart trade-off to address the data imbalance problem inherent in rare failure prediction within a compressed simulation timescale.

---

### 2. Is lazy-load + double-checked locking in `shared/score.py` overengineered for the Lambda case?

**Verdict: Keep it. It's robust, not overengineered.**

The implementation (assuming `functools.lru_cache` or a standard double-checked locking pattern) is a common and efficient way to handle singletons or expensive resource loading.

*   **Lambda:** While a single Lambda invocation is single-threaded, a *warmed-up container* can serve multiple *sequential* invocations. The caching mechanism prevents reloading the model for each subsequent call, improving performance and reducing latency after cold start.
*   **Local Runtime:** Even with an async-single-loop, the cache still optimizes performance by avoiding repeated disk I/O.
*   **Future-Proofing:** Your justification regarding "hypothetical future ProcessPoolExecutor fan-out" is key. A `shared` library component should be robust against potential changes in its consumption environment. The overhead of a `Lock` when not contended is negligible.

This design provides cheap insurance for future scalability or consumption patterns, making `shared.score` more robust without significant performance or complexity penalties in the current use cases.

---

### 3. AUC of 0.998 — is that too good?

**Verdict: Yes, it's suspiciously good. Document the implications.**

An AUC of 0.998 on a held-out set is almost perfect and, in a real-world scenario, would immediately trigger an in-depth investigation for data leakage or an overly simplistic problem statement.

Given this is a simulated environment where:
*   The underlying physics model is deterministic (plus controlled noise).
*   All pumps follow the same failure trajectory "physics."
*   The features are direct measurements derived from this known physics.

It's entirely plausible for a `HistGradientBoostingClassifier` to achieve near-perfect separation. The model is likely learning the precise `DEGRADING` phase signature that your simulator generates.

**Risks:**
*   **Limited Generalizability:** The model might be *memorizing* the simulated patterns rather than learning truly generalizable fault characteristics. If the "physics" or noise characteristics were to change, or if deployed to a real-world scenario with different noise profiles, the performance could degrade sharply.
*   **Portfolio Perception:** While technically correct for your simulated data, such a high metric might inadvertently give the impression that the problem was trivial or that the simulation is not challenging enough.

**Recommendation:**
This is not a blocker, and adding a cross-seed AUC distribution check is indeed scope creep for *this* session. However, to pre-empt reviewer questions and align with "one polished repo," add a brief note to `context/model.md` (under the "shipped" status) or ADR 0006. Explain that the high AUC is a direct consequence of the perfectly known, deterministic (plus minor noise) physics of the simulated environment, and that real-world deployment would necessitate a more nuanced performance target and rigorous validation against diverse, real-world data. This acknowledges the reality of the simulation vs. real-world ML.

---

### 4. Bundling 300 KB classifier + 5 KB JSON in the deploy zip + scikit-learn (~25 MB unzipped) — is the Lambda zip still going to fit under the 250 MB unzipped quota?

**Verdict: Acknowledge the risk; conduct an early measurement.**

This is a critical, potentially show-stopping issue that warrants early investigation. While `lambda_scorer` is slated to handle the build, the *choice of model library* (scikit-learn) has the largest impact here, making it a concern for the `model` session.

`scikit-learn` (and its dependency `numpy`) can indeed be substantial. An "unzipped" estimate of 25 MB for `scikit-learn` alone is conservative; typically, `numpy` and `scipy` (often a transitive dependency) add significant size, pushing the total for a scientific Python stack easily into 50-100MB+ for unzipped `site-packages`. Combined with the Python runtime, other dependencies, and the model artifacts, the 250 MB unzipped limit could easily be breached.

**Recommendation:**
Deferring full build/optimization to `lambda_scorer` is fine, but it's essential to perform an early, rough measurement *now*.

**Actionable:** Before completing this session, or as the very first task of the `lambda_scorer` session, explicitly measure the unzipped size of `scikit-learn`, `numpy`, and `joblib` in a clean Python virtual environment representative of the Lambda runtime (e.g., Python 3.9 or 3.10). If this footprint already exceeds ~100-120 MB, it indicates a high risk and may necessitate considering alternatives like:
*   **Smaller ML libraries:** E.g., `lightgbm` or `xgboost` which can be more compact, or even a custom C-extension build for optimal size.
*   **Lambda Container Images:** Using ECR container images (which have a 10 GB limit) instead of zip deployments (250 MB) would solve the size problem but add deployment complexity.

This early check prevents a major architectural pivot late in the project.

---

### 5. Feature-schema mismatch raises `ScoreError` at load time, but only when `score()` is first called.

**Verdict: Acceptable for a portfolio project.**

This "first-message-failure" is a common and generally acceptable pattern for lazy-loaded resources.

*   **Clarity:** A `ScoreError` upon the first invocation provides explicit, immediate feedback that the model artifact is misconfigured or corrupt. This is clear and easy to diagnose.
*   **Lambda Cold Start:** While eager loading at module import could catch issues marginally earlier (during Lambda initialization), the difference in detection time is usually negligible for deployment-time errors. Both methods prevent the Lambda from successfully processing any real requests.
*   **Resource Efficiency:** Lazy loading avoids deserializing potentially large models if the `score()` function is never called within a specific container lifecycle (e.g., if other Lambda paths are triggered).

For a portfolio project, the current approach is a good balance of explicit error handling and resource efficiency without adding unnecessary complexity for eager validation.

---

### 6. `shared.score._load_classifier` imports `joblib` lazily inside the function body.

**Verdict: Move to top-level import for consistency and clarity.**

The benefit of lazy importing `joblib` is marginal. `joblib` is a relatively lightweight library, and `shared.score` exists primarily to provide the `score()` function, which *will* use `joblib`.

*   **Standard Practice:** Python convention strongly favors placing `import` statements at the top of the module. This improves readability by making all dependencies explicit upfront.
*   **Minimal Gain:** The memory or performance saving from avoiding a `joblib` import is negligible in the context of `scikit-learn` and `numpy` (which are likely imported at the top of other related modules, or transitively).
*   **Readability:** While not "branchy," it's slightly less straightforward than a top-level import.

**Recommendation:** Move `import joblib` to the top of `shared/score.py`. It aligns with Python best practices and makes the module's dependencies clear, with almost no downside.

---

### 7. `model/tests/test_score_wiring.py::test_score_orders_healthy_below_pre_failure` uses two hand-crafted feature dicts. The test only asserts `pre_fail > healthy`, not absolute thresholds.

**Verdict: The current ordering check is sufficient and appropriate for a "wiring" test.**

This test's purpose, as implied by its name (`test_score_wiring.py`) and the "structural parity" context, is to verify that the scoring pipeline is correctly integrated and behaves directionally as expected.

*   **Robustness:** Asserting `pre_fail > healthy` makes the test robust against minor variations in model output due to retraining (e.g., different random seeds, minor hyperparameter tweaks, or even `scikit-learn` version updates). If you were to assert specific numerical thresholds (e.g., `healthy < 0.1`, `pre_fail > 0.5`), the test would become brittle and require frequent updates without necessarily reflecting a true regression in model performance.
*   **Separation of Concerns:** Quantitative performance evaluation (e.g., AUC, F1-score, precision/recall at specific thresholds) should be handled by dedicated tests within `model/tests/test_train.py` that evaluate the trained model's generalization capabilities on a proper test set. The `test_score_wiring.py` focuses on the functional contract.

The current test design correctly targets the qualitative behavior and integration, which is its appropriate scope.
