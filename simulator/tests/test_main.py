"""Unit tests for simulator.__main__ — specifically the _ShutdownState
state machine added 2026-05-28 (second-Ctrl+C escalation).

The signal-handler installation paths (``loop.add_signal_handler`` /
``signal.signal``) are not tested directly — they're platform-specific
and well-covered by the manual Mosquitto + AWS IoT smoke tests. The
state machine itself is plain Python and worth pinning in unit tests
because the escalation path runs ``os._exit(130)`` in production, which
isn't something we can exercise live.
"""

from __future__ import annotations

from typing import Any

from simulator.__main__ import FORCE_EXIT_CODE, _ShutdownState


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
