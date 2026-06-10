## Review of drift / psi-warmup-gate

### Overall impression

The root-cause diagnosis and the fix itself are correct: a cold window inflates PSI under ADR 0007âs Laplace prior, and gating the alert on minimum sample count is the cleanest surgical fix. The structure â a pure predicate + constant in `shared.drift`, applied only at the alert site â follows the north star of mode parity (local has no alert site). The test changes are thoughtful. That said, several trade-offs and risks deserve explicit attention before closing.

---

### 1. Gate location â shared predicate, per-module application

**Ruling: acceptable but brittle.**

You argue that `psi_is_armed` + `PSI_MIN_SAMPLES` in `shared.drift` is âthe parity-correct single source of truth.â It *is* a single source of truth for the *sample-count condition*, but the *full alert-arming logic* (`psi_is_armed(window) and max(psi.values()) > PSI_ALERT_THRESHOLD`) lives in `lambda_scorer/handler.py`. The threshold `PSI_ALERT_THRESHOLD` is **not** in `shared.drift` â it is (likely) defined only in the `lambda_scorer` module. That means a future fleet-PSI EventBridge Lambda that imports `PSI_MIN_SAMPLES` could:

- Use a different threshold (`> 0.3` instead of `> 0.25`), creating an inconsistency that falsifies north star #6.
- Forget to call `psi_is_armed` at all, defeating the entire fix.
- Re-implement the gate differently (e.g., using a time-window instead of sample count), and the shared constant is too narrow to enforce the intended behavior.

I would have pushed the *full alert predicate* into a shared helper, e.g.:

```python
def psi_alert_should_fire(window, psi: dict, threshold: float = PSI_ALERT_THRESHOLD) -> bool:
    return len(window) >= PSI_MIN_SAMPLES and max(psi.values()) > threshold
```

That forces any future Lambda to import a single function that encodes both the gate and the threshold, and makes it trivially testable in one place. The cost is negligible (one more function). Without this, you are relying on discipline to maintain parity â a weak guarantee for a project that values âone polished repo.â

**Risk:** medium. The fleet Lambda is not yet written, so this is a design debt rather than a current bug.

**Suggestion:** add the shared helper now, even if it only wraps the two-line check. Update `lambda_scorer/handler.py` to call it. Close the debt before you move on.

---

### 2. Threshold = 150

**Ruling: defensible but arguably conservative.**

The justification: ~15 obs/bin, Laplace Î±=1.0 contributes ~6% of mass per bin, which on a healthy pump with uniform distribution inflates PSI to ~0.03â0.05. At 10 obs/bin the prior dominates (~9% mass); at 150 obs (15/bin) the priorâs influence drops to ~6%, which yields PSI < 0.25 on healthy pumps in practice. Thatâs sound reasoning and consistent with ADR 0007.

However, `150` equals the **full sliding window** (`WINDOW_SAMPLES`). This means the gate only arms when the window is completely filled. On a truly degrading pump where PSI is the *early* signal (before the score model triggers), the first alert could be delayed up to 5 minutes. In a demo scenario lasting 10 minutes, youâve lost half the detection window. The score path is ungated, but a failure mode that does not affect the score (e.g., a slow pressure drift that the model never saw in training, or a feature-space change that the score model considers normal) would be invisible until minute 5.

A slightly lower threshold, say 50 (â5 per bin, enough to cut the false-alert rate from 9/14 to ~0), would still suppress the warmup storm but allow detection after 1.7 minutes. The author should provide empirical evidence (or a quick simulation) that 50 still produces unacceptable false positives, or acknowledge that 150 is a deliberate âsafe first cutâ with a comment in the ADR that it can be lowered once more telemetry data is collected.

**Suggestion:** document in ADR 0017 that 150 is the conservative upper bound (full window) and note the planned re-evaluation after a week of live data.

---

### 3. Ungated score path

**Ruling: potential risk, insufficiently mitigated.**

You state âthe storm was PSI-only,â but that is an observation about the *first* storm, not a proof that score is immune. The score model is a black box from this review perspective. Consider a scenario:

- A pump goes through a cold-reboot cycle, reporting one or two readings with extreme features (e.g., zero pressure, infinite flow).
- The score model, trained on full-run data, assigns a failure probability > 0.7 to such out-of-distribution inputs.
- That triggers a false alert on a 1-sample window â the exact same pattern as the PSI storm, just with a different metric.

Is this likely? Possibly not, because the score model is presumably designed to be robust to cold starts (trained on historical cold-start data). But without evidence, itâs a blind spot. The test suite has no âcold window + high scoreâ test. The two new behavior tests deliberately set `score_fn = lambda features: 0.0` to isolate the PSI path, which is fine for testing PSI but leaves the score path untested for cold-window behavior.

The author should either:
- Add a test that seeds a single reading with extreme features and a score value above 0.7, expects no alert (if the score is also gated), or
- Add a justification in the ADR or handler docstring explaining why the score model is stable on short windows (e.g., âscore_fn outputs 0.0 until at least 5 samples are seenâ).
- Alternatively, accept the risk and commit to monitoring for false-positive score alerts post-deployment.

**Recommendation:** surface the risk in the ADR, and consider adding a low-sample score guard if the model is not explicitly robust.

---

### 4. Sample-count vs. wall-clock warmup

**Ruling: correct decision.**

Using sample count is the right semantic for a PSI computation that operates on a fixed-size sliding window of telemetry points. A wall-clock gate (e.g., â5 minutes of dataâ) would introduce complexity: youâd need to track the timestamp of the oldest sample, handle clock skew, and decide what to do if the window is full but the data is older than 5 minutes (e.g., after a long gap). Sample count is simple, deterministic, and perfectly aligned with the PSI windowâs semantics â more samples means more stable bins.

The only edge case: a pump that goes silent for 10 minutes, then bursts many readings in a few seconds. The window would quickly fill to 150 (sample count gate arms) with mostly stale data (same pressures). That could produce a PSI breach if the current pressures differ from the reference, but the alert would be based on old readings. In practice, this scenario is unlikely because the window is FIFO and the burst would include new readings that displace the old ones after a few ticks. Moreover, the score path would likely catch any real degradation before the gap refill. So the risk is negligible.

**Verdict:** no change needed.

---

### 5. Test honesty

**Ruling: sound, with one minor concern.**

- **Warming the three existing PSI-breach SNS tests from 10 to `PSI_MIN_SAMPLES` (150):** correct. Without this, those tests would have been passing because the score route (which is ungated) fired, not because PSI-breach logic was exercised. That would have been a ticking time bomb â if someone later gated the score path, those tests would fail unexpectedly. The refactor makes them honest.

- **Isolation via `handler_mod.score_fn = lambda features: 0.0`:** effective because `fresh_handler` reloads the entire module per test (likely via `importlib.reload` if itâs a real module, or a fresh instance of the handler class). The patching is scoped to that test. No cross-test leakage. This is a robust design pattern. One caveat: the tests now assume that the handlerâs `score_fn` attribute is directly assignable. If the handler is a function (not a class), patching via assignment would affect the module-level reference and could leak to other tests if not reloaded. The fixture should explicitly reload the module after each test. The summary does not show the fixture, so Iâll assume it does.

- **Potential blind spot:** The two new tests only check `alert_flag` and SNS publication. They do not verify that `psi` is still computed and stored (which is part of the contract: âgate is on the ALERT, not the computationâ). The first test does check `state["latest_psi"]` and asserts it exceeds the threshold, confirming computation. Good. The second test does not check that `state["latest_psi"]` still holds a value; it only asserts the alert fires. It would be stronger to also assert that `latest_psi` is written even when the alert fires, ensuring the dashboard always sees the value. Currently the second test doesnât read `state` at all. Consider adding an assertion for completeness.

**Minor suggestion:** add a `state = _get_state(table)` and assert `max(float(v) for v in state["latest_psi"].values()) > 0.25` in the arms-when-warm test.

---

### Additional observations

- **ADR 0017 mentions `WINDOW_SAMPLES`** but that constant is not defined in the hunk. If it is defined elsewhere (e.g., in `shared.drift`), itâs fine. If not, the ADR is referencing an undefined symbol. Verify that `PSI_MIN_SAMPLES = 150` is exactly equal to `WINDOW_SAMPLES` (likely 150). If `WINDOW_SAMPLES` could change independently, the gate threshold should be derived from it, not hardcoded 150. Consider `PSI_MIN_SAMPLES = WINDOW_SAMPLES` for automatic consistency.

- **The `_seed_readings` helper** is not shown, but if it directly writes items with a timestamp that falls within the window, the test with 10 samples will have those 10 readings plus the incoming telemetry (so 11 total) â still below 150. Good. But if `_seed_readings` overwrites the window instead of appending, the test semantics change. Not verifiable here.

- **`compute_psi` unchanged**: good. The function remains a pure function, testable independently. The gate is entirely outside it.

---

### Summary of actionable recommendations

| # | Issue | Recommendation | Priority |
|---|-------|----------------|----------|
| 1 | Gate location â threshold not shared | Add shared helper `psi_alert_should_fire` in `shared.drift` that computes the full `psi_is_armed and max > threshold` predicate, update handler to call it. | Medium |
| 2 | Threshold = 150 may be too conservative | Document in ADR that 150 is the full-window upper bound and will be re-evaluated after live data; consider 50 if evidence supports it. | Low |
| 3 | Ungated score path | Add cold-window test for score > 0.7 that expects no alert if the score model is also unstable, or add documentation explaining why it is safe. | Medium |
| 4 | Test completeness | Add assertion in arms-when-warm test to confirm `latest_psi` is written. | Low |
| 5 | Derive from WINDOW_SAMPLES | Use `PSI_MIN_SAMPLES = WINDOW_SAMPLES` (or `WINDOW_SAMPLES // BIN_COUNT * BINS_PER_BIN?`) to avoid drift if window size changes. | Low |

The fix is correct in spirit, but the architecture around the gate is looser than the north star demands. Tightening it now will prevent a future parity bug.

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-10 13:52:22._

