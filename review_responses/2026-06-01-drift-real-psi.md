This is an excellent, thorough review packet. The clear articulation of changes, constraints, and specific questions makes the review process very efficient. The ADR and session logs are invaluable for deeper dives.

Here are the responses to your questions:

## Gemini Review Responses

### Q1 — Laplace α = 1.0 vs. Jeffreys α = 0.5 vs. ε-style

*   **Disposition:** Accepted.
*   **Reasoning:**
    *   For the purpose of detecting *meaningful* distribution shifts (which PSI is designed for), the choice between α=1.0 (Laplace) and α=0.5 (Jeffreys) is unlikely to dramatically change the overall signal, especially with a 1800-sample window. Both are principled Bayesian priors.
    *   Laplace (α=1.0) is simpler to explain ("add one pseudo-count per bin") and typically has a slightly more "smoothing" effect than Jeffreys, which can be beneficial in making the metric less noisy on smaller bins without completely dulling the signal.
    *   The "one phantom observation per bin" is indeed the correct Bayesian framing for Laplace smoothing, and it inherently serves as div-by-zero protection. It's a robust choice.
    *   Sensitivity to α primarily matters when dealing with very sparse data or extremely tight thresholds. Given the 1800-sample window and the typical range of drift detection (0.10, 0.25+), this choice is appropriate.
*   **Action:** None.

### Q2 — `reference=None` lazy-load semantics

*   **Disposition:** Reconsider (minor refactoring).
*   **Reasoning:**
    *   While the current approach is pragmatic for preserving the existing call site, the lazy disk-load as a side effect within `compute_psi` creates a less pure function and introduces an implicit dependency.
    *   The existence of `_reset_reference_cache()` for testing is a strong signal that the module-cached state makes testing harder and less isolated. Shared module-level state, especially for I/O, often leads to such test helpers which are a form of technical debt.
    *   It generally improves clarity and testability if functions explicitly declare all their inputs.
*   **Action:**
    1.  Refactor `shared.drift.compute_psi` to explicitly require the `reference_distribution` (or a pre-parsed reference object) as an argument.
    2.  Create a separate, module-level function `shared.drift.load_reference(path: Path)` (or similar) that handles the actual disk loading and the *caching*. This function would be called once during service initialization (or on the Lambda cold start path).
    3.  `local_runtime/service.py` would then call `load_reference()` once and store the result, passing it explicitly to `compute_psi` on each call.
    4.  Remove `_reset_reference_cache()`. Tests would now pass the loaded reference directly to `compute_psi`, making them more isolated and predictable.

### Q3 — Per-tick PSI cadence

*   **Disposition:** Accepted.
*   **Reasoning:**
    *   This approach perfectly balances the `$0 ceiling` constraint with the need for observability. Computing PSI on every Nth tick and emitting `psi=None` on non-compute ticks is a standard and effective pattern for intermittent metrics.
    *   Grafana's `last` aggregator is precisely designed for this use case, providing a good user experience by showing the most recent computed value without inferring data that doesn't exist.
    *   The argument "missing fields suggest broken telemetry" is valid in some contexts, but for a metric that is explicitly computed intermittently, `null` is semantically correct. It means "no value *was computed* at this precise moment," not "the system failed to compute a value."
    *   Rejecting (c) "separate `pump_drift` measurement" as scope creep is reasonable for a portfolio project. While it offers database schema purity, the current approach is perfectly functional and simpler.
    *   The decision to start computing PSI at tick N (avoiding a 1-sample PSI) is a good default, preventing misleading early signals. Waiting until the deque is full (M samples) would also be defensible, but the N-tick approach is simpler and likely sufficient given the larger window size.
*   **Action:** None.

### Q4 — Model/reference version-match check inside `_load_reference`

*   **Disposition:** Accepted with minor changes.
*   **Reasoning:**
    *   Placing the `model_version` check within `_load_reference` is a sensible design. The `drift` module is responsible for using the reference, so it's fitting that it validates the reference's compatibility. It avoids forcing the `score` module to load artifacts it doesn't otherwise need.
    *   Lazy-importing `joblib` is an excellent solution to maintain the `context/drift.md` dependency constraint.
    *   However, the `except Exception` is too broad. It could mask unrelated Python errors, making debugging difficult.
    *   The `model.pkl` absent → check skipped for dev environments is a pragmatic compromise. It needs to be well-documented (e.g., in the ADR or a prominent code comment) to ensure developers understand the parity difference.
*   **Action:**
    1.  Refine the `try-except` block to catch more specific exceptions relevant to loading `joblib` models and accessing attributes (e.g., `FileNotFoundError`, `joblib.BadZipFile`, `EOFError`, `AttributeError`, `pickle.UnpicklingError`). This makes the error handling more precise.
    2.  Add explicit comments in the code and/or ADR 0007 explaining why the `model.pkl` check is skipped in environments where it's absent (e.g., `DEV_MODE`).

### Q5 — Reference-Validity carry-in measurement

*   **Disposition:** Accepted (recommendation for Option a).
*   **Reasoning:**
    *   The measurement (PSI 1.3–6.7 against training data for "healthy" demo traffic) is the critical piece of evidence here. If the system's baseline "healthy" state already triggers high drift values, the metric becomes unactionable due to constant "false positives" or high noise, leading to alert fatigue.
    *   **"Demo-paced healthy" as baseline (Option a):** This is the pragmatic and operationally sound choice. The purpose of PSI in an operational system is to detect *deviations from current normal behavior*. While the training-time baseline is philosophically tied to the model, operational contexts often drift from training conditions. Using a "healthy operating baseline" ensures the drift metric is sensitive to *new* or *anomalous* shifts, which is what operators care about. Thresholds should then be set relative to *this* new baseline.
    *   **"Dual references" (Option b):** This adds significant architectural and operational complexity (more artifacts, more logic, potentially more confusion for users) without providing a compelling advantage for a portfolio project. It's often reserved for very mature systems with specific compliance or research needs.
*   **Action:** Proceed with recomputing the reference from "demo-paced healthy" data in a follow-up session. Ensure this new reference is clearly versioned or named (e.g., `operational_reference_distribution.json`) and that its provenance is documented.

### Q6 — Test magnitudes are pinned analytically rather than via deterministic seeds

*   **Disposition:** Accepted.
*   **Reasoning:**
    *   For core numerical computations like PSI, analytically derived "golden tests" are highly valuable. They provide an exact verification that the formula is implemented precisely as intended. This ensures high confidence in the correctness of the fundamental calculation.
    *   The brittleness argument is valid: a correct change to the formula (e.g., a required alteration of α) *will* break the test, requiring manual recalculation. However, for a foundational metric, this forced re-validation is often a feature, not a bug, ensuring that changes are deliberate and fully understood.
    *   This approach is particularly suitable for verifying the precise output of the Laplace smoothing and the logarithmic contributions.
*   **Action:** None. (Consider adding comments to these specific tests explaining they are "golden tests" with analytically derived values to aid future maintainers.)

### Q7 — Service.py's two-deque shape

*   **Disposition:** Accepted.
*   **Reasoning:**
    *   The presence of two deques (`_window` for telemetry stats, `_feature_history` for PSI) is a clear separation of concerns, addressing distinct functional requirements with potentially different window sizes (5 minutes vs. 1 hour). This is good design.
    *   The asymmetry between the local runtime (in-memory deques) and the Lambda hot path (rebuilding from DynamoDB) is a common and expected consequence of designing for a serverless environment. The local runtime can leverage in-memory state, while Lambda functions, being stateless between invocations, must persist and retrieve state from external stores. This is a pragmatic adaptation to the deployment model, not a code smell.
    *   As long as the `shared/drift.py` module itself remains stateless and maintains mode parity for the *computation* (which it does), the `local_runtime/service.py`'s state management approach is appropriate for its environment.
*   **Action:** Ensure that the documentation (e.g., in `context/_global.md` or a `README` within `local_runtime`) explicitly highlights this difference in state management for `_window` and `_feature_history` between local and cloud deployments, explaining how they are rebuilt in Lambda from DynamoDB.

---

Overall, this is a very strong implementation and review packet. The team has clearly thought through the constraints, trade-offs, and potential issues. The ADR is well-referenced and provides excellent background. Good job!
