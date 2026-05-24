"""Pump-fleet simulator.

Synthetic telemetry for ~15 industrial pumps per PLAN.md §2.2. This package
owns the physical model and (in later sessions) the MQTT publishing layer.
"""

from simulator.pump import Pump, PumpState

__all__ = ["Pump", "PumpState"]
