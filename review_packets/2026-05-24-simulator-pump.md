# Review Packet 2026-05-24 — simulator — pump-model-and-state-machine

> Paste this entire file into Gemini via:
> `.\scripts\gemini_review.ps1 -Slug simulator-pump`

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md` §2.2. Telemetry contract: `context/_interfaces.md`.

## Summary of the change
First implementation pass on the `simulator` component. Adds `simulator/pump.py` (the `Pump` class with physical equations from PLAN.md §2.2 and a four-state lifecycle machine: HEALTHY → DEGRADING → FAILING → FAILED) and a 29-test pytest suite covering all four states, the telemetry-dict shape against `context/_interfaces.md`, RNG reproducibility, and timestamp formatting. No MQTT, no asyncio, no config-file loading — those are later sessions. Also adds a `.gitignore` covering Python + Terraform + project secrets as the first commit of the session.

## Diff
Four new files. Inline below in dependency order.

### `.gitignore` (first commit)
[Inline omitted for brevity in this revision; see file at repo root.]

### `simulator/__init__.py`
```python
"""Pump-fleet simulator.

Synthetic telemetry for ~15 industrial pumps per PLAN.md §2.2. This package
owns the physical model and (in later sessions) the MQTT publishing layer.
"""

from simulator.pump import Pump, PumpState

__all__ = ["Pump", "PumpState"]
```

### `simulator/pump.py`
See full file at the path above. Key design choices made on top of the §2.2 spec (which only defines the equations and names the four states):

| Choice | What I picked | Why |
|---|---|---|
| Degradation trajectory | Per-state `rate_per_tick` + `ceiling`, accumulated each `.step()`, clamped to `[0, 1]` | Simplest first-principles model; ceilings make per-state envelopes visible and testable. |
| FAILED behavior | Keep emitting telemetry, `degradation` pinned to 1.0 | Lets downstream scoring/drift see the failure rather than starving. |
| State advancement | Automatic by `dwell_ticks`, plus manual `force_state()` | Auto-advance for "let it run" demos; manual override for scripted drift scenarios. |
| RNG | Pump-private `random.Random(seed)` | Deterministic tests, no global-state contamination. |
| Timestamps | `step(now=...)` injectable, default `datetime.now(UTC)`; format `YYYY-MM-DDTHH:MM:SS.mmmZ` | Reproducible test assertions; matches `_interfaces.md`. |
| Defaults | `ambient=22.0`, `setpoint=1800.0`, dwell=24h/200/200/∞ | Recruiter-readable, mentioned in code comments. |

## Specific questions for Gemini
1. Degradation-trajectory model — defensible as first-principles?
2. Tick ordering — emit-reflects-entering vs transition-then-sample?
3. RPM independent of degradation — strict to §2.2 or add coupling?
4. Test fairness — does the at-ceiling comparison hide a derivative bug?
5. Default dwell times — keep, defer, or cite a source?
6. Hidden AWS-specificity — anything that quietly couples to the AWS path?
7. The two `Pump(...)` calls in `test_degrading_caps_at_ceiling` — keep or delete?

## What I'm NOT looking for in this review
- Style / lint nits.
- MQTT publishing logic — out of scope.
- Config-file loading — separate session.

## Resolution (filled in 2026-05-25 after Gemini response)

| # | Disposition | Notes |
|---|---|---|
| 1. Linear ramp vs P-F curve | **Addressed** | Added a docstring paragraph in `pump.py` acknowledging real degradation follows the P-F curve with exponential acceleration; rationale (predictable test envelopes, pipeline-first focus) made explicit. |
| 2. Tick ordering | **Confirmed (no change)** | Gemini agreed with the Moore-machine semantic (emit reflects entering-state; transition-then-sample would create an off-by-one attribution bug). |
| 3. RPM independent of degradation | **Addressed via ADR 0002** | Replaced RPM equation: `RPM = setpoint * (1 - degradation) + N(0, 5 + 15 * degradation)`. PLAN.md §2.2 updated in-place; ADR 0002 is the authoritative justification. Cascade: `test_failing_telemetry_hotter_than_degrading` renamed to `..._higher_vibration_...` (bearing temp is no longer monotonic in degradation — physically correct), `test_failed_state_keeps_emitting_telemetry` envelope updated (RPM near 0 with stutter, bearing ≈37°C, vibration > 2.5). |
| 4. Derivative-fairness test | **Addressed** | Added `test_failing_vibration_rises_faster_than_degrading_from_zero` — both states start at `degradation=0`, step 50 ticks, assert FAILING vibration > DEGRADING vibration + 0.05. Used 50 ticks instead of Gemini's suggested 10 because at 10 the per-state delta (~0.06) is barely above noise σ=0.05; 50 ticks puts S/N well above 3σ. |
| 5. Default dwell times (24h HEALTHY recruiter-trap) | **Deferred to config-yaml session** | Added a TODO comment in `DEFAULT_PROFILES` documenting the trap and the planned `demo_mode` shortcut (compress HEALTHY to ~60 ticks). Don't want to add a constructor flag for it now — natural fit for the YAML loader. |
| 6. Hidden AWS-specificity | **Confirmed (no change in this PR)** | ISO-8601-ms timestamps validated. Payload-size warning (AWS IoT bills per 5KB) carried forward to the lambda_s3_batcher / lambda_scorer sessions — noted in `context/_interfaces.md` follow-ups. |
| 7. Dead `Pump()` instantiation | **Addressed** | Removed the shadowing first `Pump(...)` in `test_degrading_caps_at_ceiling`. Single instantiation now, comment clarifies the long-dwell override. |
| Bonus — `.gitignore` additions | **Addressed** | Added `.envrc` (direnv) and `.python-version` (pyenv) with an attribution comment pointing at this review. |
| Bonus — FAILED-state design choice (b) | **Confirmed** | Gemini validated: keep emitting at degradation=1.0 so downstream anomaly detection sees the failure rather than treating it as a network disconnect. |

Final post-resolution test count: **30 passing, 0 failing** (added 1 derivative test, kept all original coverage). Test runtime ~0.06 s.
