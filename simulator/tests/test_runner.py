"""Unit tests for simulator.runner.

Uses an in-memory ``FakePublisher`` (records publishes, optionally fails a
fixed number of connects) so the runner is exercised without a real
broker. Backoff constants are monkeypatched to milliseconds where needed
so the time-based tests finish quickly; the exact-sequence test (Q7) uses
a monkeypatched ``_wait_or_shutdown`` to capture the backoff math
deterministically without any real sleep.
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
from simulator.publisher import (
    AwsIotPublisher,
    LocalPublisher,
    Publisher,
    PublisherError,
)
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
    # base_seed=100 -> pump seeds 100, 101, 102.
    pumps = [p for p, _ in fleet.members]
    assert pumps[0].degradation == 0.0  # before any step
    assert pumps[0].state is PumpState.HEALTHY


def test_from_config_uses_local_publisher_for_local_target():
    fleet = Fleet.from_config(_config(target=BrokerTarget.LOCAL))
    publishers = [pub for _, pub in fleet.members]
    assert all(isinstance(p, LocalPublisher) for p in publishers)


@pytest.mark.parametrize(
    "scenario, expected_cls_name",
    [
        (ScenarioKind.SEASONAL_DRIFT, "SeasonalDrift"),
        (ScenarioKind.FLEET_EXPANSION, "FleetExpansion"),
        (ScenarioKind.REAL_FAILURE, "RealFailure"),
        (ScenarioKind.HEALTHY, "HealthyScenario"),
    ],
)
def test_from_config_accepts_all_scenarios(scenario: ScenarioKind, expected_cls_name: str):
    """The NotImplementedError reject was dropped on 2026-05-28 when the
    scenario layer landed (ADR 0004 "Tick-Driven Scenario Controller").
    All four ScenarioKind values now build a concrete Scenario.

    Mirrors the parallel 2026-05-27 change for ``broker.target: aws-iot``
    (ADR 0003 §Addendum 2026-05-27 "AwsIotPublisher wired"), where the
    similar guard at this layer was dropped because the implementation
    landed."""
    fleet = Fleet.from_config(_config(scenario=scenario))
    assert type(fleet.scenario).__name__ == expected_cls_name


def test_from_config_uses_aws_iot_publisher_for_aws_iot_target():
    """The aws-iot reject was dropped on 2026-05-27 when AwsIotPublisher
    landed (ADR 0003 §Addendum 2026-05-27 "AwsIotPublisher wired"). The
    publisher itself now gates on missing certs at connect time; here we
    only assert from_config wires the right subclass and the TlsConfig
    threads through to it. No certs are opened at construction time."""
    tls = TlsConfig(
        cert_path="certs/cert.pem",
        key_path="certs/key.pem",
        ca_path="certs/ca.pem",
    )
    fleet = Fleet.from_config(_config(target=BrokerTarget.AWS_IOT, tls=tls))
    publishers = [pub for _, pub in fleet.members]
    assert all(isinstance(p, AwsIotPublisher) for p in publishers)
    assert all(p.tls == tls for p in publishers)


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


def test_fleet_run_continues_after_failed_connects(monkeypatch):
    """A FlakyPublisher that fails the first 2 connect attempts should
    eventually succeed; the runner reconnects with backoff and proceeds.
    Verifies the broad "connect-retry loop reaches a successful state"
    property — exact backoff math is covered by
    ``test_fleet_backoff_climbs_to_cap_then_holds``."""
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


def test_fleet_backoff_climbs_to_cap_then_holds(monkeypatch):
    """Per Gemini Q3+Q7 (2026-05-25 mqtt-publishing review): a publisher
    that keeps failing connect must produce backoffs of exactly
    [1, 2, 4, 8, 16, 30, 30, ...], with the cap engaging at the 6th
    failure and holding thereafter.

    The original implementation reset backoff on successful CONNECT,
    which opened a flapping vulnerability (a publisher that connects but
    is denied PUBLISH would loop forever at 1s, never reaching the cap).
    The current implementation resets on successful PUBLISH, so a
    connect-only success doesn't bypass the cap. This test exercises the
    pure climb-to-cap path with a publisher that never connects.

    Implementation note: we monkeypatch ``Fleet._wait_or_shutdown`` to
    capture the requested backoff durations without actually sleeping.
    Per Gemini's suggestion (Q7), this gives deterministic coverage of
    the backoff math without flaky timing.
    """
    from simulator.pump import Pump
    import simulator.runner as runner_module

    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 1.0)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 30.0)

    recorded_waits: list[float] = []

    # tick_seconds=999.0 disambiguates the scenario-task's per-tick
    # wait (added 2026-05-28 with the scenario layer — ADR 0004) from
    # the backoff sequence we want to record. The pre-existing
    # test_fleet_backoff_resets_on_successful_publish uses the same
    # pattern for the same reason.
    TICK_SECONDS = 999.0

    async def _recording_wait(seconds: float, shutdown: asyncio.Event) -> bool:
        if seconds != TICK_SECONDS:
            recorded_waits.append(seconds)
        # Let the test collect 7 backoff values, then return True (the
        # signal that means "shutdown observed — exit the retry loop").
        return len(recorded_waits) >= 7

    monkeypatch.setattr(
        runner_module.Fleet,
        "_wait_or_shutdown",
        staticmethod(_recording_wait),
    )

    pumps = [Pump("P-00", seed=0)]
    pubs = [FlakyPublisher(fail_count=1_000_000, pump_id="P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=TICK_SECONDS)

    async def _go():
        await asyncio.wait_for(fleet.run(), timeout=1.0)

    _run(_go())

    assert recorded_waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_fleet_backoff_resets_on_successful_publish(monkeypatch):
    """Per Gemini Q3 (2026-05-25 mqtt-publishing review): backoff resets
    on a successful publish, NOT on a successful connect.

    Strategy: a publisher that connects, fails to publish on the first
    attempt, reconnects (which exits via aexit and restarts the outer
    loop), publishes once successfully, then fails on the next publish.
    The first failure-cycle's backoff wait should be 1.0s. After the
    successful publish in between, the next failure-cycle's wait should
    ALSO be 1.0s (reset by the publish), NOT 2.0s.
    """
    from simulator.pump import Pump
    import simulator.runner as runner_module

    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 1.0)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 30.0)

    class PublishOncePublisher(FakePublisher):
        """Connects fine. First publish raises; second publish succeeds
        (and resets backoff); third publish raises.
        """

        def __init__(self, pump_id: str = "P-??") -> None:
            super().__init__(pump_id=pump_id)
            self._publish_calls = 0

        async def publish(self, topic, payload):
            self._publish_calls += 1
            if self._publish_calls in (1, 3):
                raise PublisherError(
                    f"simulated publish failure (call={self._publish_calls})"
                )
            await super().publish(topic, payload)

    # Use a tick value that's clearly distinguishable from any backoff
    # value (backoffs climb 1, 2, 4, ..., 30) so we can filter the
    # post-publish "wait one tick" calls out of the recorded sequence.
    TICK_SECONDS = 99.0

    recorded_backoffs: list[float] = []

    async def _recording_wait(seconds: float, shutdown: asyncio.Event) -> bool:
        if seconds != TICK_SECONDS:
            recorded_backoffs.append(seconds)
        # Exit after collecting 2 backoff values — one per failure cycle.
        return len(recorded_backoffs) >= 2

    monkeypatch.setattr(
        runner_module.Fleet,
        "_wait_or_shutdown",
        staticmethod(_recording_wait),
    )

    pumps = [Pump("P-00", seed=0)]
    pubs = [PublishOncePublisher(pump_id="P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=TICK_SECONDS)

    async def _go():
        await asyncio.wait_for(fleet.run(), timeout=1.0)

    _run(_go())

    # Both failure cycles should have used the INITIAL backoff: the
    # successful publish in between reset the counter.
    assert recorded_backoffs == [1.0, 1.0]
