"""Unit tests for simulator.__main__ — _ShutdownState state machine
(added 2026-05-28 for second-Ctrl+C escalation) and main() exit codes,
including the PUBLISHER_CONFIG_ERROR_CODE=4 path added the same day
per Gemini Q3 (2026-05-27 aws-iot-publisher review).

The signal-handler installation paths (``loop.add_signal_handler`` /
``signal.signal``) are not tested directly — they're platform-specific
and well-covered by the manual Mosquitto + AWS IoT smoke tests. The
state machine and the main()-level exit-code wiring are plain Python
and worth pinning in unit tests.
"""

from __future__ import annotations

import sys
import textwrap
from typing import Any

import pytest

from simulator.__main__ import (
    FORCE_EXIT_CODE,
    PUBLISHER_CONFIG_ERROR_CODE,
    _ShutdownState,
    main,
)
from simulator.publisher import PublisherConfigError


# main()'s asyncio.run(..., loop_factory=...) is Python 3.12+ (the
# loop_factory kwarg was added then). The sandbox runs 3.10; Adar runs
# 3.12+ on Windows. Tests that traverse asyncio.run get skipped on
# older interpreters. Tests that fail in main() before asyncio.run is
# reached (e.g., load_config or Fleet.from_config raising) are unaffected.
_REQUIRES_LOOP_FACTORY = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="asyncio.run(loop_factory=...) requires Python 3.12+",
)


# -- _ShutdownState state machine ------------------------------------------


class _FleetSpy:
    """Records request_shutdown calls without needing real asyncio state."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def request_shutdown(self) -> None:
        self.shutdown_calls += 1


class _ExitSpy:
    """Stand-in for os._exit. Records the code instead of exiting so
    tests can assert on it (the real os._exit would terminate pytest)."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, code: int) -> None:
        self.calls.append(code)


def test_first_call_requests_shutdown_does_not_force():
    fleet = _FleetSpy()
    exit_spy = _ExitSpy()
    state = _ShutdownState(fleet, force_exit=exit_spy)

    assert state.requested is False
    state()

    assert state.requested is True
    assert fleet.shutdown_calls == 1
    assert exit_spy.calls == [], "first call must not force-exit"


def test_second_call_force_exits_with_130():
    fleet = _FleetSpy()
    exit_spy = _ExitSpy()
    state = _ShutdownState(fleet, force_exit=exit_spy)

    state()
    state()

    assert fleet.shutdown_calls == 1, "second call must NOT re-request shutdown"
    assert exit_spy.calls == [FORCE_EXIT_CODE]


def test_third_call_force_exits_again():
    """A held-down Ctrl+C could fire many SIGINTs. Each one after the
    first should force-exit (the real os._exit would have terminated the
    process by then, but the state machine must remain idempotently
    aggressive)."""
    fleet = _FleetSpy()
    exit_spy = _ExitSpy()
    state = _ShutdownState(fleet, force_exit=exit_spy)

    state()  # request
    state()  # force
    state()  # force again (in case force_exit was mocked)

    assert fleet.shutdown_calls == 1
    assert exit_spy.calls == [FORCE_EXIT_CODE, FORCE_EXIT_CODE]


def test_force_exit_code_is_130_posix_convention():
    """130 = 128 + signal number for SIGINT (2). POSIX convention for
    ``$?`` after a Ctrl+C-killed process. Pinned so a future change is a
    conscious decision, not a typo."""
    assert FORCE_EXIT_CODE == 130


def test_fleet_is_only_called_via_request_shutdown_not_via_force():
    """Defensive: a force_exit should NOT also call fleet.request_shutdown
    again — the fleet is already on its way down and won't see the
    second event. (Also helps catch a refactor that accidentally calls
    both on the second invocation.)"""
    fleet = _FleetSpy()
    exit_spy = _ExitSpy()
    state = _ShutdownState(fleet, force_exit=exit_spy)

    for _ in range(5):
        state()

    assert fleet.shutdown_calls == 1, "request_shutdown should fire exactly once"
    assert len(exit_spy.calls) == 4


# -- main() exit code wiring (Gemini Q3 — PublisherConfigError = exit 4) --


def test_publisher_config_error_code_is_4_distinct_from_other_failures():
    """Pin the exit code so CI scripts can rely on the value. 2 was
    config-parse (ConfigError from load_config), 3 was runner-construction
    failure (NotImplementedError), 4 is PublisherConfigError. The three
    must remain distinct."""
    assert PUBLISHER_CONFIG_ERROR_CODE == 4


def _write_valid_local_config(tmp_path) -> str:
    """Write a minimal valid local-target config; return path."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""\
        fleet:
          pump_count: 1
          setpoint_rpm: 1800.0
          ambient_celsius: 22.0
          base_seed: 0
        scenario: healthy
        broker:
          target: local
          url: "mqtt://localhost:1883"
        demo_mode: false
        """))
    return str(p)


def _write_aws_iot_config_with_missing_certs(tmp_path) -> str:
    """Write a valid-on-paper aws-iot config that points at cert files
    that don't exist on disk. Triggers PublisherConfigError at __aenter__
    time from inside the per-pump task."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""\
        fleet:
          pump_count: 1
          setpoint_rpm: 1800.0
          ambient_celsius: 22.0
          base_seed: 0
        scenario: healthy
        broker:
          target: aws-iot
          url: "endpoint.iot.eu-central-1.amazonaws.com"
          tls:
            cert_path: "/nonexistent/P-00.cert.pem"
            key_path:  "/nonexistent/P-00.private.key"
            ca_path:   "/nonexistent/AmazonRootCA1.pem"
        demo_mode: false
        """))
    return str(p)


@_REQUIRES_LOOP_FACTORY
def test_main_returns_4_when_publisher_config_error_propagates_from_run(tmp_path):
    """End-to-end: a valid aws-iot config pointing at missing certs
    triggers PublisherConfigError from inside the per-pump task, which
    Fleet.run re-raises, which main() catches via the asyncio.run
    except clause and converts to exit code 4."""
    config_path = _write_aws_iot_config_with_missing_certs(tmp_path)
    rc = main(["--config", config_path, "--log-level", "ERROR"])
    assert rc == PUBLISHER_CONFIG_ERROR_CODE


def test_main_returns_4_when_publisher_config_error_at_construction(tmp_path, monkeypatch):
    """Bad URL surfaces from _parse_mqtt_url during publisher __init__,
    which happens in Fleet.from_config (before asyncio.run). main()
    catches it on the from_config path, returns 4. This test does NOT
    require Python 3.12 because the failure path doesn't reach
    asyncio.run."""
    config_path = _write_valid_local_config(tmp_path)

    from simulator import publisher as publisher_mod

    def raising_parse(url, default_port):
        raise PublisherConfigError(
            f"could not parse host from MQTT url: {url!r}"
        )
    monkeypatch.setattr(publisher_mod, "_parse_mqtt_url", raising_parse)

    rc = main(["--config", config_path, "--log-level", "ERROR"])
    assert rc == PUBLISHER_CONFIG_ERROR_CODE


def test_main_returns_2_when_config_parse_error(tmp_path):
    """Pin that 2 (config error) is still distinct from 4 (publisher
    config error). A malformed YAML at load time returns 2."""
    p = tmp_path / "bad.yaml"
    p.write_text("not: yaml: at all:\n  - unbalanced [\n")
    rc = main(["--config", str(p), "--log-level", "ERROR"])
    assert rc == 2


def test_main_returns_3_when_scenario_not_implemented(tmp_path):
    """Pin that 3 (NotImplementedError from runner) is still distinct
    from 4. A non-healthy scenario in valid YAML returns 3."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""\
        fleet:
          pump_count: 1
          setpoint_rpm: 1800.0
          ambient_celsius: 22.0
          base_seed: 0
        scenario: seasonal_drift
        broker:
          target: local
          url: "mqtt://localhost:1883"
        demo_mode: false
        """))
    rc = main(["--config", str(p), "--log-level", "ERROR"])
    assert rc == 3
