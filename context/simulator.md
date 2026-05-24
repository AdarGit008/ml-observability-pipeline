# simulator

## Purpose
Synthetic fleet of ~15 industrial pumps. Publishes telemetry JSON every 2 seconds to MQTT (local Mosquitto) or AWS IoT Core. Drives the three drift demo scenarios.

## Current state
- [x] `pump.py` physical model + four-state machine landed 2026-05-24 (29 pytest tests passing, pre-Gemini). See `docs/sessions/2026-05-24-simulator-pump-model.md`.
- [ ] Gemini review on the pump model — packet at `review_packets/2026-05-24-simulator-pump.md`, response not yet generated.
- [ ] `simulator/config.yaml` loading.
- [ ] MQTT publishing (paho-mqtt, asyncio).
- [ ] Scenario scripting (seasonal drift, fleet expansion, real failure).
- Spec source: `PLAN.md §2.2`. Telemetry dict matches `context/_interfaces.md`.

## Interfaces (in / out)
- **Out:** MQTT topic `factory/pumps/{pump_id}/telemetry` with the telemetry JSON in `_interfaces.md`.
- **In:** `simulator/config.yaml` (pump count, scenario selection, broker target).
- **Switchable target:** local Mosquitto vs AWS IoT Core with mTLS. Same code path.

## Physical model
See `PLAN.md §2.2` for equations. Per-pump state machine: `HEALTHY → DEGRADING → FAILING → FAILED`.

## Open questions
- Calibrate noise/degradation against NASA IMS or Case Western Reserve datasets, or pure first-principles? (HANDOFF.md §6 Q2 — default: first-principles.)
- Concurrency model: single asyncio loop with 15 tasks, or process-per-pump? Default: asyncio + paho-mqtt.

## Related ADRs
None yet. Likely ADRs: synthetic data strategy, asyncio choice, mTLS provisioning flow.
