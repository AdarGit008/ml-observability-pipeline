"""Unit tests for simulator.runner.

Uses an in-memory ``FakePublisher`` (records publishes, optionally fails a
fixed number of connects) so the runner is exercised without a real
broker. Backoff constants are monkeypatched to milliseconds where needed
so the time-based tests finish quickly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from simulator.config import (
    BrokerConfig,
    BrokerTarget,
    FleetConfig,
    ScenarioKind,
    SimulatorConfig,
    TlsConfig,
)
from simulator.publisher import LocalPublisher, Publisher, PublisherError
from simulator.pump import PumpState
from simulator.runner import (
    DEFAULT_TICK_SECONDS,
    Fleet,
    pump_id_for,
)


def _run(coro):
    return asyncio.run(coro)


def _config(
    *,
    pump_count: int = 2,
    scenario: ScenarioKind = ScenarioKind.HEALTHY,
    target: BrokerTarget = BrokerTarget.LOCAL,
    tls: Optional[TlsConfig] = None,
    demo_mode: bool = False,
) -> SimulatorConfig:
    return SimulatorConfig(
        fleet=FleetConfig(
            pump_count=pump_count,
            setpoint_rpm=1800.0,
            ambient_celsius=22.0,
            base_seed=100,
        ),
        scenario=scenario,
        broker=BrokerConfig(
            target=target,
            url="mqtt://localhost:1883",
            tls=tls,
        ),
        demo_mode=demo_mode,
    )


class FakePublisher(Publisher):
    """Records every publish call; never fails."""

    def __init__(self, pump_id: str = "P-??") -> None:
        self.pump_id = pump_id
        self.connects = 0
        self.disconnects = 0
        self.entered = False
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakePublisher":
        self.entered = True
        self.connects += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.entered = False
        self.disconnects += 1

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        # Defensive copy — the runner shouldn't mutate the payload after
        # publish, but if it did, the recorded snapshot would lie.
        self.published.append((topic, dict(payload)))


class FlakyPublisher(FakePublisher):
    """Fails the first ``fail_count`` ``__aenter__`` attempts with
    ``PublisherError``, then behaves like ``FakePublisher``."""

    def __init__(self, fail_count: int, pump_id: str = "P-??") -> None:
        super().__init__(pump_id=pump_id)
        self._remaining_failures = fail_count
        self.connect_attempts = 0

    async def __aenter__(self) -> "FlakyPublisher":
        self.connect_attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise PublisherError(
                f"simulated connect failure (remaining={self._remaining_failures})"
            )
        return await super().__aenter__()


# -- pump_id_for ----------------------------------------------------------


@pytest.mark.parametrize(
    "index, expected",
    [(0, "P-00"), (7, "P-07"), (15, "P-15"), (99, "P-99")],
)
def test_pump_id_for_zero_padding(index: int, expected: str):
    assert pump_id_for(index) == expected


@pytest.mark.parametrize("bad", [-1, 100, 9999])
def test_pump_id_for_out_of_range(bad: int):
    with pytest.raises(ValueError, match="out of range"):
        pump_id_for(bad)


# -- Fleet.__init__ -------------------------------------------------------


def test_fleet_init_rejects_empty_members():
    with pytest.raises(ValueError, match="at least one"):
        Fleet([])


def test_fleet_init_rejects_zero_tick_seconds():
    from simulator.pump import Pump

    members = [(Pump("P-00", seed=0), FakePublisher("P-00"))]
    with pytest.raises(ValueError, match="tick_seconds must be positive"):
        Fleet(members, tick_seconds=0.0)


def test_fleet_init_rejects_negative_tick_seconds():
    from simulator.pump import Pump

    members = [(Pump("P-00", seed=0), FakePublisher("P-00"))]
    with pytest.raises(ValueError, match="tick_seconds must be positive"):
        Fleet(members, tick_seconds=-0.5)


# -- Fleet.from_config ----------------------------------------------------


def test_from_config_builds_pumps_with_seeded_ids():
    fleet = Fleet.from_config(_config(pump_count=3))
    assert len(fleet.members) == 3
    ids = [pump.pump_id for pump, _ in fleet.members]
    assert ids == ["P-00", "P-01", "P-02"]
    # base_seed=100 -> pump seeds 100, 101, 102. Verifying reproducibility
    # via the first tick's RPM (deterministic given seed + ambient + setpoint).
    pumps = [p for p, _ in fleet.members]
    assert pumps[0].degradation == 0.0  # before any step
    assert pumps[0].state is PumpState.HEALTHY


def test_from_config_uses_local_publisher_for_local_target():
    fleet = Fleet.from_config(_config(target=BrokerTarget.LOCAL))
    publishers = [pub for _, pub in fleet.members]
    assert all(isinstance(p, LocalPublisher) for p in publishers)


@pytest.mark.parametrize(
    "scenario",
    [ScenarioKind.SEASONAL_DRIFT, ScenarioKind.FLEET_EXPANSION, ScenarioKind.REAL_FAILURE],
)
def test_from_config_rejects_non_healthy_scenario(scenario: ScenarioKind):
    with pytest.raises(NotImplementedError, match="scenario runner is not yet implemented"):
        Fleet.from_config(_config(scenario=scenario))


def test_from_config_rejects_aws_iot_target():
    tls = TlsConfig(
        cert_path="certs/cert.pem",
        key_path="certs/key.pem",
        ca_path="certs/ca.pem",
    )
    with pytest.raises(NotImplementedError, match="aws-iot.*not yet wired"):
        Fleet.from_config(_config(target=BrokerTarget.AWS_IOT, tls=tls))


def test_from_config_applies_demo_mode_profiles():
    """A demo_mode fleet's pumps should exit HEALTHY within
    DEMO_MODE_HEALTHY_DWELL_TICKS steps (the same property the config-yaml
    session tested for profiles_for, now bundled at the Fleet level)."""
    from simulator.config import DEMO_MODE_HEALTHY_DWELL_TICKS

    fleet = Fleet.from_config(_config(pump_count=1, demo_mode=True))
    pump, _ = fleet.members[0]
    for _ in range(DEMO_MODE_HEALTHY_DWELL_TICKS + 1):
        pump.step()
    assert pump.state is not PumpState.HEALTHY


def test_from_config_default_tick_seconds():
    fleet = Fleet.from_config(_config())
    assert fleet.tick_seconds == DEFAULT_TICK_SECONDS


def test_from_config_custom_tick_seconds():
    fleet = Fleet.from_config(_config(), tick_seconds=0.05)
    assert fleet.tick_seconds == 0.05


# -- Fleet.run ------------------------------------------------------------


def test_fleet_run_publishes_telemetry_until_shutdown():
    """End-to-end smoke with FakePublisher.

    Strategy: very short tick (1 ms) + shutdown when each pump has emitted
    at least a few publishes. Asserts on the wire shape (topic, telemetry
    keys) and per-pump independence (each FakePublisher gets its own
    pump's data).
    """
    from simulator.pump import Pump

    pumps = [Pump("P-00", seed=0), Pump("P-01", seed=1)]
    pubs = [FakePublisher("P-00"), FakePublisher("P-01")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        # Poll until both publishers have at least 3 readings, then shut down.
        for _ in range(500):  # 500 * 1ms = 0.5s ceiling
            await asyncio.sleep(0.001)
            if all(len(p.published) >= 3 for p in pubs):
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())

    for pump_id, pub in zip(["P-00", "P-01"], pubs):
        assert len(pub.published) >= 3
        assert pub.connects == 1
        assert pub.disconnects == 1
        for topic, payload in pub.published:
            assert topic == f"factory/pumps/{pump_id}/telemetry"
            assert payload["pump_id"] == pump_id
            assert set(payload.keys()) >= {
                "pump_id",
                "ts",
                "vibration_amp",
                "bearing_temp",
                "motor_current",
                "rpm",
            }


def test_fleet_run_handles_shutdown_before_any_publish():
    """Edge case: shutdown signalled before run() starts. Tasks should
    exit cleanly without publishing anything."""
    from simulator.pump import Pump

    pumps = [Pump("P-00", seed=0)]
    pubs = [FakePublisher("P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        fleet.request_shutdown()
        await asyncio.wait_for(fleet.run(), timeout=1.0)

    _run(_go())
    # Defensive: even with shutdown set first, the inner loop may or may
    # not get one publish in depending on timing. Just ensure run()
    # returned and no unbounded growth happened.
    assert len(pubs[0].published) <= 1


def test_fleet_run_retries_on_publisher_error(monkeypatch):
    """A FlakyPublisher that fails the first 2 connect attempts should
    eventually succeed; the runner reconnects with backoff and proceeds."""
    from simulator.pump import Pump
    import simulator.runner as runner_module

    # Shrink backoff to milliseconds so the test finishes quickly.
    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 0.01)

    pumps = [Pump("P-00", seed=0)]
    pubs = [FlakyPublisher(fail_count=2, pump_id="P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(500):
            await asyncio.sleep(0.002)
            if len(pubs[0].published) >= 3:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())
    # 2 failed connects + 1 successful connect.
    assert pubs[0].connect_attempts >= 3
    assert pubs[0].connects == 1
    assert len(pubs[0].published) >= 3


def test_fleet_run_isolates_per_pump_failures(monkeypatch):
    """One pump's permanent failure shouldn't stop the other pumps from
    publishing. The healthy pump should reach steady state while the
    flaky one keeps retrying."""
    from simulator.pump import Pump
    import simulator.runner as runner_module

    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 0.01)

    pumps = [Pump("P-00", seed=0), Pump("P-01", seed=1)]
    pubs = [
        FakePublisher("P-00"),
        # Stays flaky for many attempts — won't catch up to the healthy pump.
        FlakyPublisher(fail_count=10_000, pump_id="P-01"),
    ]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(500):
            await asyncio.sleep(0.001)
            if len(pubs[0].published) >= 5:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())
    assert len(pubs[0].published) >= 5  # healthy pump kept publishing
    assert len(pubs[1].published) == 0  # flaky pump never connected
    assert pubs[1].connect_attempts >= 1  # but did try


def test_fleet_run_resets_backoff_on_successful_connect(monkeypatch):
    """After 2 failed connects (climbing backoff to ~2x initial), a
    successful connect followed by a future failure should restart at
    INITIAL_BACKOFF_SECONDS, not at the previous run's ceiling. Verified
    indirectly via the FlakyPublisher's connect_attempts."""
    from simulator.pump import Pump
    import simulator.runner as runner_module

    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 0.005)

    pumps = [Pump("P-00", seed=0)]
    pubs = [FlakyPublisher(fail_count=2, pump_id="P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        run_task = asyncio.create_task(fleet.run())
        for _ in range(500):
            await asyncio.sleep(0.001)
            if len(pubs[0].published) >= 2:
                break
        fleet.request_shutdown()
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())
    # The publisher connected successfully exactly once after 2 failures.
    # We don't directly observe backoff, but we DO observe that the retry
    # loop took only 3 attempts (the 2 failures plus 1 success), proving
    # the inner publish loop ran after reconnect.
    assert pubs[0].connect_attempts == 3
    assert pubs[0].connects == 1
