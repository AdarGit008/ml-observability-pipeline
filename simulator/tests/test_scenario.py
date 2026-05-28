"""Unit tests for simulator.scenario.

Covers four areas:

1. Scenario ABC + make_scenario factory (interface contract).
2. SeasonalDrift: sine-wave ambient modulation across all pumps.
3. FleetExpansion: adds N pumps mid-run with a vibration baseline shift.
4. RealFailure: escalates one pump through HEALTHY -> FAILED on schedule.

Plus end-to-end integration: the scenario controller actually runs
alongside the per-pump publish loop in Fleet.run and produces the
documented per-scenario invariants (PLAN.md §4 demo script).

Tests use small fleets (1-5 pumps), monkeypatched tick_seconds, and the
existing FakePublisher / FlakyPublisher doubles from test_runner.py
(re-imported here so no real broker is touched). No new test doubles
needed.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from simulator.config import (
    BrokerConfig,
    BrokerTarget,
    FleetConfig,
    ScenarioKind,
    SimulatorConfig,
)
from simulator.pump import Pump, PumpState, StateProfile
from simulator.publisher import Publisher, PublisherError
from simulator.runner import Fleet
from simulator.scenario import (
    DEFAULT_EXPANSION_NEW_COUNT,
    DEFAULT_EXPANSION_TICK,
    DEFAULT_EXPANSION_VIBRATION_SHIFT,
    DEFAULT_REAL_FAILURE_SCHEDULE,
    DEFAULT_REAL_FAILURE_TARGET,
    DEFAULT_SEASONAL_AMPLITUDE_C,
    DEFAULT_SEASONAL_PERIOD_TICKS,
    FleetExpansion,
    HealthyScenario,
    RealFailure,
    Scenario,
    ScenarioError,
    SeasonalDrift,
    make_scenario,
)


def _run(coro):
    return asyncio.run(coro)


def _config(scenario: ScenarioKind = ScenarioKind.HEALTHY) -> SimulatorConfig:
    return SimulatorConfig(
        fleet=FleetConfig(
            pump_count=2,
            setpoint_rpm=1800.0,
            ambient_celsius=22.0,
            base_seed=100,
        ),
        scenario=scenario,
        broker=BrokerConfig(
            target=BrokerTarget.LOCAL,
            url="mqtt://localhost:1883",
            tls=None,
        ),
        demo_mode=False,
    )


class _RecordingPublisher(Publisher):
    """Records publishes; never fails. Mirrors FakePublisher in
    test_runner.py — duplicated here only to keep the scenario test
    file self-contained for review."""

    def __init__(self, pump_id: str = "P-??") -> None:
        self.pump_id = pump_id
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_RecordingPublisher":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, dict(payload)))


# ============================================================
# 1. Scenario ABC + make_scenario factory
# ============================================================


def test_scenario_is_abstract():
    """Scenario cannot be instantiated directly (it's an ABC)."""
    with pytest.raises(TypeError):
        Scenario()  # type: ignore[abstract]


def test_scenario_apply_is_async():
    """Subclasses must expose ``apply`` as a coroutine. HealthyScenario
    is the simplest example — its apply must be awaitable."""
    s = HealthyScenario()
    coro = s.apply(fleet=None, tick=0)  # type: ignore[arg-type]
    assert asyncio.iscoroutine(coro)
    coro.close()  # don't leak


def test_healthy_scenario_is_noop():
    """HealthyScenario.apply must NOT mutate any pump or fleet state."""
    pump = Pump("P-00", ambient=22.0, seed=0)
    before_ambient = pump.ambient
    before_state = pump.state

    class _MiniFleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    _run(HealthyScenario().apply(_MiniFleet(), tick=0))  # type: ignore[arg-type]
    _run(HealthyScenario().apply(_MiniFleet(), tick=100))  # type: ignore[arg-type]
    assert pump.ambient == before_ambient
    assert pump.state is before_state


@pytest.mark.parametrize(
    "kind, cls",
    [
        (ScenarioKind.HEALTHY, HealthyScenario),
        (ScenarioKind.SEASONAL_DRIFT, SeasonalDrift),
        (ScenarioKind.FLEET_EXPANSION, FleetExpansion),
        (ScenarioKind.REAL_FAILURE, RealFailure),
    ],
)
def test_make_scenario_dispatches_by_kind(kind: ScenarioKind, cls):
    """All four ScenarioKind values must produce the corresponding
    concrete Scenario class — none raise NotImplementedError. This is
    the contract the 2026-05-28 session signed up for (ADR 0004)."""
    scenario = make_scenario(_config(kind))
    assert isinstance(scenario, cls)


# ============================================================
# 2. SeasonalDrift
# ============================================================


def test_seasonal_drift_validates_period():
    with pytest.raises(ValueError, match="period_ticks must be positive"):
        SeasonalDrift(period_ticks=0)
    with pytest.raises(ValueError, match="period_ticks must be positive"):
        SeasonalDrift(period_ticks=-5)


def test_seasonal_drift_validates_amplitude():
    with pytest.raises(ValueError, match="amplitude_celsius must be non-negative"):
        SeasonalDrift(amplitude_celsius=-1.0)


def test_seasonal_drift_initial_tick_is_at_base():
    """At tick 0 the sine is 0, so ambient should equal the captured
    base (no shift on the first apply). Verifies the 'capture base on
    first sight' contract — drift is around whatever the YAML said,
    not a hardcoded 22 °C."""
    pump = Pump("P-00", ambient=25.0, seed=0)

    class _Fleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    # sin(0) == 0 -> delta == 0 -> ambient unchanged from base
    assert pump.ambient == pytest.approx(25.0)


def test_seasonal_drift_quarter_period_hits_peak():
    """At tick = period/4 the sine is 1, so ambient = base + amplitude."""
    pump = Pump("P-00", ambient=20.0, seed=0)

    class _Fleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    # Apply tick 0 first to capture the base.
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    _run(s.apply(_Fleet(), tick=45))  # 180/4 # type: ignore[arg-type]
    assert pump.ambient == pytest.approx(28.0, abs=1e-9)


def test_seasonal_drift_half_period_returns_to_base():
    """At tick = period/2 the sine is 0 again — back to base."""
    pump = Pump("P-00", ambient=20.0, seed=0)

    class _Fleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    _run(s.apply(_Fleet(), tick=45))  # peak # type: ignore[arg-type]
    _run(s.apply(_Fleet(), tick=90))  # back to base # type: ignore[arg-type]
    assert pump.ambient == pytest.approx(20.0, abs=1e-9)


def test_seasonal_drift_modulates_all_pumps_in_fleet():
    """Every pump in the fleet should see the same delta on the same
    tick — fleet-wide signal is what PLAN.md §4 Scenario 1 wants the
    fleet-level PSI detector to catch."""
    p0 = Pump("P-00", ambient=20.0, seed=0)
    p1 = Pump("P-01", ambient=22.0, seed=1)
    p2 = Pump("P-02", ambient=24.0, seed=2)

    class _Fleet:
        members = [
            (p0, _RecordingPublisher("P-00")),
            (p1, _RecordingPublisher("P-01")),
            (p2, _RecordingPublisher("P-02")),
        ]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    # Apply quarter-period -> +amplitude
    _run(s.apply(_Fleet(), tick=45))  # type: ignore[arg-type]
    # Each pump should be at its own base + amplitude, not all clamped
    # to one shared value.
    assert p0.ambient == pytest.approx(28.0, abs=1e-9)
    assert p1.ambient == pytest.approx(30.0, abs=1e-9)
    assert p2.ambient == pytest.approx(32.0, abs=1e-9)


def test_seasonal_drift_cycles_cleanly_across_period_boundary():
    """tick = period and tick = 0 should produce the same ambient
    (sine wraps at 2π) — no off-by-one drift accumulating across
    cycles. Drift detectors that flag this as a 'new' baseline would
    fire false positives every period if this broke."""
    pump = Pump("P-00", ambient=20.0, seed=0)

    class _Fleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    after_t0 = pump.ambient
    _run(s.apply(_Fleet(), tick=180))  # type: ignore[arg-type]
    after_full_period = pump.ambient
    assert after_t0 == pytest.approx(after_full_period, abs=1e-9)


def test_seasonal_drift_bearing_temp_follows_ambient():
    """Sanity check the equation: bearing_temp shift should match
    ambient shift (the noise floor is N(0, 0.5)). Pump.step at peak
    ambient should produce bearing_temp roughly amplitude °C higher
    than at base ambient — the signal a fleet PSI detector picks up.
    """
    pump = Pump("P-00", ambient=20.0, seed=42)

    class _Fleet:
        members = [(pump, _RecordingPublisher("P-00"))]

    s = SeasonalDrift(period_ticks=180, amplitude_celsius=8.0)
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    base_readings = [pump.step()["bearing_temp"] for _ in range(50)]
    base_avg = sum(base_readings) / len(base_readings)

    _run(s.apply(_Fleet(), tick=45))  # peak # type: ignore[arg-type]
    peak_readings = [pump.step()["bearing_temp"] for _ in range(50)]
    peak_avg = sum(peak_readings) / len(peak_readings)

    # The amplitude is 8 °C; the noise floor is 0.5 °C. Mean shift
    # across 50 samples should be well within 1 °C of amplitude.
    assert peak_avg - base_avg == pytest.approx(8.0, abs=1.0)


# ============================================================
# 3. FleetExpansion
# ============================================================


def test_fleet_expansion_validates_expand_at_tick():
    with pytest.raises(ValueError, match="expand_at_tick must be non-negative"):
        FleetExpansion(expand_at_tick=-1)


def test_fleet_expansion_validates_new_pump_count():
    with pytest.raises(ValueError, match="new_pump_count must be positive"):
        FleetExpansion(new_pump_count=0)
    with pytest.raises(ValueError, match="new_pump_count must be positive"):
        FleetExpansion(new_pump_count=-3)


def test_fleet_expansion_before_expand_tick_is_noop():
    """Ticks earlier than ``expand_at_tick`` must NOT grow the fleet."""
    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config)
    initial_count = len(fleet.members)

    s = FleetExpansion(expand_at_tick=60, new_pump_count=3)
    for tick in (0, 30, 59):
        _run(s.apply(fleet, tick))

    assert len(fleet.members) == initial_count
    assert s.fired is False


def test_fleet_expansion_at_tick_grows_fleet():
    """At ``expand_at_tick`` the fleet should grow by ``new_pump_count``."""
    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config)
    initial_count = len(fleet.members)

    s = FleetExpansion(expand_at_tick=5, new_pump_count=3)
    _run(s.apply(fleet, tick=5))

    assert len(fleet.members) == initial_count + 3
    assert s.fired is True


def test_fleet_expansion_is_idempotent():
    """Calling apply multiple times after expand_at_tick must not add
    pumps repeatedly — one expansion per scenario instance."""
    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config)
    initial_count = len(fleet.members)

    s = FleetExpansion(expand_at_tick=5, new_pump_count=3)
    _run(s.apply(fleet, tick=5))
    _run(s.apply(fleet, tick=6))
    _run(s.apply(fleet, tick=100))

    assert len(fleet.members) == initial_count + 3


def test_fleet_expansion_new_pump_ids_do_not_collide():
    """Added pumps must get unique ids that don't collide with existing
    members. With a 2-pump initial fleet (P-00, P-01), the expansion
    should pick P-02..P-04."""
    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config)
    existing_ids = {p.pump_id for p, _ in fleet.members}

    s = FleetExpansion(expand_at_tick=0, new_pump_count=3)
    _run(s.apply(fleet, tick=0))

    new_ids = {p.pump_id for p, _ in fleet.members} - existing_ids
    assert len(new_ids) == 3
    assert all(nid.startswith("P-") for nid in new_ids)


def test_fleet_expansion_shifts_new_pump_vibration_baseline():
    """The new pumps should have a HEALTHY ceiling higher than the
    fleet default (PumpState.HEALTHY ceiling=0.05). The exact bump
    depends on the shift; we just check the ceiling moved."""
    from simulator.pump import DEFAULT_PROFILES

    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config)
    existing_ids = {p.pump_id for p, _ in fleet.members}

    s = FleetExpansion(
        expand_at_tick=0, new_pump_count=2, vibration_baseline_shift=0.5
    )
    _run(s.apply(fleet, tick=0))

    new_members = [
        (p, pub) for p, pub in fleet.members if p.pump_id not in existing_ids
    ]
    assert len(new_members) == 2
    default_healthy_ceiling = DEFAULT_PROFILES[PumpState.HEALTHY].ceiling
    for pump, _ in new_members:
        assert pump.get_profile(PumpState.HEALTHY).ceiling > default_healthy_ceiling


def test_fleet_expansion_without_factories_raises_scenario_error():
    """A directly-constructed Fleet (no factories) cannot grow. The
    scenario controller must surface a clear error rather than a
    bare AttributeError."""
    members = [(Pump("P-00", seed=0), _RecordingPublisher("P-00"))]
    fleet = Fleet(members, tick_seconds=0.01)
    s = FleetExpansion(expand_at_tick=0, new_pump_count=1)
    with pytest.raises(ScenarioError, match="cannot add_pump"):
        _run(s.apply(fleet, tick=0))


# ============================================================
# 4. RealFailure
# ============================================================


def test_real_failure_validates_target_pump_id():
    with pytest.raises(ValueError, match="target_pump_id must be a non-empty string"):
        RealFailure(target_pump_id="")


def test_real_failure_validates_negative_tick_in_schedule():
    with pytest.raises(ValueError, match="must be non-negative"):
        RealFailure(
            target_pump_id="P-07",
            schedule={PumpState.DEGRADING: -1},
        )


def test_real_failure_validates_duplicate_ticks():
    """Two transitions at the same tick would race within one apply()
    call — schedule must be unique on the tick side."""
    with pytest.raises(ValueError, match="duplicate ticks"):
        RealFailure(
            target_pump_id="P-07",
            schedule={
                PumpState.DEGRADING: 10,
                PumpState.FAILING: 10,
            },
        )


def test_real_failure_escalates_target_through_schedule():
    """Walk the target pump through HEALTHY -> DEGRADING -> FAILING ->
    FAILED on the documented schedule. Other pumps are untouched."""
    p7 = Pump("P-07", seed=0)
    p8 = Pump("P-08", seed=0)
    p9 = Pump("P-09", seed=0)

    class _Fleet:
        members = [
            (p7, _RecordingPublisher("P-07")),
            (p8, _RecordingPublisher("P-08")),
            (p9, _RecordingPublisher("P-09")),
        ]

        def get_pump(self, pump_id: str) -> Pump:
            for p, _ in self.members:
                if p.pump_id == pump_id:
                    return p
            raise KeyError(pump_id)

    s = RealFailure(
        target_pump_id="P-07",
        schedule={
            PumpState.DEGRADING: 5,
            PumpState.FAILING: 10,
            PumpState.FAILED: 15,
        },
    )

    # Tick 0 — nothing scheduled, no transitions.
    _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]
    assert p7.state is PumpState.HEALTHY

    # Tick 5 — DEGRADING.
    _run(s.apply(_Fleet(), tick=5))  # type: ignore[arg-type]
    assert p7.state is PumpState.DEGRADING

    # Tick 10 — FAILING.
    _run(s.apply(_Fleet(), tick=10))  # type: ignore[arg-type]
    assert p7.state is PumpState.FAILING

    # Tick 15 — FAILED. Pump.force_state pins degradation to 1.0.
    _run(s.apply(_Fleet(), tick=15))  # type: ignore[arg-type]
    assert p7.state is PumpState.FAILED
    assert p7.degradation == 1.0

    # Other pumps are still HEALTHY throughout — the scenario is a
    # single-pump failure, not a fleet-wide one.
    assert p8.state is PumpState.HEALTHY
    assert p9.state is PumpState.HEALTHY


def test_real_failure_missing_target_raises_scenario_error():
    """If the target pump isn't in the fleet (e.g., a typo in the
    config), the scenario surfaces ScenarioError — runner halts the
    fleet rather than retry-forever (per ADR 0004)."""

    class _Fleet:
        members = [(Pump("P-00", seed=0), _RecordingPublisher("P-00"))]

        def get_pump(self, pump_id: str) -> Pump:
            raise KeyError(pump_id)

    s = RealFailure(
        target_pump_id="P-99",
        schedule={PumpState.DEGRADING: 0},
    )
    with pytest.raises(ScenarioError, match="not in the fleet"):
        _run(s.apply(_Fleet(), tick=0))  # type: ignore[arg-type]


def test_real_failure_default_target_matches_plan_md_demo():
    """PLAN.md §4 Scenario 3 names ``P-07`` explicitly. The default
    target must match — recruiters watching the demo see the same
    pump id the script mentions."""
    assert DEFAULT_REAL_FAILURE_TARGET == "P-07"


def test_real_failure_default_schedule_walks_full_lifecycle():
    """The default schedule must visit each non-HEALTHY state exactly
    once and in forward order, so the lifecycle is observable in the
    demo without bespoke config."""
    s = DEFAULT_REAL_FAILURE_SCHEDULE
    assert set(s.keys()) == {PumpState.DEGRADING, PumpState.FAILING, PumpState.FAILED}
    # Forward ticks: DEGRADING before FAILING before FAILED.
    assert s[PumpState.DEGRADING] < s[PumpState.FAILING] < s[PumpState.FAILED]


# ============================================================
# 5. End-to-end Fleet.run + scenario integration
# ============================================================


def test_fleet_run_runs_scenario_alongside_pumps():
    """Smoke: a Fleet with a custom scenario (here, a counter) ticks the
    scenario task and the per-pump task in parallel. The scenario
    controller is one extra asyncio task; Fleet.run gather()s it with
    the per-pump tasks."""

    class _CountingScenario(Scenario):
        def __init__(self) -> None:
            self.apply_ticks: list[int] = []

        async def apply(self, fleet, tick):
            self.apply_ticks.append(tick)

    pumps = [Pump("P-00", seed=0)]
    pubs = [_RecordingPublisher("P-00")]
    counter = _CountingScenario()
    fleet = Fleet(
        list(zip(pumps, pubs)),
        tick_seconds=0.001,
        scenario=counter,
    )

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(500):
            await asyncio.sleep(0.001)
            if len(counter.apply_ticks) >= 5 and len(pubs[0].published) >= 5:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())

    # Scenario ticks are monotonic from 0.
    assert counter.apply_ticks[:5] == [0, 1, 2, 3, 4]
    assert len(pubs[0].published) >= 5


def test_fleet_run_with_default_scenario_uses_healthy_no_op():
    """Fleet without an explicit scenario gets HealthyScenario by
    default — same wire shape as before scenarios landed, no surprise
    behavior change for callers that don't care."""
    pumps = [Pump("P-00", seed=0)]
    pubs = [_RecordingPublisher("P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    assert isinstance(fleet.scenario, HealthyScenario)
    before_state = pumps[0].state
    before_ambient = pumps[0].ambient

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(200):
            await asyncio.sleep(0.001)
            if len(pubs[0].published) >= 3:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())

    # Healthy scenario didn't perturb pump state.
    assert pumps[0].state is before_state
    assert pumps[0].ambient == before_ambient


def test_fleet_run_scenario_error_halts_fleet():
    """A scenario that raises ScenarioError must halt the fleet (like
    PublisherConfigError). Fleet.run re-raises so main() can exit with
    the dedicated code (ADR 0004)."""

    class _BrokenScenario(Scenario):
        async def apply(self, fleet, tick):
            if tick >= 1:
                raise ScenarioError("simulated scenario logic failure")

    pumps = [Pump("P-00", seed=0)]
    pubs = [_RecordingPublisher("P-00")]
    fleet = Fleet(
        list(zip(pumps, pubs)),
        tick_seconds=0.001,
        scenario=_BrokenScenario(),
    )

    async def _go():
        with pytest.raises(ScenarioError, match="simulated scenario logic failure"):
            await asyncio.wait_for(fleet.run(), timeout=1.0)

    _run(_go())


def test_fleet_run_wraps_unexpected_scenario_exception_in_scenario_error():
    """A scenario that raises some non-ScenarioError exception must NOT
    leak that type to the caller. _run_scenario wraps in ScenarioError
    with the original exception chained via __cause__."""

    class _ZeroDivScenario(Scenario):
        async def apply(self, fleet, tick):
            if tick >= 1:
                _ = 1 / 0

    pumps = [Pump("P-00", seed=0)]
    pubs = [_RecordingPublisher("P-00")]
    fleet = Fleet(
        list(zip(pumps, pubs)),
        tick_seconds=0.001,
        scenario=_ZeroDivScenario(),
    )

    async def _go():
        with pytest.raises(ScenarioError) as exc_info:
            await asyncio.wait_for(fleet.run(), timeout=1.0)
        assert isinstance(exc_info.value.__cause__, ZeroDivisionError)

    _run(_go())


def test_fleet_from_config_with_fleet_expansion_grows_at_runtime(monkeypatch):
    """End-to-end: Fleet.from_config with FLEET_EXPANSION builds a
    publisher_factory + pump_factory closure; the scenario uses these
    to add pumps mid-run. We can't run the real factory (it would try
    to connect to a broker), so we monkeypatch make_publisher to
    return RecordingPublisher instances. Verifies the wiring: the
    expansion adds pumps, their tasks spawn, and they begin
    publishing."""
    import simulator.runner as runner_mod
    import simulator.scenario as scenario_mod

    def _fake_make_publisher(*, target, url, client_id, tls):
        return _RecordingPublisher(client_id)

    monkeypatch.setattr(runner_mod, "make_publisher", _fake_make_publisher)

    # Tighten the expansion schedule for the test loop.
    monkeypatch.setattr(scenario_mod, "DEFAULT_EXPANSION_TICK", 2)
    monkeypatch.setattr(scenario_mod, "DEFAULT_EXPANSION_NEW_COUNT", 2)

    config = _config(ScenarioKind.FLEET_EXPANSION)
    fleet = Fleet.from_config(config, tick_seconds=0.005)
    initial_count = len(fleet.members)

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(500):
            await asyncio.sleep(0.005)
            if len(fleet.members) > initial_count:
                # Give the new pump tasks a moment to publish.
                await asyncio.sleep(0.05)
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=2.0)

    _run(_go())

    assert len(fleet.members) > initial_count
    # The new pumps should have published at least once.
    new_pubs = [
        pub for pump, pub in fleet.members
        if pump.pump_id not in {"P-00", "P-01"}
    ]
    assert all(len(p.published) >= 1 for p in new_pubs)


# ============================================================
# 6. Regression: Fleet.run awaits add_pump tasks (Gemini Q2)
# ============================================================


def test_add_pump_task_is_awaited_on_shutdown(monkeypatch):
    """Per Gemini Q2 (2026-05-28 review): the original asyncio.gather
    in Fleet.run evaluated args ONCE, so an add_pump-created task was
    orphaned. The fix (asyncio.wait FIRST_COMPLETED loop that re-folds
    self._tasks) means the new task IS awaited. This test pins that
    behaviour: after shutdown, the mid-run-added task is .done() (not
    still pending — which would surface as "Task was destroyed but it
    is pending!" warnings).
    """
    import simulator.runner as runner_mod

    def _fake_make_publisher(*, target, url, client_id, tls):
        return _RecordingPublisher(client_id)

    monkeypatch.setattr(runner_mod, "make_publisher", _fake_make_publisher)

    class _OneShotExpand(Scenario):
        def __init__(self) -> None:
            self.fired = False

        async def apply(self, fleet, tick):
            if not self.fired and tick >= 1:
                fleet.add_pump("P-50")
                self.fired = True

    config = _config(ScenarioKind.HEALTHY)
    fleet = Fleet.from_config(config, tick_seconds=0.005)
    # Replace the scenario with our one-shot.
    fleet._scenario = _OneShotExpand()  # type: ignore[attr-defined]

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        # Wait until the new pump is added and has published.
        for _ in range(500):
            await asyncio.sleep(0.005)
            new = [
                pub for pump, pub in fleet.members
                if pump.pump_id == "P-50"
            ]
            if new and len(new[0].published) >= 1:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=2.0)
        return run_task

    run_task = _run(_go())

    # The new pump exists.
    new_pump_ids = [p.pump_id for p, _ in fleet.members]
    assert "P-50" in new_pump_ids

    # The new pump's task is done (was properly awaited). If the
    # original gather-only code path were still in place, this task
    # would NOT be in self._tasks until after spawn, but the gather
    # had already evaluated its args — leaving the task running
    # until the event loop teardown forced it. Now it should be
    # cleanly .done().
    new_pump_tasks = [
        t for t in fleet._tasks if t.get_name() == "pump-P-50"
    ]
    assert len(new_pump_tasks) == 1
    assert new_pump_tasks[0].done()
    # And no exception left on it.
    assert new_pump_tasks[0].exception() is None


# ============================================================
# 7. Seed plumbing (Gemini Q6)
# ============================================================


def test_scenario_seed_defaults_to_none():
    """A Scenario built without a seed has ``seed is None``. No
    stochasticity today, but the interface exposes the field for
    future scenarios — per Gemini Q6 review."""
    assert HealthyScenario().seed is None
    assert SeasonalDrift().seed is None
    assert FleetExpansion().seed is None
    assert RealFailure().seed is None


def test_scenario_seed_threaded_through_constructor():
    """All four concretes accept ``seed=`` and expose it via the
    ``seed`` property — the contract for future stochastic scenarios."""
    assert HealthyScenario(seed=42).seed == 42
    assert SeasonalDrift(seed=99).seed == 99
    assert FleetExpansion(seed=7).seed == 7
    assert RealFailure(seed=123).seed == 123


def test_make_scenario_passes_config_base_seed_through():
    """``make_scenario(config)`` must thread ``config.fleet.base_seed``
    into the constructed Scenario. Future stochastic demos seeded with
    the same value as the pumps stay reproducible across reruns."""
    config = SimulatorConfig(
        fleet=FleetConfig(
            pump_count=2,
            setpoint_rpm=1800.0,
            ambient_celsius=22.0,
            base_seed=4242,
        ),
        scenario=ScenarioKind.SEASONAL_DRIFT,
        broker=BrokerConfig(
            target=BrokerTarget.LOCAL, url="mqtt://localhost:1883", tls=None
        ),
        demo_mode=False,
    )
    s = make_scenario(config)
    assert isinstance(s, SeasonalDrift)
    assert s.seed == 4242
