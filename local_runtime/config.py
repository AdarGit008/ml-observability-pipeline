"""Local runtime configuration: schema, loader, defaults.

The local subscriber/scorer service has two pieces of state that
need configuring:

1. **MQTT broker URL** — where the simulator publishes. Default points
   at the docker-compose Mosquitto on ``mqtt://localhost:1883``.
2. **InfluxDB destination** — URL, token, org, and bucket. Defaults
   match the docker-compose InfluxDB v2 service (``localhost:8086``,
   organization ``ml-obs``, bucket ``pump_telemetry``).

The same loader-style schema as ``simulator/config.py`` so a future
session can lift the helpers into a shared module without churn. We
deliberately do NOT share a config file with the simulator — the two
processes have different concerns (publishing vs. consuming) and
sharing a file would tangle the schemas. ``context/local_runtime.md``
calls this out as a "what we don't do" guarantee.

Tick semantics: ``tick_seconds`` matches the simulator's publish
cadence — it's how we derive the window size (300s / tick_seconds =
window depth in samples). Keeping this in the local runtime's config
rather than asking the broker is the right call because a misconfigured
simulator is something the local runtime should still produce sensible
output for: if the simulator drifts off its 2-second cadence, the
window depth here is unaffected and we just see drift in the per-pump
rolling stats.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


# Rolling feature window length, in seconds. PLAN.md §2.3 specifies
# "5-minute rolling mean and std of vibration and bearing temp" — the
# 5 minutes IS the window. Tick-to-samples conversion lives in
# ``FeatureWindow``; the seconds-level value is the contract.
FEATURE_WINDOW_SECONDS: float = 300.0

# PSI rolling window length, in seconds. PLAN.md §2.7 and
# ``context/_interfaces.md`` §"PSI parameters" pin this at 1 hour per
# pump. Tick-to-samples conversion lives in the service orchestrator;
# the seconds-level value is the contract. Locked in ADR 0007.
PSI_WINDOW_SECONDS: float = 3600.0

# PSI compute cadence, in seconds. ADR 0005 §Addendum 2026-05-29 Q3
# carried this as an open question; ADR 0007 resolves it as "every Nth
# tick where N corresponds to ~once per minute." At the default 2s
# tick, that's every 30 ticks. The seconds-level value here is the
# contract; ticks are derived via ``LocalRuntimeConfig.psi_period_ticks``.
PSI_COMPUTE_EVERY_SECONDS: float = 60.0


@dataclass(frozen=True)
class MqttConfig:
    """Where we subscribe.

    Wildcard topic is hard-coded to ``factory/pumps/+/telemetry`` per
    ``context/_interfaces.md`` — there's no scenario where a local
    subscriber would want a different topic, and exposing it as YAML
    would let it drift out of sync with the simulator silently.
    """

    url: str
    client_id: str


@dataclass(frozen=True)
class InfluxConfig:
    """InfluxDB v2 connection parameters.

    All four fields are required. ``token`` is loaded from an env var
    (``INFLUX_TOKEN``) by default so the YAML can be committed without
    secrets; if the YAML supplies a literal token, that wins.
    """

    url: str
    token: str
    org: str
    bucket: str


@dataclass(frozen=True)
class LocalRuntimeConfig:
    """Top-level config for ``python -m local_runtime``."""

    mqtt: MqttConfig
    influx: InfluxConfig
    tick_seconds: float

    @property
    def window_samples(self) -> int:
        """Window length in samples = ceil(FEATURE_WINDOW_SECONDS / tick_seconds).

        At the default 2-second tick this gives 150 samples. The ceil
        is so non-integer tick rates (e.g., 3.5s for debugging) still
        produce a window long enough to cover the full 5 minutes.
        """
        return max(1, math.ceil(FEATURE_WINDOW_SECONDS / self.tick_seconds))

    @property
    def psi_window_samples(self) -> int:
        """PSI window length in samples = ceil(PSI_WINDOW_SECONDS / tick_seconds).

        At the default 2-second tick this gives 1800 samples = exactly
        1 hour of wall clock, per PLAN.md §2.7's "rolling 1-hour window
        per pump."
        """
        return max(1, math.ceil(PSI_WINDOW_SECONDS / self.tick_seconds))

    @property
    def psi_period_ticks(self) -> int:
        """How many ticks between PSI computations.

        Derived from ``PSI_COMPUTE_EVERY_SECONDS``. At the default 2s
        tick this gives 30 ticks ~ once per minute. See ADR 0007 for
        the cadence rationale (every-tick = wasted CPU, separate
        measurement = schema churn; every-Nth was the middle path).
        """
        return max(1, math.ceil(PSI_COMPUTE_EVERY_SECONDS / self.tick_seconds))


class ConfigError(Exception):
    """Raised when YAML fails schema validation.

    Mirrors ``simulator.config.ConfigError`` — inherits from
    ``Exception`` (not ``ValueError``) so ``except ValueError`` in
    caller code doesn't accidentally swallow it.
    """


# -- Schema constants -----------------------------------------------------

_TOP_LEVEL_KEYS = {"mqtt", "influx", "tick_seconds"}
_MQTT_KEYS = {"url", "client_id"}
_INFLUX_KEYS = {"url", "token", "org", "bucket"}

_TICK_MIN = 0.1
_TICK_MAX = 60.0


# -- Public API -----------------------------------------------------------


def load_config(path: str | os.PathLike[str]) -> LocalRuntimeConfig:
    """Parse a YAML file and return a validated ``LocalRuntimeConfig``.

    Raises:
        ConfigError: file missing, unparseable, or schema violation.
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


# -- Internals ------------------------------------------------------------


def _validate(raw: dict[str, Any]) -> LocalRuntimeConfig:
    _assert_exact_keys(raw, _TOP_LEVEL_KEYS, "top-level")

    mqtt = _validate_mqtt(raw["mqtt"])
    influx = _validate_influx(raw["influx"])
    tick = _as_float(raw["tick_seconds"], "tick_seconds")
    if not _TICK_MIN <= tick <= _TICK_MAX:
        raise ConfigError(
            f"tick_seconds must be in [{_TICK_MIN}, {_TICK_MAX}], got {tick}"
        )

    return LocalRuntimeConfig(mqtt=mqtt, influx=influx, tick_seconds=tick)


def _validate_mqtt(raw: Any) -> MqttConfig:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"`mqtt` must be a mapping, got {type(raw).__name__}"
        )
    _assert_exact_keys(raw, _MQTT_KEYS, "mqtt")
    return MqttConfig(
        url=_as_non_empty_str(raw["url"], "mqtt.url"),
        client_id=_as_non_empty_str(raw["client_id"], "mqtt.client_id"),
    )


def _validate_influx(raw: Any) -> InfluxConfig:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"`influx` must be a mapping, got {type(raw).__name__}"
        )
    _assert_exact_keys(raw, _INFLUX_KEYS, "influx")

    # Token may use the form ``${ENV_VAR}`` to defer to an env var so the
    # YAML can be committed without secrets. A bare value is accepted
    # verbatim. Empty after substitution is a hard error: a downstream
    # InfluxDB write with an empty token would fail with a generic 401,
    # but a clear error at load time saves a debugging round trip.
    token_raw = _as_non_empty_str(raw["token"], "influx.token")
    token = _resolve_env(token_raw)
    if not token:
        raise ConfigError(
            f"influx.token resolved to empty string "
            f"(env var referenced by {token_raw!r} is unset or empty)"
        )

    return InfluxConfig(
        url=_as_non_empty_str(raw["url"], "influx.url"),
        token=token,
        org=_as_non_empty_str(raw["org"], "influx.org"),
        bucket=_as_non_empty_str(raw["bucket"], "influx.bucket"),
    )


def _resolve_env(value: str) -> str:
    """Resolve ``${VAR}`` to ``os.environ['VAR']``; pass through otherwise.

    Only the exact form ``${VAR}`` (entire string) is substituted —
    interpolation in the middle of a string would invite shell-style
    surprises this config doesn't need.
    """
    if value.startswith("${") and value.endswith("}"):
        var = value[2:-1]
        return os.environ.get(var, "")
    return value


def _assert_exact_keys(raw: dict[str, Any], expected: set[str], where: str) -> None:
    actual = set(raw.keys())
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append(f"missing keys: {sorted(missing)}")
        if unknown:
            parts.append(f"unknown keys: {sorted(unknown)}")
        raise ConfigError(f"{where} schema mismatch -- " + "; ".join(parts))


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{field} must be a number, got {type(value).__name__}"
        )
    return float(value)


def _as_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value
