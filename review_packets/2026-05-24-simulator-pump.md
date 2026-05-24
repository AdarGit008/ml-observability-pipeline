# Review Packet 2026-05-24 — simulator — pump-model-and-state-machine

> Paste this entire file into Gemini via:
> `gemini -p "$(cat review_packets/2026-05-24-simulator-pump.md)" > review_responses/2026-05-24-simulator-pump.md`

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
```
# Python
__pycache__/
*.py[cod]
*.so
.Python
*.egg-info/
.venv/
venv/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
.ruff_cache/

# Terraform
# Note: .terraform.lock.hcl is intentionally NOT ignored (HashiCorp guidance —
# commit it so provider versions stay reproducible across machines).
.terraform/
*.tfstate
*.tfstate.*
*.tfvars
!*.tfvars.example
crash.log
crash.*.log
.terraformrc
terraform.rc

# Project secrets / certs (per _interfaces.md §2.4 — IoT x.509 certs)
simulator/.secrets/
**/.env
*.pem
*.key
*.crt

# Local data (Docker volumes — InfluxDB, Mosquitto persistence when local_runtime lands)
data/
local_runtime/data/

# IDE / OS
.vscode/
.idea/
.DS_Store
Thumbs.db
```

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
| FAILED behavior | Keep emitting telemetry, `degradation` pinned to 1.0 | Lets downstream scoring/drift see the failure rather than starving. (a)/(c) alternatives would force a separate code path. |
| State advancement | Automatic by `dwell_ticks`, plus manual `force_state()` | Auto-advance for "let it run" demos; manual override for scripted drift scenarios. |
| RNG | Pump-private `random.Random(seed)` | Deterministic tests, no global-state contamination. |
| Timestamps | `step(now=...)` injectable, default `datetime.now(UTC)`; format `YYYY-MM-DDTHH:MM:SS.mmmZ` (truncate µs → ms) | Reproducible test assertions; matches `_interfaces.md` example exactly. |
| Defaults | `ambient=22.0`, `setpoint=1800.0`, dwell=24h/200/200/∞ | Recruiter-readable, mentioned in code comments. |

Tick order in `step()`:
1. Advance degradation (apply rate, clamp to ceiling, clamp to [0,1]).
2. Sample sensor noise → build telemetry dict.
3. Increment `ticks_in_state`.
4. Check `dwell_ticks` → maybe auto-transition to next state.

→ Step 4 happens *after* emission, so a reading "in state X" reflects X's envelope; the transition takes effect on the next tick. Calling this out because it's a judgment call (alternative: transition first, then sample).

### `simulator/tests/test_pump.py`
29 pytest tests. Test inventory:

```
pump_id validation:      6 parametrized rejects + 1 accept
telemetry dict shape:    keys, types, pump_id roundtrip          (3)
timestamps:              ISO-8601 ms, naive→UTC, default recent  (3)
HEALTHY:                 initial=0, stays under ceiling, band    (3)
DEGRADING:               monotonic rise, ceiling cap             (2)
FAILING:                 hotter than DEGRADING at same seed      (1)
FAILED:                  pinned, still emits, no auto-advance    (3)
Transitions:             auto through all four, manual force,    (3)
                         force rejects non-PumpState
Reproducibility:         same seed identical, diff seed diverges (2)
Constructor validation:  out-of-range deg, FAILED snaps to 1     (2)
                                                          TOTAL: 29
```

All pass in ~0.06s.

## Specific questions for Gemini

1. **Degradation-trajectory model.** I picked a per-state `(rate_per_tick, ceiling)` pair, linearly accumulating. PLAN.md §2.2 says "configurable dwell times" but gives no trajectory shape. Is a linear-ramp-to-ceiling defensible as "first-principles" per `simulator.md`'s open question? What would a more physically realistic model look like (e.g. nonlinear acceleration in FAILING, shock spikes, partial-recovery on maintenance) and is it worth the complexity *for a portfolio piece*?

2. **Tick ordering.** In `Pump.step()` I advance degradation → sample → then check auto-transition. Should the transition check happen *before* sampling instead, so a tick that crosses a state boundary reflects the new state's envelope? Or is the current "emit reflects entering-state, transition affects next tick" semantic cleaner?

3. **RPM is independent of degradation.** PLAN.md §2.2's RPM equation has no degradation term: `RPM = setpoint + N(0, 5)`. So a FAILED pump still emits ~1800 RPM, which is physically implausible — real failed pumps don't spin at setpoint. Strict adherence to §2.2 vs. realism: should I add a degradation-coupled RPM term (e.g. `RPM = setpoint * (1 - 0.5*degradation) + N(0, 5 + 20*degradation)`) and update §2.2 via an ADR, or stay literal?

4. **Test for "FAILING hotter than DEGRADING"** (`test_failing_telemetry_hotter_than_degrading`). I instantiate each pump at the state's *ceiling* degradation to compare steady-state envelopes. Does this hide a bug — e.g. if the ceiling-enforcement code were wrong but the rate-only difference still happened to produce the right inequality? Should I also assert from-zero-rampup behavior?

5. **Default dwell times.** HEALTHY=43_200 ticks (~24h real time), DEGRADING=200, FAILING=200, FAILED=∞. These are educated guesses, not calibrated against any dataset. `simulator.md` open question Q2 (NASA IMS / Case Western Reserve) suggests data-calibrated alternatives. Should the v1 commit (a) keep first-principles defaults as documented, (b) defer until calibration data is reviewed, or (c) cite a paper / specsheet for at least the orders of magnitude?

6. **Hidden AWS-specificity.** Per north star #4 (mode parity), nothing in `pump.py` should presume the AWS path. Is there anything in this module that quietly couples to AWS (e.g. timestamp format that AWS IoT prefers and Mosquitto doesn't, an envelope shape that only Lambda needs)?

7. **The two `Pump(...)` calls inside `test_degrading_caps_at_ceiling`.** I shadow the first `p` with a re-constructed `p` that has a long-dwell profile. The first construction is unused after the rewrite. Bug or intentional? (Calling out: it's a leftover from an earlier iteration — please confirm whether to keep the structure for readability or delete the dead instantiation.)

## What I'm NOT looking for in this review
- Style / lint nits — a separate `ruff`/`black` pass will run before merge.
- MQTT publishing logic — explicitly out of scope per session brief; coming in a later session.
- Config-file (YAML) loading — separate session.
- More coverage of dwell-tick *boundary* arithmetic — already covered by `test_auto_transitions_through_all_four_states` with `FAST_DWELL` (dwell=2).
- AWS infra concerns — no Terraform touched.

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. | Addressed / Deferred / Rejected | |
| 2. | | |
| 3. | | |
| 4. | | |
| 5. | | |
| 6. | | |
| 7. | | |
