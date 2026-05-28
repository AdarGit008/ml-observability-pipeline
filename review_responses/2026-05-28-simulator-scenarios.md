Here is a review of the Scenario Runner implementation, addressing your specific questions and surfacing architectural risks.

### 1. Interface shape: Is the fleet-level shape over-general?
**Verdict:** Stick with the fleet-level `apply(fleet, tick)` shape. **Do not** add per-pump hooks. 
*Why:* For single-PC simulation of a small/medium fleet, the overhead of iterating `fleet.get_pumps()` inside the scenario is negligible. Adding `before_pump_tick` forces the runner to orchestrate those calls, adding loop complexity and tight coupling between the runner's tick execution and the scenario. A single `apply(fleet, tick)` keeps the runner dumb and the scenario fully empowered to do whatever it needs (mutating one pump, all pumps, or inter-pump relationships). It is exactly as general as it needs to be.

### 2. `Fleet.add_pump` mid-run safety: Are there race conditions?
**Verdict:** There is no thread/loop boundary race (asyncio is cooperatively single-threaded), **but there is a severe task-lifecycle bug here.**
*The Risk:* If `Fleet.run()` uses `asyncio.gather(*self._tasks)` to await the pumps, that `gather` evaluates its arguments *exactly once* at the moment it is called. 
When `add_pump` calls `asyncio.create_task()` and appends it to `self._tasks` mid-run, the new task is **not** added to the running `gather`. 
*Impact:* 
1. If the simulation shuts down gracefully, `gather` completes when the *original* pumps complete. The dynamically added pump task is left orphaned, leading to `Task was destroyed but it is pending!` warnings polluting the console.
2. If the dynamically added pump raises an exception, the main `gather` won't catch it. 
*Fix:* If you are on Python 3.11+, refactor `Fleet.run()` to use an `asyncio.TaskGroup`. `TaskGroup.create_task()` allows safe, dynamic task addition mid-run and guarantees clean teardown. If you are on <3.11, you must use an `asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)` loop that dynamically refreshes the task set.

### 3. `ScenarioError` partial-mutation: Do we need snapshot/restore?
**Verdict:** Document the contract and move on. YAGNI.
*Why:* By your own definition, a `ScenarioError` results in a fatal exit (Code 5) mirroring `PublisherConfigError`. If the process is intentionally committing suicide immediately after the error is raised, the in-memory state of the fleet doesn't matter because the OS is about to reclaim the memory anyway. Adding snapshot-and-restore or transactional rollback for a fatal error path is over-engineering.

### 4. Private-attribute access in `FleetExpansion._bias_pump` (`pump._profiles`)
**Verdict:** Refactor this. It looks unpolished for a portfolio project.
*Why:* Code reviewers for senior roles will flag external mutation of private (`_`) attributes as a design smell. It signals that the domain model (`Pump`) is incomplete. 
*Fix:* Go with **Option B** (constructing the new pump with custom profiles passed to the factory). It preserves the pump's immutability-after-creation and is much more robust. If threading the override through the factory is too heavy, **Option A** (`pump.set_profile(state, profile)`) is entirely acceptable for a simulator designed specifically for fault injection. 

### 5. `SeasonalDrift` magnitudes: Is an 8 °C swing enough for PSI?
**Verdict:** It is actually massive, perhaps *too* massive, but perfect for a demo.
*Why:* You noted the noise floor is `N(0, 0.5)`. An 8 °C shift is a 16-standard-deviation ($\sigma$) shift in the mean of the bearing temperature. In statistical terms, the "healthy" and "drifted" distributions will have almost zero overlap. When you calculate Population Stability Index (PSI) later, the bins that used to have 100% of the data will have 0%, and vice versa. This will blow past the 0.25 PSI threshold (likely resulting in a very high or mathematically undefined PSI without epsilon smoothing). It will absolutely trigger the alert in §2.7. 

### 6. Scenario seed plumbing: Worth wiring it through?
**Verdict:** Wire it through now. 
*Why:* Reproducibility is the holy grail of simulation testing. Right now, your concretes are deterministic, but the minute someone (or you, in a later session) wants to add jitter to a scenario (`FleetExpansion` varying start times, `SeasonalDrift` having a stochastic amplitude), they will reach for `random`. If the scenario doesn't have the global seed, it will cause flaky demo outputs. Pass `config.fleet.base_seed` to `make_scenario(config)` to future-proof the contract.

### 7. AWS-specificity: Does this violate North Star #3?
**Verdict:** No violation. This implementation is cleanly agnostic.
*Why:* Scenarios purely mutate the abstract domain layer (`Pump` state, `Fleet` membership). They operate entirely "behind" the `Publisher` layer. As long as no AWS/IoT-specific logic (like MQTT topic generation or AWS SDK calls) leaks into `simulator/scenario.py`, you maintain perfect local/AWS parity. 

---
**Reviewer Summary:**
The overarching design (tick-driven, fleet-level apply, strict exit codes) is highly aligned with your constraints. Address the `asyncio` task-gathering lifecycle bug for dynamically added pumps, replace the `_profiles` private access with a public mechanism, and wire the seed through. Once those three are fixed, this is ready to merge.
