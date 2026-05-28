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

Two exception classes carry transport errors back to the runner so the
runner doesn't import aiomqtt/paho/ssl directly:

- ``PublisherError`` — transient transport failure (connect refused,
  dropped mid-publish, broker timing out). The runner's retry-forever
  loop catches and reconnects with backoff (ADR 0003 §Decision 4).
- ``PublisherConfigError`` (subclass) — static configuration error
  (missing cert file, malformed PEM, key/cert mismatch, unparseable
  URL). The runner halts the fleet on this rather than looping — per
  Gemini Q3 review (2026-05-27 aws-iot-publisher) and ADR 0003
  §Addendum 2026-05-28 "Static config errors halt the fleet". Reason:
  loud-loop-forever on a missing cert just buries the error in the
  log; on a single-PC dev machine the developer wants an immediate
  crash so they can fix their YAML.

Concurrency note: each pump gets its own ``Publisher`` and its own MQTT
connection (per the 2026-05-25 mqtt-publishing session brief — Q2 picked
"per-pump in both modes"). 15 connections to local Mosquitto is well
within its capacity, and the topology matches AWS IoT's "one Thing per
pump = one client_id" model for mode parity.

Disconnect timeout: ``__aexit__`` in both implementations wraps the
inner ``aiomqtt.Client.__aexit__`` in ``asyncio.wait_for`` with a
``DISCONNECT_TIMEOUT_SECONDS`` ceiling. Without this, the 2026-05-28
follow-up to the AwsIotPublisher smoke test showed that a TLS
close_notify handshake (or paho's keepalive flush) could block the
exit indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiomqtt

from simulator.config import BrokerTarget, TlsConfig


log = logging.getLogger(__name__)


# Upper bound on the time ``__aexit__`` will wait on the inner client's
# disconnect path before logging "forced" and returning. 3 s is generous
# for both Mosquitto (sub-millisecond LAN) and AWS IoT Core (typical
# DISCONNECT round-trip is <500 ms over the public internet) while still
# bounding a hung TLS teardown. Monkeypatchable in tests.
DISCONNECT_TIMEOUT_SECONDS: float = 3.0


class PublisherError(Exception):
    """Wraps any transient transport-level error from the underlying MQTT
    client. The runner's retry-forever loop catches this and reconnects
    with backoff (ADR 0003 §Decision 4).

    Defined here so ``simulator.runner.Fleet`` can catch a single
    exception type without importing aiomqtt. The original exception is
    chained via ``__cause__`` so debuggers and tracebacks see the full
    picture.
    """


class PublisherConfigError(PublisherError):
    """Static configuration error — missing cert file, malformed PEM,
    key/cert mismatch, unparseable URL. Subclass of ``PublisherError``
    so generic catch sites still work; the runner inspects the specific
    type to decide between retry-forever and halt-the-fleet.

    Per Gemini Q3 (2026-05-27 aws-iot-publisher review) and ADR 0003
    §Addendum 2026-05-28 "Static config errors halt the fleet": looping
    forever on a missing cert is the wrong UX on a single-PC dev
    machine — the developer wants an immediate crash so they can fix
    the YAML or file paths, not a 30-second polling loop that buries
    the error.

    Catch ordering matters: ``except PublisherConfigError`` must come
    BEFORE ``except PublisherError`` (Python catches in source order).
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
            await asyncio.wait_for(
                client.__aexit__(exc_type, exc, tb),
                timeout=DISCONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning(
                "MQTT disconnect for %s timed out after %.1fs; forcing",
                self._client_id, DISCONNECT_TIMEOUT_SECONDS,
            )
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
    """aiomqtt-backed publisher for AWS IoT Core (mTLS).

    Same wire shape as ``LocalPublisher`` — JSON payload, QoS 0,
    ``retain=False``, ``client_id == pump_id`` for per-Thing observability
    in CloudWatch. The only differences from the local path are:

    - **TLS material is loaded at connect time.** ``__aenter__`` checks
      that ``tls.cert_path`` / ``tls.key_path`` / ``tls.ca_path`` all
      resolve to existing files (clean ``PublisherConfigError`` if any
      is missing) before building an ``ssl.SSLContext`` and handing it
      to ``aiomqtt.Client(tls_context=...)``. Per ADR 0003 Decision 5,
      the loader does shape-only validation of the paths; the
      file-existence and cert-parsing checks live here, where the file
      is actually opened.
    - **Default port is 8883.** AWS IoT Core's mTLS endpoint listens on
      8883; the ``:443`` ALPN-with-``x-amzn-mqtt-ca`` fallback is for
      networks that block 8883, which the project's home-network setup
      doesn't need. URLs in the YAML are parsed leniently
      (``mqtts://endpoint:8883`` or bare hostname both work).

    Cert parsing failures (malformed PEM, key/cert mismatch, expired
    CA, encrypted PKCS#8 key without password, etc.) surface from
    ``ssl.SSLContext.load_*`` as ``ssl.SSLError`` / ``OSError`` /
    ``ValueError`` (per Gemini Q2 — ``ValueError`` was missing from the
    initial 2026-05-27 implementation) and get translated to
    ``PublisherConfigError`` with a message pointing at the file that
    failed. The runner halts on ``PublisherConfigError`` (vs.
    retry-forever on transient ``PublisherError``), so a malformed cert
    produces one clean stack trace rather than 30-second-cap polling.
    """

    DEFAULT_PORT = 8883  # AWS IoT Core mTLS endpoint

    def __init__(self, url: str, client_id: str, tls: TlsConfig) -> None:
        host, port = _parse_mqtt_url(url, self.DEFAULT_PORT)
        self._host = host
        self._port = port
        self._url = url
        self._client_id = client_id
        self._tls = tls
        self._client: Optional[aiomqtt.Client] = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def tls(self) -> TlsConfig:
        return self._tls

    async def __aenter__(self) -> "AwsIotPublisher":
        # Existence checks first — clearer error than "FileNotFoundError
        # inside ssl.SSLContext.load_*" with no field name attached.
        # Missing file is a static config error; raise the subclass so
        # the runner halts the fleet rather than looping forever.
        for field, path_str in (
            ("cert_path", self._tls.cert_path),
            ("key_path", self._tls.key_path),
            ("ca_path", self._tls.ca_path),
        ):
            if not Path(path_str).is_file():
                raise PublisherConfigError(
                    f"AWS IoT mTLS {field} not found: {path_str!r} "
                    f"(check broker.tls in your config; the loader does not "
                    f"check existence — see ADR 0003 Decision 5)"
                )

        # Build an SSLContext that validates the AWS IoT server cert
        # against the Amazon Root CA and presents the per-Thing client
        # cert for mTLS. Failure modes:
        # - ssl.SSLError: malformed PEM, key/cert mismatch, expired CA
        # - OSError: race condition (cert rotated mid-__aenter__, file
        #   removed between is_file() and create_default_context opening it)
        # - ValueError: encrypted PKCS#8 key without a password, or an
        #   unsupported key type (per Gemini Q2; added 2026-05-28)
        # All three are static config errors → PublisherConfigError.
        try:
            tls_context = ssl.create_default_context(
                purpose=ssl.Purpose.SERVER_AUTH,
                cafile=self._tls.ca_path,
            )
            tls_context.load_cert_chain(
                certfile=self._tls.cert_path,
                keyfile=self._tls.key_path,
            )
        except (ssl.SSLError, OSError, ValueError) as e:
            raise PublisherConfigError(
                f"failed to build TLS context for {self._client_id!r} "
                f"(cert={self._tls.cert_path!r}, key={self._tls.key_path!r}, "
                f"ca={self._tls.ca_path!r}): {e}"
            ) from e

        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            identifier=self._client_id,
            tls_context=tls_context,
        )
        try:
            await self._client.__aenter__()
        except aiomqtt.MqttError as e:
            self._client = None
            # Transport-level connect failure (CONNREFUSED, network down,
            # broker rejecting the policy). This IS transient — the
            # broker might come back, the policy might get fixed, the
            # network might recover. Stay in retry-forever, do not halt.
            raise PublisherError(
                f"failed to connect to AWS IoT Core at {self._host}:{self._port} "
                f"as {self._client_id!r}: {e}"
            ) from e
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await asyncio.wait_for(
                client.__aexit__(exc_type, exc, tb),
                timeout=DISCONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # The 2026-05-28 follow-up to the aws-iot smoke test surfaced
            # this: TLS close_notify handshake (or paho's keepalive flush)
            # can block exit indefinitely. We log + return; the underlying
            # socket is reaped by the OS when the process exits. Worst
            # case is a half-closed connection at the broker, which AWS IoT
            # tears down on keepalive timeout anyway.
            log.warning(
                "AWS IoT disconnect for %s timed out after %.1fs; forcing",
                self._client_id, DISCONNECT_TIMEOUT_SECONDS,
            )
        except aiomqtt.MqttError:
            # Same rationale as LocalPublisher.__aexit__ — disconnect-time
            # transport blips don't deserve to hijack the exit path.
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

    A URL that doesn't yield a parseable host is a static config error
    (the YAML field is wrong); raise ``PublisherConfigError`` so the
    runner halts. This is also called from publisher constructors, so a
    bad URL surfaces from ``Fleet.from_config`` at startup rather than
    inside a per-pump task.
    """
    parsed = urlparse(url if "://" in url else f"mqtt://{url}")
    host = parsed.hostname
    if not host:
        raise PublisherConfigError(
            f"could not parse host from MQTT url: {url!r}"
        )
    port = parsed.port if parsed.port is not None else default_port
    return host, port
