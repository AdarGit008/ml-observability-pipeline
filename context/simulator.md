# simulator

## Purpose
Synthetic fleet of ~15 industrial pumps. Publishes telemetry JSON every 2 seconds to MQTT (local Mosquitto) or AWS IoT Core. Drives the three drift demo scenarios.

## Current state
- [x] `pump.py` physical model + four-state machine landed 2026-05-24 (30 pytest tests passing). See `docs/sessions/2026-05-24-simulator-pump-model.md`.
- [x] Gemini review on the pump model completed 2026-05-25; resolution committed alongside. See `review_responses/2026-05-24-simulator-pump.md` and the filled Resolution table in `review_packets/2026-05-24-simulator-pump.md`.
- [x] ADR 0002 — RPM coupled to degradation. PLAN.md §2.2 updated in-place to match.
- [x] `simulator/config.yaml` loading + `demo_mode` HEALTHY-dwell shortcut landed 2026-05-25 (47 config tests). See `docs/sessions/2026-05-25-simulator-config-yaml.md`. Schema in `simulator/config.py`; annotated example in `simulator/config.example.yaml`. PyYAML added as first runtime dep.
- [x] MQTT publishing (aiomqtt, asyncio) landed 2026-05-25 — Publisher ABC + LocalPublisher (Mosquitto-backed) + AwsIotPublisher stub + Fleet runner. 138 tests passing total (30 pump + 63 config + 21 publisher + 24 runner). See `docs/sessions/2026-05-25-simulator-mqtt-publishing.md` and ADR 0003. Schema gained a conditional `broker.tls` sub-block. `python -m simulator --config simulator/config.yaml` is the entry point.
- [ ] AWS IoT mTLS publisher — `AwsIotPublisher.__aenter__` raises `NotImplementedError` and `Fleet.from_config` rejects `target: aws-iot` up front. Blocked on AWS account provisioning (⬜ in `ml-obs-pipeline-context`). Schema is ready; only the connect path is missing.
- [ ] Scenario scripting (seasonal drift, fleet expansion, real failure). All four `scenario` values parse cleanly; `Fleet.from_config` raises `NotImplementedError` for non-healthy values. The signal moved from `load_config` (config-yaml session) to `Fleet.from_config` (mqtt-publishing session) — see ADR 0003.
- Spec source: `PLAN.md §2.2` (with ADR 0002 deviation on RPM, ADR 0003 on the runner/publisher shape). Telemetry dict matches `context/_interfaces.md`.

## Interfaces (in / out)

**Out — MQTT.** Topic `factory/pumps/{pump_id}/telemetry`, JSON payload per `_interfaces.md`. QoS 0, retain=False (telemetry is time-series). One MQTT connection per pump in BOTH modes (per ADR 0003) — `client_id` matches `pump_id` so broker logs map cleanly to fleet members.

**Out — `Publisher` ABC** (`simulator/publisher.py`). Three abstract methods: `__aenter__` connects, `__aexit__` disconnects, `publish(topic, payload)` emits one JSON message. Implementations:
- `LocalPublisher` — wraps `aiomqtt.Client`, Mosquitto-targeted, unauthenticated.
- `AwsIotPublisher` — STUB. Accepts `TlsConfig` at construction; raises `NotImplementedError` on `__aenter__`. Replaced when the AWS-IoT session opens.
- Transport errors from either subclass surface as `PublisherError` so the `Fleet` runner doesn't import aiomqtt directly.

**In — `simulator/config.yaml`.** Loader in `simulator/config.py` (pure schema validation: required-keys, unknown-keys-reject, type + range, conditional `broker.tls`). Deserializes to a frozen `SimulatorConfig`. `profiles_for(config)` produces the per-state `StateProfile` dict for `Pump`, applying the `demo_mode` HEALTHY-dwell override. See `simulator/config.example.yaml` for the canonical annotated example, including the commented-out `tls:` block for aws-iot.

**Runner — `Fleet`** (`simulator/runner.py`). `from_config(config)` builds N `(Pump, Publisher)` pairs (seeds = `base_seed + pump_index`, ids = `P-00`..`P-NN`), rejects non-healthy scenarios and `target: aws-iot` up front. `run()` spawns one asyncio task per pump on `tick_seconds` cadence (default 2.0s, PLAN.md §2.2). Retry-forever per-pump backoff (1s → 30s, reset on successful reconnect). Shutdown via `asyncio.Event` set from `request_shutdown()` (signal-handler safe).

**Switchable target:** local Mosquitto vs AWS IoT Core with mTLS. Same `Publisher` ABC, same per-pump connection topology, same `Fleet` runner. The implementation gap is exactly one class (`AwsIotPublisher.__aenter__`).

## Physical model
See `PLAN.md §2.2` (RPM equation per ADR 0002) for equations. Per-pump state machine: `HEALTHY → DEGRADING → FAILING → FAILED`. Degradation evolves linearly with per-state `(rate_per_tick, ceiling)`; FAILED pins to 1.0 and emits stationary-with-stutter RPM.

## Open questions

- Calibrate noise/degradation against NASA IMS or Case Western Reserve datasets, or pure first-principles? (HANDOFF.md §6 Q2 — default: first-principles. Gemini agreed for portfolio context; calibration is deferred indefinitely unless a recruiter asks.)
- ~~Concurrency model~~ — **RESOLVED 2026-05-25 (ADR 0003):** single asyncio event loop, one task per pump, one MQTT connection per pump in both modes (mode parity per north star #6).

## Related ADRs

- **ADR 0002** — RPM coupled to degradation (supersedes PLAN.md §2.2 RPM equation).
- **ADR 0003** — Asyncio + aiomqtt, per-pump connection (both modes), retry-forever backoff, shape-only TLS schema validation. Bundles the five interlocking MQTT-publishing choices made on 2026-05-25.
- Likely future ADRs: scenario runner shape, AWS IoT mTLS provisioning flow.
