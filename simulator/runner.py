"""Fleet runner: per-pump asyncio tasks that publish telemetry via MQTT.

A ``Fleet`` owns ``(Pump, Publisher)`` pairs and orchestrates them under a
single asyncio event loop (per ADR 0003). On ``run()``:

- One asyncio task per pump (15 tasks for the PLAN.md target fleet size).
- Each task enters its ``Publisher`` (which connects to MQTT) and ticks
  every ``tick_seconds`` (default 2.0s, per PLAN.md §2.2), publishing the
  telemetry dict returned by ``Pump.step()``.
- On ``PublisherError`` (connection refused, dropped, etc.), the task
  waits with exponential backoff and reconnects — independently of the
  other 14 pumps. This is the "retry-forever" partial-failure policy
  picked at session-brief time (Q4): a real fleet has pumps going offline
  all the time; a single pump's bad day shouldn't stop the rest.

Backoff is reset on each **successful publish**, not on each successful
connect. Resetting on connect (the original implementation) opened a
flapping vulnerability: a publisher that connects cleanly but is denied
permission to publish (e.g., an AWS IoT policy that allows MQTT CONNECT
but not PUBLISH on the topic) would loop forever at the 1s initial
backoff, never reaching the 30s cap. Resetting on successful publish
requires actually getting one message through before we trust the
connection. Per Gemini Q3, 2026-05-25 mqtt-publishing review.

The non-healthy-scenario and aws-iot-not-wired ``NotImplementedError``
guards live in ``Fleet.from_config`` — caught at the top of the stack so a
user with a bad config sees a clear message before any pump tries to
connect. ``Publisher`` subclasses also self-guard (``AwsIotPublisher``
raises ``NotImplementedError`` from ``__aenter__``), so a direct caller
that bypasses ``from_config`` still hits a loud failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from simulator.config import (
    BrokerTarget,
    ScenarioKind,
    SimulatorConfig,
    profiles_for,
)
from simulator.publisher import (
    Publisher,
    PublisherError,
    make_publisher,
    topic_for,
)
from simulator.pump import Pump


log = logging.getLogger(__name__)


# Defaults. Exposed at module scope (not configurable via YAML today) so
# tests can swap them and so future tuning has a single home.
DEFAULT_TICK_SECONDS: float = 2.0  # PLAN.md §2.2: "telemetry every 2 seconds"
INITIAL_BACKOFF_SECONDS: float = 1.0
MAX_BACKOFF_SECONDS: float = 30.0


def pump_id_for(index: int) -> str:
    """Map a 0-indexed pump number to the canonical ``P-NN`` id.

    Matches the regex enforced by ``Pump.__init__`` (``^P-\\d{2}$``), so
    fleets of up to 100 pumps fit cleanly. Aligns with
    ``context/_interfaces.md``.
    """
    if not 0 <= index <= 99:
        raise ValueError(f"pump index out of range [0, 99]: {index}")
    return f"P-{index:02d}"


class Fleet:
    """A simulated pump fleet that ticks telemetry over MQTT."""

    def __init__(
        self,
        members: Sequence[tuple[Pump, Publisher]],
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        if not members:
            raise ValueError(
                "Fleet must have at least one (pump, publisher) member"
            )
        if tick_seconds <= 0.0:
            raise ValueError(
                f"tick_seconds must be positive, got {tick_seconds}"
            )
        self._members: list[tuple[Pump, Publisher]] = list(members)
        self._tick_seconds = tick_seconds
        # Created lazily on first use so a Fleet can be constructed without
        # a running event loop. asyncio.Event binds to the loop that exists
        # when it's instantiated.
        self._shutdown: Optional[asyncio.Event] = None

    @classmethod
    def from_config(
        cls,
        config: SimulatorConfig,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> "Fleet":
        """Build a fleet from a validated ``SimulatorConfig``.

        Rejects two cases up front (cleaner UX than letting them surface
        from inside a per-pump task):

        - ``config.scenario`` other than ``HEALTHY`` -> ``NotImplementedError``.
          This check used to live in ``load_config`` as a ``UserWarning``
          (2026-05-25 config-yaml session) and was moved here during the
          2026-05-25 mqtt-publishing session so the loader is pure schema
          validation (per ADR 0003).

        - ``config.broker.target == AWS_IOT`` -> ``NotImplementedError``.
          The ``AwsIotPublisher`` stub would also refuse to ``__aenter__``,
          but failing here means the user sees "AWS IoT not yet wired"
          before any pump even tries to connect. The stub stays as a
          backstop for callers that bypass ``from_config``.
        """
        if config.scenario is not ScenarioKind.HEALTHY:
            raise NotImplementedError(
                f"scenario {config.scenario.value!r} is parsed but the "
                "scenario runner is not yet implemented; only "
                f"{ScenarioKind.HEALTHY.value!r} produces behavior today. "
                "See context/simulator.md for scenario-runner status."
            )
        if config.broker.target is BrokerTarget.AWS_IOT:
            raise NotImplementedError(
                "broker.target 'aws-iot' is parsed but not yet wired. "
                "The AWS IoT mTLS publisher lands in a later session, "
                "after the AWS account is provisioned. See ADR 0003 and "
                "context/simulator.md for status; set "
                "`broker.target: local` in your config for the meantime."
            )
        profiles = profiles_for(config)
        members: list[tuple[Pump, Publisher]] = []
        for i in range(config.fleet.pump_count):
            pid = pump_id_for(i)
            pump = Pump(
                pid,
                ambient=config.fleet.ambient_celsius,
                setpoint=config.fleet.setpoint_rpm,
                seed=config.fleet.base_seed + i,
                profiles=profiles,
            )
            publisher = make_publisher(
                target=config.broker.target,
                url=config.broker.url,
                client_id=pid,
                tls=config.broker.tls,
            )
            members.append((pump, publisher))
        return cls(members, tick_seconds=tick_seconds)

    @property
    def members(self) -> list[tuple[Pump, Publisher]]:
        return list(self._members)

    @property
    def tick_seconds(self) -> float:
        return self._tick_seconds

    def _ensure_shutdown(self) -> asyncio.Event:
        if self._shutdown is None:
            self._shutdown = asyncio.Event()
        return self._shutdown

    def request_shutdown(self) -> None:
        """Ask all per-pump tasks to wind down.

        Safe to call from a signal handler installed via
        ``loop.add_signal_handler`` or via ``signal.signal`` +
        ``loop.call_soon_threadsafe``. Per-pump tasks check the event
        between publishes and between backoff sleeps, so a shutdown
        request lands within at most ``tick_seconds`` (or
        ``MAX_BACKOFF_SECONDS`` for a task currently in backoff).
        """
        self._ensure_shutdown().set()

    async def run(self) -> None:
        """Run all per-pump tasks until shutdown is requested.

        Each task is independent — a failure in one pump's connect/publish
        loop does not affect the others (retry-forever policy). Returns
        cleanly once every task has observed shutdown and exited its
        Publisher context (the disconnect path).
        """
        shutdown = self._ensure_shutdown()
        tasks = [
            asyncio.create_task(
                self._run_pump(pump, publisher),
                name=f"pump-{pump.pump_id}",
            )
            for pump, publisher in self._members
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Outer cancellation (e.g., the runner itself was cancelled
            # from a signal handler that couldn't use add_signal_handler):
            # signal shutdown so the per-pump tasks exit their Publisher
            # contexts cleanly, then drain.
            shutdown.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _run_pump(self, pump: Pump, publisher: Publisher) -> None:
        shutdown = self._ensure_shutdown()
        topic = topic_for(pump.pump_id)
        backoff = INITIAL_BACKOFF_SECONDS
        while not shutdown.is_set():
            try:
                async with publisher:
                    log.info("pump %s connected", pump.pump_id)
                    while not shutdown.is_set():
                        reading = pump.step()
                        await publisher.publish(topic, reading)
                        # Reset backoff on each successful publish (NOT on
                        # connect). See module docstring + ADR 0003 for the
                        # flapping vulnerability this closes (Gemini Q3,
                        # 2026-05-25 mqtt-publishing review).
                        backoff = INITIAL_BACKOFF_SECONDS
                        if await self._wait_or_shutdown(
                            self._tick_seconds, shutdown
                        ):
                            return
            except PublisherError as e:
                log.warning(
                    "publisher error for %s (will retry in %.1fs): %s",
                    pump.pump_id,
                    backoff,
                    e,
                )
                if await self._wait_or_shutdown(backoff, shutdown):
                    return
                backoff = min(backoff * 2.0, MAX_BACKOFF_SECONDS)

    @staticmethod
    async def _wait_or_shutdown(
        seconds: float, shutdown: asyncio.Event
    ) -> bool:
        """Sleep for ``seconds``, returning True early if shutdown is set.

        Returns True iff shutdown was observed (caller should exit); False
        on normal timeout (caller should continue the loop).
        """
        if shutdown.is_set():
            return True
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False
