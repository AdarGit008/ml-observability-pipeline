"""Pump physical model + lifecycle state machine.

Implements PLAN.md §2.2:

    bearing_temp   = ambient + 0.02 * RPM + degradation * 15  + N(0, 0.5)
    vibration_amp  = 0.3            + degradation * 2.5       + N(0, 0.05)
    motor_current  = 4.0            + degradation * 1.2       + N(0, 0.1)
    RPM            = setpoint                                 + N(0, 5)

`degradation ∈ [0, 1]`. State machine per pump:
HEALTHY → DEGRADING → FAILING → FAILED with configurable dwell times.

`.step()` returns a telemetry dict matching context/_interfaces.md. MQTT
publishing lives in a later session — this module is pure simulation.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

# Telemetry dict shape (per context/_interfaces.md)
TELEMETRY_KEYS = (
    "pump_id",
    "ts",
    "vibration_amp",
    "bearing_temp",
    "motor_current",
    "rpm",
)

_PUMP_ID_RE = re.compile(r"^P-\d{2}$")


class PumpState(str, Enum):
    """Pump lifecycle states. Order matters: this is the forward sequence."""

    HEALTHY = "HEALTHY"
    DEGRADING = "DEGRADING"
    FAILING = "FAILING"
    FAILED = "FAILED"


# Forward-transition sequence used by automatic dwell-based advancement.
_FORWARD_SEQUENCE = (
    PumpState.HEALTHY,
    PumpState.DEGRADING,
    PumpState.FAILING,
    PumpState.FAILED,
)


@dataclass(frozen=True)
class StateProfile:
    """How `degradation` evolves and how long the pump dwells in this state.

    - `rate_per_tick`: amount added to `degradation` on each `.step()`.
    - `ceiling`: degradation is clamped to this value while in this state.
      (`FAILED` pins degradation to exactly 1.0 — see `Pump._advance_degradation`.)
    - `dwell_ticks`: after this many ticks in this state, auto-advance to the
      next state in `_FORWARD_SEQUENCE`. `None` means "stay indefinitely" — the
      natural choice for `FAILED`.
    """

    rate_per_tick: float
    ceiling: float
    dwell_ticks: Optional[int]


# Defaults chosen for first-principles realism (per simulator.md open question):
#   - HEALTHY: degradation noise around 0, long dwell measured in real days.
#   - DEGRADING: ~3 minutes (90 ticks @ 2s) to reach the 0.30 ceiling.
#   - FAILING:  ~5 minutes (150 ticks @ 2s) of accelerating wear up to 0.85.
#   - FAILED:   pinned to 1.0, never auto-leaves.
# All overrideable via the Pump constructor.
DEFAULT_PROFILES: dict[PumpState, StateProfile] = {
    PumpState.HEALTHY: StateProfile(
        rate_per_tick=0.0, ceiling=0.05, dwell_ticks=43_200  # ~24h @ 2s/tick
    ),
    PumpState.DEGRADING: StateProfile(
        rate_per_tick=0.0015, ceiling=0.30, dwell_ticks=200
    ),
    PumpState.FAILING: StateProfile(
        rate_per_tick=0.0040, ceiling=0.85, dwell_ticks=200
    ),
    PumpState.FAILED: StateProfile(
        rate_per_tick=0.0, ceiling=1.0, dwell_ticks=None
    ),
}


class Pump:
    """Single simulated industrial pump.

    Parameters
    ----------
    pump_id:
        Fleet identifier, must match ``P-NN`` (zero-padded).
    ambient:
        Ambient temperature in °C. Feeds the bearing-temp equation.
    setpoint:
        Target RPM. Actual RPM is setpoint + N(0, 5).
    seed:
        Optional integer seed for the pump's private RNG. Two pumps with the
        same seed and same call sequence will produce identical telemetry.
    initial_state:
        Starting lifecycle state. Defaults to HEALTHY.
    profiles:
        Optional override map for per-state ``StateProfile``s. Missing keys
        fall back to ``DEFAULT_PROFILES``.
    initial_degradation:
        Starting value of ``degradation``. Useful for tests. Clamped to [0, 1].
    """

    def __init__(
        self,
        pump_id: str,
        *,
        ambient: float = 22.0,
        setpoint: float = 1800.0,
        seed: Optional[int] = None,
        initial_state: PumpState = PumpState.HEALTHY,
        profiles: Optional[dict[PumpState, StateProfile]] = None,
        initial_degradation: float = 0.0,
    ) -> None:
        if not isinstance(pump_id, str) or not _PUMP_ID_RE.match(pump_id):
            raise ValueError(
                f"pump_id must match 'P-NN' (zero-padded), got {pump_id!r}"
            )
        if not 0.0 <= initial_degradation <= 1.0:
            raise ValueError(
                f"initial_degradation must be in [0, 1], got {initial_degradation}"
            )

        self._pump_id = pump_id
        self._ambient = float(ambient)
        self._setpoint = float(setpoint)
        self._rng = random.Random(seed)
        self._state: PumpState = initial_state
        self._ticks_in_state: int = 0
        self._degradation: float = float(initial_degradation)

        # Build the per-state profile map: caller overrides win, defaults fill gaps.
        merged: dict[PumpState, StateProfile] = dict(DEFAULT_PROFILES)
        if profiles:
            merged.update(profiles)
        self._profiles: dict[PumpState, StateProfile] = merged

        # If we started directly in FAILED, snap degradation to 1.0 to match
        # the "pinned" semantic. Avoids needing a real .step() to enter the
        # pinned regime in tests.
        if self._state is PumpState.FAILED:
            self._degradation = 1.0

    # -- Public read-only properties ----------------------------------------

    @property
    def pump_id(self) -> str:
        return self._pump_id

    @property
    def state(self) -> PumpState:
        return self._state

    @property
    def degradation(self) -> float:
        return self._degradation

    @property
    def ticks_in_state(self) -> int:
        return self._ticks_in_state

    # -- State control ------------------------------------------------------

    def force_state(self, state: PumpState) -> None:
        """Manually transition to ``state``.

        Resets the in-state tick counter. Used by scenario scripts (drift
        demos drive transitions deterministically rather than waiting for
        dwell timers).

        Snaps degradation to 1.0 when entering FAILED so the FAILED-pinned
        invariant holds immediately, not just after the first .step().
        """

        if not isinstance(state, PumpState):
            raise TypeError(f"state must be a PumpState, got {type(state).__name__}")
        self._state = state
        self._ticks_in_state = 0
        if state is PumpState.FAILED:
            self._degradation = 1.0

    # -- Tick ---------------------------------------------------------------

    def step(self, now: Optional[datetime] = None) -> dict:
        """Advance one tick and return a telemetry reading.

        The order is: (1) advance degradation under the current state's rule,
        (2) sample sensor noise, (3) check whether dwell elapsed → auto-advance.
        Step (3) happens AFTER emission so a tick emitted "in state X" still
        reflects X's noise envelope; the transition takes effect on the NEXT
        tick.

        Parameters
        ----------
        now:
            Timestamp to stamp on the reading. Defaults to ``datetime.now(UTC)``.
            Injectable for deterministic tests.

        Returns
        -------
        dict
            Telemetry payload matching context/_interfaces.md exactly:
            ``{pump_id, ts, vibration_amp, bearing_temp, motor_current, rpm}``.
        """

        self._advance_degradation()
        reading = self._sample(now)
        self._ticks_in_state += 1
        self._maybe_auto_transition()
        return reading

    # -- Internals ----------------------------------------------------------

    def _advance_degradation(self) -> None:
        profile = self._profiles[self._state]
        if self._state is PumpState.FAILED:
            # Pinned. Even if a caller tinkered with profiles, FAILED stays at 1.
            self._degradation = 1.0
            return
        new_value = self._degradation + profile.rate_per_tick
        # Clamp to the per-state ceiling AND the global [0, 1] invariant.
        self._degradation = max(0.0, min(profile.ceiling, new_value, 1.0))

    def _sample(self, now: Optional[datetime]) -> dict:
        rng = self._rng
        d = self._degradation

        rpm = self._setpoint + rng.gauss(0.0, 5.0)
        bearing_temp = self._ambient + 0.02 * rpm + d * 15.0 + rng.gauss(0.0, 0.5)
        vibration_amp = 0.3 + d * 2.5 + rng.gauss(0.0, 0.05)
        motor_current = 4.0 + d * 1.2 + rng.gauss(0.0, 0.1)

        ts = now if now is not None else datetime.now(timezone.utc)
        return {
            "pump_id": self._pump_id,
            "ts": _iso8601_ms(ts),
            "vibration_amp": float(vibration_amp),
            "bearing_temp": float(bearing_temp),
            "motor_current": float(motor_current),
            "rpm": float(rpm),
        }

    def _maybe_auto_transition(self) -> None:
        profile = self._profiles[self._state]
        if profile.dwell_ticks is None:
            return  # terminal-by-config (FAILED, by default)
        if self._ticks_in_state < profile.dwell_ticks:
            return
        next_state = _next_state(self._state)
        if next_state is None:
            return  # already at the end of the forward sequence
        self._state = next_state
        self._ticks_in_state = 0
        if next_state is PumpState.FAILED:
            self._degradation = 1.0


# -- Module helpers ---------------------------------------------------------


def _next_state(state: PumpState) -> Optional[PumpState]:
    """Return the next state in the forward lifecycle, or None at the end."""
    idx = _FORWARD_SEQUENCE.index(state)
    if idx + 1 >= len(_FORWARD_SEQUENCE):
        return None
    return _FORWARD_SEQUENCE[idx + 1]


def _iso8601_ms(ts: datetime) -> str:
    """Format ``ts`` as ISO-8601 UTC with millisecond precision and a 'Z' suffix.

    Matches the example in context/_interfaces.md:
        "2026-05-24T14:32:01.123Z"
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # Truncate microseconds to milliseconds (no rounding — deterministic).
    ms = ts.microsecond // 1000
    return f"{ts.strftime('%Y-%m-%dT%H:%M:%S')}.{ms:03d}Z"
