# ADR 0004 — Tick-Driven Scenario Controller, Fleet-Level, Single-Method Interface

- **Status:** Accepted
- **Date:** 2026-05-28
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

`PLAN.md §4` describes three demo scenarios the simulator must drive:

1. **seasonal_drift** — ambient temperature rises across the plant; bearing temps climb fleet-wide; fleet-level PSI catches it.
2. **fleet_expansion** — three new pumps appear mid-run with a different vibration baseline; per-pump PSI catches the new pumps while the rest stay clean.
3. **real_failure** — pump `P-07` genuinely fails (`HEALTHY → DEGRADING → FAILING → FAILED`); per-pump PSI and the model both flag it.

All four `ScenarioKind` values already parse cleanly (config-yaml session, 2026-05-25). `Fleet.from_config` raised `NotImplementedError` for the three non-healthy cases until this session (mqtt-publishing session, 2026-05-25). The 2026-05-27 aws-iot-publisher session set the precedent for "drop the guard when the implementation lands" — this session does the same for scenarios.

Four interlocking design choices fall out of the brief, none obvious enough to leave implicit:

1. **Event-driven vs. tick-driven.** Scenarios can either fire callbacks at scheduled wall-clock moments (asyncio sleeps + tasks) or run synchronously on a tick cadence (called once per `tick_seconds`).
2. **Per-pump hook vs. fleet-level orchestrator.** Each per-pump task could call `Scenario.before_tick(pump)` before `pump.step()`, OR a single fleet-level scenario task could iterate the fleet and mutate it.
3. **Single `apply` method vs. lifecycle hooks** (`start` / `tick` / `stop` / per-state callbacks). Larger surface = more flexibility but more contract to keep.
4. **Where do mid-run new pumps get their `Publisher`?** `FleetExpansion` needs to construct a `Publisher` mid-run, but the scenario shouldn't know broker config details (target, URL, TLS).

Anchors from `context/_global.md`: mode parity (north star #6, the wire shape must NOT change between scenarios), single-PC dev (#2), $0 spend (#1, so no real-broker tests). Anchors from `simulator.md`: publisher layer is frozen (the session brief's constraint #1) — scenarios mutate `Pump` state and `Fleet` membership, never the wire shape or `Publisher` API.

## Decision

The simulator adopts the following four-part design for scenario control:

1. **Tick-driven.** A single `_run_scenario` coroutine on the `Fleet` ticks at `tick_seconds` cadence (same cadence as publishers, default 2.0 s) and calls `scenario.apply(self, tick)`. `tick` starts at 0 and increments by 1 per call. No wall-clock scheduling, no per-scenario sub-coroutines.
2. **Fleet-level orchestrator.** `Scenario.apply(fleet, tick)` sees the whole fleet on each tick — it iterates `fleet.members`, calls `fleet.get_pump(pump_id)`, or `fleet.add_pump(pump_id)` to grow the fleet mid-run. No per-pump hook on `Pump.step`.
3. **Single async `apply(fleet, tick)` method.** Scenarios manage their own state (cycle counters, "have I fired yet" flags) as instance attributes. There are no `start` / `stop` / per-state hooks. `HealthyScenario` is a no-op default that keeps the runner's task graph uniform (always exactly one scenario task).
4. **`Fleet.from_config` stashes `pump_factory` and `publisher_factory` closures.** `Fleet.add_pump(pump_id)` calls them to construct new (Pump, Publisher) pairs with the same broker config + ambient/setpoint as the original fleet. Scenarios never touch broker config directly.

Plus one halt-the-fleet contract addition: `ScenarioError` (defined in `simulator/scenario.py`) is treated like `PublisherConfigError` in `Fleet.run` — re-raised through `gather()`, other tasks drain via the shutdown event + the `DISCONNECT_TIMEOUT_SECONDS` ceiling, and `main()` exits with the distinct code `5`.

The three concrete scenarios fit this shape:

- **`SeasonalDrift`** — modulates `pump.set_ambient(base + amp * sin(2π · t / period))` for every pump on every tick. Base ambient is captured from each pump on first apply (so the modulation is around whatever the YAML resolved to, not a hardcoded 22 °C).
- **`FleetExpansion`** — at `expand_at_tick`, fires once: calls `fleet.add_pump(new_id)` for `new_pump_count` ids, applies a `vibration_baseline_shift` to each new pump's HEALTHY `StateProfile` ceiling. Idempotent on subsequent ticks via a `_fired` flag.
- **`RealFailure`** — schedule `dict[PumpState, int]` of "force pump X into state Y at tick N". On each scheduled tick, looks up the target pump and calls `pump.force_state(state)`. Other pumps are untouched.

## Alternatives considered

### 1. Tick-driven vs. event-driven

**A. Tick-driven (the decision).** One coroutine, awakes per `tick_seconds`, calls `apply`. Deterministic; tests are synchronous; integrates naturally with the existing tick-driven `Pump.step`.

**B. Event-driven** — each scenario spawns its own coroutines with `asyncio.sleep(wall_time)` until events fire. Rejected: more concurrency primitives for no expressive gain (all three scenarios reduce to "at tick N, do X"). Per-event sleeps would also drift relative to the publisher tick, complicating tests that want to assert "scenario fired between pump tick K and K+1".

**C. External clock (real wall-clock seconds).** Rejected: scenarios would need to know `tick_seconds` to translate between wall-clock and tick counts, AND tests would need to manipulate wall-clock to be deterministic. The `tick` counter is a clean abstraction; scenarios that need wall-clock can compute it as `tick * fleet.tick_seconds`.

### 2. Per-pump hook vs. fleet-level orchestrator

**A. Fleet-level orchestrator (the decision).** `Scenario.apply(fleet, tick)` sees the whole fleet. Some scenarios (`FleetExpansion`) inherently need fleet-level access — adding pumps cannot be expressed as a per-pump hook. Making the others (which only need one pump or all pumps uniformly) use the same shape keeps the interface uniform.

**B. Per-pump hook on `Pump.step`.** `Scenario.before_pump_tick(pump, tick)` called inside `_run_pump`. Rejected: `FleetExpansion` would become a special-case "scenario that doesn't fit the contract", AND mutations would be visible only after the next pump tick rather than all-at-once (subtle race semantics that don't actually exist with a fleet-level controller).

**C. Hybrid — both hooks.** Per-pump for cheap mutations, fleet-level coroutine for membership changes. Rejected: doubles the interface surface and forces every scenario implementer to decide which hook to use. The fleet-level shape covers all three scenarios with less ceremony.

### 3. Single `apply` vs. lifecycle hooks

**A. Single `apply(fleet, tick)` (the decision).** Smallest possible contract. Scenarios manage their own state as instance attributes (cycle counters, fired flags, schedule dicts). Mock scenarios in tests are 3 lines (just a class with one async method).

**B. `start(fleet)` / `tick(fleet, tick)` / `stop(fleet)`.** Rejected: none of the three concrete scenarios need a start hook (their state is initialised in `__init__`) or a stop hook (the shutdown event + `gather(return_exceptions=True)` handles cleanup). Adding the hooks "in case" is speculative work.

**C. Per-state callbacks** (e.g., `Scenario.on_pump_state_change(pump, old, new)`). Rejected: would require Pump to emit events, which is a bigger change than this session warrants. None of the three scenarios actually want this — they fire by tick number, not by state transition.

### 4. Where do mid-run new pumps get their Publisher?

**A. Factories captured by `Fleet.from_config` (the decision).** `_make_pump` and `_make_publisher` closures over the config; stored on the Fleet; called by `Fleet.add_pump`. The scenario never sees `BrokerConfig`. Direct-construction tests pass factories explicitly if they need to exercise `add_pump`; tests that don't want to grow the fleet can omit them.

**B. Scenario calls `make_publisher` directly.** Rejected: the scenario would need to know broker config (target, URL, TLS), reaching across module boundaries the rest of the simulator carefully decouples.

**C. Pre-build all conceivable pumps at startup, mark them "asleep" until activation.** Rejected: a guess at fleet size that almost certainly differs from `FleetExpansion`'s `new_pump_count`, AND publishers would hold idle TCP sockets to the broker.

## Consequences

**Positive:**

- **Three scenarios, one interface.** All concrete scenarios (`SeasonalDrift`, `FleetExpansion`, `RealFailure`) live in `simulator/scenario.py` with the same `apply(fleet, tick)` shape. Future scenarios (e.g., the stretch goal "burst noise on N pumps for M ticks") fit the same shape.
- **Publisher layer untouched.** Per the session brief's constraint #1. The wire shape — JSON topic + payload, QoS 0, retain=False — is unchanged. Same `Publisher` ABC, same `make_publisher` factory. Mode parity (north star #6) preserved automatically.
- **`Fleet.from_config` is now a no-`NotImplementedError` constructor for all four scenarios + both broker targets.** Mirrors the 2026-05-27 aws-iot drop. The only halt-the-fleet paths are `PublisherConfigError` (transport/cert) and `ScenarioError` (scenario logic).
- **Single asyncio loop is unchanged.** The scenario controller is one extra task per fleet, regardless of fleet size. At PLAN.md's 15-pump target, that's 16 tasks total — well within asyncio's comfort zone.
- **Deterministic tests.** `tick` is a counter, not a wall-clock value. Scenario tests run synchronously by calling `apply(fleet, tick)` directly, with no `asyncio.sleep`. Integration tests in `test_scenario.py` use `tick_seconds=0.001` to compress demo time into milliseconds.
- **`ScenarioError` halts the fleet symmetrically with `PublisherConfigError`.** The exit-code matrix (2, 4, 5) is distinct enough for CI to pinpoint the failure mode without parsing log messages.

**Negative:**

- **Pump state is now mutable from outside `Pump`** (`set_ambient`, plus the pre-existing `force_state`). Two privileged callers — `simulator/scenario.py` modules. Anyone else mutating from outside would violate the contract; lints can't catch this. Documented in the `Pump.set_ambient` docstring.
- **Mutation of `Pump._profiles` in `FleetExpansion._bias_pump`.** Reaching into a private attribute (`_profiles`) to override the HEALTHY ceiling. Cleaner alternatives (a `Pump.set_profile(state, profile)` setter) were skipped to keep the API surface narrow — the only caller is the scenario module, and the alternative ("scenario builds a new pump from scratch with a custom profile") would duplicate the pump construction logic in `Fleet.from_config`. Worth revisiting if a second scenario wants similar customisation.
- **No YAML-level parametrization of scenarios.** Defaults are hardcoded in `simulator/scenario.py` (e.g., `DEFAULT_SEASONAL_PERIOD_TICKS = 180`). The YAML config picks *which* scenario, not its parameters. Deferred until the drift/model session shows what magnitudes actually exercise the detector (the parameters then have a concrete justification rather than a guess). See "Follow-ups" below.
- **No real-broker integration test for FleetExpansion.** The end-to-end `test_fleet_from_config_with_fleet_expansion_grows_at_runtime` monkeypatches `make_publisher` to return `_RecordingPublisher`. A real-broker smoke test would catch issues with mid-run connect that the unit suite can't (the same trade-off ADR 0003 calls out for publisher-side tests). Acceptable: scenarios are pure in-memory mutations of the fleet; the connect path is the same as the initial fleet startup, which IS exercised by the existing manual smoke tests.
- **`add_pump` mutates `self._members` while `run()` is iterating over per-pump tasks.** Safe today because the iteration in `run()` happens once (initial spawn) and `self._tasks` is the list that grows; the snapshot returned by `Fleet.members` (a defensive copy) won't reflect the new pump until the next call. If a future change starts iterating `self._members` while `add_pump` could fire, this becomes a hazard — flag for the next architectural touch.

**Follow-ups:**

- **YAML-parametric scenarios.** Add `scenario_params` (or per-scenario sub-blocks) to `SimulatorConfig` once the drift/model sessions identify the magnitudes the PSI detector actually responds to. Currently tracked as an open question in `context/simulator.md`.
- **Compositional scenarios.** PLAN.md §4 frames the three as standalone, but a stretch demo could combine them (e.g., "real failure during seasonal drift"). The current `make_scenario` returns one Scenario per config; a `CompositeScenario` that iterates child scenarios in order would land cleanly on this interface without ABC changes.
- **`ScenarioError` mid-tick partial-mutation problem.** If a scenario's `apply` mutates pump A, then raises, pump A is left in the new state. Today's three scenarios don't mutate-then-fail (each does at most one mutation per tick), but the contract should be tightened or documented if a future scenario does multi-pump batch updates. Worth a Gemini question.

## References

- `PLAN.md §2.2` (pump model — ambient drives bearing_temp), §2.7 (drift detector), §4 (demo script — scenario expected behaviors)
- `context/simulator.md` (open `[ ]` scenario item, "Likely future ADRs: scenario runner shape")
- ADR 0002 (RPM coupled to degradation — the FAILED-pump RPM behavior `RealFailure` relies on for its lifecycle signal)
- ADR 0003 (publisher layer + retry-forever; the contract scenarios compose with but do NOT change)
- Session log: `docs/sessions/2026-05-28-simulator-scenarios.md`
- Review packet: `review_packets/2026-05-28-simulator-scenarios.md`

## Addendum 2026-05-28 — Gemini review fixes

Three changes landed after Gemini's review of the initial implementation (see `review_responses/2026-05-28-simulator-scenarios.md`):

- **`Fleet.run` task lifecycle (Gemini Q2).** The initial `asyncio.gather(*all_tasks)` evaluated its argument list exactly once at call time, so tasks created by `Fleet.add_pump` mid-run were orphaned — never awaited, exceptions lost, and "Task was destroyed but it is pending!" warnings on shutdown. Fixed by switching to an `asyncio.wait(FIRST_COMPLETED)` loop that re-folds `self._tasks` on each iteration. `asyncio.TaskGroup` (Python 3.11+) would be cleaner but the test sandbox runs 3.10. Regression test pinned in `test_scenario.py::test_add_pump_task_is_awaited_on_shutdown`.

- **`Pump.get_profile` / `Pump.set_profile` (Gemini Q4).** `FleetExpansion._bias_pump` had been mutating `pump._profiles` directly. The cleaner public API is now in `simulator/pump.py`; `_bias_pump` uses it. Five new Pump tests pin the contract.

- **Scenario seed plumbing (Gemini Q6).** `Scenario.__init__` now accepts `seed: Optional[int]`; `make_scenario(config)` passes `config.fleet.base_seed`. None of today's three concretes use it (all deterministic), but threading it through now means future stochastic scenarios are reproducible from day one. Three new tests pin the contract.

The three Gemini points that did NOT result in code changes — interface shape (Q1: validated), partial-mutation contract (Q3: documented as YAGNI), magnitudes (Q5: validated — 8 °C is ~16σ above noise), AWS-specificity (Q7: validated — scenarios are cleanly agnostic) — are summarised in the session log Resolution table.
