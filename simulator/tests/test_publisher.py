"""Unit tests for simulator.publisher.

Real-broker validation is a documented manual smoke step
(``docker run -p 1883:1883 eclipse-mosquitto`` + ``mosquitto_sub``); the
unit tests below monkeypatch ``aiomqtt.Client`` so they exercise the wire
shape (topic, payload, QoS, retain) without needing a network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiomqtt
import pytest

from simulator.config import BrokerTarget, TlsConfig
from simulator.publisher import (
    AwsIotPublisher,
    LocalPublisher,
    Publisher,
    PublisherError,
    _parse_mqtt_url,
    make_publisher,
    topic_for,
)


# -- Helpers ---------------------------------------------------------------


def _run(coro):
    """Run a coroutine to completion in a fresh event loop."""
    return asyncio.run(coro)


class FakeAiomqttClient:
    """Records every call so tests can assert on the wire shape.

    Mirrors the subset of ``aiomqtt.Client`` that ``LocalPublisher`` uses:
    ``__aenter__`` / ``__aexit__`` / ``publish``. Constructor signature
    matches aiomqtt v2 (hostname/port/identifier).
    """

    instances: list["FakeAiomqttClient"] = []

    def __init__(self, *, hostname: str, port: int, identifier: str) -> None:
        self.hostname = hostname
        self.port = port
        self.identifier = identifier
        self.entered = False
        self.exited = False
        self.published: list[tuple[str, bytes, int, bool]] = []
        FakeAiomqttClient.instances.append(self)

    async def __aenter__(self) -> "FakeAiomqttClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    async def publish(self, topic: str, *, payload: Any, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


@pytest.fixture(autouse=True)
def _reset_fake_clients():
    FakeAiomqttClient.instances.clear()
    yield
    FakeAiomqttClient.instances.clear()


# -- topic_for / _parse_mqtt_url ------------------------------------------


def test_topic_for_canonical_format():
    assert topic_for("P-07") == "factory/pumps/P-07/telemetry"


def test_parse_mqtt_url_with_scheme_and_port():
    host, port = _parse_mqtt_url("mqtt://localhost:1883", default_port=1883)
    assert host == "localhost"
    assert port == 1883


def test_parse_mqtt_url_with_scheme_no_port_uses_default():
    host, port = _parse_mqtt_url("mqtts://broker.example.com", default_port=8883)
    assert host == "broker.example.com"
    assert port == 8883


def test_parse_mqtt_url_bare_host_port():
    """No scheme — accepted leniently (the YAML schema doesn't enforce one)."""
    host, port = _parse_mqtt_url("localhost:1883", default_port=1883)
    assert host == "localhost"
    assert port == 1883


def test_parse_mqtt_url_empty_raises():
    with pytest.raises(PublisherError, match="could not parse host"):
        _parse_mqtt_url("", default_port=1883)


# -- LocalPublisher --------------------------------------------------------


def test_local_publisher_properties():
    p = LocalPublisher("mqtt://broker.local:1884", client_id="P-03")
    assert p.client_id == "P-03"
    assert p.host == "broker.local"
    assert p.port == 1884


def test_local_publisher_publish_before_aenter_raises():
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        with pytest.raises(PublisherError, match="outside `async with`"):
            await p.publish("any/topic", {"hello": "world"})

    _run(_go())


def test_local_publisher_aenter_constructs_aiomqtt_with_right_args(monkeypatch):
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    p = LocalPublisher("mqtt://broker.local:1884", client_id="P-07")

    async def _go():
        async with p:
            assert FakeAiomqttClient.instances, "aiomqtt.Client should have been constructed"
            client = FakeAiomqttClient.instances[-1]
            assert client.hostname == "broker.local"
            assert client.port == 1884
            assert client.identifier == "P-07"
            assert client.entered is True

    _run(_go())
    # Exited cleanly.
    assert FakeAiomqttClient.instances[-1].exited is True


def test_local_publisher_aenter_wraps_mqtt_error(monkeypatch):
    class RaisingClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            raise aiomqtt.MqttError("simulated connect failure")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(aiomqtt, "Client", RaisingClient)
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        with pytest.raises(PublisherError, match="failed to connect"):
            async with p:
                pass

    _run(_go())


def test_local_publisher_publish_emits_qos0_retain_false_json(monkeypatch):
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")
    payload = {
        "pump_id": "P-00",
        "ts": "2026-05-25T12:00:00.000Z",
        "vibration_amp": 0.42,
        "bearing_temp": 68.3,
        "motor_current": 4.7,
        "rpm": 1798.0,
    }

    async def _go():
        async with p:
            await p.publish("factory/pumps/P-00/telemetry", payload)

    _run(_go())
    client = FakeAiomqttClient.instances[-1]
    assert len(client.published) == 1
    topic, raw_payload, qos, retain = client.published[0]
    assert topic == "factory/pumps/P-00/telemetry"
    assert qos == 0
    assert retain is False
    # Bytes, JSON-decoded back to the same dict.
    assert isinstance(raw_payload, (bytes, bytearray))
    assert json.loads(raw_payload.decode("utf-8")) == payload


def test_local_publisher_publish_wraps_mqtt_error(monkeypatch):
    class RaisingPublishClient(FakeAiomqttClient):
        async def publish(self, topic, *, payload, qos, retain):
            raise aiomqtt.MqttError("simulated publish failure")

    monkeypatch.setattr(aiomqtt, "Client", RaisingPublishClient)
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        async with p:
            with pytest.raises(PublisherError, match="publish to .* failed"):
                await p.publish("any/topic", {"k": "v"})

    _run(_go())


def test_local_publisher_aexit_is_idempotent(monkeypatch):
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        await p.__aenter__()
        await p.__aexit__(None, None, None)
        # Second exit is a no-op (no double-disconnect blowup).
        await p.__aexit__(None, None, None)

    _run(_go())


def test_local_publisher_aexit_swallows_mqtt_error(monkeypatch):
    """Disconnect-time MqttError is silenced: the runner has already
    decided to leave the context, no point hijacking that with a transport
    blip on the way out."""

    class ExitRaisingClient(FakeAiomqttClient):
        async def __aexit__(self, exc_type, exc, tb):
            raise aiomqtt.MqttError("simulated disconnect blip")

    monkeypatch.setattr(aiomqtt, "Client", ExitRaisingClient)
    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        async with p:
            pass  # exit path triggers the raise; should be swallowed

    _run(_go())  # no exception


# -- AwsIotPublisher (stub) ------------------------------------------------


def _tls() -> TlsConfig:
    return TlsConfig(
        cert_path="certs/cert.pem",
        key_path="certs/key.pem",
        ca_path="certs/ca.pem",
    )


def test_aws_iot_publisher_stores_config():
    p = AwsIotPublisher("mqtt://endpoint:8883", client_id="P-07", tls=_tls())
    assert p.url == "mqtt://endpoint:8883"
    assert p.client_id == "P-07"
    assert p.tls == _tls()


def test_aws_iot_publisher_aenter_raises_not_implemented():
    p = AwsIotPublisher("mqtt://endpoint:8883", client_id="P-00", tls=_tls())

    async def _go():
        with pytest.raises(NotImplementedError, match="ADR 0003"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_publisher_publish_raises_not_implemented():
    p = AwsIotPublisher("mqtt://endpoint:8883", client_id="P-00", tls=_tls())

    async def _go():
        with pytest.raises(NotImplementedError):
            await p.publish("any/topic", {"k": "v"})

    _run(_go())


def test_aws_iot_publisher_is_a_publisher():
    p = AwsIotPublisher("mqtt://endpoint:8883", client_id="P-00", tls=_tls())
    assert isinstance(p, Publisher)


# -- make_publisher --------------------------------------------------------


def test_make_publisher_local():
    p = make_publisher(
        target=BrokerTarget.LOCAL,
        url="mqtt://localhost:1883",
        client_id="P-00",
        tls=None,
    )
    assert isinstance(p, LocalPublisher)


def test_make_publisher_aws_iot():
    p = make_publisher(
        target=BrokerTarget.AWS_IOT,
        url="mqtt://endpoint:8883",
        client_id="P-00",
        tls=_tls(),
    )
    assert isinstance(p, AwsIotPublisher)
    assert p.tls == _tls()


def test_make_publisher_local_with_tls_rejects():
    """Defensive check: the loader rejects this combo, but if a caller
    constructs SimulatorConfig directly with a bad pairing, we still want
    a loud error."""
    with pytest.raises(PublisherError, match="tls set for local target"):
        make_publisher(
            target=BrokerTarget.LOCAL,
            url="mqtt://localhost:1883",
            client_id="P-00",
            tls=_tls(),
        )


def test_make_publisher_aws_iot_without_tls_rejects():
    with pytest.raises(PublisherError, match="tls missing for aws-iot"):
        make_publisher(
            target=BrokerTarget.AWS_IOT,
            url="mqtt://endpoint:8883",
            client_id="P-00",
            tls=None,
        )
