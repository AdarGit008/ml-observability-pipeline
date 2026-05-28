# Review Packet 2026-05-28 — simulator — scenarios

> Paste this entire file into Gemini via:
> `.\scripts\gemini_review.ps1 -Slug 2026-05-28-simulator-scenarios`

## Role for Gemini

You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalised past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)

1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.
6. Tick-driven scenarios — same wire shape as before the scenarios landed (publisher layer is frozen this session).

Full constraint set: `context/_global.md`. Full plan: `PLAN.md` (§2.2 pump model, §2.7 drift, §4 demo script).

## Summary of the change

Implemented the scenario runner the previous two sessions deferred. `Fleet.from_config` had raised `NotImplementedError` for `seasonal_drift` / `fleet_expansion` / `real_failure`; this session drops that guard (mirroring the 2026-05-27 drop of the parallel `target: aws-iot` guard when `AwsIotPublisher` landed) and ships `simulator/scenario.py` with `Scenario` ABC + four concretes (`HealthyScenario`, `SeasonalDrift`, `FleetExpansion`, `RealFailure`) + `make_scenario(config)` factory + `ScenarioError`. `Fleet.run` spawns one extra asyncio task — the scenario controller — alongside the per-pump tasks. The controller calls `scenario.apply(fleet, tick)` once per `tick_seconds`. `ScenarioError` halts the fleet symmetrically with `PublisherConfigError` (new exit code 5). 35 new tests across 5 sections (interface, SeasonalDrift, FleetExpansion, RealFailure, end-to-end). All 179 prior tests still pass (one was inverted, parallel to the 2026-05-27 aws-iot pattern). ADR 0004 captures the four interface trade-offs.

## Diff

Inline diff is long; the files changed are:

- **New:** `simulator/scenario.py` (442 lines), `simulator/tests/test_scenario.py` (673 lines), `docs/adr/0004-tick-driven-scenario-controller.md`.
- **Modified:** `simulator/runner.py` (scenario plumbing, `add_pump`, `get_pump`, `_run_scenario`, halt-on-`ScenarioError`), `simulator/pump.py` (`ambient` property + `set_ambient` setter), `simulator/__main__.py` (exit code 5 for `ScenarioError`, retired exit code 3), `simulator/__init__.py` (re-exports), `simulator/tests/test_runner.py` (1 test inverted, 1 test updated for scenario task's extra `_wait_or_shutdown` calls), `simulator/tests/test_main.py` (1 test replaced), `context/simulator.md` (scenario `[ ]` ticked, open questions updated).

Code reading order suggestion: ADR 0004 → `simulator/scenario.py` → `simulator/runner.py` (Fleet diff) → `simulator/tests/test_scenario.py` (the integration tests at the bottom show the end-to-end behaviour).

## Specific questions for Gemini

1. **Interface shape.** Tick-driven, fleet-level, single `apply(fleet, tick)` method (ADR 0004 §Decision). Two of the three concrete scenarios (`SeasonalDrift`, `RealFailure`) don't need fleet-level access — they only mutate one pump or all pumps uniformly. Is the fleet-level shape over-general? Would a per-pump hook `before_pump_tick(pump, tick)` plus a fleet-level `on_fleet_tick(fleet, tick)` (or similar) be cleaner?

2. **`Fleet.add_pump` mid-run safety.** `add_pump` mutates `self._members` and calls `asyncio.create_task` on the running loop (ADR 0004 §Negative bullet 5; `simulator/runner.py::add_pump`). The claim is "safe today because `run()` only iterates `self._members` once at task-spawn time; `self._tasks` is the list that the gather walks". Is that correct, or is there a present-tense race I missed? Specifically: between `add_pump`'s `self._members.append(...)` and the next pump-task's `pump.step()`, is there any thread/loop boundary the data crosses?

3. **`ScenarioError` partial-mutation.** Today's three scenarios do at most one mutation per `apply` call, so a `ScenarioError` raised mid-`apply` can't leave the fleet in a partial state. If a future scenario does multi-pump batch updates (e.g., "shift ambient for pumps P-00..P-04, then raise"), pump P-00 is left mutated. ADR 0004 §"Follow-ups" flags this. Should the runner snapshot-and-restore, should scenarios be expected to be transactional themselves, or is "document the contract and move on" right?

4. **Private-attribute access in `FleetExpansion._bias_pump`.** `simulator/scenario.py::FleetExpansion._bias_pump` reaches into `pump._profiles` to override the HEALTHY ceiling. Cleaner alternatives: (a) a public `Pump.set_profile(state, profile)` setter, (b) constructing the new pump with a custom `profiles=` dict in `Fleet.add_pump` (would require threading the override through the factory). Is the privileged-caller pattern fine, or worth either alternative?

5. **`SeasonalDrift` magnitudes.** Defaults: period 180 ticks (6 min @ 2 s/tick), amplitude 8 °C. The bearing-temp equation is `ambient + 0.02*RPM + d*15 + N(0, 0.5)`. An 8 °C ambient swing should produce an 8 °C bearing-temp swing — well above the 0.5 °C noise floor. Is this plausibly enough to push PSI past the 0.25 "significant shift" threshold (PLAN.md §2.7)? I deferred parametric YAML config until the drift session can compute actual PSI, but the magnitude question is worth raising now.

6. **Scenario seed plumbing.** `make_scenario(config)` doesn't pass `config.fleet.base_seed` into the scenario. None of the three concretes have stochasticity today (sine is deterministic; expansion is deterministic; force_state is deterministic). Worth wiring it through for the interface's sake, or is "scenarios are deterministic unless they need a seed" a fine contract?

7. **AWS-specificity.** Is anything in this change unnecessarily AWS-specific where local would suffice (north star #3 inverted — does it work the same locally without AWS-specific assumptions)?

## What I'm NOT looking for in this review

- Test-count nit-picks. The brief estimated ~15-25 new tests; I shipped 35. Tighter coverage; no redundancy with prior modules.
- Style / formatting nits — covered by the project's existing conventions.
- Wire-shape regressions — the publisher layer is unmodified and the existing 34 publisher tests + 26 runner tests cover it.

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. Interface shape — keep fleet-level apply(fleet, tick), don't add per-pump hooks | **Validated** (no change) | Gemini agreed: "It is exactly as general as it needs to be." `Scenario.apply(fleet, tick)` is the single interface. |
| 2. `add_pump` task-lifecycle bug — `gather(*tasks)` evaluates args once, mid-run-added tasks orphaned | **Addressed** | Real bug, caught here. `Fleet.run` rewritten to use `asyncio.wait(FIRST_COMPLETED)` in a re-folding loop. Regression test `test_add_pump_task_is_awaited_on_shutdown` pins the fix. TaskGroup (3.11+) was the alternative; rejected because the sandbox runs 3.10. ADR 0004 §Addendum 2026-05-28. |
| 3. `ScenarioError` partial-mutation — snapshot/restore? | **Rejected** (YAGNI, document the contract) | Gemini agreed with the original framing: process exits immediately on ScenarioError (exit code 5), so in-memory state at the moment of the raise doesn't matter. Documented as a contract note in `simulator/scenario.py`. |
| 4. Private `_profiles` access in `FleetExpansion._bias_pump` | **Addressed** | Added `Pump.get_profile(state)` and `Pump.set_profile(state, profile)` (Option A from the packet). `_bias_pump` uses both. 5 new Pump tests pin the contract. ADR 0004 §Addendum 2026-05-28. |
| 5. `SeasonalDrift` magnitudes — is 8 °C ambient swing enough? | **Validated** (no change) | Gemini called it "massive, perhaps too massive" — a 16σ shift. Will trigger PSI > 0.25 cleanly, may need epsilon smoothing on the PSI side (drift session's problem, not ours). Defaults stay. |
| 6. Scenario seed plumbing | **Addressed** | `Scenario.__init__` accepts `seed: Optional[int]`; all four concretes thread it through; `make_scenario(config)` passes `config.fleet.base_seed`. None use it today (no stochasticity), but the contract is in place. 3 new tests. ADR 0004 §Addendum 2026-05-28. |
| 7. AWS-specificity | **Validated** (no change) | Gemini confirmed: scenarios mutate the abstract domain layer (Pump/Fleet), never touch the Publisher API. Mode parity preserved. |

**Summary:** 3 of 7 points produced code changes (Q2, Q4, Q6); 4 validated the design (Q1, Q3, Q5, Q7). The Q2 fix is the most consequential — a real task-lifecycle bug Gemini caught that the test suite couldn't have surfaced as a failing test, only as a "Task was destroyed" warning at process exit. +9 new tests across the three fixes (1 regression + 5 Pump + 3 seed). Final test count: 223 passing, 1 skipped (was 214 + 1 pre-Gemini).
