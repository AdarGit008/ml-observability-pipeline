"""Unit tests for PublisherConfigError — the static-config carve-out
from retry-forever added 2026-05-28 per Gemini Q3 review.

Background: a missing cert / malformed PEM / unparseable URL is a static
configuration problem. The 2026-05-27 implementation raised plain
``PublisherError`` for these, which fed the retry-forever loop and
produced "cert_path not found" warnings every 30 s indefinitely. Gemini's
Q3 disposition: introduce ``PublisherConfigError`` (subclass), have the
runner halt the fleet on it, retry only on the transient parent class.

These tests pin: (1) the right exceptions raise the subclass, not the
parent; (2) MqttError on connect still raises the parent (transient);
(3) the subclass IS-A parent so generic ``except PublisherError``
sites keep working; (4) ValueError on cert load (Gemini Q2 — encrypted
PKCS#8 key without password) is now caught and wrapped.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiomqtt
import pytest

from simulator import publisher as publisher_mod
from simulator.config import TlsConfig
from simulator.publisher import (
    AwsIotPublisher,
    LocalPublisher,
    PublisherConfigError,
    PublisherError,
)


def _run(coro):
    return asyncio.run(coro)


def _make_tls_files(tmp_path) -> TlsConfig:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    cert.write_text("c")
    key.write_text("k")
    ca.write_text("ca")
    return TlsConfig(cert_path=str(cert), key_path=str(key), ca_path=str(ca))


# -- Subclass relationship (load-bearing for runner.py's except ordering) --


def test_publisher_config_error_is_a_publisher_error():
    """generic ``except PublisherError`` sites still catch
    ``PublisherConfigError`` — load-bearing for the runner's behavior
    when a future caller adds a generic catch and doesn't know about
    the subclass."""
    assert issubclass(PublisherConfigError, PublisherError)


def test_publisher_config_error_can_be_raised_and_caught():
    """Smoke: confirm the class is constructible and the parent catches it."""
    try:
        raise PublisherConfigError("smoke")
    except PublisherError as e:
        assert isinstance(e, PublisherConfigError)


# -- AwsIotPublisher: missing file raises PublisherConfigError -------------


def test_aws_iot_missing_cert_raises_config_error(tmp_path):
    """Per Gemini Q3: missing-file is a static config error, not
    transient. Must raise the SUBCLASS so the runner halts the fleet."""
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    key.write_text("k")
    ca.write_text("ca")
    tls = TlsConfig(
        cert_path=str(tmp_path / "missing.pem"),
        key_path=str(key),
        ca_path=str(ca),
    )
    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="cert_path not found"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_missing_key_raises_config_error(tmp_path):
    cert = tmp_path / "cert.pem"
    ca = tmp_path / "ca.pem"
    cert.write_text("c")
    ca.write_text("ca")
    tls = TlsConfig(
        cert_path=str(cert),
        key_path=str(tmp_path / "missing.key"),
        ca_path=str(ca),
    )
    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="key_path not found"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_missing_ca_raises_config_error(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("c")
    key.write_text("k")
    tls = TlsConfig(
        cert_path=str(cert),
        key_path=str(key),
        ca_path=str(tmp_path / "missing.ca"),
    )
    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="ca_path not found"):
            async with p:
                pass

    _run(_go())


# -- AwsIotPublisher: SSL/OS/ValueError on load_cert_chain -> ConfigError --


def test_aws_iot_ssl_error_raises_config_error(tmp_path, monkeypatch):
    """Malformed PEM, key/cert mismatch -> ssl.SSLError. Should wrap
    as PublisherConfigError so the runner halts."""
    tls = _make_tls_files(tmp_path)

    class RaisingContext:
        def load_cert_chain(self, *, certfile, keyfile):
            raise ssl.SSLError("bad PEM")

    def fake_create(*, purpose=None, cafile=None):
        return RaisingContext()

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", fake_create
    )

    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="failed to build TLS context"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_value_error_raises_config_error(tmp_path, monkeypatch):
    """Per Gemini Q2: encrypted PKCS#8 key without a password (and a
    few other malformed-PEM cases) raise ValueError, not SSLError. Must
    be wrapped as PublisherConfigError too — without this catch, the
    runner crashes with an unhandled ValueError instead of producing
    a clean halt-the-fleet message."""
    tls = _make_tls_files(tmp_path)

    class RaisingContext:
        def load_cert_chain(self, *, certfile, keyfile):
            raise ValueError(
                "Could not deserialize key data. "
                "The data may be in an incorrect format, the provided password "
                "may be incorrect, or it may be encrypted with an unsupported "
                "algorithm."
            )

    def fake_create(*, purpose=None, cafile=None):
        return RaisingContext()

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", fake_create
    )

    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="failed to build TLS context"):
            async with p:
                pass

    _run(_go())


def test_aws_iot_oserror_raises_config_error(tmp_path, monkeypatch):
    """Race condition: cert removed mid-__aenter__ between the
    is_file() check and create_default_context opening the cafile."""
    tls = _make_tls_files(tmp_path)

    def raising_create(*, purpose=None, cafile=None):
        raise OSError("file removed mid-rotation")

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", raising_create
    )

    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherConfigError, match="failed to build TLS context"):
            async with p:
                pass

    _run(_go())


# -- AwsIotPublisher: MqttError on connect stays transient (NOT config) ----


def test_aws_iot_mqtt_error_on_connect_is_transient(tmp_path, monkeypatch):
    """Network down, CONNREFUSED, policy mismatch — these ARE transient.
    The broker might come back, the policy might get fixed, the network
    might recover. Must stay PublisherError (parent class) so the runner
    retries instead of halting. Pinning this distinction explicitly."""
    tls = _make_tls_files(tmp_path)

    class FakeContext:
        def load_cert_chain(self, *, certfile, keyfile):
            pass

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context",
        lambda *, purpose=None, cafile=None: FakeContext(),
    )

    class RaisingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise aiomqtt.MqttError("CONNREFUSED")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(aiomqtt, "Client", RaisingClient)

    p = AwsIotPublisher("endpoint", "P-00", tls)

    async def _go():
        with pytest.raises(PublisherError) as exc_info:
            async with p:
                pass
        # CRITICAL: must NOT be the subclass, or the runner would halt
        # the fleet on a transient network blip.
        assert not isinstance(exc_info.value, PublisherConfigError), (
            "MqttError on connect must remain transient PublisherError, "
            "not promote to PublisherConfigError"
        )

    _run(_go())


# -- LocalPublisher: bad URL -> ConfigError --------------------------------


def test_local_publisher_bad_url_raises_config_error_at_construction():
    """Empty URL fails in _parse_mqtt_url during __init__. Bad URL is
    static config, so raise the subclass — the error surfaces from
    Fleet.from_config, caught by main() with exit code 4."""
    with pytest.raises(PublisherConfigError, match="could not parse host"):
        LocalPublisher("", client_id="P-00")


def test_aws_iot_publisher_bad_url_raises_config_error_at_construction(tmp_path):
    tls = _make_tls_files(tmp_path)
    with pytest.raises(PublisherConfigError, match="could not parse host"):
        AwsIotPublisher("", "P-00", tls)
