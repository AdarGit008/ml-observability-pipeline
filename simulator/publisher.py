"""MQTT publishers — abstract interface + Mosquitto/AWS-IoT implementations.

A ``Publisher`` is a per-pump MQTT client: it owns a TCP connection to the
broker, encodes telemetry dicts as JSON, and publishes them to a topic.
The abstraction lets the ``Fleet`` runner stay broker-agnostic — the AWS
IoT mTLS path swaps in transparently via the ABC (per ADR 0003).

Usage pattern (driven by ``simulator.runner.Fleet``):

    async with publisher:                 # connects
        await publisher.publish(topic, telemetry_dict)
        ...                               # more publishes per tick
    # disconnects on context exit (incl. via exception or cancellation)

Exceptions from the underlying transport are translated to
``PublisherError`` so the runner doesn't need to import aiomqtt or paho.
The runner uses this to implement connect-with-backoff (ADR 0003).

Concurrency note: each pump gets its own ``Publisher`` and its own MQTT
connection (per the 2026-05-25 mqtt-publishing session brief — Q2 picked
"per-pump in both modes"). 15 connections to local Mosquitto is well
within its capacity, and the topology matches AWS IoT's "one Thing per
pump = one client_id" model for mode parity.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional
from urllib.parse import urlparse

import aiomqtt

from simulator.config import BrokerTarget, TlsConfig


class PublisherError(Exception):
    """Wraps any transport-level error from the underlying MQTT client.

    Defined here so ``simulator.runner.Fleet`` can catch a single exception
    type without importing aiomqtt. The original exception is chained via
    ``__cause__`` so debuggers and tracebacks see the full picture.
    """


def topic_for(pump_id: str) -> str:
    """Canonical MQTT topic for a pump's telemetry.

    Authoritative source: ``context/_interfaces.md`` — ``factory/pumps/{pump_id}/telemetry``.
    """
    return f"factory/pumps/{pump_id}/telemetry"


class Publisher(ABC):
    """Abstract per-pump MQTT publisher.

    Subclasses connect on ``__aenter__`` and disconnect on ``__aexit__``.
    ``publish`` is only valid inside the ``async with`` block.
    """

    @abstractmethod
    async def __aenter__(self) -> "Publisher":
        ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc, tb) -> None:
        ...

    @abstractmethod
    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        ...


class LocalPublisher(Publisher):
    """aiomqtt-backed publisher for local Mosquitto (unauthenticated).

    QoS 0, retain=False — telemetry is time-series, drops are tolerable
    and retention is wrong for sensor data. ``client_id`` matches the
    pump_id so Mosquitto logs map cleanly back to fleet members.
    """

    DEFAULT_PORT = 1883

    def __init__(self, url: str, client_id: str) -> None:
        host, port = _parse_mqtt_url(url, self.DEFAULT_PORT)
        self._host = host
        self._port = port
        self._client_id = client_id
        # aiomqtt.Client instantiation is deferred to __aenter__: a
        # never-entered Publisher should not hold a half-built paho client.
        self._client: Optional[aiomqtt.Client] = None

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    async def __aenter__(self) -> "LocalPublisher":
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
        )
        try:
            await self._client.__aenter__()
        except aiomqtt.MqttError as e:
            self._client = None
            raise PublisherError(
                f"failed to connect to {self._host}:{self._port} "
                f"as {self._client_id!r}: {e}"
            ) from e
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is None:
            return
        client = self._client
        # Drop the reference first so a failure during disconnect doesn't
        # leave a half-closed client lingering on the instance.
        self._client = None
        try:
            await client.__aexit__(exc_type, exc, tb)
        except aiomqtt.MqttError:
            # Disconnect path: we're already on the way out; swallowing the
            # transport error here matches paho's own quiet-on-disconnect
            # behavior. The runner is making its own decision about whether
            # to reconnect based on the higher-level signal that triggered
            # the exit.
            pass

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise PublisherError(
                "publish() called outside `async with` — connect first"
            )
        try:
            await self._client.publish(
                topic,
                payload=json.dumps(payload).encode("utf-8"),
                qos=0,
                retain=False,
            )
        except aiomqtt.MqttError as e:
            raise PublisherError(f"publish to {topic} failed: {e}") from e


class AwsIotPublisher(Publisher):
    """STUB — AWS IoT Core mTLS publisher (lands in a later session).

    Accepts a ``TlsConfig`` at construction so the runner can instantiate
    the object today (and so the abstract surface is satisfied), but
    ``__aenter__`` raises ``NotImplementedError``. Reason: mTLS
    provisioning needs an AWS account, which is still on the ⬜ list per
    ``ml-obs-pipeline-context``. Writing the cert-loading code without
    certs to test against would be speculative.

    Per ADR 0003 (shape-only schema validation), the loader does NOT check
    that ``tls.cert_path`` / ``tls.key_path`` / ``tls.ca_path`` exist on
    disk — that check happens here, in the place that actually opens the
    files, when this class is wired.
    """

    def __init__(self, url: str, client_id: str, tls: TlsConfig) -> None:
        self._url = url
        self._client_id = client_id
        self._tls = tls

    @property
    def url(self) -> str:
        return self._url

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def tls(self) -> TlsConfig:
        return self._tls

    async def __aenter__(self) -> "AwsIotPublisher":
        raise NotImplementedError(
            "AwsIotPublisher is not yet wired. The AWS IoT mTLS publisher "
            "lands in a later session, after the AWS account is provisioned. "
            "See context/simulator.md and ADR 0003 for status; set "
            "`broker.target: local` in your config for the meantime."
        )

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # __aenter__ raised, so PEP 492 means __aexit__ is never called.
        # This branch only fires if a subclass overrides __aenter__ and
        # forgets to call super(). Kept as a defensive no-op.
        return None

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError(
            "AwsIotPublisher.publish is not yet wired; __aenter__ would "
            "have raised first under normal usage."
        )


def make_publisher(
    *,
    target: BrokerTarget,
    url: str,
    client_id: str,
    tls: Optional[TlsConfig],
) -> Publisher:
    """Build the right ``Publisher`` for a ``BrokerConfig``.

    The (target, tls) combination is the loader's responsibility — by the
    time we get here, `load_config` has already rejected the four invalid
    pairings. The defensive checks below catch programming errors (e.g.,
    constructing a ``SimulatorConfig`` directly in tests with a bad combo).
    """
    if target is BrokerTarget.LOCAL:
        if tls is not None:
            raise PublisherError(
                "internal error: tls set for local target — should have "
                "been rejected by load_config"
            )
        return LocalPublisher(url=url, client_id=client_id)
    if target is BrokerTarget.AWS_IOT:
        if tls is None:
            raise PublisherError(
                "internal error: tls missing for aws-iot target — should "
                "have been rejected by load_config"
            )
        return AwsIotPublisher(url=url, client_id=client_id, tls=tls)
    raise PublisherError(f"unknown broker target: {target!r}")  # pragma: no cover


def _parse_mqtt_url(url: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from a ``mqtt://host:port`` URL.

    aiomqtt.Client wants host and port as separate args; the YAML schema
    keeps a single ``url`` field for ergonomics. Schemes are accepted
    leniently (``mqtt``, ``mqtts``, ``tcp``, or bare ``host[:port]``)
    since the choice of TLS is governed by the target field, not the
    scheme.
    """
    parsed = urlparse(url if "://" in url else f"mqtt://{url}")
    host = parsed.hostname
    if not host:
        raise PublisherError(f"could not parse host from MQTT url: {url!r}")
    port = parsed.port if parsed.port is not None else default_port
    return host, port
