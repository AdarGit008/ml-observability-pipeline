"""Pump physical model + lifecycle state machine.

Implements PLAN.md §2.2 (with one ADR-tracked deviation on RPM):

    bearing_temp   = ambient + 0.02 * RPM + degradation * 15  + N(0, 0.5)
    vibration_amp  = 0.3            + degradation * 2.5       + N(0, 0.05)
    motor_current  = 4.0            + degradation * 1.2       + N(0, 0.1)
    RPM            = setpoint * (1 - degradation) + N(0, 5 + 15 * degradation)
                     # per ADR 0002 — original spec was RPM = setpoint + N(0, 5),
                     # but that left FAILED pumps emitting healthy RPM, which is
                     # physically implausible (failed pumps slow / seize).

`degradation ∈ [0, 1]`. State machine per pump:
HEALTHY → DEGRADING → FAILING → FAILED with configurable dwell times.

Degradation evolves as a per-state linear ramp toward a per-state ceiling
(see ``DEFAULT_PROFILES``). This is a deliberate simplification — real
mechanical wear follows the P-F (Potential-to-Failure) curve with
exponential acceleration near failure. Linear was chosen for predictable,
testable envelopes and to keep tuning out of the critical path; the
downstream scoring/drift pipeline is what we actually want to exercise.

`.step()` returns a telemetry dict matching context/_interfaces.md. MQTT
publishing lives in a later session — this module is pure simulation.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
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
#
# TODO (config-yaml session): 43_200 ticks for HEALTHY (~24h) is realistic
# but recruiter-hostile — anyone cloning the repo to see the lifecycle would
# wait a full day at default settings. When the YAML loader lands, expose a
# `demo_mode` shortcut that compresses HEALTHY dwell to ~60 ticks so the full
# HEALTHY → FAILED arc unfolds in <5 minutes for a local demo run.
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
    """Single simulated industrial pump."""

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

        merged: dict[PumpState, StateProfile] = dict(DEFAULT_PROFILES)
        if profiles:
            merged.update(profiles)
        self._profiles: dict[PumpState, StateProfile] = merged

        if self._state is PumpState.FAILED:
            self._degradation = 1.0

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

    def force_state(self, state: PumpState) -> None:
        """Manually transition to ``state``. Resets the in-state tick counter."""
        if not isinstance(state, PumpState):
            raise TypeError(f"state must be a PumpState, got {type(state).__name__}")
        self._state = state
        self._ticks_in_state = 0
        if state is PumpState.FAILED:
            self._degradation = 1.0

    def step(self, now: Optional[datetime] = None) -> dict:
        """Advance one tick and return a telemetry reading.

        Order: (1) advance degradation, (2) sample sensor noise, (3) check
        auto-transition. Step (3) happens AFTER emission so a tick "in state X"
        reflects X's envelope; the transition takes effect on the NEXT tick.
        """
        self._advance_degradation()
        reading = self._sample(now)
        self._ticks_in_state += 1
        self._maybe_auto_transition()
        return reading

    def _advance_degradation(self) -> None:
        profile = self._profiles[self._state]
        if self._state is PumpState.FAILED:
            self._degradation = 1.0
            return
        new_value = self._degradation + profile.rate_per_tick
        self._degradation = max(0.0, min(profile.ceiling, new_value, 1.0))

    def _sample(self, now: Optional[datetime]) -> dict:
        rng = self._rng
        d = self._degradation

        # RPM couples to degradation per ADR 0002 (supersedes PLAN.md §2.2):
        # a failing pump slows down, a fully failed pump is near-stationary
        # with high stutter. Original spec was rpm = setpoint + N(0, 5).
        rpm = self._setpoint * (1.0 - d) + rng.gauss(0.0, 5.0 + 15.0 * d)
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
            return
        if self._ticks_in_state < profile.dwell_ticks:
            return
        next_state = _next_state(self._state)
        if next_state is None:
            return
        self._state = next_state
        self._ticks_in_state = 0
        if next_state is PumpState.FAILED:
            self._degradation = 1.0


def _next_state(state: PumpState) -> Optional[PumpState]:
    idx = _FORWARD_SEQUENCE.index(state)
    if idx + 1 >= len(_FORWARD_SEQUENCE):
        return None
    return _FORWARD_SEQUENCE[idx + 1]


def _iso8601_ms(ts: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and a 'Z' suffix."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    ms = ts.microsecond // 1000
    return f"{ts.strftime('%Y-%m-%dT%H:%M:%S')}.{ms:03d}Z"
