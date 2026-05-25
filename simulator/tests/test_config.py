"""Unit tests for simulator.config.

Covers the YAML schema (required/unknown-key checks, type and range
validation, enum membership, the broker.tls sub-block's conditional
required/forbidden rule), the example file we ship, and the
``profiles_for`` overlay that the ``demo_mode`` flag drives.

Note on warnings: ``load_config`` is pure schema validation and no longer
emits warnings. The "scenario 'X' is parsed but not yet implemented"
signal that lived here as a ``UserWarning`` (config-yaml session
2026-05-25, Gemini Q1) was moved to ``Fleet.from_config`` during the
2026-05-25 mqtt-publishing session so the loader stays free of
runtime-feasibility concerns (per ADR 0003). The "no warning" tests
below guard against accidentally re-introducing a warning here.
"""

from __future__ import annotations

import warnings as _warnings
from pathlib import Path
from textwrap import dedent

import pytest

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
from simulator.pump import DEFAULT_PROFILES, PumpState

# -- Helpers ---------------------------------------------------------------

VALID_YAML = dedent(
    """\
    fleet:
      pump_count: 15
      setpoint_rpm: 1800.0
      ambient_celsius: 22.0
      base_seed: 0
    scenario: healthy
    broker:
      target: local
      url: "mqtt://localhost:1883"
    demo_mode: false
    """
)

# A YAML snippet for aws-iot targets — used by the tls block tests.
AWS_IOT_YAML = dedent(
    """\
    fleet:
      pump_count: 15
      setpoint_rpm: 1800.0
      ambient_celsius: 22.0
      base_seed: 0
    scenario: healthy
    broker:
      target: aws-iot
      url: "mqtt://my-endpoint.amazonaws.com:8883"
      tls:
        cert_path: "certs/cert.pem"
        key_path: "certs/key.pem"
        ca_path: "certs/ca.pem"
    demo_mode: false
    """
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# -- Happy paths -----------------------------------------------------------


def test_load_minimal_valid(tmp_path: Path):
    cfg = load_config(_write(tmp_path, VALID_YAML))
    assert isinstance(cfg, SimulatorConfig)
    assert cfg.fleet == FleetConfig(
        pump_count=15, setpoint_rpm=1800.0, ambient_celsius=22.0, base_seed=0
    )
    assert cfg.scenario is ScenarioKind.HEALTHY
    assert cfg.broker == BrokerConfig(
        target=BrokerTarget.LOCAL, url="mqtt://localhost:1883", tls=None
    )
    assert cfg.demo_mode is False


def test_example_yaml_round_trips():
    """The example file we ship must parse cleanly — no rot."""
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example)
    assert isinstance(cfg, SimulatorConfig)
    # Spot-check a couple of values from the example.
    assert cfg.fleet.pump_count == 15
    assert cfg.scenario is ScenarioKind.HEALTHY
    assert cfg.demo_mode is False
    # Default local target has no tls (the aws-iot block is commented out).
    assert cfg.broker.target is BrokerTarget.LOCAL
    assert cfg.broker.tls is None


def test_config_is_frozen(tmp_path: Path):
    """SimulatorConfig + nested dataclasses are frozen."""
    cfg = load_config(_write(tmp_path, VALID_YAML))
    with pytest.raises((AttributeError, Exception)):
        cfg.demo_mode = True  # type: ignore[misc]
    with pytest.raises((AttributeError, Exception)):
        cfg.fleet.pump_count = 99  # type: ignore[misc]
    with pytest.raises((AttributeError, Exception)):
        cfg.broker.tls = None  # type: ignore[misc]


def test_tls_config_is_frozen(tmp_path: Path):
    cfg = load_config(_write(tmp_path, AWS_IOT_YAML))
    assert cfg.broker.tls is not None
    with pytest.raises((AttributeError, Exception)):
        cfg.broker.tls.cert_path = "/etc/passwd"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    ["seasonal_drift", "fleet_expansion", "real_failure"],
)
def test_non_healthy_scenarios_parse_without_warning(tmp_path: Path, value: str):
    """Per ADR 0003 (mqtt-publishing session 2026-05-25): non-healthy
    scenarios parse cleanly without ANY warning at load time. The
    "parsed but not implemented" signal moved to ``Fleet.from_config`` so
    the loader is pure schema validation. Guards against re-introducing a
    UserWarning here by accident.
    """
    yaml_text = VALID_YAML.replace("scenario: healthy", f"scenario: {value}")
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")  # any warning -> test failure
        cfg = load_config(_write(tmp_path, yaml_text))
    assert cfg.scenario.value == value


def test_healthy_scenario_emits_no_warning(tmp_path: Path):
    """Belt-and-braces: HEALTHY must also be silent."""
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        load_config(_write(tmp_path, VALID_YAML))


def test_aws_iot_load_emits_no_warning(tmp_path: Path):
    """The aws-iot path is also silent at load time (the NotImplementedError
    fires in Fleet.from_config, not here)."""
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        load_config(_write(tmp_path, AWS_IOT_YAML))


# -- Top-level schema errors -----------------------------------------------


def test_missing_top_level_key(tmp_path: Path):
    yaml_text = VALID_YAML.replace("demo_mode: false\n", "")
    with pytest.raises(ConfigError, match="missing keys.*demo_mode"):
        load_config(_write(tmp_path, yaml_text))


def test_unknown_top_level_key(tmp_path: Path):
    yaml_text = VALID_YAML + "tpyo_field: 1\n"
    with pytest.raises(ConfigError, match="unknown keys.*tpyo_field"):
        load_config(_write(tmp_path, yaml_text))


def test_empty_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="empty"):
        load_config(_write(tmp_path, ""))


def test_non_mapping_top_level(tmp_path: Path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, "- a\n- b\n"))


def test_file_not_found(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_malformed_yaml(tmp_path: Path):
    with pytest.raises(ConfigError, match="YAML parse error"):
        load_config(_write(tmp_path, "fleet: {pump_count: [unterminated\n"))


# -- fleet block errors ----------------------------------------------------


def test_fleet_missing_subkey(tmp_path: Path):
    yaml_text = VALID_YAML.replace("  base_seed: 0\n", "")
    with pytest.raises(ConfigError, match="fleet.*missing keys.*base_seed"):
        load_config(_write(tmp_path, yaml_text))


def test_fleet_unknown_subkey(tmp_path: Path):
    yaml_text = VALID_YAML.replace(
        "  base_seed: 0\n", "  base_seed: 0\n  extra_field: 1\n"
    )
    with pytest.raises(ConfigError, match="fleet.*unknown keys.*extra_field"):
        load_config(_write(tmp_path, yaml_text))


def test_fleet_not_a_mapping(tmp_path: Path):
    yaml_text = dedent(
        """\
        fleet: "oops"
        scenario: healthy
        broker:
          target: local
          url: "mqtt://localhost:1883"
        demo_mode: false
        """
    )
    with pytest.raises(ConfigError, match="`fleet` must be a mapping"):
        load_config(_write(tmp_path, yaml_text))


@pytest.mark.parametrize("bad_count", [0, -1, 101, 9999])
def test_pump_count_out_of_range(tmp_path: Path, bad_count: int):
    yaml_text = VALID_YAML.replace("pump_count: 15", f"pump_count: {bad_count}")
    with pytest.raises(ConfigError, match="pump_count must be in"):
        load_config(_write(tmp_path, yaml_text))


def test_pump_count_rejects_bool(tmp_path: Path):
    """``bool`` is a subclass of ``int`` — make sure ``pump_count: true``
    isn't silently parsed as 1."""
    yaml_text = VALID_YAML.replace("pump_count: 15", "pump_count: true")
    with pytest.raises(ConfigError, match="pump_count must be an integer"):
        load_config(_write(tmp_path, yaml_text))


def test_pump_count_rejects_float(tmp_path: Path):
    yaml_text = VALID_YAML.replace("pump_count: 15", "pump_count: 15.5")
    with pytest.raises(ConfigError, match="pump_count must be an integer"):
        load_config(_write(tmp_path, yaml_text))


@pytest.mark.parametrize("bad_setpoint", [0.0, -10.0, 10_001.0])
def test_setpoint_out_of_range(tmp_path: Path, bad_setpoint: float):
    yaml_text = VALID_YAML.replace("setpoint_rpm: 1800.0", f"setpoint_rpm: {bad_setpoint}")
    with pytest.raises(ConfigError, match="setpoint_rpm must be in"):
        load_config(_write(tmp_path, yaml_text))


def test_setpoint_rpm_accepts_int_form(tmp_path: Path):
    """YAML may parse ``1800`` as int even though our field is float. The
    loader should coerce."""
    yaml_text = VALID_YAML.replace("setpoint_rpm: 1800.0", "setpoint_rpm: 1800")
    cfg = load_config(_write(tmp_path, yaml_text))
    assert cfg.fleet.setpoint_rpm == 1800.0
    assert isinstance(cfg.fleet.setpoint_rpm, float)


@pytest.mark.parametrize("bad_amb", [-50.1, 80.1, 999])
def test_ambient_out_of_range(tmp_path: Path, bad_amb: float):
    yaml_text = VALID_YAML.replace("ambient_celsius: 22.0", f"ambient_celsius: {bad_amb}")
    with pytest.raises(ConfigError, match="ambient_celsius must be in"):
        load_config(_write(tmp_path, yaml_text))


def test_base_seed_rejects_bool(tmp_path: Path):
    yaml_text = VALID_YAML.replace("base_seed: 0", "base_seed: true")
    with pytest.raises(ConfigError, match="base_seed must be an integer"):
        load_config(_write(tmp_path, yaml_text))


# -- scenario errors -------------------------------------------------------


def test_unknown_scenario_string(tmp_path: Path):
    yaml_text = VALID_YAML.replace("scenario: healthy", "scenario: chaos_monkey")
    with pytest.raises(ConfigError, match="scenario must be one of"):
        load_config(_write(tmp_path, yaml_text))


def test_scenario_must_be_string(tmp_path: Path):
    yaml_text = VALID_YAML.replace("scenario: healthy", "scenario: 7")
    with pytest.raises(ConfigError, match="scenario must be a string"):
        load_config(_write(tmp_path, yaml_text))


# -- broker errors ---------------------------------------------------------


def test_unknown_broker_target(tmp_path: Path):
    yaml_text = VALID_YAML.replace("target: local", "target: kafka")
    with pytest.raises(ConfigError, match="broker.target must be one of"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_url_empty(tmp_path: Path):
    yaml_text = VALID_YAML.replace('url: "mqtt://localhost:1883"', 'url: ""')
    with pytest.raises(ConfigError, match="broker.url must be a non-empty string"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_url_whitespace_only(tmp_path: Path):
    yaml_text = VALID_YAML.replace('url: "mqtt://localhost:1883"', 'url: "   "')
    with pytest.raises(ConfigError, match="broker.url must be a non-empty string"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_not_a_mapping(tmp_path: Path):
    yaml_text = dedent(
        """\
        fleet:
          pump_count: 15
          setpoint_rpm: 1800.0
          ambient_celsius: 22.0
          base_seed: 0
        scenario: healthy
        broker: "oops"
        demo_mode: false
        """
    )
    with pytest.raises(ConfigError, match="`broker` must be a mapping"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_missing_target(tmp_path: Path):
    yaml_text = VALID_YAML.replace("  target: local\n", "")
    with pytest.raises(ConfigError, match="broker.*missing keys.*target"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_missing_url(tmp_path: Path):
    yaml_text = VALID_YAML.replace('  url: "mqtt://localhost:1883"\n', "")
    with pytest.raises(ConfigError, match="broker.*missing keys.*url"):
        load_config(_write(tmp_path, yaml_text))


def test_broker_unknown_key(tmp_path: Path):
    yaml_text = VALID_YAML.replace(
        '  url: "mqtt://localhost:1883"\n',
        '  url: "mqtt://localhost:1883"\n  extra: oops\n',
    )
    with pytest.raises(ConfigError, match="broker.*unknown keys.*extra"):
        load_config(_write(tmp_path, yaml_text))


# -- broker.tls — conditional on target ------------------------------------


def test_aws_iot_with_tls_block_parses(tmp_path: Path):
    cfg = load_config(_write(tmp_path, AWS_IOT_YAML))
    assert cfg.broker.target is BrokerTarget.AWS_IOT
    assert cfg.broker.tls == TlsConfig(
        cert_path="certs/cert.pem",
        key_path="certs/key.pem",
        ca_path="certs/ca.pem",
    )


def test_aws_iot_without_tls_raises(tmp_path: Path):
    """The conditional rule: aws-iot REQUIRES the tls sub-block."""
    yaml_text = VALID_YAML.replace("target: local", "target: aws-iot")
    with pytest.raises(
        ConfigError, match="broker.tls is required when broker.target is 'aws-iot'"
    ):
        load_config(_write(tmp_path, yaml_text))


def test_local_with_tls_block_raises(tmp_path: Path):
    """The conditional rule: local FORBIDS the tls sub-block."""
    yaml_text = AWS_IOT_YAML.replace("target: aws-iot", "target: local")
    with pytest.raises(
        ConfigError, match="broker.tls must not be set when broker.target is 'local'"
    ):
        load_config(_write(tmp_path, yaml_text))


def test_tls_must_be_mapping(tmp_path: Path):
    yaml_text = dedent(
        """\
        fleet:
          pump_count: 15
          setpoint_rpm: 1800.0
          ambient_celsius: 22.0
          base_seed: 0
        scenario: healthy
        broker:
          target: aws-iot
          url: "mqtt://endpoint:8883"
          tls: "oops"
        demo_mode: false
        """
    )
    with pytest.raises(ConfigError, match="broker.tls must be a mapping"):
        load_config(_write(tmp_path, yaml_text))


def test_tls_missing_cert_path(tmp_path: Path):
    yaml_text = AWS_IOT_YAML.replace('    cert_path: "certs/cert.pem"\n', "")
    with pytest.raises(ConfigError, match="broker.tls.*missing keys.*cert_path"):
        load_config(_write(tmp_path, yaml_text))


def test_tls_missing_key_path(tmp_path: Path):
    yaml_text = AWS_IOT_YAML.replace('    key_path: "certs/key.pem"\n', "")
    with pytest.raises(ConfigError, match="broker.tls.*missing keys.*key_path"):
        load_config(_write(tmp_path, yaml_text))


def test_tls_missing_ca_path(tmp_path: Path):
    yaml_text = AWS_IOT_YAML.replace('    ca_path: "certs/ca.pem"\n', "")
    with pytest.raises(ConfigError, match="broker.tls.*missing keys.*ca_path"):
        load_config(_write(tmp_path, yaml_text))


def test_tls_unknown_subkey(tmp_path: Path):
    yaml_text = AWS_IOT_YAML.replace(
        '    ca_path: "certs/ca.pem"\n',
        '    ca_path: "certs/ca.pem"\n    extra: oops\n',
    )
    with pytest.raises(ConfigError, match="broker.tls.*unknown keys.*extra"):
        load_config(_write(tmp_path, yaml_text))


@pytest.mark.parametrize("field", ["cert_path", "key_path", "ca_path"])
def test_tls_path_empty(tmp_path: Path, field: str):
    """Each tls path must be a non-empty string. Build the YAML by
    replacing the matching line directly so the test stays robust to
    minor formatting drift in AWS_IOT_YAML."""
    new_lines: list[str] = []
    for line in AWS_IOT_YAML.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(f"{field}:"):
            indent = line[: len(line) - len(stripped)]
            new_lines.append(f'{indent}{field}: ""')
        else:
            new_lines.append(line)
    yaml_text = "\n".join(new_lines)
    with pytest.raises(
        ConfigError, match=f"broker.tls.{field} must be a non-empty string"
    ):
        load_config(_write(tmp_path, yaml_text))


def test_tls_path_wrong_type(tmp_path: Path):
    yaml_text = AWS_IOT_YAML.replace(
        'cert_path: "certs/cert.pem"', "cert_path: 7"
    )
    with pytest.raises(
        ConfigError, match="broker.tls.cert_path must be a non-empty string"
    ):
        load_config(_write(tmp_path, yaml_text))


# -- demo_mode errors ------------------------------------------------------


def test_demo_mode_must_be_bool(tmp_path: Path):
    yaml_text = VALID_YAML.replace("demo_mode: false", "demo_mode: 1")
    with pytest.raises(ConfigError, match="demo_mode must be a boolean"):
        load_config(_write(tmp_path, yaml_text))


# -- profiles_for: the demo_mode overlay -----------------------------------


def _config_with_demo_mode(enabled: bool) -> SimulatorConfig:
    return SimulatorConfig(
        fleet=FleetConfig(
            pump_count=15, setpoint_rpm=1800.0, ambient_celsius=22.0, base_seed=0
        ),
        scenario=ScenarioKind.HEALTHY,
        broker=BrokerConfig(
            target=BrokerTarget.LOCAL, url="mqtt://localhost:1883", tls=None
        ),
        demo_mode=enabled,
    )


def test_profiles_for_demo_mode_off_returns_defaults():
    profiles = profiles_for(_config_with_demo_mode(False))
    assert profiles == DEFAULT_PROFILES


def test_profiles_for_demo_mode_on_overrides_healthy_dwell():
    profiles = profiles_for(_config_with_demo_mode(True))
    assert profiles[PumpState.HEALTHY].dwell_ticks == DEMO_MODE_HEALTHY_DWELL_TICKS


def test_profiles_for_demo_mode_on_preserves_healthy_rate_and_ceiling():
    """Only dwell collapses — rate/ceiling stay so HEALTHY-state envelope is
    still realistic, just briefer."""
    profiles = profiles_for(_config_with_demo_mode(True))
    healthy = profiles[PumpState.HEALTHY]
    assert healthy.rate_per_tick == DEFAULT_PROFILES[PumpState.HEALTHY].rate_per_tick
    assert healthy.ceiling == DEFAULT_PROFILES[PumpState.HEALTHY].ceiling


@pytest.mark.parametrize(
    "state", [PumpState.DEGRADING, PumpState.FAILING, PumpState.FAILED]
)
def test_profiles_for_demo_mode_on_leaves_non_healthy_states_untouched(state: PumpState):
    profiles = profiles_for(_config_with_demo_mode(True))
    assert profiles[state] == DEFAULT_PROFILES[state]


def test_profiles_for_returns_independent_dict():
    """Mutating the returned dict must NOT corrupt DEFAULT_PROFILES."""
    snapshot = dict(DEFAULT_PROFILES)
    profiles = profiles_for(_config_with_demo_mode(False))
    profiles.pop(PumpState.HEALTHY)
    assert DEFAULT_PROFILES == snapshot


def test_profiles_for_can_drive_a_pump_through_demo_mode_arc():
    """End-to-end smoke: a Pump built with demo_mode profiles transitions
    out of HEALTHY within DEMO_MODE_HEALTHY_DWELL_TICKS steps."""
    from simulator.pump import Pump

    profiles = profiles_for(_config_with_demo_mode(True))
    p = Pump("P-01", seed=0, profiles=profiles)
    for _ in range(DEMO_MODE_HEALTHY_DWELL_TICKS + 1):
        p.step()
    assert p.state is not PumpState.HEALTHY


# -- YAML safety (Gemini review, additional observation B) -----------------


def test_yaml_safe_load_rejects_python_tag_attack(tmp_path: Path):
    """``yaml.safe_load`` must reject the classic PyYAML RCE vector. Proves
    the safety property to a reviewer (and guards against an accidental
    swap of safe_load -> load in a future edit). The constructor error
    surfaces as a ConfigError via the existing yaml.YAMLError handler."""
    attack = "!!python/object/apply:os.system ['echo hacked']\n"
    with pytest.raises(ConfigError, match="YAML parse error"):
        load_config(_write(tmp_path, attack))
