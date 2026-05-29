"""Tests for local_runtime.config.load_config.

Mirrors simulator/tests/test_config.py patterns — strict schema,
unknown-key rejection, type coercion guards, ${ENV_VAR} substitution
for the InfluxDB token.
"""

from __future__ import annotations

import math

import pytest

from local_runtime.config import (
    FEATURE_WINDOW_SECONDS,
    ConfigError,
    LocalRuntimeConfig,
    load_config,
)


def _write_yaml(tmp_path, content: str):
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


_VALID_YAML = """\
mqtt:
  url: "mqtt://localhost:1883"
  client_id: "local-runtime"
influx:
  url: "http://localhost:8086"
  token: "literal-token"
  org: "ml-obs"
  bucket: "pump_telemetry"
tick_seconds: 2.0
"""


def test_load_valid_config(tmp_path):
    p = _write_yaml(tmp_path, _VALID_YAML)
    cfg = load_config(p)
    assert isinstance(cfg, LocalRuntimeConfig)
    assert cfg.mqtt.url == "mqtt://localhost:1883"
    assert cfg.mqtt.client_id == "local-runtime"
    assert cfg.influx.url == "http://localhost:8086"
    assert cfg.influx.token == "literal-token"
    assert cfg.influx.org == "ml-obs"
    assert cfg.influx.bucket == "pump_telemetry"
    assert cfg.tick_seconds == 2.0


def test_window_samples_derived_from_tick(tmp_path):
    """window_samples = ceil(FEATURE_WINDOW_SECONDS / tick_seconds)."""
    p = _write_yaml(tmp_path, _VALID_YAML)
    cfg = load_config(p)
    expected = math.ceil(FEATURE_WINDOW_SECONDS / 2.0)
    assert cfg.window_samples == expected
    assert cfg.window_samples == 150  # 5 minutes @ 2s tick


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_empty_file_raises(tmp_path):
    p = _write_yaml(tmp_path, "")
    with pytest.raises(ConfigError, match="empty"):
        load_config(p)


def test_unknown_top_level_key_raises(tmp_path):
    yaml = _VALID_YAML + "extra_key: 1\n"
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(p)


def test_missing_top_level_key_raises(tmp_path):
    yaml = _VALID_YAML.replace("tick_seconds: 2.0\n", "")
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="missing keys"):
        load_config(p)


def test_tick_seconds_out_of_range_raises(tmp_path):
    yaml = _VALID_YAML.replace("tick_seconds: 2.0", "tick_seconds: 0.0")
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="tick_seconds must be in"):
        load_config(p)


def test_tick_seconds_bool_rejected(tmp_path):
    """Reject booleans as tick_seconds (a Python gotcha — bool is int)."""
    yaml = _VALID_YAML.replace("tick_seconds: 2.0", "tick_seconds: true")
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="tick_seconds must be a number"):
        load_config(p)


def test_env_var_token_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_RUNTIME_TEST_TOKEN", "real-token-xyz")
    yaml = _VALID_YAML.replace(
        'token: "literal-token"',
        'token: "${LOCAL_RUNTIME_TEST_TOKEN}"',
    )
    p = _write_yaml(tmp_path, yaml)
    cfg = load_config(p)
    assert cfg.influx.token == "real-token-xyz"


def test_env_var_token_unset_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_RUNTIME_TEST_TOKEN", raising=False)
    yaml = _VALID_YAML.replace(
        'token: "literal-token"',
        'token: "${LOCAL_RUNTIME_TEST_TOKEN}"',
    )
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="resolved to empty"):
        load_config(p)


def test_empty_mqtt_url_raises(tmp_path):
    yaml = _VALID_YAML.replace(
        'url: "mqtt://localhost:1883"', 'url: ""'
    )
    p = _write_yaml(tmp_path, yaml)
    with pytest.raises(ConfigError, match="mqtt.url must be a non-empty string"):
        load_config(p)
