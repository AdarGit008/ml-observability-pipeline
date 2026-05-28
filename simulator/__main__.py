"""Run the simulator fleet from the command line.

Usage:
    python -m simulator [--config simulator/config.yaml] [--log-level INFO]

The default config path matches the documented layout: a user copies
``simulator/config.example.yaml`` -> ``simulator/config.yaml`` (gitignored)
and tunes it for their run.

Ctrl-C (SIGINT) or SIGTERM triggers a clean shutdown: each per-pump task
exits its Publisher context (disconnects cleanly), then ``run()`` returns.

Signal handling uses a two-tier strategy (per Gemini Q8, 2026-05-25
mqtt-publishing review):

1. **Asyncio-native** via ``loop.add_signal_handler`` — preferred on
   platforms where it works (Unix). The handler runs on the event loop
   thread with no marshalling required.
2. **Sync signal handler bridging into the loop** via ``signal.signal``
   plus ``loop.call_soon_threadsafe`` — fallback on Windows
   ProactorEventLoop, which doesn't expose ``add_signal_handler``. This
   avoids the ``KeyboardInterrupt`` -> ``CancelledError`` -> aggressive
   loop teardown chain that Windows previously fell back to, which could
   leave the MQTT DISCONNECT packet unsent and surface as
   "Task was destroyed but it is pending!" warnings.

**2026-05-28 follow-up — second-Ctrl+C escalation.** The first signal
asks Fleet for a graceful shutdown (sets the shutdown event; per-pump
tasks finish their tick and exit their Publisher contexts). The
publishers' own ``__aexit__`` is now timeout-bounded
(``DISCONNECT_TIMEOUT_SECONDS = 3 s``), so a stuck TLS teardown can't
pin shutdown indefinitely. But if the user does Ctrl+C *again* — for
instance because the first one looked like it was being ignored — the
``_ShutdownState`` machine escalates to ``os._exit(130)`` immediately.
The 130 exit code is the POSIX convention for "killed by SIGINT". This
matches the operator UX of ``uvicorn`` / ``aiohttp``.

**Exit codes:**

- ``0`` — normal exit (graceful shutdown via signal or end of work).
- ``2`` — YAML/schema config error from ``load_config``.
- ``3`` — runner refused to construct fleet (non-healthy scenario).
- ``4`` — publisher static config error (``PublisherConfigError`` —
  missing cert, malformed PEM, bad URL). Distinct from 2 so CI can tell
  "YAML is malformed" from "YAML is valid but cert is missing". Added
  2026-05-28 per Gemini Q3 (2026-05-27 aws-iot-publisher review) and
  ADR 0003 §Addendum 2026-05-28 "Static config errors halt the fleet".
- ``130`` — forced exit on second Ctrl+C (POSIX SIGINT convention,
  128+2). Emitted by ``_ShutdownState.__call__`` via ``os._exit``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from simulator.config import ConfigError, load_config
from simulator.publisher import PublisherConfigError
from simulator.runner import Fleet


log = logging.getLogger(__name__)


# Exit code on second-Ctrl+C escalation. 130 is the POSIX convention for
# "process terminated by SIGINT" (128 + signal number 2). Documented
# separately so tests can assert against the same constant.
FORCE_EXIT_CODE: int = 130

# Exit code when a publisher raises PublisherConfigError (missing cert,
# malformed PEM, bad URL). Distinct from 2 (YAML/schema error from
# load_config) and 3 (runner NotImplementedError) so CI can distinguish
# the failure modes. Added 2026-05-28 per Gemini Q3.
PUBLISHER_CONFIG_ERROR_CODE: int = 4


def _loop_factory():
    """Pick the right event loop class for this platform.

    Windows uses ProactorEventLoop by default (Python 3.8+). paho-mqtt
    registers its socket via ``loop.add_reader``/``add_writer``, which
    ``ProactorEventLoop`` does NOT implement (raises ``NotImplementedError``
    before any bytes are sent — the broker never sees the connection
    attempt, the publisher reports "Operation timed out").
    ``SelectorEventLoop`` supports the reader/writer APIs and is what
    aiomqtt/paho expect. We don't use subprocess-async features that
    ProactorEventLoop is better at, so the swap is pure win for this project.

    On Unix, ``None`` falls through to the default factory (which is
    ``SelectorEventLoop`` already).

    NOTE: the older ``asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())``
    pattern also works but emits ``DeprecationWarning`` on Python 3.12+
    (both APIs are slated for removal in 3.16). The ``loop_factory``
    parameter on ``asyncio.run`` is the modern equivalent and is
    non-deprecated.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m simulator",
        description="Run the simulated pump fleet (publishes MQTT telemetry).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("simulator/config.yaml"),
        help="Path to the YAML config (default: simulator/config.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level (default: INFO).",
    )
    return parser.parse_args(argv)


class _ShutdownState:
    """Single-flight shutdown request with second-signal escalation.

    First call: log a heads-up, ask ``Fleet`` to wind down gracefully.
    Second call: log a forcing warning and exit the process immediately
    via ``force_exit(130)``. ``force_exit`` is parameterised so tests can
    monkeypatch ``os._exit``.

    Designed to be called from either an asyncio-native signal handler
    (``loop.add_signal_handler``) or a sync signal handler bridged via
    ``loop.call_soon_threadsafe`` — both paths invoke the instance the
    same way (``self()`` with no args).
    """

    def __init__(self, fleet: Fleet, force_exit=os._exit) -> None:
        self._fleet = fleet
        self._requested = False
        self._force_exit = force_exit

    @property
    def requested(self) -> bool:
        return self._requested

    def __call__(self) -> None:
        if self._requested:
            log.warning(
                "second shutdown signal received; forcing exit (%d). "
                "If a publisher disconnect hung, the OS will reap the socket.",
                FORCE_EXIT_CODE,
            )
            self._force_exit(FORCE_EXIT_CODE)
            return  # only reached if force_exit was mocked
        self._requested = True
        log.info(
            "shutdown requested; pumps will finish their current tick and "
            "disconnect. Ctrl+C again to force immediate exit."
        )
        self._fleet.request_shutdown()


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, fleet: Fleet
) -> _ShutdownState:
    """Wire SIGINT and SIGTERM to a single ``_ShutdownState`` instance.

    Tries the asyncio-native API first; falls back to a sync ``signal``
    handler that hops into the loop via ``call_soon_threadsafe`` (this
    works on Windows ProactorEventLoop, where ``add_signal_handler``
    raises ``NotImplementedError``).

    Returns the state machine so callers/tests can introspect whether a
    shutdown has been requested.
    """
    state = _ShutdownState(fleet)
    try:
        loop.add_signal_handler(signal.SIGINT, state)
        loop.add_signal_handler(signal.SIGTERM, state)
        return state
    except (NotImplementedError, RuntimeError):
        # Asyncio-native handlers unavailable (Windows ProactorEventLoop,
        # or we're not on the main thread). Fall through to signal.signal.
        pass

    def _bridge(signum, frame):  # noqa: ARG001 — signal API shape
        try:
            loop.call_soon_threadsafe(state)
        except RuntimeError:
            # Loop already closed mid-shutdown; nothing more to do.
            pass

    # signal.signal works on the main thread (which is where asyncio.run
    # puts us). SIGTERM may not exist or may not be settable in all
    # Windows configurations — swallow the relevant exceptions.
    signal.signal(signal.SIGINT, _bridge)
    try:
        signal.signal(signal.SIGTERM, _bridge)
    except (ValueError, AttributeError, OSError):
        pass
    return state


async def _run(fleet: Fleet) -> None:
    loop = asyncio.get_running_loop()
    _install_shutdown_handlers(loop, fleet)
    await fleet.run()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    # Fleet.from_config can also raise PublisherConfigError indirectly
    # via make_publisher() -> Publisher.__init__ -> _parse_mqtt_url for
    # an unparseable URL. Catch both NotImplementedError (non-healthy
    # scenario) and PublisherConfigError (bad URL at construction).
    try:
        fleet = Fleet.from_config(config)
    except NotImplementedError as e:
        print(f"runner error: {e}", file=sys.stderr)
        return 3
    except PublisherConfigError as e:
        print(f"publisher config error: {e}", file=sys.stderr)
        return PUBLISHER_CONFIG_ERROR_CODE

    try:
        asyncio.run(_run(fleet), loop_factory=_loop_factory())
    except KeyboardInterrupt:
        # Paranoia backstop: with the sync signal bridge installed, SIGINT
        # is consumed before Python translates it to KeyboardInterrupt.
        # This branch only fires if signal-handler installation failed
        # entirely (e.g., running on a non-main thread). The Fleet.run
        # CancelledError path still handles the cleanup.
        pass
    except PublisherConfigError as e:
        # Propagated up from a per-pump task that hit a static cert error
        # at __aenter__ time. Fleet.run drained the other pumps via the
        # disconnect-bound wait_for ceiling, then re-raised. We exit with
        # the distinct code so CI / ops can distinguish "your YAML is
        # malformed" (exit 2) from "your YAML is valid but the cert is
        # missing on disk" (exit 4). Added 2026-05-28 per Gemini Q3.
        print(f"publisher config error: {e}", file=sys.stderr)
        return PUBLISHER_CONFIG_ERROR_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
