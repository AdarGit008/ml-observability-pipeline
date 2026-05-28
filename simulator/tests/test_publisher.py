"""Unit tests for simulator.publisher.

Real-broker validation is a documented manual smoke step
(``docker run -p 1883:1883 eclipse-mosquitto`` + ``mosquitto_sub`` for the
local path; AWS IoT MQTT test client for the aws-iot path — see
``docs/sessions/2026-05-27-simulator-aws-iot-publisher.md``); the unit
tests below monkeypatch ``aiomqtt.Client`` so they exercise the wire shape
(topic, payload, QoS, retain, TLS context) without needing a network. The
mTLS code paths in particular use placeholder cert files via ``tmp_path``
and a monkeypatched ``ssl.create_default_context`` so no real x.509
material lives in the test tree (DoD #4 of the 2026-05-27 brief).
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any

import aiomqtt
import pytest

from simulator import publisher as publisher_mod
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

    Mirrors the subset of ``aiomqtt.Client`` that the publishers use:
    ``__aenter__`` / ``__aexit__`` / ``publish``. Constructor signature
    matches aiomqtt v2 (hostname/port/identifier) and captures the optional
    ``tls_context`` kwarg used by ``AwsIotPublisher``.
    """

    instances: list["FakeAiomqttClient"] = []

    def __init__(
        self,
        *,
        hostname: str,
        port: int,
        identifier: str,
        tls_context: Any = None,
    ) -> None:
        self.hostname = hostname
        self.port = port
        self.identifier = identifier
        self.tls_context = tls_context
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


def _make_tls_files(tmp_path) -> TlsConfig:
    """Drop placeholder PEM-shaped files in tmp_path and return a TlsConfig.

    The files are not real certs — the SSL build step is monkeypatched in
    the tests that need it. The only thing we need from disk is for
    ``Path(...).is_file()`` to return True so the existence check passes.
    """
    cert = tmp_path / "P-00.cert.pem"
    key = tmp_path / "P-00.private.key"
    ca = tmp_path / "AmazonRootCA1.pem"
    cert.write_text("placeholder cert")
    key.write_text("placeholder key")
    ca.write_text("placeholder ca")
    return TlsConfig(
        cert_path=str(cert),
        key_path=str(key),
        ca_path=str(ca),
    )


class FakeSSLContext:
    """Captures load_cert_chain args; stands in for ssl.SSLContext."""

    def __init__(self) -> None:
        self.loaded_cert: str | None = None
        self.loaded_key: str | None = None
        self.load_cert_chain_calls = 0

    def load_cert_chain(self, *, certfile, keyfile):
        self.loaded_cert = certfile
        self.loaded_key = keyfile
        self.load_cert_chain_calls += 1


def _install_fake_ssl(monkeypatch) -> dict[str, Any]:
    """Monkeypatch ``ssl.create_default_context`` (as imported into
    ``simulator.publisher``) to return a ``FakeSSLContext`` and record the
    kwargs it was called with. Returns the capture dict.
    """
    captured: dict[str, Any] = {"ca": None, "purpose": None, "context": None}

    def fake_create(*, purpose=None, cafile=None):
        captured["purpose"] = purpose
        captured["ca"] = cafile
        ctx = FakeSSLContext()
        captured["context"] = ctx
        return ctx

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", fake_create
    )
    return captured


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
            assert client.tls_context is None  # LocalPublisher does not set TLS
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


# -- AwsIotPublisher (implementation; wired 2026-05-27 — see ADR 0003) -----


def test_aws_iot_publisher_stores_config(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher(
        "a1b2c3-ats.iot.eu-central-1.amazonaws.com",
        client_id="P-07",
        tls=tls,
    )
    assert p.url == "a1b2c3-ats.iot.eu-central-1.amazonaws.com"
    assert p.host == "a1b2c3-ats.iot.eu-central-1.amazonaws.com"
    assert p.port == 8883  # AWS IoT Core mTLS default
    assert p.client_id == "P-07"
    assert p.tls == tls


def test_aws_iot_publisher_is_a_publisher(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)
    assert isinstance(p, Publisher)


def test_aws_iot_publisher_default_port_is_8883(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher("endpoint.iot.eu-central-1.amazonaws.com", "P-00", tls)
    assert p.port == 8883


def test_aws_iot_publisher_explicit_port_overrides_default(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher("mqtts://endpoint:443", "P-00", tls)
    assert p.port == 443


def test_aws_iot_publisher_publish_before_aenter_raises(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="outside `async with`"):
            await p.publish("any/topic", {"hello": "world"})

    _run(_go())


def test_aws_iot_aenter_missing_cert_raises(tmp_path):
    """File-existence check on cert_path runs before any SSL/aiomqtt work."""
    # Only key + ca exist; cert_path points at a non-existent file.
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    key.write_text("k")
    ca.write_text("c")
    tls = TlsConfig(
        cert_path=str(tmp_path / "definitely-missing.cert.pem"),
        key_path=str(key),
        ca_path=str(ca),
    )
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="cert_path not found"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_aenter_missing_key_raises(tmp_path):
    cert = tmp_path / "cert.pem"
    ca = tmp_path / "ca.pem"
    cert.write_text("c")
    ca.write_text("ca")
    tls = TlsConfig(
        cert_path=str(cert),
        key_path=str(tmp_path / "missing.key"),
        ca_path=str(ca),
    )
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="key_path not found"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_aenter_missing_ca_raises(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("c")
    key.write_text("k")
    tls = TlsConfig(
        cert_path=str(cert),
        key_path=str(key),
        ca_path=str(tmp_path / "missing.ca.pem"),
    )
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="ca_path not found"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_aenter_missing_cert_does_not_touch_ssl_or_aiomqtt(
    tmp_path, monkeypatch
):
    """Existence check fires BEFORE ssl.create_default_context — confirms
    the cheap check runs first so a missing file produces a precise field
    name rather than 'ssl.SSLError: No such file or directory'."""
    captured = _install_fake_ssl(monkeypatch)
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)

    tls = TlsConfig(
        cert_path=str(tmp_path / "missing.cert.pem"),
        key_path=str(tmp_path / "key.pem"),  # also missing, but cert is checked first
        ca_path=str(tmp_path / "ca.pem"),
    )
    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherError, match="cert_path not found"):
            async with p:
                pass

    _run(_go())
    # Neither SSL nor aiomqtt were touched on the missing-file path.
    assert captured["context"] is None
    assert FakeAiomqttClient.instances == []


def test_aws_iot_aenter_builds_ssl_context_and_passes_to_aiomqtt(
    tmp_path, monkeypatch
):
    """Happy path: existence check passes, SSL context built with the right
    cafile/cert/key, aiomqtt.Client gets ``tls_context`` plus the parsed
    hostname/port/identifier."""
    tls = _make_tls_files(tmp_path)
    captured = _install_fake_ssl(monkeypatch)
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)

    p = AwsIotPublisher(
        "a1b2c3-ats.iot.eu-central-1.amazonaws.com",
        client_id="P-00",
        tls=tls,
    )

    async def _go():
        async with p:
            assert FakeAiomqttClient.instances, "aiomqtt.Client should have been constructed"
            client = FakeAiomqttClient.instances[-1]
            assert client.hostname == "a1b2c3-ats.iot.eu-central-1.amazonaws.com"
            assert client.port == 8883
            assert client.identifier == "P-00"
            # The context we built was handed to aiomqtt verbatim.
            assert client.tls_context is captured["context"]
            assert client.entered is True

    _run(_go())

    # ssl.create_default_context called with the CA path and SERVER_AUTH purpose.
    assert captured["purpose"] is ssl.Purpose.SERVER_AUTH
    assert captured["ca"] == tls.ca_path
    # load_cert_chain called exactly once with the right cert + key files.
    ctx = captured["context"]
    assert ctx.load_cert_chain_calls == 1
    assert ctx.loaded_cert == tls.cert_path
    assert ctx.loaded_key == tls.key_path
    # Exited cleanly.
    assert FakeAiomqttClient.instances[-1].exited is True


def test_aws_iot_aenter_wraps_ssl_error(tmp_path, monkeypatch):
    """A malformed cert / mismatched key / bad CA surfaces from
    ssl.SSLContext.* as ssl.SSLError; we wrap it in PublisherError with a
    message naming the files involved."""
    tls = _make_tls_files(tmp_path)

    class RaisingContext(FakeSSLContext):
        def load_cert_chain(self, *, certfile, keyfile):
            raise ssl.SSLError("bad PEM data")

    def fake_create(*, purpose=None, cafile=None):
        return RaisingContext()

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", fake_create
    )
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)

    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="failed to build TLS context"):
            async with p:
                pass

    _run(_go())
    # aiomqtt was never touched — we fail before the connect.
    assert FakeAiomqttClient.instances == []


def test_aws_iot_aenter_wraps_oserror_on_ca_read(tmp_path, monkeypatch):
    """Race condition: the cert was rotated mid-connect (file removed
    between the is_file() check and ssl.create_default_context opening
    it). The OSError should be wrapped as PublisherError, not bubble up."""
    tls = _make_tls_files(tmp_path)

    def raising_create(*, purpose=None, cafile=None):
        raise OSError("file removed mid-rotation")

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", raising_create
    )
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)

    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="failed to build TLS context"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_aenter_wraps_mqtt_error(tmp_path, monkeypatch):
    """TLS context built fine; the actual MQTT connect refused. Wrap as
    PublisherError so the runner's retry loop catches it the same way it
    does for local."""
    tls = _make_tls_files(tmp_path)
    _install_fake_ssl(monkeypatch)

    class RaisingClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            raise aiomqtt.MqttError("CONNREFUSED — policy mismatch")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(aiomqtt, "Client", RaisingClient)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        with pytest.raises(PublisherError, match="failed to connect to AWS IoT Core"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_publish_emits_qos0_retain_false_json(tmp_path, monkeypatch):
    """Wire shape parity with LocalPublisher: QoS 0, retain=False, JSON
    payload, same topic. ADR 0003 §Decision drives this — mode parity."""
    tls = _make_tls_files(tmp_path)
    _install_fake_ssl(monkeypatch)
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)

    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)
    payload = {
        "pump_id": "P-00",
        "ts": "2026-05-27T10:00:00.000Z",
        "vibration_amp": 0.31,
        "bearing_temp": 64.1,
        "motor_current": 4.5,
        "rpm": 1801.0,
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
    assert isinstance(raw_payload, (bytes, bytearray))
    assert json.loads(raw_payload.decode("utf-8")) == payload


def test_aws_iot_publish_wraps_mqtt_error(tmp_path, monkeypatch):
    tls = _make_tls_files(tmp_path)
    _install_fake_ssl(monkeypatch)

    class RaisingPublishClient(FakeAiomqttClient):
        async def publish(self, topic, *, payload, qos, retain):
            raise aiomqtt.MqttError("UNAUTHORIZED — policy denies publish")

    monkeypatch.setattr(aiomqtt, "Client", RaisingPublishClient)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        async with p:
            with pytest.raises(PublisherError, match="publish to .* failed"):
                await p.publish("factory/pumps/P-00/telemetry", {"k": "v"})

    _run(_go())


def test_aws_iot_aexit_is_idempotent(tmp_path, monkeypatch):
    tls = _make_tls_files(tmp_path)
    _install_fake_ssl(monkeypatch)
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        await p.__aenter__()
        await p.__aexit__(None, None, None)
        # Second exit is a no-op.
        await p.__aexit__(None, None, None)

    _run(_go())


def test_aws_iot_aexit_swallows_mqtt_error(tmp_path, monkeypatch):
    tls = _make_tls_files(tmp_path)
    _install_fake_ssl(monkeypatch)

    class ExitRaisingClient(FakeAiomqttClient):
        async def __aexit__(self, exc_type, exc, tb):
            raise aiomqtt.MqttError("simulated disconnect blip")

    monkeypatch.setattr(aiomqtt, "Client", ExitRaisingClient)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        async with p:
            pass

    _run(_go())  # no exception


# -- make_publisher --------------------------------------------------------


def test_make_publisher_local():
    p = make_publisher(
        target=BrokerTarget.LOCAL,
        url="mqtt://localhost:1883",
        client_id="P-00",
        tls=None,
    )
    assert isinstance(p, LocalPublisher)


def test_make_publisher_aws_iot(tmp_path):
    tls = _make_tls_files(tmp_path)
    p = make_publisher(
        target=BrokerTarget.AWS_IOT,
        url="endpoint.iot.eu-central-1.amazonaws.com",
        client_id="P-00",
        tls=tls,
    )
    assert isinstance(p, AwsIotPublisher)
    assert p.tls == tls


def test_make_publisher_local_with_tls_rejects(tmp_path):
    """Defensive check: the loader rejects this combo, but if a caller
    constructs SimulatorConfig directly with a bad pairing, we still want
    a loud error."""
    tls = _make_tls_files(tmp_path)
    with pytest.raises(PublisherError, match="tls set for local target"):
        make_publisher(
            target=BrokerTarget.LOCAL,
            url="mqtt://localhost:1883",
            client_id="P-00",
            tls=tls,
        )


def test_make_publisher_aws_iot_without_tls_rejects():
    with pytest.raises(PublisherError, match="tls missing for aws-iot"):
        make_publisher(
            target=BrokerTarget.AWS_IOT,
            url="endpoint",
            client_id="P-00",
            tls=None,
        )
