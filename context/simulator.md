# simulator

## Purpose
Synthetic fleet of ~15 industrial pumps. Publishes telemetry JSON every 2 seconds to MQTT (local Mosquitto) or AWS IoT Core. Drives the three drift demo scenarios.

## Current state
- [x] `pump.py` physical model + four-state machine landed 2026-05-24 (30 pytest tests passing). See `docs/sessions/2026-05-24-simulator-pump-model.md`.
- [x] Gemini review on the pump model completed 2026-05-25; resolution committed alongside. See `review_responses/2026-05-24-simulator-pump.md` and the filled Resolution table in `review_packets/2026-05-24-simulator-pump.md`.
- [x] ADR 0002 — RPM coupled to degradation. PLAN.md §2.2 updated in-place to match.
- [x] `simulator/config.yaml` loading + `demo_mode` HEALTHY-dwell shortcut landed 2026-05-25 (47 config tests). See `docs/sessions/2026-05-25-simulator-config-yaml.md`. Schema in `simulator/config.py`; annotated example in `simulator/config.example.yaml`. PyYAML added as first runtime dep.
- [x] MQTT publishing (aiomqtt, asyncio) landed 2026-05-25 — Publisher ABC + LocalPublisher (Mosquitto-backed) + AwsIotPublisher stub + Fleet runner. See `docs/sessions/2026-05-25-simulator-mqtt-publishing.md` and ADR 0003. Schema gained a conditional `broker.tls` sub-block. `python -m simulator --config simulator/config.yaml` is the entry point.
- [x] AWS IoT mTLS publisher — `AwsIotPublisher.__aenter__` landed 2026-05-27. File-existence checks → SSLContext build (`create_default_context(SERVER_AUTH, cafile=ca_path)` + `load_cert_chain`) → `aiomqtt.Client(tls_context=..., port=8883)`. SSL/OS errors and MqttError both wrap as `PublisherError` and feed the runner's retry-forever loop. `Fleet.from_config` no longer rejects `target: aws-iot`; the publisher itself is the gate. See `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` and ADR 0003 §Addendum 2026-05-27 "AwsIotPublisher wired".
- [x] **Scenario scripting (seasonal_drift, fleet_expansion, real_failure) landed 2026-05-28.** `simulator/scenario.py` ships a `Scenario` ABC + four concretes (`HealthyScenario`, `SeasonalDrift`, `FleetExpansion`, `RealFailure`) + a `make_scenario(config)` factory. `Fleet.from_config` accepts all four `ScenarioKind` values — the `NotImplementedError` reject was dropped (mirroring the 2026-05-27 aws-iot drop). The runner spawns a single scenario controller task alongside the per-pump publish tasks; it calls `scenario.apply(fleet, tick)` once per `tick_seconds`. `ScenarioError` halts the fleet symmetrically with `PublisherConfigError` (exit code 5). Gemini review caught a real `asyncio.gather` task-lifecycle bug for `Fleet.add_pump` (Q2 — fixed via `asyncio.wait(FIRST_COMPLETED)` loop); also drove `Pump.get_profile` / `set_profile` (Q4 — drops `_profiles` private access) and seed plumbing through scenarios (Q6 — for future stochastic scenarios). 44 new tests (total 179 → 223). See `docs/sessions/2026-05-28-simulator-scenarios.md`, ADR 0004 (with 2026-05-28 Addendum), and the review packet/response under `review_packets/` / `review_responses/`.
- Spec source: `PLAN.md §2.2` (pump model with ADR 0002 deviation on RPM) and `PLAN.md §4` (per-scenario expected behaviors). Telemetry dict matches `context/_interfaces.md`.

## Interfaces (in / out)

**Out — MQTT.** Topic `factory/pumps/{pump_id}/telemetry`, JSON payload per `_interfaces.md`. QoS 0, retain=False (telemetry is time-series). One MQTT connection per pump in BOTH modes (per ADR 0003) — `client_id` matches `pump_id` so broker logs map cleanly to fleet members.

**Out — `Publisher` ABC** (`simulator/publisher.py`). Three abstract methods: `__aenter__` connects, `__aexit__` disconnects, `publish(topic, payload)` emits one JSON message. Implementations:
- `LocalPublisher` — wraps `aiomqtt.Client`, Mosquitto-targeted, unauthenticated, default port 1883.
- `AwsIotPublisher` — wraps `aiomqtt.Client` with mTLS. Constructed with a `TlsConfig`; `__aenter__` checks the three paths exist on disk, builds an `ssl.SSLContext` (Amazon Root CA validates the server cert, per-Thing cert+key authenticate the client), and connects to the AWS IoT ATS endpoint on port 8883. Wired 2026-05-27. Wire shape identical to LocalPublisher.
- Transport errors from either subclass surface as `PublisherError`. Static config errors (missing cert, malformed PEM, bad URL) surface as `PublisherConfigError` (subclass) — halt-the-fleet rather than retry-forever (Gemini Q3, 2026-05-27 review).

**In — `simulator/config.yaml`.** Loader in `simulator/config.py` (pure schema validation: required-keys, unknown-keys-reject, type + range, conditional `broker.tls`). Deserializes to a frozen `SimulatorConfig`. `profiles_for(config)` produces the per-state `StateProfile` dict for `Pump`, applying the `demo_mode` HEALTHY-dwell override.

**Scenario — `Scenario` ABC** (`simulator/scenario.py`). One abstract method: `apply(fleet, tick) -> None` (async). Called once per `tick_seconds` by `Fleet._run_scenario`. Four concretes ship today, all parametrised by constructor args with defaults in module-level `DEFAULT_*` constants:
- `HealthyScenario` — no-op. Used when `config.scenario == HEALTHY`.
- `SeasonalDrift` — modulates each pump's `ambient` on a sine wave (`base + amplitude * sin(2π · tick / period)`). Base captured per pump on first apply.
- `FleetExpansion` — at `expand_at_tick`, calls `fleet.add_pump` for `new_pump_count` ids, applies a vibration-baseline shift by mutating the new pumps' HEALTHY ceiling. Idempotent.
- `RealFailure` — schedule `dict[PumpState, int]` of "force target pump into state X at tick T". Default target is `P-07` (PLAN.md §4 names it). `Pump.force_state` resets the in-state tick counter so dwell-based auto-advancement starts fresh.
- `ScenarioError` (raises from inside `apply` or wrapped by `_run_scenario` for unexpected exceptions) is halt-the-fleet — mirrors `PublisherConfigError`. Maps to exit code 5 in `main()`.
- `make_scenario(config)` factory dispatches by `ScenarioKind`.

**Runner — `Fleet`** (`simulator/runner.py`). `from_config(config)` builds N `(Pump, Publisher)` pairs (seeds = `base_seed + pump_index`, ids = `P-00`..`P-NN`), constructs the scenario via `make_scenario`, and stashes `pump_factory` + `publisher_factory` closures so `FleetExpansion` can grow the fleet mid-run. `run()` spawns one asyncio task per pump on `tick_seconds` cadence (default 2.0s, PLAN.md §2.2) plus one extra task for the scenario controller. Retry-forever per-pump backoff (1s → 30s, reset on successful **publish**). Halt-the-fleet on `PublisherConfigError` or `ScenarioError`. Shutdown via `asyncio.Event` set from `request_shutdown()` (signal-handler safe).

**Switchable target:** local Mosquitto vs AWS IoT Core with mTLS. Same `Publisher` ABC, same per-pump connection topology, same `Fleet` runner. Scenarios run identically on both (the scenario layer never touches the publisher).

## Physical model
See `PLAN.md §2.2` (RPM equation per ADR 0002) for equations. Per-pump state machine: `HEALTHY → DEGRADING → FAILING → FAILED`. Degradation evolves linearly with per-state `(rate_per_tick, ceiling)`; FAILED pins to 1.0 and emits stationary-with-stutter RPM. `Pump.set_ambient(value)`, `Pump.force_state(state)`, and `Pump.get_profile`/`set_profile(state, profile)` are scenario-callable mutators (the profile getter/setter added 2026-05-28 per Gemini Q4 review); all four are documented as scenario-only on the methods.

## Open questions

- Calibrate noise/degradation against NASA IMS or Case Western Reserve datasets, or pure first-principles? (HANDOFF.md §6 Q2 — default: first-principles. Gemini agreed for portfolio context; calibration is deferred indefinitely unless a recruiter asks.)
- **Parametric scenarios in YAML.** Scenario defaults today are hardcoded in `simulator/scenario.py` (`DEFAULT_SEASONAL_PERIOD_TICKS = 180`, etc.). YAML-level overrides are deferred until the drift/model session shows what magnitudes actually exercise the PSI detector — that's when the parameters have a concrete justification. ADR 0004 §"Follow-ups".
- **Mid-tick partial-mutation contract.** Today's three scenarios do at most one mutation per `apply` call, so a `ScenarioError` mid-mutation isn't reachable. If a future scenario does multi-pump batched updates, the partial-mutation visibility contract needs tightening or documenting. Flagged in ADR 0004 §"Follow-ups". Gemini question candidate.
- ~~Concurrency model~~ — **RESOLVED 2026-05-25 (ADR 0003):** single asyncio event loop, one task per pump, one MQTT connection per pump in both modes (mode parity per north star #6).
- ~~AWS IoT mTLS implementation~~ — **RESOLVED 2026-05-27:** `AwsIotPublisher.__aenter__` wired with file-existence + SSLContext + aiomqtt mTLS connect. See session log + ADR 0003 §Addendum 2026-05-27.
- ~~Scenario runner shape~~ — **RESOLVED 2026-05-28 (ADR 0004):** tick-driven, fleet-level orchestrator, single `apply(fleet, tick)` method. See session log + ADR 0004.

## Related ADRs

- **ADR 0002** — RPM coupled to degradation (supersedes PLAN.md §2.2 RPM equation).
- **ADR 0003** — Asyncio + aiomqtt, per-pump connection (both modes), retry-forever backoff, shape-only TLS schema validation. Addendum 2026-05-27 "Windows event-loop policy" + Addendum 2026-05-27 "AwsIotPublisher wired" + Addendum 2026-05-28 "Static config errors halt the fleet".
- **ADR 0004** — Tick-driven scenario controller, fleet-level, single-method interface. Bundles the four design choices for the scenario layer.
- Likely future ADRs: Terraform-managed IoT Things/policies (currently Console-provisioned per the 2026-05-27 brief), YAML-parametric scenarios (deferred per ADR 0004 §"Follow-ups").
