"""aiomqtt-based MQTT subscriber for the local pump fleet.

ONE wildcard subscription on ``factory/pumps/+/telemetry`` — not 15
per-pump subscriptions. The per-pump topology in ADR 0003 applies to
*publishers*: AWS IoT's Thing-per-pump model means each simulator pump
needs its own client_id and TCP connection. Subscribers have no such
constraint, so one connection with the ``+`` wildcard is the cleaner
fit. The pump_id is parsed from the topic on each incoming message.

This module is local-only — it has no AWS analogue. The hot path
Lambda is invoked per-message by an IoT Rule, not via subscription, so
the subscription topology doesn't replicate to AWS mode. Documented in
ADR 0005 (subscriber topology).

Error handling mirrors the simulator's publisher: ``aiomqtt.MqttError``
is wrapped in ``SubscriberError`` so the orchestrator doesn't need to
import aiomqtt directly. Retry-forever with exponential backoff is the
right policy here for the same reason it is on the publish side —
local Mosquitto may restart, and silently giving up on the consumer
would leave the InfluxDB store stale until the next restart of the
local_runtime process.

The error class hierarchy intentionally mirrors ``simulator.publisher``
(``PublisherError`` / ``PublisherConfigError``) for symmetry; a future
session can extract a tiny shared error module if the duplication
starts to itch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiomqtt


log = logging.getLogger(__name__)


# Wildcard topic. Hard-coded — see ``context/_interfaces.md`` for the
# canonical pattern. If this ever needs to change, both this module and
# the simulator's ``topic_for`` should move together.
TELEMETRY_WILDCARD = "factory/pumps/+/telemetry"

# Topic pattern used to extract ``pump_id`` from concrete topics. The
# pump_id format is enforced by ``simulator.runner.pump_id_for`` and
# matches the regex below (P- followed by two digits). We match the
# pump_id portion strictly so a misrouted message on a similar-shape
# topic (e.g., ``factory/pumps/X/telemetry``) doesn't silently land in
# the window.
_PUMP_ID_RE = re.compile(r"^factory/pumps/(P-\d{2})/telemetry$")


class SubscriberError(Exception):
    """Transient transport-level subscriber error.

    Mirrors ``simulator.publisher.PublisherError`` so the orchestrator
    can catch a single exception type without depending on aiomqtt.
    The original exception is chained via ``__cause__``.
    """


# Callback signature: a coroutine that takes (pump_id, telemetry_dict)
# and processes the message. Defined as a type alias for clarity at
# call sites and so the test suite can pin the contract.
MessageHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class TelemetrySubscriber:
    """Single-connection MQTT subscriber for the pump fleet.

    Connects to the broker on ``__aenter__``, subscribes to the
    wildcard topic, and exposes ``run(handler)`` which drives messages
    into the supplied callback until cancelled.

    The connection is one TCP socket, one client_id, one subscription.
    All 15+ pumps' telemetry flows through it.
    """

    def __init__(self, url: str, client_id: str) -> None:
        host, port = _parse_mqtt_url(url, default_port=1883)
        self._host = host
        self._port = port
        self._client_id = client_id
        self._client: Optional[aiomqtt.Client] = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def client_id(self) -> str:
        return self._client_id

    async def __aenter__(self) -> "TelemetrySubscriber":
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
        )
        try:
            await self._client.__aenter__()
            await self._client.subscribe(TELEMETRY_WILDCARD)
        except aiomqtt.MqttError as e:
            self._client = None
            raise SubscriberError(
                f"failed to connect/subscribe to {self._host}:{self._port} "
                f"as {self._client_id!r}: {e}"
            ) from e
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.__aexit__(exc_type, exc, tb)
        except aiomqtt.MqttError:
            # Same rationale as ``simulator.publisher`` __aexit__:
            # disconnect-time transport blips don't deserve to hijack
            # the exit path.
            pass

    async def messages(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Iterate ``(pump_id, telemetry_dict)`` pairs from the broker.

        Skips messages whose topic doesn't match the pump_id regex
        (defensive against misconfigured publishers landing on the
        wildcard) and messages whose payload isn't valid JSON. Both
        are logged at WARNING and the iterator continues — a single
        bad message shouldn't kill the consumer.

        Raises:
            SubscriberError: on transport-level failure during iteration.
        """
        if self._client is None:
            raise SubscriberError(
                "messages() called outside `async with` — connect first"
            )
        try:
            async for msg in self._client.messages:
                topic_str = str(msg.topic)
                match = _PUMP_ID_RE.match(topic_str)
                if not match:
                    log.warning(
                        "telemetry on unexpected topic %r; ignoring", topic_str
                    )
                    continue
                pump_id = match.group(1)
                try:
                    payload = json.loads(_payload_bytes(msg).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    log.warning(
                        "telemetry on %s had invalid JSON payload: %s; ignoring",
                        topic_str, e,
                    )
                    continue
                if not isinstance(payload, dict):
                    log.warning(
                        "telemetry on %s decoded to %s, not dict; ignoring",
                        topic_str, type(payload).__name__,
                    )
                    continue
                yield pump_id, payload
        except aiomqtt.MqttError as e:
            raise SubscriberError(f"subscriber iteration failed: {e}") from e

    async def run(self, handler: MessageHandler) -> None:
        """Drive incoming messages into ``handler`` forever.

        ``handler`` is awaited inline (no fan-out task per message) —
        the message rate is 7.5 msg/s at PLAN.md's 15-pump target, so
        handler latency dominating loop throughput would be a sign the
        handler is doing too much synchronous work. Mode parity reason:
        the Lambda hot path is also a single-threaded sync-per-message
        execution, so doing async fan-out here would diverge.
        """
        async for pump_id, payload in self.messages():
            await handler(pump_id, payload)


def _payload_bytes(msg: Any) -> bytes:
    """Extract bytes from an aiomqtt Message regardless of paho version.

    aiomqtt v2 surfaces ``msg.payload`` as ``bytes``; older paho releases
    used a ``str``-or-``bytes`` union. Normalize defensively so the
    decode step has a stable input.
    """
    payload = msg.payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, bytes):
        return payload
    return bytes(payload)


def _parse_mqtt_url(url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a ``mqtt://host:port`` URL.

    Lifted from ``simulator.publisher._parse_mqtt_url`` semantics for
    consistency. We don't import that one directly because doing so
    would tangle simulator → local_runtime, and the local consumer
    should not depend on the simulator package (they share a project
    but ship independently).
    """
    parsed = urlparse(url if "://" in url else f"mqtt://{url}")
    host = parsed.hostname
    if not host:
        raise SubscriberError(f"could not parse host from MQTT url: {url!r}")
    port = parsed.port if parsed.port is not None else default_port
    return host, port


async def retry_forever(
    factory: Callable[[], TelemetrySubscriber],
    handler: MessageHandler,
    *,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run a subscriber, reconnect on transport failure with backoff.

    Mirrors ``simulator.runner``'s retry-forever policy: a transient
    connection blip should be invisible to the operator beyond a log
    line. Backoff doubles on each failed cycle up to ``max_backoff``,
    resets after a successful message run (which happens when ``run``
    returns normally — i.e., the iterator was closed, which only
    happens on a transport failure or external cancellation, so the
    "reset on success" path is mostly the cancellation case).

    ``sleep`` is injected for tests so they can run the loop without
    blocking on real time.
    """
    backoff = initial_backoff
    while True:
        try:
            async with factory() as sub:
                await sub.run(handler)
            # Reached normal end-of-iteration — reset backoff for the
            # next cycle.
            backoff = initial_backoff
        except SubscriberError as e:
            log.warning(
                "subscriber transport failed: %s; reconnecting in %.1fs",
                e, backoff,
            )
            await sleep(backoff)
            backoff = min(max_backoff, backoff * 2)
