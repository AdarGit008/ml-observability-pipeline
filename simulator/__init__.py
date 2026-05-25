"""Pump-fleet simulator.

Synthetic telemetry for ~15 industrial pumps per PLAN.md §2.2. This package
owns the physical model, the YAML config schema, and (in later sessions)
the MQTT publishing layer + scenario runner.
"""

from simulator.config import (
    DEMO_MODE_HEALTHY_DWELL_TICKS,
    BrokerConfig,
    BrokerTarget,
    ConfigError,
    FleetConfig,
    ScenarioKind,
    SimulatorConfig,
    load_config,
    profiles_for,
)
from simulator.pump import Pump, PumpState

__all__ = [
    # Physical model
    "Pump",
    "PumpState",
    # Config
    "SimulatorConfig",
    "FleetConfig",
    "BrokerConfig",
    "ScenarioKind",
    "BrokerTarget",
    "ConfigError",
    "load_config",
    "profiles_for",
    "DEMO_MODE_HEALTHY_DWELL_TICKS",
]
