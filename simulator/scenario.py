"""Scenario controllers — schedule fleet-level mutations during a run.

A ``Scenario`` runs as a single asyncio task alongside the per-pump
publish loops (one extra task, regardless of fleet size). It wakes on the
same tick cadence as the publishers (``tick_seconds``) and applies state
changes to the fleet: modulating pump parameters, forcing state
transitions, or growing the fleet mid-run.

Three concrete scenarios match PLAN.md §4's demo script:

- ``SeasonalDrift`` — ambient temperature oscillates fleet-wide on a
  sine wave. Bearing temps climb together; the model over-predicts
  failure; fleet-level PSI catches it.
- ``FleetExpansion`` — N new pumps appear at a scheduled tick with a
  shifted vibration baseline. Per-pump PSI lights up only those new
  pumps; the rest stay clean.
- ``RealFailure`` — one chosen pump escalates ``HEALTHY → DEGRADING →
  FAILING → FAILED`` on a schedule; the rest stay healthy. Drift flags
  the failing pump alongside rising failure scores.

The scenario layer is a thin orchestrator. It does NOT touch the
``Publisher`` layer (per session brief, frozen this session) — wire
shape is unchanged; scenarios mutate ``Pump`` state and ``Fleet``
membership, and the existing per-pump publish loop picks up the
mutations on its next ``pump.step()`` call.

See ADR 0004 for the interface trade-offs (tick-driven vs event-driven,
fleet-level orchestrator vs per-pump hook, single ``apply`` vs
lifecycle hooks).
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional, Sequence

from simulator.config import ScenarioKind, SimulatorConfig
from simulator.pump import PumpState

if TYPE_CHECKING:  # pragma: no cover — type-only import to break the cycle
    from simulator.runner import Fleet


log = logging.getLogger(__name__)


# -- Defaults for the three concrete scenarios ---------------------------
#
# PLAN.md doesn't pin specific magnitudes for the demos; these values are
# tuned to (a) make the lifecycle visible inside a few minutes of demo_mode
# wall-clock, (b) produce signal a downstream PSI detector with the
# documented thresholds (>0.25 = significant shift) should plausibly catch.
# The downstream model + drift sessions will revisit if the actual PSI
# values don't land in range. All overrideable via Scenario constructors.

# SeasonalDrift defaults. ~6-minute period at 2 s/tick (180 ticks) and a
# ±8 °C swing around ambient drives bearing_temp through a ~16 °C range —
# well outside the N(0, 0.5) noise envelope, easy fleet-wide signal.
DEFAULT_SEASONAL_PERIOD_TICKS: int = 180
DEFAULT_SEASONAL_AMPLITUDE_C: float = 8.0

# FleetExpansion defaults. Add 3 pumps at tick 60 (~2 min into demo_mode),
# each with vibration baseline shifted by +0.4 from the model's training
# baseline of 0.3 (so vibration_amp lands near 0.7 + degradation*2.5 + noise
# — outside the model's seen distribution but not crazy).
DEFAULT_EXPANSION_TICK: int = 60
DEFAULT_EXPANSION_NEW_COUNT: int = 3
DEFAULT_EXPANSION_VIBRATION_SHIFT: float = 0.4

# RealFailure defaults. The PLAN.md §4 demo names "pump P-07" explicitly.
# Schedule walks the four states across ~3 minutes of demo time:
#   tick 30 → DEGRADING, tick 90 → FAILING, tick 150 → FAILED.
DEFAULT_REAL_FAILURE_TARGET: str = "P-07"
DEFAULT_REAL_FAILURE_SCHEDULE: dict[PumpState, int] = {
    PumpState.DEGRADING: 30,
    PumpState.FAILING: 90,
    PumpState.FAILED: 150,
}


class ScenarioError(Exception):
    """A scenario tried to do something the fleet cannot satisfy.

    Distinct from ``PublisherError`` (transport-bound, retry-forever) and
    ``PublisherConfigError`` (static config, halt-the-fleet). A
    ``ScenarioError`` is *also* a halt-the-fleet condition: scenarios are
    pure logic + fleet mutation; if one fails, retrying won't fix it.
    Surfaced from inside the scenario task and propagated to ``main()``.
    """


class Scenario(ABC):
    """Mutates fleet state on a tick cadence.

    The scenario controller (``Fleet._run_scenario``) calls ``apply``
    once per ``tick_seconds`` with the current fleet and a monotonically
    increasing tick counter (starts at 0). Implementations can:

    - read ``fleet.members`` to enumerate (pump, publisher) pairs
    - call ``pump.set_ambient(value)`` / ``pump.force_state(state)`` /
      ``pump.set_profile(...)`` to mutate pump state
    - call ``fleet.add_pump(pump_id)`` to grow the fleet mid-run
    - track their own internal state (scheduled events, cycle counters)

    Subclasses accept an optional ``seed`` so any randomness they add
    later is reproducible. None of the three concretes ship today use
    it (sine wave + scheduled transitions + idempotent expansion are
    all deterministic), but the interface threads the seed through so
    a future stochastic scenario (e.g. random vibration noise bursts)
    is reproducible from the start. Per Gemini Q6 (2026-05-28 scenarios
    review).

    Mutation safety: the asyncio event loop is single-threaded and
    cooperative. ``pump.step()`` is synchronous and never yields
    mid-call; ``Scenario.apply`` should likewise avoid yielding between
    mutations to a single pump. Across pumps, yielding is safe because
    each pump's task only reads its own state.
    """

    def __init__(self, *, seed: Optional[int] = None) -> None:
        self._seed = seed

    @property
    def seed(self) -> Optional[int]:
        """The seed handed to this scenario at construction time.

        ``None`` means "no seed plumbed" — concretes that need
        randomness should pass it to ``random.Random(...)`` themselves
        (the interface keeps the field rather than the RNG so each
        scenario picks the granularity it wants — per-tick rebuild,
        single shared, etc.).
        """
        return self._seed

    @abstractmethod
    async def apply(self, fleet: "Fleet", tick: int) -> None:
        """Apply this scenario's effects for ``tick``.

        Called once per scenario tick (same cadence as publisher tick).
        Must not block on I/O — scenarios are CPU-bound mutators of
        in-memory state. Use ``asyncio.sleep`` only for explicit pacing
        (not currently needed by any concrete scenario).
        """


class HealthyScenario(Scenario):
    """No-op scenario. Used when ``config.scenario == HEALTHY``.

    Spawning a no-op controller task (instead of branching on
    ``scenario is None``) keeps the runner shape uniform: there is
    always exactly one scenario task per fleet.
    """

    def __init__(self, *, seed: Optional[int] = None) -> None:
        super().__init__(seed=seed)

    async def apply(self, fleet: "Fleet", tick: int) -> None:
        return


class SeasonalDrift(Scenario):
    """Modulates ambient temperature fleet-wide on a sine wave.

    ``ambient(t) = base_ambient + amplitude * sin(2π · t / period)``

    Drives ``bearing_temp = ambient + 0.02 * RPM + d * 15 + N(0, 0.5)``
    (PLAN.md §2.2) — a ±``amplitude_c`` °C ambient swing produces a
    fleet-wide ±``amplitude_c`` °C bearing-temp shift on top of the
    noise floor. Per PLAN.md §4 (Scenario 1) and §2.7, fleet-level PSI
    is what should catch this.

    The base ambient is captured from each pump on first apply (so the
    scenario's modulation is around whatever the YAML's
    ``fleet.ambient_celsius`` resolved to, not a hardcoded 22 °C).
    """

    def __init__(
        self,
        *,
        period_ticks: int = DEFAULT_SEASONAL_PERIOD_TICKS,
        amplitude_celsius: float = DEFAULT_SEASONAL_AMPLITUDE_C,
        seed: Optional[int] = None,
    ) -> None:
        super().__init__(seed=seed)
        if period_ticks <= 0:
            raise ValueError(
                f"period_ticks must be positive, got {period_ticks}"
            )
        if amplitude_celsius < 0.0:
            raise ValueError(
                f"amplitude_celsius must be non-negative, got {amplitude_celsius}"
            )
        self._period = int(period_ticks)
        self._amplitude = float(amplitude_celsius)
        # Captured on first apply (one entry per pump_id). New pumps that
        # appear later (e.g. via a composed FleetExpansion in a future
        # session) get captured on their first sight.
        self._base_ambient: dict[str, float] = {}

    @property
    def period_ticks(self) -> int:
        return self._period

    @property
    def amplitude_celsius(self) -> float:
        return self._amplitude

    async def apply(self, fleet: "Fleet", tick: int) -> None:
        # sin(2π·t/T): tick=0 is at the base, tick=T/4 hits +amplitude,
        # tick=T/2 returns to base, tick=3T/4 hits -amplitude, tick=T
        # wraps cleanly.
        phase = 2.0 * math.pi * (tick % self._period) / self._period
        delta = self._amplitude * math.sin(phase)
        for pump, _ in fleet.members:
            if pump.pump_id not in self._base_ambient:
                # First time seeing this pump — capture its starting ambient
                # before we apply our delta on top of it.
                self._base_ambient[pump.pump_id] = pump.ambient
            base = self._base_ambient[pump.pump_id]
            pump.set_ambient(base + delta)


class FleetExpansion(Scenario):
    """Grows the fleet by ``new_pump_count`` at ``expand_at_tick``.

    The new pumps are added to ``fleet.members`` and start publishing
    (one new asyncio task per pump). Their vibration baseline is shifted
    by ``vibration_baseline_shift`` — the simplest expression of "these
    pumps weren't in the model's training distribution." Per PLAN.md §4
    Scenario 2 and §2.7, per-pump PSI should flag the new pumps while
    fleet-level PSI stays stable.

    The vibration shift is applied by overriding ``StateProfile`` ceilings
    for the new pumps' HEALTHY state (a small ``rate_per_tick`` keeps
    degradation moving toward an elevated ceiling). This routes through
    the existing Pump.step pipeline rather than adding a new
    "static-offset" field, so the wire shape and publisher layer stay
    untouched (session-brief constraint #1).

    Once the expansion fires, the scenario is idempotent: subsequent
    ticks are no-ops. Multiple expansion events in one demo would need
    a different scenario type.
    """

    def __init__(
        self,
        *,
        expand_at_tick: int = DEFAULT_EXPANSION_TICK,
        new_pump_count: int = DEFAULT_EXPANSION_NEW_COUNT,
        vibration_baseline_shift: float = DEFAULT_EXPANSION_VIBRATION_SHIFT,
        seed: Optional[int] = None,
    ) -> None:
        super().__init__(seed=seed)
        if expand_at_tick < 0:
            raise ValueError(
                f"expand_at_tick must be non-negative, got {expand_at_tick}"
            )
        if new_pump_count <= 0:
            raise ValueError(
                f"new_pump_count must be positive, got {new_pump_count}"
            )
        self._expand_at = int(expand_at_tick)
        self._count = int(new_pump_count)
        self._shift = float(vibration_baseline_shift)
        self._fired = False

    @property
    def expand_at_tick(self) -> int:
        return self._expand_at

    @property
    def new_pump_count(self) -> int:
        return self._count

    @property
    def vibration_baseline_shift(self) -> float:
        return self._shift

    @property
    def fired(self) -> bool:
        return self._fired

    async def apply(self, fleet: "Fleet", tick: int) -> None:
        if self._fired or tick < self._expand_at:
            return
        # Fire exactly once. Set the flag BEFORE add_pump so any retry on
        # exception still leaves the scenario in a known state.
        self._fired = True
        existing_ids = {pump.pump_id for pump, _ in fleet.members}
        # Find the next contiguous P-NN ids that don't collide.
        added = 0
        candidate = 0
        while added < self._count:
            if candidate >= 100:
                raise ScenarioError(
                    f"FleetExpansion cannot find {self._count} free pump ids "
                    f"in P-00..P-99 (already used: {sorted(existing_ids)})"
                )
            new_id = f"P-{candidate:02d}"
            if new_id not in existing_ids:
                # Bias-shift on vibration is implemented by mutating the
                # pump's profile dict in-place at the HEALTHY ceiling.
                # The downstream model sees: vibration_amp ≈ 0.3 + d*2.5
                # + N(0, 0.05), with the new pumps living at higher d
                # than the model's training distribution.
                pump, publisher = fleet.add_pump(new_id)
                self._bias_pump(pump)
                existing_ids.add(new_id)
                added += 1
                log.info(
                    "FleetExpansion: added pump %s at tick %d "
                    "(vibration_baseline_shift=%.2f)",
                    new_id, tick, self._shift,
                )
            candidate += 1

    def _bias_pump(self, pump) -> None:
        """Shift a newly-added pump's HEALTHY ceiling so vibration runs hot.

        Mutates the pump's HEALTHY ``StateProfile`` to push degradation
        toward a higher ceiling — vibration_amp scales with degradation
        (factor 2.5 per PLAN.md §2.2), so a ceiling shift of
        ``shift / 2.5`` lands the vibration baseline ``shift`` higher.

        Uses ``Pump.get_profile`` / ``Pump.set_profile`` (added
        2026-05-28 per Gemini Q4 review) rather than reaching into
        ``pump._profiles`` directly — the senior-role-review red flag
        of mutating private attributes from outside the class is now
        gone.
        """
        from simulator.pump import StateProfile

        # Cap the equivalent degradation shift at 0.5 — anything higher
        # collides with the DEGRADING ceiling and we'd lose the
        # "looks healthy, but not the same baseline" contract that the
        # demo wants. The 2.5 factor matches PLAN.md §2.2.
        equiv_d_shift = min(0.5, self._shift / 2.5)
        existing = pump.get_profile(PumpState.HEALTHY)
        # Reuse rate_per_tick + dwell_ticks; only ceiling changes.
        biased = StateProfile(
            rate_per_tick=max(existing.rate_per_tick, 0.001),
            ceiling=min(0.20, existing.ceiling + equiv_d_shift),
            dwell_ticks=existing.dwell_ticks,
        )
        pump.set_profile(PumpState.HEALTHY, biased)


class RealFailure(Scenario):
    """Escalates one pump through HEALTHY → DEGRADING → FAILING → FAILED.

    The PLAN.md §4 Scenario 3 demo names ``P-07`` explicitly. The
    schedule is a ``dict[PumpState, int]`` of "force pump into state X
    at tick T". Other pumps are untouched — they continue to run on
    their default profiles.

    Each scheduled transition uses ``Pump.force_state(state)``, which
    resets the in-state tick counter so dwell-based auto-advancement
    starts fresh from there. Once FAILED, the pump pins to
    ``degradation=1.0`` (per Pump's ``_advance_degradation`` + the
    HEALTHY/FAILED contract) and the scenario stops firing for that
    pump.
    """

    def __init__(
        self,
        *,
        target_pump_id: str = DEFAULT_REAL_FAILURE_TARGET,
        schedule: Optional[dict[PumpState, int]] = None,
        seed: Optional[int] = None,
    ) -> None:
        super().__init__(seed=seed)
        if not isinstance(target_pump_id, str) or not target_pump_id:
            raise ValueError(
                f"target_pump_id must be a non-empty string, got {target_pump_id!r}"
            )
        schedule = (
            dict(DEFAULT_REAL_FAILURE_SCHEDULE) if schedule is None else dict(schedule)
        )
        # Reject ticks that overlap — one transition per scheduled tick
        # is what the demo describes. Two simultaneous transitions on
        # the same pump would race within the same apply() call.
        if len(set(schedule.values())) != len(schedule.values()):
            raise ValueError(
                f"schedule has duplicate ticks: {schedule}"
            )
        # Reject negative ticks.
        for state, tick in schedule.items():
            if tick < 0:
                raise ValueError(
                    f"schedule tick for {state} must be non-negative, got {tick}"
                )
        # Reject unknown PumpStates in the schedule (programmer error).
        for state in schedule:
            if not isinstance(state, PumpState):
                raise TypeError(
                    f"schedule key must be PumpState, got {type(state).__name__}"
                )
        self._target = target_pump_id
        # Invert for O(1) lookup: tick -> state.
        self._by_tick: dict[int, PumpState] = {t: s for s, t in schedule.items()}
        self._schedule = schedule
        # Track applied transitions so a tick repeat (impossible today,
        # but cheap insurance) is a no-op.
        self._applied: set[int] = set()

    @property
    def target_pump_id(self) -> str:
        return self._target

    @property
    def schedule(self) -> dict[PumpState, int]:
        return dict(self._schedule)

    async def apply(self, fleet: "Fleet", tick: int) -> None:
        target_state = self._by_tick.get(tick)
        if target_state is None or tick in self._applied:
            return
        try:
            pump = fleet.get_pump(self._target)
        except KeyError as e:
            raise ScenarioError(
                f"RealFailure target pump {self._target!r} is not in the fleet"
            ) from e
        pump.force_state(target_state)
        self._applied.add(tick)
        log.info(
            "RealFailure: forced pump %s into %s at tick %d",
            self._target, target_state.value, tick,
        )


def make_scenario(config: SimulatorConfig) -> Scenario:
    """Build the right ``Scenario`` for a ``SimulatorConfig``.

    All four ``ScenarioKind`` values dispatch to a concrete class —
    including ``HEALTHY``, which gets a ``HealthyScenario`` no-op so the
    runner always has a controller task to spawn. Mirrors the
    ``make_publisher`` pattern in ``simulator.publisher``.

    Defaults are baked into each Scenario class (see the
    ``DEFAULT_*`` module constants); the YAML config picks *which*
    scenario, not its parameters. Parametric YAML support is deferred
    until the drift/model session shows what magnitudes actually
    exercise the detector — see context/simulator.md open question and
    ADR 0004 "Follow-ups".
    """
    # Pass config.fleet.base_seed so any future stochastic scenario
    # is reproducible from the same seed the pumps already use. Per
    # Gemini Q6 (2026-05-28 scenarios review).
    seed = config.fleet.base_seed
    if config.scenario is ScenarioKind.HEALTHY:
        return HealthyScenario(seed=seed)
    if config.scenario is ScenarioKind.SEASONAL_DRIFT:
        return SeasonalDrift(seed=seed)
    if config.scenario is ScenarioKind.FLEET_EXPANSION:
        return FleetExpansion(seed=seed)
    if config.scenario is ScenarioKind.REAL_FAILURE:
        return RealFailure(seed=seed)
    raise ScenarioError(  # pragma: no cover — guarded by ScenarioKind enum
        f"unknown scenario kind: {config.scenario!r}"
    )


__all__ = [
    # Defaults
    "DEFAULT_SEASONAL_PERIOD_TICKS",
    "DEFAULT_SEASONAL_AMPLITUDE_C",
    "DEFAULT_EXPANSION_TICK",
    "DEFAULT_EXPANSION_NEW_COUNT",
    "DEFAULT_EXPANSION_VIBRATION_SHIFT",
    "DEFAULT_REAL_FAILURE_TARGET",
    "DEFAULT_REAL_FAILURE_SCHEDULE",
    # ABC + concretes
    "Scenario",
    "HealthyScenario",
    "SeasonalDrift",
    "FleetExpansion",
    "RealFailure",
    # Factory + error
    "make_scenario",
    "ScenarioError",
]
