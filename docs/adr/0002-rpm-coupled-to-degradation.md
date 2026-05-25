# ADR 0002 — Couple RPM to Degradation in the Pump Physical Model

- **Status:** Accepted
- **Date:** 2026-05-25
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)
- **Supersedes (in part):** PLAN.md §2.2 — RPM equation

## Context

PLAN.md §2.2 specifies the pump physical model as four equations driven by a shared `degradation ∈ [0, 1]` term:

```
bearing_temp   = ambient + 0.02 * RPM + degradation * 15 + N(0, 0.5)
vibration_amp  = 0.3            + degradation * 2.5      + N(0, 0.05)
motor_current  = 4.0            + degradation * 1.2      + N(0, 0.1)
RPM            = setpoint                                + N(0, 5)
```

Three of the four are degradation-coupled; RPM is not. The Gemini review of the 2026-05-24 simulator implementation (review_packets/2026-05-24-simulator-pump.md, question #3) flagged this as physically implausible: a pump in the FAILED state with `degradation = 1.0` still emits ~1800 RPM, which is the rotation rate of a healthy unit. Real pumps slow, stutter, or seize as bearings degrade.

For a portfolio project explicitly framed around AWS-aligned industrial IoT (per `context/_global.md` north star #3), this kind of inconsistency reads as "toy generator" rather than "industrial simulator" — a reviewer with even passing IoT experience will spot it.

## Decision

Replace the RPM equation with a degradation-coupled form:

```
RPM = setpoint * (1 - degradation) + N(0, 5 + 15 * degradation)
```

Behavior at the endpoints:
- `degradation = 0` (HEALTHY): `RPM = setpoint + N(0, 5)` — identical to PLAN.md §2.2.
- `degradation = 1` (FAILED): `RPM = 0 + N(0, 20)` — pump near-stationary, large stutter.

The other three equations (bearing, vibration, motor current) are unchanged from PLAN.md §2.2.

PLAN.md §2.2 has been updated in-place to match (per PO sign-off, option 2 of the 2026-05-25 resolution exchange). This ADR remains the authoritative *justification* and decision record; PLAN.md remains the *spec snapshot*.

## Alternatives considered

**A. Stay literal to PLAN.md §2.2.** Original implementation. Rejected: physical implausibility undermines the portfolio narrative, and PLAN.md is a working document, not a frozen contract — DEV_NORMS §9 explicitly allows reality-driven divergence as long as both sides are updated and an ADR is written.

**B. Stop emitting RPM in FAILED state (return 0 or None).** Avoids the implausibility but creates a discontinuity in the telemetry shape that downstream scoring/drift code would have to handle as a special case. Rejected — keeps the contract uniform (one telemetry dict shape across all states, per `context/_interfaces.md`).

**C. Add a separate "pump_running" boolean field to the telemetry dict.** Two-line implementation, but balloons the contract surface area and forces every downstream consumer to honor the flag. Rejected — out of proportion for the underlying physical issue, which a continuous RPM-degradation coupling handles cleanly.

**D. Use a more complex RPM model** (exponential decay, threshold-based cutoff, bearing-resonance harmonics, etc.). Higher physical fidelity. Rejected: violates the same "first-principles simplicity" rationale that keeps the degradation-evolution model linear (see `pump.py` docstring on P-F curves). The portfolio narrative is "real-time observability pipeline," not "high-fidelity pump physics simulator."

## Consequences

**Positive:**
- FAILED-state telemetry now physically defensible: stationary pump, hot bearings (from accumulated wear), high vibration (linear in degradation).
- Bearing-temp envelope automatically reflects the RPM coupling — a failing pump runs cooler at the bearings because it's not spinning, even though degradation contributes a +15°C term. This is the correct physical picture.
- Single ADR-tracked deviation from PLAN.md; trade-off explicit in the docstring.

**Negative:**
- `bearing_temp` is no longer monotonic in degradation. At low degradation, RPM and the direct `degradation * 15` term both contribute; as degradation rises, the RPM contribution shrinks faster than the direct term grows, so bearing temp peaks somewhere in DEGRADING territory and then falls. This is physically correct but breaks the naive intuition "higher state = hotter pump" that an earlier test relied on. Test renamed and asserts on vibration (which remains cleanly monotonic in degradation).
- One small downstream consideration for the ML model session: features derived from bearing temp need to handle the non-monotonicity (e.g. rolling-window std will be a better wear indicator than raw bearing temp alone). Noted as a follow-up for `context/model.md`.

**Follow-ups:**
- Carry the bearing-temp non-monotonicity note into `context/model.md` when the model session opens, so feature engineering doesn't pre-suppose monotonicity.
- If future sessions add more physical fidelity (e.g. cavitation harmonics), open a new ADR; do not silently extend this one.

## References

- PLAN.md §2.2 — updated in-place to match this ADR.
- Review packet: `review_packets/2026-05-24-simulator-pump.md` (question #3).
- Review response: `review_responses/2026-05-24-simulator-pump.md` (point #3 — Gemini recommended).
- Implementation: `simulator/pump.py::_sample` — the equation lives here with an inline pointer back to this ADR.
- Tests: `simulator/tests/test_pump.py::test_failed_state_keeps_emitting_telemetry` (new envelope), `test_failing_has_higher_vibration_than_degrading_at_ceiling` (renamed from bearing-temp comparison).
- DEV_NORMS §9 — process rationale for PLAN.md ↔ ADR divergence.
