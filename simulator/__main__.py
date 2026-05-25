"""Run the simulator fleet from the command line.

Usage:
    python -m simulator [--config simulator/config.yaml] [--log-level INFO]

The default config path matches the documented layout: a user copies
``simulator/config.example.yaml`` -> ``simulator/config.yaml`` (gitignored)
and tunes it for their run.

Ctrl-C (SIGINT) or SIGTERM triggers a clean shutdown: each per-pump task
exits its Publisher context (disconnects cleanly), then ``run()`` returns.
On Windows, where asyncio doesn't expose ``add_signal_handler``, the
KeyboardInterrupt path inside ``asyncio.run`` cancels the tasks and we
catch ``CancelledError`` in ``Fleet.run`` to achieve the same outcome.
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


async def _run(fleet: Fleet) -> None:
    loop = asyncio.get_running_loop()
    # Wire ctrl-C / SIGTERM to a clean shutdown. add_signal_handler is
    # not available on Windows ProactorEventLoop — there, KeyboardInterrupt
    # propagates up through asyncio.run and the CancelledError path in
    # Fleet.run handles graceful teardown.
    try:
        loop.add_signal_handler(signal.SIGINT, fleet.request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, fleet.request_shutdown)
    except (NotImplementedError, RuntimeError):
        pass
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
        # On Windows, KeyboardInterrupt may bubble out of asyncio.run
        # before our signal handlers (or their absence) take effect. The
        # per-pump tasks will have been cancelled by asyncio.run's
        # teardown, which routes through Fleet.run's CancelledError branch
        # and disconnects each Publisher cleanly.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
