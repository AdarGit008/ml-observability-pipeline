"""Simulator configuration: schema, loader, and DEFAULT_PROFILES overlay.

This module translates ``simulator/config.yaml`` into a typed
``SimulatorConfig`` and produces a per-state ``StateProfile`` dict that
``Pump`` can consume.

Schema lives here, not in YAML comments, so validation is reproducible and
unknown-key typos are caught at load time. See ``simulator/config.example.yaml``
for the canonical example with inline commentary on every field.

What this module does NOT do (deliberately — those live in later sessions):
- Instantiate ``Pump`` objects or run a fleet.
- Publish telemetry to MQTT.
- Decide which scenario actually runs.

The ``scenario`` and ``broker`` fields are parsed and validated here purely
so the YAML schema is stable from day one; only ``demo_mode`` and
``DEFAULT_PROFILES`` overlay logic is actually wired this session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from simulator.pump import DEFAULT_PROFILES, PumpState, StateProfile

# How many 2-second ticks HEALTHY collapses to in demo_mode (~2 minutes wall
# clock). Chosen so the full HEALTHY -> DEGRADING -> FAILING -> FAILED arc
# completes in under 5 minutes for a fresh local clone — see the TODO in
# simulator/pump.py::DEFAULT_PROFILES that this session retires.
DEMO_MODE_HEALTHY_DWELL_TICKS: int = 60


class ScenarioKind(str, Enum):
    """Scenarios planned in PLAN.md. Only ``healthy`` is wired this session.

    The other three are accepted at load time so demo manifests can be
    authored ahead of the scenario controller; trying to *run* a non-healthy
    scenario will fail in the (future) scenario-runner module, not here.
    """

    HEALTHY = "healthy"
    SEASONAL_DRIFT = "seasonal_drift"
    FLEET_EXPANSION = "fleet_expansion"
    REAL_FAILURE = "real_failure"


class BrokerTarget(str, Enum):
    """MQTT broker the simulator publishes to. Same code path either side
    (per simulator.md), so this is the only switch the runtime needs.
    """

    LOCAL = "local"
    AWS_IOT = "aws-iot"


@dataclass(frozen=True)
class FleetConfig:
    """Fleet-wide simulation parameters."""

    pump_count: int
    setpoint_rpm: float
    ambient_celsius: float
    base_seed: int


@dataclass(frozen=True)
class BrokerConfig:
    """Where telemetry will be published (no client is wired this session)."""

    target: BrokerTarget
    url: str


@dataclass(frozen=True)
class SimulatorConfig:
    """Top-level config — what a YAML file deserializes into."""

    fleet: FleetConfig
    scenario: ScenarioKind
    broker: BrokerConfig
    demo_mode: bool


class ConfigError(ValueError):
    """Raised when YAML fails schema validation. Subclass of ValueError so
    callers can catch ``ValueError`` if they want a coarser net."""


# -- Schema constants -----------------------------------------------------

_TOP_LEVEL_KEYS = {"fleet", "scenario", "broker", "demo_mode"}
_FLEET_KEYS = {"pump_count", "setpoint_rpm", "ambient_celsius", "base_seed"}
_BROKER_KEYS = {"target", "url"}

# Validation ranges. Chosen to catch typos (e.g. pump_count: 1500) while
# leaving room for non-default but legitimate values. None of these are
# physical limits — the Pump class enforces no fleet-level cap of its own.
_PUMP_COUNT_MIN = 1
_PUMP_COUNT_MAX = 50
_SETPOINT_MIN = 1.0
_SETPOINT_MAX = 10_000.0
_AMBIENT_MIN = -50.0
_AMBIENT_MAX = 80.0


# -- Public API -----------------------------------------------------------


def load_config(path: str | os.PathLike[str]) -> SimulatorConfig:
    """Parse a YAML file and return a validated ``SimulatorConfig``.

    Raises:
        ConfigError: file missing, unparseable, or any schema violation.
            The message is precise enough to fix the YAML without a debugger.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except OSError as e:
        raise ConfigError(f"could not read {p}: {e}") from e

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {p}: {e}") from e

    if raw is None:
        raise ConfigError(f"config file is empty: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    return _validate(raw)


def profiles_for(config: SimulatorConfig) -> dict[PumpState, StateProfile]:
    """Return the per-state ``StateProfile`` dict implied by ``config``.

    With ``demo_mode: false`` (the default), this returns a fresh copy of
    ``DEFAULT_PROFILES`` unchanged.

    With ``demo_mode: true``, HEALTHY's ``dwell_ticks`` is overridden to
    ``DEMO_MODE_HEALTHY_DWELL_TICKS`` so a fresh clone exercises the full
    lifecycle in a few minutes. DEGRADING/FAILING/FAILED are untouched —
    those dwells are already short and changing them would distort the
    drift signal the demo is supposed to showcase.
    """
    profiles: dict[PumpState, StateProfile] = dict(DEFAULT_PROFILES)
    if config.demo_mode:
        healthy_default = DEFAULT_PROFILES[PumpState.HEALTHY]
        profiles[PumpState.HEALTHY] = StateProfile(
            rate_per_tick=healthy_default.rate_per_tick,
            ceiling=healthy_default.ceiling,
            dwell_ticks=DEMO_MODE_HEALTHY_DWELL_TICKS,
        )
    return profiles


# -- Internals ------------------------------------------------------------


def _validate(raw: dict[str, Any]) -> SimulatorConfig:
    _assert_exact_keys(raw, _TOP_LEVEL_KEYS, "top-level")

    fleet_raw = raw["fleet"]
    if not isinstance(fleet_raw, dict):
        raise ConfigError(
            f"`fleet` must be a mapping, got {type(fleet_raw).__name__}"
        )
    _assert_exact_keys(fleet_raw, _FLEET_KEYS, "fleet")

    pump_count = _as_int(fleet_raw["pump_count"], "fleet.pump_count")
    if not _PUMP_COUNT_MIN <= pump_count <= _PUMP_COUNT_MAX:
        raise ConfigError(
            f"fleet.pump_count must be in "
            f"[{_PUMP_COUNT_MIN}, {_PUMP_COUNT_MAX}], got {pump_count}"
        )

    setpoint = _as_float(fleet_raw["setpoint_rpm"], "fleet.setpoint_rpm")
    if not _SETPOINT_MIN <= setpoint <= _SETPOINT_MAX:
        raise ConfigError(
            f"fleet.setpoint_rpm must be in "
            f"[{_SETPOINT_MIN}, {_SETPOINT_MAX}], got {setpoint}"
        )

    ambient = _as_float(fleet_raw["ambient_celsius"], "fleet.ambient_celsius")
    if not _AMBIENT_MIN <= ambient <= _AMBIENT_MAX:
        raise ConfigError(
            f"fleet.ambient_celsius must be in "
            f"[{_AMBIENT_MIN}, {_AMBIENT_MAX}], got {ambient}"
        )

    base_seed = _as_int(fleet_raw["base_seed"], "fleet.base_seed")

    fleet = FleetConfig(
        pump_count=pump_count,
        setpoint_rpm=setpoint,
        ambient_celsius=ambient,
        base_seed=base_seed,
    )

    scenario = _enum_from(raw["scenario"], ScenarioKind, "scenario")

    broker_raw = raw["broker"]
    if not isinstance(broker_raw, dict):
        raise ConfigError(
            f"`broker` must be a mapping, got {type(broker_raw).__name__}"
        )
    _assert_exact_keys(broker_raw, _BROKER_KEYS, "broker")

    target = _enum_from(broker_raw["target"], BrokerTarget, "broker.target")

    url = broker_raw["url"]
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("broker.url must be a non-empty string")

    broker = BrokerConfig(target=target, url=url)

    demo_mode_raw = raw["demo_mode"]
    if not isinstance(demo_mode_raw, bool):
        raise ConfigError(
            f"demo_mode must be a boolean, got {type(demo_mode_raw).__name__}"
        )

    return SimulatorConfig(
        fleet=fleet,
        scenario=scenario,
        broker=broker,
        demo_mode=demo_mode_raw,
    )


def _assert_exact_keys(raw: dict[str, Any], expected: set[str], where: str) -> None:
    """Reject unknown keys (catches typos) and missing keys (no silent defaults)."""
    actual = set(raw.keys())
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append(f"missing keys: {sorted(missing)}")
        if unknown:
            parts.append(f"unknown keys: {sorted(unknown)}")
        raise ConfigError(f"{where} schema mismatch — " + "; ".join(parts))


def _as_int(value: Any, field: str) -> int:
    # ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)``
    # is True. Reject booleans explicitly so ``pump_count: true`` doesn't
    # silently parse as ``1``.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{field} must be an integer, got {type(value).__name__}"
        )
    return value


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{field} must be a number, got {type(value).__name__}"
        )
    return float(value)


def _enum_from(value: Any, enum_cls: type[Enum], field: str) -> Any:
    if not isinstance(value, str):
        raise ConfigError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(repr(m.value) for m in enum_cls)
        raise ConfigError(
            f"{field} must be one of [{valid}], got {value!r}"
        ) from None
