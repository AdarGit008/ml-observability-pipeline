# Review Packet 2026-06-10 — drift / lambda_scorer — psi-warmup-gate

> Run via: `.\scripts\run_review.ps1 -Slug drift-psi-warmup-gate` (DeepSeek, ADR 0011 §Addendum 2026-06-10)

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.
6. (Operational corollary) Any local/AWS divergence in scoring/drift is a bug or an ADR.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

Fixes the 2026-06-07 first-live-apply PSI warmup alert storm: on a `healthy` fleet, 9/14 pumps fired `alert_flag: true` within minute 1 — PSI-driven (`max(psi) > 0.25`) on sub-minute sample windows, scores all ≤ 0.02. Root cause: `lambda_scorer` computes PSI every invocation, and on a cold window the 10-bin reference + Laplace α=1.0 prior (ADR 0007) inflate PSI even on a healthy pump. Fix (ADR 0017): a minimum-sample **warmup gate**. The threshold + predicate live in `shared.drift` (`PSI_MIN_SAMPLES = 150`, `psi_is_armed(window)`) — the parity-correct single source of truth — and are applied at the `lambda_scorer` alert-arming site: `psi_breach = psi_is_armed(window) and max(psi) > 0.25`. `score > 0.7` stays ungated (the storm was PSI-only). `compute_psi` is **unchanged** (parity boundary untouched) and still writes `latest_psi` on cold windows, so the dashboard shows PSI warming up — only the alert is gated. `local_runtime` has no alert site, so it is unaffected; the shared constant binds the future fleet-PSI EventBridge Lambda.

## Changed files

- `shared/drift.py` — new `PSI_MIN_SAMPLES = 150` constant + pure `psi_is_armed(window)` predicate. `compute_psi` untouched.
- `lambda_scorer/handler.py` — `psi_breach` now gated by `psi_is_armed(window)`; import + docstring + cold-pump comment updated.
- `lambda_scorer/tests/test_handler.py` — new structural-parity guard for `psi_is_armed`; two new behavior tests (gated-below-warmup, arms-when-warm); the three existing PSI-breach SNS tests warmed to `PSI_MIN_SAMPLES`.
- `local_runtime/tests/test_shared_stubs.py` — new `test_psi_is_armed_boundary` (numpy-free boundary pin).
- `docs/adr/0017-psi-warmup-gate.md` — new ADR.
- `context/drift.md`, `context/lambda_scorer.md` — open items closed.

## Core hunks

`shared/drift.py` — the new constant + predicate (compute_psi unchanged):

```python
PSI_MIN_SAMPLES: int = 150  # 5 min at the 2 s tick = WINDOW_SAMPLES; ~15 obs/bin

def psi_is_armed(window: Sized) -> bool:
    """Return True when ``window`` holds enough samples for a PSI breach
    to ARM an alert (``len(window) >= PSI_MIN_SAMPLES``), per ADR 0017.
    ... gate is on the ALERT, not the computation; local_runtime has no
    alert site so does not call this; the future fleet-PSI Lambda will
    consult the SAME constant (north star #6).
    """
    return len(window) >= PSI_MIN_SAMPLES
```

`lambda_scorer/handler.py` — the gated arming (score_breach ungated):

```python
    psi = compute_psi(window, reference=REFERENCE)

    # Warmup gate (ADR 0017): a PSI breach may ARM an alert only once the
    # window holds >= PSI_MIN_SAMPLES. score_breach is NOT gated.
    psi_breach = psi_is_armed(window) and max(psi.values()) > PSI_ALERT_THRESHOLD
    score_breach = score_value > SCORE_ALERT_THRESHOLD
    alert_flag = psi_breach or score_breach
```

`lambda_scorer/tests/test_handler.py` — the two behavior tests (score route neutralized to isolate the PSI gate):

```python
def test_psi_alert_gated_below_warmup(fresh_handler):
    handler_mod, table = fresh_handler
    sns_stub = mock.MagicMock(); handler_mod._SNS = sns_stub
    handler_mod.score_fn = lambda features: 0.0       # isolate the PSI route
    _seed_readings(table, 10, values=_EXTREME)        # below the 150 floor
    result = handler_mod.handler(_telemetry(ts="2026-06-02T14:32:01.123Z", **_EXTREME))
    assert result["alert_flag"] is False
    sns_stub.publish.assert_not_called()
    state = _get_state(table)
    # PSI WAS computed + stored and DOES breach — gate is on the alert, not the value
    assert max(float(v) for v in state["latest_psi"].values()) > handler_mod.PSI_ALERT_THRESHOLD

def test_psi_alert_arms_when_warm(fresh_handler):
    handler_mod, table = fresh_handler
    sns_stub = mock.MagicMock(); handler_mod._SNS = sns_stub
    handler_mod.score_fn = lambda features: 0.0
    _seed_readings(table, PSI_MIN_SAMPLES, values=_EXTREME)   # warm window
    result = handler_mod.handler(_telemetry(ts="2026-06-02T14:32:01.123Z", **_EXTREME))
    assert result["alert_flag"] is True
    assert sns_stub.publish.call_count == 1
    assert json.loads(sns_stub.publish.call_args.kwargs["Message"])["alert_type"] == "psi_breach"
```

## Specific questions for the reviewer

1. **Gate location.** The threshold + predicate live in `shared.drift` but are *applied* only at the `lambda_scorer` alert site (local mode has no alert site). Is this the right parity posture, or does leaving the application out of `shared/` risk the future fleet-PSI Lambda re-deriving the gate inconsistently despite importing the constant? Would you have pushed the application into a shared helper too?

2. **Threshold = 150.** Justified as `WINDOW_SAMPLES` (5 min) → ~15 obs/bin → Laplace prior ~6% of mass. Is 150 defensible against ADR 0007's binning, or would you argue for the ≥5-per-bin floor (50) or something tied more directly to the bin count? Is there a demo-length risk (a `degrading` scenario that needs to arm before 5 min of warm-up has elapsed)?

3. **Ungated score path.** `score > 0.7` deliberately bypasses the warmup gate (a high failure probability is meaningful on a short window; the storm was PSI-only). Is there a failure mode where an ungated score on a 1-sample window produces its own false-alert storm, mirroring the PSI one we just fixed?

4. **Sample-count vs. wall-clock warmup.** The gate counts samples, not elapsed time. For a pump with a reporting gap, is sample-count the right semantics, or could it under-warm (arm too early after a gap refills the window with stale-but-numerous rows)?

5. **Test honesty.** Three pre-existing PSI-breach SNS tests were "warmed" (seed 10 → `PSI_MIN_SAMPLES`) so they exercise the PSI route under the gate rather than silently passing via the ungated score route. Is warming them the right call, or should they have been left to prove the score route still fires? Are the two new tests' use of `handler_mod.score_fn = lambda ...` to isolate the PSI route sound given the fixture reloads the module per test?

## What I'm NOT looking for
- Style / formatting — handled by linter.
- Re-litigating ADR 0007/0008/0009 (binning, smoothing, reference source, 4-key surface) — those are accepted and unchanged here.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Gate location — threshold not shared | **Accepted** | Added `shared.drift.psi_alert_should_fire` + `PSI_SIGNIFICANT_THRESHOLD` (gate AND threshold colocated); handler calls it; 6th structural-parity guard pins it. |
| 2. Threshold = 150 conservative | **Accepted (doc)** | ADR §Consequences now frames 150 as the conservative first cut = full window; re-evaluate after live data; ~50 is a one-constant change. PO keeps 150. |
| 3. Ungated score path could storm | **Accepted** | Handler comment + ADR §3 explain the deliberate asymmetry (per-sample model output vs distributional statistic); new `test_score_alert_not_gated_by_warmup` pins cold-window+high-score DOES alert; OOD-robustness is the model surface's concern, monitored post-deploy. |
| 4. Sample-count vs wall-clock | **Validated** | No change — sample count is the correct semantic; gap-then-burst covered by FIFO + ungated score. |
| 5. Test honesty / completeness | **Accepted** | Added `latest_psi`-written assertion to arms-when-warm + shared `test_psi_alert_should_fire_composite`. |
| obs. derive from `WINDOW_SAMPLES` | **Rejected** | `WINDOW_SAMPLES` is in `lambda_scorer`; `shared.drift` must not depend on it (inverts parity dep; breaks drift-without-scorer, ADR 0007 §4). Distinct constants sharing a value. |
