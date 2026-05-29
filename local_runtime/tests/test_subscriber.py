"""Tests for local_runtime.subscriber.TelemetrySubscriber.

Mirrors simulator/tests/test_publisher.py's monkeypatching style —
``aiomqtt.Client`` is replaced with a fake that records subscription
+ yields canned messages. No real broker is touched. Real-broker
validation is the docs/sessions smoke step (mosquitto + simulator).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable

import aiomqtt
import pytest

from local_runtime import subscriber as sub_mod
from local_runtime.subscriber import (
    SubscriberError,
    TELEMETRY_WILDCARD,
    TelemetrySubscriber,
    retry_forever,
)


def _run(coro):
    return asyncio.run(coro)


class FakeTopic:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = FakeTopic(topic)
        self.payload = payload


class FakeAiomqttClient:
    """Records subscribe + yields a canned message stream."""

    instances: list["FakeAiomqttClient"] = []
    queued_messages: list[FakeMessage] = []

    def __init__(self, *, hostname: str, port: int, identifier: str) -> None:
        self.hostname = hostname
        self.port = port
        self.identifier = identifier
        self.entered = False
        self.subscribed_to: list[str] = []
        FakeAiomqttClient.instances.append(self)

    async def __aenter__(self) -> "FakeAiomqttClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def subscribe(self, topic: str) -> None:
        self.subscribed_to.append(topic)

    @property
    def messages(self):
        return _FakeMessageIter(list(FakeAiomqttClient.queued_messages))


class _FakeMessageIter:
    def __init__(self, msgs: Iterable[FakeMessage]) -> None:
        self._msgs = iter(msgs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._msgs)
        except StopIteration:
            raise StopAsyncIteration


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeAiomqttClient.instances.clear()
    FakeAiomqttClient.queued_messages.clear()
    yield
    FakeAiomqttClient.instances.clear()
    FakeAiomqttClient.queued_messages.clear()


def _valid_payload(pump_id: str = "P-00") -> bytes:
    return json.dumps({
        "pump_id": pump_id,
        "ts": "2026-05-29T12:00:00.000Z",
        "vibration_amp": 0.3,
        "bearing_temp": 65.0,
        "motor_current": 4.5,
        "rpm": 1800.0,
    }).encode("utf-8")


def test_subscriber_properties_parse_url():
    s = TelemetrySubscriber("mqtt://broker.local:1884", client_id="sub-01")
    assert s.host == "broker.local"
    assert s.port == 1884
    assert s.client_id == "sub-01"


def test_subscriber_bare_host_uses_default_port():
    s = TelemetrySubscriber("localhost", client_id="sub-01")
    assert s.host == "localhost"
    assert s.port == 1883


def test_subscriber_aenter_constructs_and_subscribes(monkeypatch):
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")

    async def _go():
        async with s:
            assert FakeAiomqttClient.instances, "client should be constructed"
            client = FakeAiomqttClient.instances[-1]
            assert client.hostname == "localhost"
            assert client.port == 1883
            assert client.identifier == "sub-01"
            assert client.entered is True
            assert client.subscribed_to == [TELEMETRY_WILDCARD]

    _run(_go())


def test_subscriber_aenter_wraps_mqtt_error(monkeypatch):
    class RaisingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise aiomqtt.MqttError("connect refused")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(aiomqtt, "Client", RaisingClient)
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")

    async def _go():
        with pytest.raises(SubscriberError, match="failed to connect/subscribe"):
            async with s:
                pass

    _run(_go())


def test_subscriber_messages_outside_context_raises():
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")

    async def _go():
        with pytest.raises(SubscriberError, match="outside `async with`"):
            async for _ in s.messages():
                pass

    _run(_go())


def test_subscriber_yields_parsed_messages(monkeypatch):
    """Wildcard match → (pump_id, dict) yielded."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    FakeAiomqttClient.queued_messages.append(
        FakeMessage("factory/pumps/P-07/telemetry", _valid_payload("P-07"))
    )
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")
    collected: list[tuple[str, dict]] = []

    async def _go():
        async with s:
            async for pump_id, payload in s.messages():
                collected.append((pump_id, payload))

    _run(_go())
    assert len(collected) == 1
    pump_id, payload = collected[0]
    assert pump_id == "P-07"
    assert payload["vibration_amp"] == 0.3


def test_subscriber_skips_non_matching_topics(monkeypatch):
    """A message on a similar-shape but non-matching topic is ignored."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    FakeAiomqttClient.queued_messages.extend([
        FakeMessage("factory/pumps/X/telemetry", _valid_payload("X")),  # non-canonical
        FakeMessage("factory/pumps/P-01/telemetry", _valid_payload("P-01")),  # good
    ])
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")
    collected = []

    async def _go():
        async with s:
            async for pump_id, _ in s.messages():
                collected.append(pump_id)

    _run(_go())
    assert collected == ["P-01"]


def test_subscriber_skips_invalid_json(monkeypatch):
    """Garbled payload is skipped, not raised — one bad message
    shouldn't kill the consumer."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    FakeAiomqttClient.queued_messages.extend([
        FakeMessage("factory/pumps/P-00/telemetry", b"not json"),
        FakeMessage("factory/pumps/P-01/telemetry", _valid_payload("P-01")),
    ])
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")
    collected = []

    async def _go():
        async with s:
            async for pump_id, _ in s.messages():
                collected.append(pump_id)

    _run(_go())
    assert collected == ["P-01"]


def test_subscriber_skips_non_dict_payload(monkeypatch):
    """A JSON payload that's a list/scalar (not a dict) is skipped."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    FakeAiomqttClient.queued_messages.append(
        FakeMessage("factory/pumps/P-00/telemetry", b"[1,2,3]")
    )
    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")
    collected = []

    async def _go():
        async with s:
            async for pump_id, _ in s.messages():
                collected.append(pump_id)

    _run(_go())
    assert collected == []


def test_subscriber_run_drives_handler(monkeypatch):
    """run(handler) awaits handler once per message."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    FakeAiomqttClient.queued_messages.extend([
        FakeMessage("factory/pumps/P-00/telemetry", _valid_payload("P-00")),
        FakeMessage("factory/pumps/P-01/telemetry", _valid_payload("P-01")),
    ])
    seen = []

    async def handler(pump_id: str, telemetry: dict):
        seen.append(pump_id)

    s = TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")

    async def _go():
        async with s:
            await s.run(handler)

    _run(_go())
    assert seen == ["P-00", "P-01"]


def test_retry_forever_reconnects_after_subscriber_error(monkeypatch):
    """SubscriberError from one cycle triggers a sleep + new factory call."""
    monkeypatch.setattr(aiomqtt, "Client", FakeAiomqttClient)
    # First factory call: raises. Second: succeeds with one message.
    cycle = {"i": 0}

    class BadSub:
        async def __aenter__(self):
            raise SubscriberError("transient")

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def run(self, handler):
            pass  # pragma: no cover

    def factory():
        cycle["i"] += 1
        if cycle["i"] == 1:
            return BadSub()
        # Second cycle uses the real TelemetrySubscriber with a fake aiomqtt.
        FakeAiomqttClient.queued_messages.clear()
        FakeAiomqttClient.queued_messages.append(
            FakeMessage("factory/pumps/P-00/telemetry", _valid_payload("P-00"))
        )
        return TelemetrySubscriber("mqtt://localhost:1883", client_id="sub-01")

    seen = []

    async def handler(pump_id, telemetry):
        seen.append(pump_id)
        # Stop the loop after the first real message — without this,
        # retry_forever would loop forever on the empty third cycle.
        raise _StopRetry()

    class _StopRetry(Exception):
        pass

    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    async def _go():
        with pytest.raises(_StopRetry):
            await retry_forever(
                factory,
                handler,
                initial_backoff=0.5,
                max_backoff=4.0,
                sleep=fake_sleep,
            )

    _run(_go())
    assert seen == ["P-00"]
    # One backoff slept after the BadSub cycle
    assert slept == [0.5]
