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
- On ``PublisherConfigError`` (subclass — missing cert, malformed PEM,
  bad URL), the task does NOT retry. It re-raises, ``Fleet.run`` catches
  the propagation, sets the shutdown event so the other pumps drain
  their Publisher contexts, then re-raises so ``main()`` exits with a
  distinct status code. Per Gemini Q3 (2026-05-27 aws-iot-publisher
  review) + ADR 0003 §Addendum 2026-05-28 "Static config errors halt
  the fleet": loud-loop-forever on a missing cert just buries the
  error in the log on a single-PC dev machine.

Backoff is reset on each **successful publish**, not on each successful
connect. Resetting on connect (the original implementation) opened a
flapping vulnerability: a publisher that connects cleanly but is denied
permission to publish (e.g., an AWS IoT policy that allows MQTT CONNECT
but not PUBLISH on the topic) would loop forever at the 1s initial
backoff, never reaching the 30s cap. Resetting on successful publish
requires actually getting one message through before we trust the
connection. Per Gemini Q3, 2026-05-25 mqtt-publishing review.

The non-healthy-scenario guard was dropped (2026-05-28 session, ADR 0004
"Tick-Driven Scenario Controller") when the scenario layer landed —
``Fleet.from_config`` now builds a concrete ``Scenario`` via
``simulator.scenario.make_scenario(config)`` for all four
``ScenarioKind`` values. The ``broker.target: aws-iot`` guard had been
dropped earlier (2026-05-27, see ADR 0003 §Addendum). With both guards
gone, the only halt-the-fleet exits are ``PublisherConfigError``
(transport/cert) and ``ScenarioError`` (scenario logic / fleet
mutation). Transient errors still go through retry-forever.

Scenario task: ``Fleet.run`` spawns one extra asyncio task — the
scenario controller — alongside the per-pump publish tasks. The
controller wakes every ``tick_seconds`` and calls
``scenario.apply(fleet, tick)``. Mutations to pump state land
atomically between pump ticks because the event loop is
single-threaded and ``Pump.step`` is synchronous. See ADR 0004.

Task-lifecycle handling for mid-run ``add_pump``: ``run()`` uses
``asyncio.wait(FIRST_COMPLETED)`` in a re-folding loop rather than
``asyncio.gather(*tasks)``. Per Gemini Q2 (2026-05-28 scenarios review),
``gather`` evaluates its arguments exactly once, so tasks created by
``Fleet.add_pump`` mid-run would be orphaned. ``asyncio.TaskGroup``
would be cleaner but is Python 3.11+; the test sandbox runs 3.10.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Sequence

from simulator.config import (
    PUMP_ID_PLACEHOLDER,
    ScenarioKind,
    SimulatorConfig,
    profiles_for,
    tls_for_pump,
)
from simulator.publisher import (
    Publisher,
    PublisherConfigError,
    PublisherError,
    make_publisher,
    topic_for,
)
from simulator.pump import Pump
from simulator.scenario import (
    HealthyScenario,
    Scenario,
    ScenarioError,
    make_scenario,
)


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
        scenario: Optional[Scenario] = None,
        publisher_factory: Optional[Callable[[str], Publisher]] = None,
        pump_factory: Optional[Callable[[str], Pump]] = None,
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
        # HealthyScenario as the default rather than None — keeps the
        # run() shape uniform (always exactly one scenario controller
        # task) and the apply() call site branch-free. See ADR 0004.
        self._scenario: Scenario = scenario if scenario is not None else HealthyScenario()
        # Factories used by add_pump() to construct new (Pump, Publisher)
        # pairs at scenario-mutation time. Both are optional in __init__
        # to keep direct-construction tests simple; Fleet.from_config
        # always populates them. If a scenario calls add_pump() without
        # them, add_pump raises ScenarioError.
        self._publisher_factory = publisher_factory
        self._pump_factory = pump_factory
        # Created lazily on first use so a Fleet can be constructed without
        # a running event loop. asyncio.Event binds to the loop that exists
        # when it's instantiated.
        self._shutdown: Optional[asyncio.Event] = None
        # Per-pump tasks registered when run() spawns them. add_pump
        # appends here so the new pump's task is gathered/cancelled
        # alongside the originals.
        self._tasks: list[asyncio.Task] = []

    @classmethod
    def from_config(
        cls,
        config: SimulatorConfig,
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> "Fleet":
        """Build a fleet from a validated ``SimulatorConfig``.

        All four ``ScenarioKind`` values are accepted — the scenario
        layer landed 2026-05-28 (ADR 0004 "Tick-Driven Scenario
        Controller"). The previous ``NotImplementedError`` for
        non-healthy scenarios was dropped, mirroring the
        ``broker.target: aws-iot`` drop on 2026-05-27 (ADR 0003
        §Addendum). The scenario instance is built via
        ``simulator.scenario.make_scenario`` and attached to the fleet
        for its lifetime.

        ``config.broker.target == AWS_IOT`` is also not rejected here —
        the ``AwsIotPublisher`` (wired 2026-05-27, see ADR 0003 §Addendum
        2026-05-27 "AwsIotPublisher wired") is the gate. Missing certs
        or connect failures surface as ``PublisherError`` /
        ``PublisherConfigError`` and are handled by ``_run_pump``
        (transient ones via retry-forever, static ones by halting the
        fleet).

        Pump / Publisher factories: closures over the config are stashed
        so ``Fleet.add_pump`` (called by ``FleetExpansion``) can
        construct further pumps mid-run with the same broker target +
        ambient/setpoint settings. See ADR 0004.
        """
        profiles = profiles_for(config)

        # A multi-pump aws-iot fleet whose cert/key paths carry no
        # {pump_id} placeholder shares ONE certificate across the whole
        # fleet. The thing-variable policy (ADR 0016) then denies
        # CONNECT for every pump but the one Thing the cert is attached
        # to -- which surfaces as transient PublisherError and a silent
        # 30s-cap retry loop for the other N-1 pumps: exactly the
        # buried-error UX ADR 0003 §Addendum 2026-05-28 exists to
        # avoid. Not an error (a non-AWS mTLS broker may legitimately
        # share certs), but loud enough to find in the first screen of
        # logs.
        if (
            config.broker.tls is not None
            and config.fleet.pump_count > 1
            and PUMP_ID_PLACEHOLDER
            not in (config.broker.tls.cert_path + config.broker.tls.key_path)
        ):
            log.warning(
                "broker.tls paths contain no %r placeholder but "
                "fleet.pump_count is %d -- all pumps will present the "
                "SAME certificate. Against AWS IoT Core's per-Thing "
                "policy (ADR 0016) every pump except the cert's "
                "attached Thing will be denied CONNECT and retry "
                "forever. See simulator/config.example.yaml.",
                PUMP_ID_PLACEHOLDER,
                config.fleet.pump_count,
            )

        def _make_pump(pump_id: str) -> Pump:
            idx = int(pump_id.split("-")[1])
            return Pump(
                pump_id,
                ambient=config.fleet.ambient_celsius,
                setpoint=config.fleet.setpoint_rpm,
                seed=config.fleet.base_seed + idx,
                profiles=profiles,
            )

        def _make_publisher(pump_id: str) -> Publisher:
            # Per-pump mTLS identity (ADR 0016): expand the {pump_id}
            # placeholder in the tls paths for THIS pump. None for
            # target: local. Because this closure is also stashed as
            # the publisher_factory, FleetExpansion's add_pump mints
            # per-pump identities too.
            tls = config.broker.tls
            if tls is not None:
                tls = tls_for_pump(tls, pump_id)
            return make_publisher(
                target=config.broker.target,
                url=config.broker.url,
                client_id=pump_id,
                tls=tls,
            )

        members: list[tuple[Pump, Publisher]] = []
        for i in range(config.fleet.pump_count):
            pid = pump_id_for(i)
            members.append((_make_pump(pid), _make_publisher(pid)))

        return cls(
            members,
            tick_seconds=tick_seconds,
            scenario=make_scenario(config),
            publisher_factory=_make_publisher,
            pump_factory=_make_pump,
        )

    @property
    def members(self) -> list[tuple[Pump, Publisher]]:
        return list(self._members)

    @property
    def tick_seconds(self) -> float:
        return self._tick_seconds

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    def get_pump(self, pump_id: str) -> Pump:
        """Look up a pump by id. Raises ``KeyError`` if not present.

        Scenario controllers use this to target specific pumps (e.g.
        ``RealFailure`` walks ``P-07`` through the lifecycle).
        """
        for pump, _ in self._members:
            if pump.pump_id == pump_id:
                return pump
        raise KeyError(f"no pump with id {pump_id!r} in fleet")

    def add_pump(self, pump_id: str) -> tuple[Pump, Publisher]:
        """Add a new (Pump, Publisher) pair mid-run.

        Called by scenarios that grow the fleet (``FleetExpansion``).
        Builds a fresh pump + publisher via the factories stored at
        construction time (set by ``Fleet.from_config``), appends them
        to ``self._members``, and — if the fleet is currently running —
        spawns a per-pump task for the new pair on the running event
        loop. Returns the new pair so the caller can apply further
        customisation (e.g. shifting the vibration baseline).

        Raises ``ScenarioError`` if no factories are configured (a
        direct ``Fleet(...)`` construction without factories cannot
        grow the fleet) or the id collides with an existing member.

        The new task is appended to ``self._tasks``. ``Fleet.run``'s
        asyncio.wait-loop folds it into the awaited set on the next
        iteration — per Gemini Q2 (2026-05-28 scenarios review).
        """
        if self._publisher_factory is None or self._pump_factory is None:
            raise ScenarioError(
                f"Fleet was constructed without pump/publisher factories "
                f"— cannot add_pump({pump_id!r}). Build via Fleet.from_config "
                "or pass factories to Fleet.__init__."
            )
        for existing, _ in self._members:
            if existing.pump_id == pump_id:
                raise ScenarioError(
                    f"pump {pump_id!r} is already in the fleet"
                )
        pump = self._pump_factory(pump_id)
        publisher = self._publisher_factory(pump_id)
        self._members.append((pump, publisher))
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None  # type: ignore[assignment]
        if running_loop is not None and self._tasks:
            task = asyncio.create_task(
                self._run_pump(pump, publisher),
                name=f"pump-{pump.pump_id}",
            )
            self._tasks.append(task)
        return pump, publisher

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
        """Run all per-pump tasks + the scenario controller until shutdown.

        Each per-pump task is independent for transient errors — a
        flaky pump cannot drag the rest of the fleet down
        (retry-forever policy). Static config errors
        (``PublisherConfigError``) and scenario errors
        (``ScenarioError``) are halt-the-fleet conditions: any task
        raising one stops the rest. On halt, the shutdown event is
        set, other per-pump tasks drain their Publisher contexts
        within ``DISCONNECT_TIMEOUT_SECONDS``, and the original
        exception is re-raised so ``main()`` can return a distinct
        exit code.

        Scenario controller: a single extra task wakes every
        ``tick_seconds`` and calls ``scenario.apply(self, tick)``. Per
        ADR 0004, this is the simplest shape that supports all three
        concrete scenarios.

        Task lifecycle: uses ``asyncio.wait(FIRST_COMPLETED)`` in a
        re-folding loop rather than a single ``asyncio.gather`` so
        that tasks created by ``add_pump`` mid-run are picked up on
        the next iteration. Per Gemini Q2 review (2026-05-28
        scenarios): ``gather(*tasks)`` evaluates its arguments
        exactly once, so a mid-run-added pump task would be orphaned
        — never awaited, exceptions lost, "Task was destroyed but it
        is pending" warnings on shutdown. ``asyncio.TaskGroup`` would
        be cleaner but is Python 3.11+; the sandbox tests run on
        3.10 so we use the wait-loop pattern instead.
        """
        shutdown = self._ensure_shutdown()
        self._tasks = [
            asyncio.create_task(
                self._run_pump(pump, publisher),
                name=f"pump-{pump.pump_id}",
            )
            for pump, publisher in self._members
        ]
        scenario_task = asyncio.create_task(
            self._run_scenario(),
            name="scenario",
        )
        # ``awaited`` is the running registry of every task we've
        # observed. ``pending`` is the subset still running. After
        # each wait we re-scan ``self._tasks`` for newcomers (added
        # by add_pump while we were awaiting) and fold them in.
        awaited: set[asyncio.Task] = {scenario_task, *self._tasks}
        pending: set[asyncio.Task] = set(awaited)
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                pending -= done
                for t in self._tasks:
                    if t not in awaited:
                        awaited.add(t)
                        pending.add(t)
                for t in done:
                    exc = t.exception()
                    if exc is not None:
                        raise exc
        except (PublisherConfigError, ScenarioError, asyncio.CancelledError):
            shutdown.set()
            for t in self._tasks:
                if t not in awaited:
                    awaited.add(t)
            leftover = {t for t in awaited if not t.done()}
            if leftover:
                await asyncio.gather(*leftover, return_exceptions=True)
            raise

    async def _run_scenario(self) -> None:
        """Tick the scenario controller until shutdown.

        Wakes on ``tick_seconds`` cadence. Calls
        ``self._scenario.apply(self, tick)`` once per tick. If the
        scenario raises ``ScenarioError``, this method re-raises (the
        run() awaiter catches and triggers fleet halt). Other
        exceptions are wrapped in ``ScenarioError`` so scenarios can't
        leak undocumented exception types to the caller.
        """
        shutdown = self._ensure_shutdown()
        tick = 0
        while not shutdown.is_set():
            try:
                await self._scenario.apply(self, tick)
            except ScenarioError:
                raise
            except Exception as e:  # noqa: BLE001 — see docstring
                raise ScenarioError(
                    f"scenario {type(self._scenario).__name__} raised at tick {tick}: {e}"
                ) from e
            tick += 1
            if await self._wait_or_shutdown(self._tick_seconds, shutdown):
                return

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
                        backoff = INITIAL_BACKOFF_SECONDS
                        if await self._wait_or_shutdown(
                            self._tick_seconds, shutdown
                        ):
                            return
            except PublisherConfigError as e:
                log.error(
                    "pump %s static config error (halting fleet): %s",
                    pump.pump_id, e,
                )
                raise
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
