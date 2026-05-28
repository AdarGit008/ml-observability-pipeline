"""Unit tests for the publisher disconnect timeout (added 2026-05-28).

Background: the 2026-05-27 AwsIotPublisher smoke test surfaced a Windows
shutdown bug. Ctrl+C set the shutdown event and the per-pump task exited
its inner loop, but the publisher's ``__aexit__`` blocked indefinitely
on what looked like a TLS close_notify handshake (or paho's keepalive
flush). The fix in ``simulator/publisher.py`` wraps the inner
``aiomqtt.Client.__aexit__`` in ``asyncio.wait_for`` with a
``DISCONNECT_TIMEOUT_SECONDS`` ceiling (default 3.0 s, monkeypatchable).

These tests use a FakeAiomqttClient whose ``__aexit__`` blocks forever to
prove the wait_for ceiling kicks in and the publisher returns. They keep
the timeout tiny (10 ms) so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiomqtt
import pytest

from simulator import publisher as publisher_mod
from simulator.config import TlsConfig
from simulator.publisher import AwsIotPublisher, LocalPublisher


def _run(coro):
    return asyncio.run(coro)


class HangingAiomqttClient:
    """Connects fine; __aexit__ blocks forever. Mirrors the smoke-test
    failure mode where a TLS close_notify / keepalive flush stalls."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "HangingAiomqttClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Block forever; the wrapper's asyncio.wait_for should cancel us.
        await asyncio.Event().wait()

    async def publish(self, topic: str, *, payload: Any, qos: int, retain: bool) -> None:  # pragma: no cover
        pass


def _make_tls_files(tmp_path) -> TlsConfig:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    ca = tmp_path / "ca.pem"
    cert.write_text("c")
    key.write_text("k")
    ca.write_text("ca")
    return TlsConfig(cert_path=str(cert), key_path=str(key), ca_path=str(ca))


def _install_fake_ssl(monkeypatch):
    class FakeCtx:
        def load_cert_chain(self, *, certfile, keyfile):
            pass

    def fake_create(*, purpose=None, cafile=None):
        return FakeCtx()

    monkeypatch.setattr(
        publisher_mod.ssl, "create_default_context", fake_create
    )


def test_local_publisher_aexit_bounded_by_timeout(monkeypatch, caplog):
    monkeypatch.setattr(publisher_mod, "DISCONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(aiomqtt, "Client", HangingAiomqttClient)

    p = LocalPublisher("mqtt://localhost:1883", client_id="P-00")

    async def _go():
        # Enter cleanly, then leave — the inner aexit hangs, but the
        # wait_for ceiling must let our async with return.
        async with p:
            pass

    with caplog.at_level(logging.WARNING, logger="simulator.publisher"):
        # If the timeout doesn't fire, asyncio.run hangs and pytest times
        # us out. Belt-and-braces: wrap in our own wait_for too.
        _run(asyncio.wait_for(_go(), timeout=2.0))

    # The warning naming the client_id and the timeout value confirms the
    # forced-disconnect path fired.
    assert any(
        "MQTT disconnect for P-00 timed out" in r.message
        for r in caplog.records
    ), "expected forced-disconnect warning was not logged"


def test_aws_iot_publisher_aexit_bounded_by_timeout(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(publisher_mod, "DISCONNECT_TIMEOUT_SECONDS", 0.01)
    _install_fake_ssl(monkeypatch)
    monkeypatch.setattr(aiomqtt, "Client", HangingAiomqttClient)

    tls = _make_tls_files(tmp_path)
    p = AwsIotPublisher("endpoint", client_id="P-00", tls=tls)

    async def _go():
        async with p:
            pass

    with caplog.at_level(logging.WARNING, logger="simulator.publisher"):
        _run(asyncio.wait_for(_go(), timeout=2.0))

    assert any(
        "AWS IoT disconnect for P-00 timed out" in r.message
        for r in caplog.records
    ), "expected forced-disconnect warning was not logged for aws-iot"


def test_disconnect_timeout_constant_is_overridable_via_monkeypatch():
    """Defensive: the constant is module-level on simulator.publisher so
    runner tests / smoke harnesses can dial it down without changing the
    publisher code. Pinning the lookup path so a future rename surfaces."""
    assert hasattr(publisher_mod, "DISCONNECT_TIMEOUT_SECONDS")
    assert publisher_mod.DISCONNECT_TIMEOUT_SECONDS > 0.0
