"""Pump-fleet simulator.

Synthetic telemetry for ~15 industrial pumps per PLAN.md §2.2. This package
owns the physical model (``pump``), the YAML config schema (``config``),
the MQTT publishing layer (``publisher``), and the asyncio fleet runner
(``runner``).

Run from the command line:

    python -m simulator --config simulator/config.yaml
"""

from simulator.config import (
    DEMO_MODE_HEALTHY_DWELL_TICKS,
    BrokerConfig,
    BrokerTarget,
    ConfigError,
    FleetConfig,
    ScenarioKind,
    SimulatorConfig,
    TlsConfig,
    load_config,
    profiles_for,
)
from simulator.publisher import (
    AwsIotPublisher,
    LocalPublisher,
    Publisher,
    PublisherError,
    make_publisher,
    topic_for,
)
from simulator.pump import Pump, PumpState
from simulator.runner import (
    DEFAULT_TICK_SECONDS,
    INITIAL_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    Fleet,
    pump_id_for,
)

__all__ = [
    # Physical model
    "Pump",
    "PumpState",
    # Config
    "SimulatorConfig",
    "FleetConfig",
    "BrokerConfig",
    "TlsConfig",
    "ScenarioKind",
    "BrokerTarget",
    "ConfigError",
    "load_config",
    "profiles_for",
    "DEMO_MODE_HEALTHY_DWELL_TICKS",
    # Publisher
    "Publisher",
    "LocalPublisher",
    "AwsIotPublisher",
    "PublisherError",
    "make_publisher",
    "topic_for",
    # Runner
    "Fleet",
    "pump_id_for",
    "DEFAULT_TICK_SECONDS",
    "INITIAL_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
]
