# Session 2026-05-28 — simulator — scenarios

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`)
- **Context loaded:** `_global`, `simulator` (Tier 2 only — `_interfaces.md` not loaded; wire shape was frozen at session-brief time, no scenario touches it)
- **Duration:** ~2h

## Intent

Implement the scenario runner the prior two sessions deferred. `Fleet.from_config` had raised `NotImplementedError` for `seasonal_drift`, `fleet_expansion`, and `real_failure` since the config-yaml session (2026-05-25, moved to the runner in the mqtt-publishing session). This session drops that guard — mirroring the 2026-05-27 drop of the parallel `target: aws-iot` guard when `AwsIotPublisher` landed — and ships `simulator/scenario.py` with the three concretes + the abstract base.

## What changed

**New files:**

- `simulator/scenario.py` — `Scenario` ABC + four concretes (`HealthyScenario`, `SeasonalDrift`, `FleetExpansion`, `RealFailure`) + `make_scenario(config)` factory + `ScenarioError` exception. Module-level `DEFAULT_*` constants pin the demo magnitudes for each scenario (period 180 ticks, amplitude 8 °C, expansion at tick 60, real-failure target `P-07`, etc.). 442 lines.
- `simulator/tests/test_scenario.py` — 35 tests across five sections: interface contract, SeasonalDrift, FleetExpansion, RealFailure, end-to-end Fleet+scenario integration. 673 lines.
- `docs/adr/0004-tick-driven-scenario-controller.md` — bundles the four interlocking interface choices (tick-driven, fleet-level, single-method, factory-stashed Publishers). Promoted directly to Accepted (no design-pending state — three concrete scenarios validated the shape in this session).
- `review_packets/2026-05-28-simulator-scenarios.md` — 7 specific questions for Gemini, Resolution table to fill after response.

**Modified:**

- `simulator/runner.py` — `Fleet.__init__` accepts an optional `scenario: Scenario` (defaults to `HealthyScenario`), `publisher_factory`, and `pump_factory`. `Fleet.from_config` builds the scenario via `make_scenario` and stashes the two factories. Drops the `NotImplementedError` for non-healthy scenarios. New methods: `Fleet.get_pump(pump_id)`, `Fleet.add_pump(pump_id)`, `Fleet._run_scenario()`. `Fleet.run` spawns the scenario task alongside per-pump tasks and adds `ScenarioError` to the halt-the-fleet catch.
- `simulator/pump.py` — added `Pump.ambient` property and `Pump.set_ambient(value)` setter. Docstrings flag both as scenario-callable.
- `simulator/__main__.py` — adds `ScenarioError` catch in `main()`, maps it to new exit code 5 (`SCENARIO_ERROR_CODE`). Module docstring retires exit code 3 (the legacy `NotImplementedError` → 3 mapping; kept unallocated rather than recycled so existing CI patterns matching exit 3 don't accidentally trigger on a new failure).
- `simulator/__init__.py` — re-exports `Scenario`, `HealthyScenario`, `SeasonalDrift`, `FleetExpansion`, `RealFailure`, `ScenarioError`, `make_scenario`.
- `simulator/tests/test_runner.py` — renamed `test_from_config_rejects_non_healthy_scenario` to `test_from_config_accepts_all_scenarios` and inverted the assertion: now checks all four `ScenarioKind` values build a Fleet with the right Scenario subclass. The backoff-climbs-to-cap test now filters out the scenario task's `tick_seconds` wait values (the controller adds one extra `_wait_or_shutdown` caller per tick).
- `simulator/tests/test_main.py` — replaced `test_main_returns_3_when_scenario_not_implemented` with `test_main_no_longer_returns_3_for_non_healthy_scenario` (asserts the retired path); updated the exit-code-pin test's docstring to reference codes 4 (PublisherConfigError) and 5 (ScenarioError).
- `context/simulator.md` — scenario `[ ]` ticked; new "Scenario" interface block under "Interfaces"; open questions reduced (scenario runner shape resolved); ADR 0004 referenced.

PR: TBD — Adar opens after commit 4.

## Decisions

**ADR 0004 — Tick-Driven Scenario Controller.** Four interlocking choices bundled into one ADR:

1. **Tick-driven** (not event-driven). `Scenario.apply(fleet, tick)` called once per `tick_seconds` by a single fleet-level coroutine.
2. **Fleet-level orchestrator** (not per-pump hook). `apply` sees the whole fleet; can mutate any pump or grow the fleet via `add_pump`.
3. **Single-method interface** (not lifecycle hooks). Scenarios manage their own state. `HealthyScenario` is the no-op default so the runner's task graph stays uniform.
4. **Publisher/pump factories stashed at `Fleet.from_config`-time.** `FleetExpansion` calls `fleet.add_pump(id)` which uses the factories to build new (Pump, Publisher) pairs — the scenario never touches `BrokerConfig`.

**`ScenarioError` as a halt-the-fleet condition.** Mirrors `PublisherConfigError` in `Fleet.run`. Distinct exit code 5 so CI scripts can tell "scenario logic broke" from "MQTT certs broke". Per ADR 0004 §Decision and the precedent set by `PublisherConfigError` (Gemini Q3, 2026-05-27 review).

**Exit code 3 retired, not recycled.** The legacy `NotImplementedError` → 3 mapping is gone, but the code itself is left unallocated rather than reassigned. Reasoning: existing CI scripts that pattern-match exit 3 should fail conspicuously rather than silently start matching a new failure mode. Documented in `simulator/__main__.py` module docstring.

## Trade-offs surfaced

- **Pump state mutable from outside `Pump`** (`set_ambient`, plus the pre-existing `force_state`). Privileged callers are `simulator.scenario` modules only; no lint enforcement. Documented in the docstring. Alternative — making Pump fully immutable and rebuilding instances on mutation — was rejected as a much bigger refactor for marginal benefit.
- **`FleetExpansion._bias_pump` reaches into `pump._profiles`.** The cleaner alternative (a `Pump.set_profile(state, profile)` setter) was skipped because it'd be a single-caller API. Worth revisiting if a second scenario wants similar profile customisation.
- **No YAML-parametric scenarios.** Defaults are hardcoded in `simulator/scenario.py`. The YAML config picks *which* scenario, not its parameters. Deferred until the drift/model session shows what magnitudes actually exercise the PSI detector. ADR 0004 §"Follow-ups".
- **No real-broker integration test for FleetExpansion.** Unit suite monkeypatches `make_publisher` to return `_RecordingPublisher` for the mid-run-add test. Same trade as ADR 0003 calls out for publisher-side tests — manual smoke covers it.
- **`Fleet.add_pump` mutates `self._members` while `run()` is iterating per-pump tasks.** Safe today because the iteration in `run()` happens once at spawn time, and `self._tasks` (not `self._members`) is the list that the gather() walks. Documented as a hazard for any future change that starts iterating `self._members` while `add_pump` could fire.
- **Test count exceeded the brief's "~15-25 new tests" estimate.** Ended at 35 net new tests. Tighter coverage; no redundancy with prior modules; the integration tests (5) are heavier per-test than the unit tests.

## Gemini review highlights

Gemini ran via `.\scripts\gemini_review.ps1 -Slug 2026-05-28-simulator-scenarios`. Full response in `review_responses/2026-05-28-simulator-scenarios.md`; the Resolution table in `review_packets/2026-05-28-simulator-scenarios.md` records dispositions for all 7 points.

Top findings:

1. **Q2: real bug — `Fleet.run`'s `asyncio.gather` evaluates args once, orphaning mid-run-added tasks.** Caught here. The simulation looked fine; the bug surfaces as "Task was destroyed but it is pending" warnings on shutdown (or lost exceptions). Fixed by switching to an `asyncio.wait(FIRST_COMPLETED)` loop that re-folds `self._tasks`. Pinned by a new regression test. ADR 0004 §Addendum 2026-05-28.
2. **Q4: private `pump._profiles` access in `FleetExpansion._bias_pump`** — Gemini flagged as a senior-role-review red flag. Added `Pump.get_profile` / `Pump.set_profile` (Option A from the packet); `_bias_pump` uses them. 5 new Pump tests.
3. **Q6: scenario seed plumbing** — Gemini argued reproducibility is "the holy grail of simulation testing" and we should wire seeds through before the first stochastic scenario forces a flaky-demo bug. Added `seed: Optional[int]` to `Scenario.__init__`; `make_scenario` passes `config.fleet.base_seed`. 3 new tests.

Points that validated the design (no code change):

- Q1 (interface shape — "exactly as general as it needs to be")
- Q3 (partial-mutation rollback — YAGNI, since ScenarioError exits the process anyway)
- Q5 (SeasonalDrift magnitudes — 8 °C is "massive, perhaps too massive" at 16σ; will trigger PSI cleanly, may need epsilon smoothing on the detector side)
- Q7 (AWS-specificity — scenarios mutate the abstract domain only; mode parity preserved)

Gemini's summary: "Once those three are fixed, this is ready to merge." All three are fixed.

## State at end of session

- Tests: **224 passing, 1 skipped** (was 179 + 1 skipped pre-session; +35 from initial scenarios drop; +9 from post-Gemini fixes — 1 regression test for add_pump lifecycle + 5 Pump getter/setter + 3 seed plumbing).
  - Suite breakdown: 35 pump + 63 config + 34 publisher + 26 runner + 11 publisher_config_error + 3 publisher_shutdown + 8 main (skip + 7 passing) + 5 runner_config_error + 39 scenario.
- Sandbox baseline: `cp -r simulator /tmp/runwork/simulator && cd /tmp/runwork && python3 -m pytest simulator/tests/ -p no:cacheprovider -q` reports `223 passed, 1 skipped`. (The skip lifts on Python 3.12+, which Adar runs on Windows.)
- Open follow-ups (carried into the next simulator-related session):
  - YAML-parametric scenarios (ADR 0004 §"Follow-ups", once drift/model lands).
  - `FleetExpansion._bias_pump` private-attribute access — possible cleanup via a public Pump setter if a second caller emerges.
  - Real-broker smoke test for FleetExpansion (deferred; manual smoke covers it).
- `context/simulator.md` updated: yes — scenario `[ ]` ticked, new Scenario interface block under "Interfaces", three open questions resolved (concurrency, aws-iot mTLS, scenario runner shape).

## Note for next session

The simulator now has a complete demo stack — pump model, config, MQTT publisher (local + aws-iot), scenarios. Three natural next-session candidates, in priority order:

1. **First downstream consumer** — `lambda_scorer` or `local_runtime` subscribes to `factory/pumps/+/telemetry` and starts feeding the scoring pipeline. The simulator side is now ready; the next piece is something on the other end.
2. **Model training** — `model/` session. Run the simulator in fast-forward (demo_mode + custom profiles, no MQTT) to generate the 30-day-of-30-pumps training set described in PLAN.md §2.3, then train the `HistGradientBoostingClassifier` and dump `model.pkl` + `reference_distribution.json`. The drift detector needs `reference_distribution.json` to compute PSI.
3. **Drift detection** — `drift/` session. PSI implementation per PLAN.md §2.7. Could be done before or after model training; the reference distribution can be a synthetic seed for early iteration.

Watch items:

- **Scenario defaults are guesses.** When the drift session computes actual PSI on the simulator output for each scenario, the magnitudes may need tuning. The defaults live in `simulator/scenario.py` (`DEFAULT_SEASONAL_PERIOD_TICKS`, `DEFAULT_SEASONAL_AMPLITUDE_C`, etc.) — easy to change without touching the scenario logic.
- **`Fleet.add_pump` is mid-run-safe today, but only because nothing iterates `self._members` during `run()`.** If a future change adds such iteration, the safety guarantee is gone — see ADR 0004 §"Negative" bullet 5.
- **`P-07` is the demo target for `RealFailure`** per PLAN.md §4. A 15-pump fleet (PLAN.md §2.2) has `P-00..P-14`, so `P-07` is always present. If `pump_count` ever drops below 8 in a demo config, the scenario would raise `ScenarioError` at the first scheduled tick.
