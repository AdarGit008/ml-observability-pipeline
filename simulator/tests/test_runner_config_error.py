"""Runner-level tests for the PublisherConfigError halt-the-fleet
behavior added 2026-05-28 per Gemini Q3 review.

Three properties:
1. A pump raising ``PublisherConfigError`` propagates out of ``Fleet.run``
   (does NOT get swallowed by the retry-forever loop).
2. The other pumps drain cleanly via the disconnect-bound (B work from
   2026-05-28 — this test is the canary that proves the two fixes
   interact correctly).
3. A pump raising plain ``PublisherError`` still retries (this is the
   regression test ensuring the new subclass check didn't accidentally
   break the existing transient-error behavior).
"""

from __future__ import annotations

import asyncio
from typing import Any

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
    Publisher,
    PublisherConfigError,
    PublisherError,
)
from simulator.pump import Pump
from simulator.runner import Fleet


def _run(coro):
    return asyncio.run(coro)


class _ConfigErrorPublisher(Publisher):
    """Raises PublisherConfigError on __aenter__ — simulates a missing
    cert without the file-system overhead."""

    def __init__(self, pump_id: str = "P-??") -> None:
        self.pump_id = pump_id
        self.connect_attempts = 0

    async def __aenter__(self) -> "_ConfigErrorPublisher":
        self.connect_attempts += 1
        raise PublisherConfigError(
            f"AWS IoT mTLS cert_path not found for {self.pump_id}"
        )

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # __aenter__ raised, so this shouldn't fire under normal usage.
        return None

    async def publish(self, topic, payload):  # pragma: no cover
        raise PublisherError("publish() called on ConfigErrorPublisher")


class _RecordingPublisher(Publisher):
    """Connects fine, records publishes, exits cleanly. Tracks whether
    __aexit__ was called — load-bearing for the 'drain cleanly' assertion."""

    def __init__(self, pump_id: str = "P-??") -> None:
        self.pump_id = pump_id
        self.entered = False
        self.exited = False
        self.publishes = 0

    async def __aenter__(self) -> "_RecordingPublisher":
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    async def publish(self, topic, payload):
        self.publishes += 1


class _TransientErrorPublisher(Publisher):
    """Raises plain PublisherError (NOT the subclass) on every connect.
    The runner should retry it forever, not halt."""

    def __init__(self, pump_id: str = "P-??") -> None:
        self.pump_id = pump_id
        self.connect_attempts = 0

    async def __aenter__(self) -> "_TransientErrorPublisher":
        self.connect_attempts += 1
        raise PublisherError(
            f"simulated transient CONNREFUSED for {self.pump_id}"
        )

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def publish(self, topic, payload):  # pragma: no cover
        raise PublisherError("publish() called on TransientErrorPublisher")


def test_fleet_halts_on_config_error_from_single_pump():
    """One pump raises PublisherConfigError -> Fleet.run propagates it."""
    pumps = [Pump("P-00", seed=0)]
    pubs = [_ConfigErrorPublisher("P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        with pytest.raises(PublisherConfigError, match="cert_path not found"):
            await asyncio.wait_for(fleet.run(), timeout=1.0)

    _run(_go())
    # Single attempt — no retry on config error.
    assert pubs[0].connect_attempts == 1


def test_fleet_drains_other_pumps_when_one_halts_with_config_error(monkeypatch):
    """A 2-pump fleet where pump 1 has a config error should:
    - propagate PublisherConfigError out of Fleet.run
    - cancel pump 0's task, which exits its async-with cleanly (the
      __aexit__ wait_for bound from the (B) work is load-bearing here)
    - prove __aexit__ ran on the good pump (drain happened)
    """
    import simulator.runner as runner_module
    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 0.01)

    pumps = [Pump("P-00", seed=0), Pump("P-01", seed=1)]
    good = _RecordingPublisher("P-00")
    bad = _ConfigErrorPublisher("P-01")
    pubs = [good, bad]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        with pytest.raises(PublisherConfigError):
            await asyncio.wait_for(fleet.run(), timeout=2.0)

    _run(_go())
    # Bad pump tried once and gave up.
    assert bad.connect_attempts == 1
    # Good pump entered AND exited — clean drain via the disconnect-bound.
    assert good.entered is True
    assert good.exited is True


def test_fleet_retries_on_plain_publisher_error_does_not_halt(monkeypatch):
    """Regression: a plain PublisherError (NOT the subclass) must
    continue feeding the retry-forever loop. Without this test, the new
    except-subclass-first logic could silently catch transient errors
    and halt the fleet for the wrong reason."""
    import simulator.runner as runner_module
    monkeypatch.setattr(runner_module, "INITIAL_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(runner_module, "MAX_BACKOFF_SECONDS", 0.005)

    pumps = [Pump("P-00", seed=0)]
    pubs = [_TransientErrorPublisher("P-00")]
    fleet = Fleet(list(zip(pumps, pubs)), tick_seconds=0.001)

    async def _go():
        # Let the retry loop spin for a bit, then shut down. The fleet
        # should NOT raise PublisherConfigError or any other exception
        # because plain PublisherError is the transient kind.
        run_task = asyncio.create_task(fleet.run())
        await asyncio.sleep(0.05)
        fleet.request_shutdown()
        # Normal shutdown — no exception expected.
        await asyncio.wait_for(run_task, timeout=1.0)

    _run(_go())
    # Many retry attempts proves the runner kept trying.
    assert pubs[0].connect_attempts >= 3, (
        f"runner should have retried plain PublisherError "
        f"(got {pubs[0].connect_attempts} attempts)"
    )
