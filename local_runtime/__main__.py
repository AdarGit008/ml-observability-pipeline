"""Run the local-runtime scorer service from the command line.

Usage:
    python -m local_runtime [--config local_runtime/config.yaml] [--log-level INFO]

The default config path mirrors the simulator's convention: copy
``local_runtime/config.example.yaml`` -> ``local_runtime/config.yaml``
(gitignored) and tune for your run.

End-to-end smoke (DoD #2 of the session brief):

    docker compose up -d influxdb mosquitto
    python -m simulator &
    python -m local_runtime

…produces rows in the ``pump_telemetry`` measurement within
``tick_seconds * 2`` (≈4 seconds at the default 2-second tick).

Signal handling and event-loop selection follow the simulator's
patterns (``simulator/__main__.py``) so a future "run both" wrapper
script can use identical shutdown semantics. The Windows
``SelectorEventLoop`` swap is load-bearing here for the same reason
it is in the simulator: ``ProactorEventLoop`` doesn't implement
``add_reader``/``add_writer``, which paho-mqtt depends on.

Exit codes:
- ``0`` — normal exit (signal-driven shutdown).
- ``2`` — YAML/schema config error.
- ``3`` — InfluxDB or subscriber setup failure outside the retry loop.
- ``130`` — second-Ctrl+C escalation (POSIX SIGINT convention).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from local_runtime.config import ConfigError, LocalRuntimeConfig, load_config
from local_runtime.influx_writer import InfluxWriter
from local_runtime.service import ScorerService
from local_runtime.subscriber import (
    SubscriberError,
    TelemetrySubscriber,
    retry_forever,
)


log = logging.getLogger(__name__)


FORCE_EXIT_CODE: int = 130
SETUP_ERROR_CODE: int = 3


def _loop_factory():
    """Pick the right event loop class for this platform.

    Mirrors ``simulator/__main__.py::_loop_factory`` — see that
    function's docstring for the full rationale (paho-mqtt's
    ``loop.add_reader`` requirement vs. Windows ProactorEventLoop).
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m local_runtime",
        description="Subscribe to local Mosquitto, score telemetry, write to InfluxDB.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("local_runtime/config.yaml"),
        help="Path to the YAML config (default: local_runtime/config.yaml).",
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

    Mirrors ``simulator/__main__.py::_ShutdownState``. First Ctrl+C
    cancels the outer task gracefully; second Ctrl+C forces process
    exit via ``os._exit(130)``.
    """

    def __init__(self, task: asyncio.Task, force_exit=os._exit) -> None:
        self._task = task
        self._requested = False
        self._force_exit = force_exit

    @property
    def requested(self) -> bool:
        return self._requested

    def __call__(self) -> None:
        if self._requested:
            log.warning(
                "second shutdown signal received; forcing exit (%d)",
                FORCE_EXIT_CODE,
            )
            self._force_exit(FORCE_EXIT_CODE)
            return
        self._requested = True
        log.info("shutdown requested; finishing current message and exiting")
        self._task.cancel()


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, task: asyncio.Task
) -> _ShutdownState:
    state = _ShutdownState(task)
    try:
        loop.add_signal_handler(signal.SIGINT, state)
        loop.add_signal_handler(signal.SIGTERM, state)
        return state
    except (NotImplementedError, RuntimeError):
        pass

    def _bridge(signum, frame):  # noqa: ARG001
        try:
            loop.call_soon_threadsafe(state)
        except RuntimeError:
            pass

    signal.signal(signal.SIGINT, _bridge)
    try:
        signal.signal(signal.SIGTERM, _bridge)
    except (ValueError, AttributeError, OSError):
        pass
    return state


async def _run(config: LocalRuntimeConfig) -> None:
    """Open the InfluxDB writer, hand the service to the subscriber's retry loop.

    Layering: the writer's lifetime brackets the entire run; the
    subscriber reconnects with backoff inside that bracket. If the
    InfluxDB connection itself dies, that's a setup error not a
    transient (we treat InfluxDB as part of the service's runtime
    surface, not a network blip) — the exception bubbles out to
    ``main()`` and the process exits.
    """
    async with InfluxWriter(config.influx) as writer:
        service = ScorerService(config, writer)

        def factory() -> TelemetrySubscriber:
            return TelemetrySubscriber(
                url=config.mqtt.url, client_id=config.mqtt.client_id
            )

        loop = asyncio.get_running_loop()
        outer_task = asyncio.current_task()
        if outer_task is not None:
            _install_shutdown_handlers(loop, outer_task)
        await retry_forever(factory, service.handle)


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

    try:
        asyncio.run(_run(config), loop_factory=_loop_factory())
    except KeyboardInterrupt:
        # Paranoia backstop — see simulator/__main__.py for the
        # full rationale.
        pass
    except asyncio.CancelledError:
        # Graceful shutdown via _ShutdownState.
        pass
    except SubscriberError as e:
        # Bubbled out past the retry loop's catch — means construction
        # failed before any reconnect attempt. Setup error.
        print(f"subscriber setup error: {e}", file=sys.stderr)
        return SETUP_ERROR_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
