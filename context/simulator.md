# simulator

## Purpose
Synthetic fleet of ~15 industrial pumps. Publishes telemetry JSON every 2 seconds to MQTT (local Mosquitto) or AWS IoT Core. Drives the three drift demo scenarios.

## Current state
- [x] `pump.py` physical model + four-state machine landed 2026-05-24 (30 pytest tests passing). See `docs/sessions/2026-05-24-simulator-pump-model.md`.
- [x] Gemini review on the pump model completed 2026-05-25; resolution committed alongside. See `review_responses/2026-05-24-simulator-pump.md` and the filled Resolution table in `review_packets/2026-05-24-simulator-pump.md`.
- [x] ADR 0002 — RPM coupled to degradation. PLAN.md §2.2 updated in-place to match.
- [ ] `simulator/config.yaml` loading (will also add `demo_mode` to compress HEALTHY dwell — see TODO in `DEFAULT_PROFILES`).
- [ ] MQTT publishing (paho-mqtt, asyncio).
- [ ] Scenario scripting (seasonal drift, fleet expansion, real failure).
- Spec source: `PLAN.md §2.2` (with ADR 0002 deviation). Telemetry dict matches `context/_interfaces.md`.

## Interfaces (in / out)
- **Out:** MQTT topic `factory/pumps/{pump_id}/telemetry` with the telemetry JSON in `_interfaces.md`. (Publishing not yet wired — `Pump.step()` currently returns the dict; subscriber lives in a later session.)
- **In:** `simulator/config.yaml` (pump count, scenario selection, broker target) — not yet implemented.
- **Switchable target:** local Mosquitto vs AWS IoT Core with mTLS. Same code path.

## Physical model
See `PLAN.md §2.2` (RPM equation now per ADR 0002) for equations. Per-pump state machine: `HEALTHY → DEGRADING → FAILING → FAILED`. Degradation evolves linearly with per-state `(rate_per_tick, ceiling)`; FAILED pins to 1.0 and emits stationary-with-stutter RPM.

## Open questions
- Calibrate noise/degradation against NASA IMS or Case Western Reserve datasets, or pure first-principles? (HANDOFF.md §6 Q2 — default: first-principles. Gemini agreed for portfolio context; calibration is deferred indefinitely unless a recruiter asks.)
- Concurrency model: single asyncio loop with 15 tasks, or process-per-pump? Default: asyncio + paho-mqtt. Decided in MQTT session.

## Related ADRs
- **ADR 0002** — RPM coupled to degradation (supersedes PLAN.md §2.2 RPM equation).
- Likely future ADRs: synthetic data strategy, asyncio choice, mTLS provisioning flow.
