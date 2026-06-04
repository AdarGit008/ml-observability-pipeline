"""Tests for local_runtime.influx_writer.

Pins:
- build_point shape (measurement, tags, fields, time).
- pump_id is a tag (not a field).
- All 8 features + score + 4 PSI fields land as flat numeric fields on
  compute ticks; psi=None tick omits psi_* fields entirely.
  Field count: 13 on compute ticks (ADR 0009 shrank PSI surface 8→4),
  9 on non-compute ticks (unchanged).
- Timestamp is normalized to UTC.
- The writer uses ``InfluxDBClientAsync`` (native async, per Gemini Q4
  of the 2026-05-29 review) -- not the sync client wrapped in
  ``asyncio.to_thread``. Tests inject an async-context-manager fake.

Drift session 2026-06-01 added the psi=None case (ADR 0007 every-Nth-
tick cadence -- non-compute ticks carry no PSI, the writer omits the
fields so InfluxDB stores nulls).

ADR 0009 (2026-06-03) shrank the PSI surface to 4 raw features. The
``psi_*`` field set goes from 8 to 4: ``psi_vibration_amp_mean_5m``,
``psi_vibration_amp_std_5m``, ``psi_bearing_temp_mean_5m``, and
``psi_bearing_temp_std_5m`` are no longer emitted. The 17-field-per-
point assertion becomes a 13-field assertion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from local_runtime.config import InfluxConfig
from local_runtime.influx_writer import (
    MEASUREMENT,
    InfluxWriter,
    ScoredRow,
    build_point,
)
from shared.features import FEATURE_NAMES, PSI_FEATURE_NAMES


def _run(coro):
    return asyncio.run(coro)


_SENTINEL = object()


def _features_all_set(default: float = 1.0) -> dict[str, float]:
    return {name: default + i for i, name in enumerate(FEATURE_NAMES)}


def _psi_all_set(default: float = 0.05) -> dict[str, float]:
    """PSI dict covering exactly ``PSI_FEATURE_NAMES`` (4 keys, ADR 0009)."""
    return {name: default for name in PSI_FEATURE_NAMES}


def _row(
    pump_id: str = "P-00",
    ts: datetime | None = None,
    features: dict[str, float] | None = None,
    score: float = 0.42,
    psi=_SENTINEL,
) -> ScoredRow:
    # Sentinel rather than ``psi or default`` so callers can pass
    # ``psi=None`` explicitly without it being replaced by the default
    # dict -- needed for the non-compute-tick test below.
    psi_value = _psi_all_set() if psi is _SENTINEL else psi
    return ScoredRow(
        pump_id=pump_id,
        timestamp=ts or datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc),
        features=features or _features_all_set(),
        score=score,
        psi=psi_value,
    )


# -- build_point ------------------------------------------------------------


def test_build_point_measurement_pinned():
    point = build_point(_row())
    assert point["measurement"] == MEASUREMENT
    assert MEASUREMENT == "pump_telemetry"


def test_build_point_pump_id_is_tag_not_field():
    """Pinned: pump_id must be a tag (indexed). Field would break query plans."""
    point = build_point(_row(pump_id="P-07"))
    assert point["tags"] == {"pump_id": "P-07"}
    assert "pump_id" not in point["fields"]


def test_build_point_all_eight_features_as_fields():
    """All 8 FEATURE_NAMES land as float fields with the right names.

    ADR 0009 only shrinks the PSI surface, not the feature dump --
    the model still consumes all 8 features, and Grafana panels read
    raw rolling-feature values without PSI on them.
    """
    point = build_point(_row())
    for name in FEATURE_NAMES:
        assert name in point["fields"], f"missing feature field {name!r}"
        assert isinstance(point["fields"][name], float)


def test_build_point_score_as_field():
    point = build_point(_row(score=0.73))
    assert point["fields"]["score"] == 0.73


def test_build_point_psi_fields_prefixed():
    """PSI values land as flat psi_<feature> fields, not nested.
    Per ADR 0009 only the 4 PSI surface names get psi_ fields."""
    psi_in = {name: 0.1 + i for i, name in enumerate(PSI_FEATURE_NAMES)}
    point = build_point(_row(psi=psi_in))
    for i, name in enumerate(PSI_FEATURE_NAMES):
        key = f"psi_{name}"
        assert key in point["fields"]
        assert point["fields"][key] == pytest.approx(0.1 + i)


def test_build_point_psi_fields_do_not_include_rolling_features():
    """ADR 0009: psi_<rolling-feature> fields are NOT emitted.

    A regression here would re-introduce the autocorrelation noise
    that ADR 0009 closes by dropping the rolling features from the
    PSI surface entirely.
    """
    point = build_point(_row())
    rolling_features = (
        "vibration_amp_mean_5m",
        "vibration_amp_std_5m",
        "bearing_temp_mean_5m",
        "bearing_temp_std_5m",
    )
    for name in rolling_features:
        assert f"psi_{name}" not in point["fields"], (
            f"psi_{name} should not be emitted (ADR 0009 PSI surface "
            f"is PSI_FEATURE_NAMES = {PSI_FEATURE_NAMES})"
        )


def test_build_point_missing_psi_feature_defaults_to_zero():
    """A sparse PSI dict (only the warning band) gets zeroes for the
    other PSI surface names. Rolling features are not in the output
    regardless."""
    point = build_point(_row(psi={"vibration_amp": 0.18}))
    assert point["fields"]["psi_vibration_amp"] == 0.18
    for name in PSI_FEATURE_NAMES:
        if name != "vibration_amp":
            assert point["fields"][f"psi_{name}"] == 0.0


def test_build_point_psi_none_omits_psi_fields():
    """When psi is None (non-compute tick under ADR 0007 cadence),
    build_point omits all psi_* fields so InfluxDB stores nulls.
    Grafana's ``last`` aggregator then surfaces the most recent
    computed value rather than snapping to 0 between computes."""
    point = build_point(_row(psi=None))
    for name in PSI_FEATURE_NAMES:
        assert f"psi_{name}" not in point["fields"], (
            f"psi_{name} should not be present when row.psi is None"
        )
    # Features + score still present.
    for name in FEATURE_NAMES:
        assert name in point["fields"]
    assert "score" in point["fields"]


def test_build_point_field_count_with_psi():
    """8 features + score + 4 psi_ = 13 fields on a compute tick
    (ADR 0009 surface shrink; was 17 pre-ADR-0009). Pinned so a stray
    field addition shows up loudly in the test diff."""
    point = build_point(_row())
    assert len(point["fields"]) == 13


def test_build_point_field_count_without_psi():
    """8 features + score = 9 fields on a non-compute tick (psi=None).
    Unchanged by ADR 0009 — the psi_* fields are already omitted in
    this branch."""
    point = build_point(_row(psi=None))
    assert len(point["fields"]) == 9


def test_build_point_missing_feature_raises_keyerror():
    """A missing feature in ScoredRow.features is a hard error."""
    sparse = {name: 1.0 for name in FEATURE_NAMES if name != "rpm"}
    with pytest.raises(KeyError, match="rpm"):
        build_point(_row(features=sparse))


def test_build_point_timestamp_passes_through_when_utc():
    ts = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    point = build_point(_row(ts=ts))
    assert point["time"] == ts


def test_build_point_normalizes_naive_to_utc():
    ts = datetime(2026, 5, 29, 12, 0, 0)  # naive
    point = build_point(_row(ts=ts))
    assert point["time"].tzinfo is not None
    assert point["time"].utcoffset().total_seconds() == 0


def test_build_point_converts_non_utc_tz():
    tz = timezone(timedelta(hours=2))
    ts = datetime(2026, 5, 29, 14, 0, 0, tzinfo=tz)
    point = build_point(_row(ts=ts))
    # 14:00 +02:00 == 12:00 UTC
    assert point["time"].hour == 12
    assert point["time"].utcoffset().total_seconds() == 0


# -- InfluxWriter (with async-context fake client) -------------------------


class FakeWriteApi:
    """Async fake -- mirrors WriteApiAsync.write signature.

    Per Gemini Q4 (2026-05-29 review), the real WriteApiAsync.write
    is async def. The fake matches that shape so tests exercise the
    same awaited code path as production.
    """

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def write(self, *, bucket: str, org: str, record: Any) -> None:
        self.writes.append({"bucket": bucket, "org": org, "record": record})


class FakeClient:
    """Async-context-manager fake for InfluxDBClientAsync."""

    instances: list["FakeClient"] = []

    def __init__(self, *, url: str, token: str, org: str) -> None:
        self.url = url
        self.token = token
        self.org = org
        self.api = FakeWriteApi()
        self.entered = False
        self.exited = False
        FakeClient.instances.append(self)

    async def __aenter__(self) -> "FakeClient":
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    def write_api(self) -> FakeWriteApi:
        return self.api


@pytest.fixture
def cfg() -> InfluxConfig:
    return InfluxConfig(
        url="http://localhost:8086",
        token="test-token",
        org="ml-obs",
        bucket="pump_telemetry",
    )


@pytest.fixture(autouse=True)
def _reset_fake_clients():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


def test_writer_uses_injected_factory(cfg):
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        async with writer:
            assert FakeClient.instances
            client = FakeClient.instances[-1]
            assert client.url == cfg.url
            assert client.token == cfg.token
            assert client.org == cfg.org

    _run(_go())


def test_writer_write_outside_context_raises(cfg):
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        with pytest.raises(RuntimeError, match="outside"):
            await writer.write(_row())

    _run(_go())


def test_writer_write_passes_bucket_and_point(cfg):
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        async with writer:
            await writer.write(_row(pump_id="P-03"))

    _run(_go())
    client = FakeClient.instances[-1]
    assert len(client.api.writes) == 1
    write = client.api.writes[0]
    assert write["bucket"] == cfg.bucket
    assert write["org"] == cfg.org
    assert write["record"]["tags"]["pump_id"] == "P-03"
    assert write["record"]["measurement"] == "pump_telemetry"


def test_writer_enters_underlying_client_context(cfg):
    """The fake's __aenter__ is awaited; if it isn't, write_api would
    be called before the client's session is open."""
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        async with writer:
            pass

    _run(_go())
    client = FakeClient.instances[-1]
    assert client.entered is True


def test_writer_aexit_exits_async_client_context(cfg):
    """The underlying InfluxDBClientAsync's __aexit__ closes the aiohttp
    session -- we just delegate. Pin that we delegate."""
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        async with writer:
            pass

    _run(_go())
    client = FakeClient.instances[-1]
    assert client.entered is True
    assert client.exited is True


def test_writer_write_is_awaited_not_to_thread(cfg):
    """Sanity test for Gemini Q4: the write is an awaited async call,
    not a sync call wrapped in asyncio.to_thread. The FakeWriteApi
    defines write as async def; if the writer ever regressed to a sync
    wrap, awaiting a non-coroutine would fail loudly."""
    writer = InfluxWriter(cfg, client_factory=FakeClient)

    async def _go():
        async with writer:
            await writer.write(_row(pump_id="P-02"))

    _run(_go())
    client = FakeClient.instances[-1]
    assert len(client.api.writes) == 1
    assert client.api.writes[0]["record"]["tags"]["pump_id"] == "P-02"
