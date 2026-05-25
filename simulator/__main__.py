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
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from simulator.config import ConfigError, load_config
from simulator.runner import Fleet


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


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, fleet: Fleet
) -> None:
    """Wire SIGINT and SIGTERM to ``fleet.request_shutdown``.

    Tries the asyncio-native API first; falls back to a sync ``signal``
    handler that hops into the loop via ``call_soon_threadsafe`` (this
    works on Windows ProactorEventLoop, where ``add_signal_handler``
    raises ``NotImplementedError``).
    """
    try:
        loop.add_signal_handler(signal.SIGINT, fleet.request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, fleet.request_shutdown)
        return
    except (NotImplementedError, RuntimeError):
        # Asyncio-native handlers unavailable (Windows ProactorEventLoop,
        # or we're not on the main thread). Fall through to signal.signal.
        pass

    def _bridge(signum, frame):  # noqa: ARG001 — signal API shape
        try:
            loop.call_soon_threadsafe(fleet.request_shutdown)
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

    try:
        fleet = Fleet.from_config(config)
    except NotImplementedError as e:
        print(f"runner error: {e}", file=sys.stderr)
        return 3

    try:
        asyncio.run(_run(fleet))
    except KeyboardInterrupt:
        # Paranoia backstop: with the sync signal bridge installed, SIGINT
        # is consumed before Python translates it to KeyboardInterrupt.
        # This branch only fires if signal-handler installation failed
        # entirely (e.g., running on a non-main thread). The Fleet.run
        # CancelledError path still handles the cleanup.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
