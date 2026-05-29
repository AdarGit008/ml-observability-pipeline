"""InfluxDB writer — translates feature rows to Influx v2 points.

Schema decision (see ADR 0005 for the full trade-off discussion):

- Measurement: pump_telemetry (one row per scored reading).
- Tags: pump_id (15-100 cardinality; low enough that InfluxDB v2's
  TSI index handles it cleanly).
- Fields: the 8 features + score + 8 per-feature PSI values
  (psi_<feature>) = 17 float fields per point.
- Timestamp: from the telemetry payload's ts (ISO-8601 UTC). Falls
  back to "now" if the upstream message is missing it, with a
  WARNING — sensor data without a timestamp is a bug worth flagging.

Why pump_id is a tag and not a field: tags are indexed in InfluxDB
and queries like pump_id="P-07" are O(log n). A field would require
a full scan. With Grafana's per-pump panel filter being the most
common query, pump_id-as-tag is the obvious shape.

Why psi_<feature> instead of a nested psi map: InfluxDB's line
protocol doesn't support nested fields. We could JSON-encode the PSI
dict into a single field, but flat columns play nicer with Grafana's
panel queries (one field = one series, no transforms needed).

Why InfluxDBClientAsync rather than the sync client wrapped in
asyncio.to_thread: the official Python client ships an aiohttp-backed
async API (influxdb_client.client.influxdb_client_async) since 1.36.
The sync-client + to_thread pattern would have one of two costs at
any meaningful throughput: thread-pool thrash (if every write
allocates a worker) or GIL contention on the asyncio loop thread.
Native async sidesteps both — per Gemini Q4 of the 2026-05-29 review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol

from local_runtime.config import InfluxConfig
from shared.features import FEATURE_NAMES


log = logging.getLogger(__name__)


# Influx measurement name. Stable across schema iterations so Grafana
# dashboards don't break on a rename. If a future session needs to
# change shape, prefer adding new fields (or a new measurement) over
# renaming.
MEASUREMENT = "pump_telemetry"


@dataclass(frozen=True)
class ScoredRow:
    """One row in the pump_telemetry measurement.

    Frozen dataclass so writers can pass it around without worrying
    about callers mutating between formation and the actual write.
    The InfluxDB writer translates this into a Point; tests assert on
    the Point shape rather than the dataclass shape because the
    dataclass is a fence the test side can see directly.
    """

    pump_id: str
    timestamp: datetime
    features: Mapping[str, float]
    score: float
    psi: Mapping[str, float]


class _WriteApiLike(Protocol):
    """Minimal async write-api interface.

    influxdb_client.client.write_api_async.WriteApiAsync exposes
    async def write(bucket, org, record, **kwargs); fake test clients
    only need to match that one method.
    """

    async def write(
        self, *, bucket: str, org: str, record: Any
    ) -> Any: ...


class _InfluxClientLike(Protocol):
    """Minimal async-client interface we need.

    The real client (InfluxDBClientAsync) is an async context manager.
    We enter it in InfluxWriter.__aenter__ and grab a write-api once
    for the lifetime of the writer.
    """

    async def __aenter__(self) -> "_InfluxClientLike": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    def write_api(self) -> _WriteApiLike: ...


# Factory signature: (url, token, org) -> _InfluxClientLike. Kept as a
# separate type alias so the test sites can inject a fake without
# pulling in the real influxdb-client module.
ClientFactory = Callable[..., _InfluxClientLike]


class InfluxWriter:
    """Native-async writer for the pump_telemetry measurement.

    Lifecycle:
        async with InfluxWriter(config) as writer:
            await writer.write(scored_row)

    The underlying InfluxDBClientAsync is itself an async context
    manager; we enter it in __aenter__ and exit it in __aexit__. Each
    write call goes through the aiohttp-backed WriteApiAsync.write so
    the asyncio loop stays responsive without thread-pool indirection.
    """

    def __init__(
        self,
        config: InfluxConfig,
        *,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self._config = config
        # Allow tests to inject a fake client factory without
        # monkeypatching the influxdb_client module — easier read at
        # the test sites and avoids order-of-import gotchas.
        self._client_factory = client_factory
        self._client: Optional[_InfluxClientLike] = None
        self._write_api: Optional[_WriteApiLike] = None

    async def __aenter__(self) -> "InfluxWriter":
        factory = self._client_factory or _default_client_factory
        client = factory(
            url=self._config.url,
            token=self._config.token,
            org=self._config.org,
        )
        # Enter the underlying client's async context (this is where
        # the aiohttp session is opened).
        await client.__aenter__()
        self._client = client
        # write_api() is sync-returning a WriteApiAsync; grab it once
        # so the per-message path is just an await.
        self._write_api = client.write_api()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        self._write_api = None
        try:
            await client.__aexit__(exc_type, exc, tb)
        except Exception as e:  # noqa: BLE001
            # On the way out — don't hijack the exit path with a
            # close-time error. Same posture as the publisher's
            # __aexit__.
            log.warning("influx writer close failed: %s", e)

    async def write(self, row: ScoredRow) -> None:
        """Write a single ScoredRow as a pump_telemetry point.

        Raises:
            RuntimeError: writer wasn't entered.
        """
        if self._write_api is None:
            raise RuntimeError(
                "InfluxWriter.write called outside async with — enter first"
            )
        point = build_point(row)
        await self._write_api.write(
            bucket=self._config.bucket,
            org=self._config.org,
            record=point,
        )


def build_point(row: ScoredRow) -> dict[str, Any]:
    """Translate a ScoredRow into the dict form the writer hands to InfluxDB.

    Returned shape is the structured dict form accepted by
    WriteApiAsync.write (alongside Point and line protocol strings).
    We use the dict form so tests can assert on it without importing
    the official client.

    Field naming convention: psi_<feature> for the per-feature PSI
    values. Pinned by the test suite; downstream Grafana dashboards
    reference these field names verbatim.
    """
    fields: dict[str, float] = {}
    # Features first — pinned order so the InfluxDB schema is stable
    # across runs.
    for name in FEATURE_NAMES:
        if name not in row.features:
            raise KeyError(
                f"ScoredRow.features missing required feature {name!r}"
            )
        fields[name] = float(row.features[name])
    fields["score"] = float(row.score)
    # PSI values — one per feature, prefixed so they don't collide
    # with the raw feature names. PSI for a feature that's not in the
    # dict is treated as 0.0 (no drift signal), not as an error — the
    # drift stub currently fills all 8, but a future implementation
    # that only flags the warning band might emit a sparse dict.
    for name in FEATURE_NAMES:
        fields[f"psi_{name}"] = float(row.psi.get(name, 0.0))

    return {
        "measurement": MEASUREMENT,
        "tags": {"pump_id": row.pump_id},
        "fields": fields,
        "time": _to_utc(row.timestamp),
    }


def _to_utc(ts: datetime) -> datetime:
    """Ensure timestamp is timezone-aware UTC.

    Naive datetimes get tagged as UTC (the telemetry contract is
    ISO-8601 UTC per context/_interfaces.md; a naive timestamp is
    almost certainly already UTC and tagging it is the safe call).
    Non-UTC tz-aware timestamps are converted.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _default_client_factory(*, url: str, token: str, org: str) -> _InfluxClientLike:
    """Construct the real InfluxDBClientAsync.

    Imported lazily so unit tests that inject a fake don't pull in the
    influxdb-client[async] dep (aiohttp transitive). The async client
    API was added in influxdb-client 1.36; we pin >=1.40 in
    requirements.txt.
    """
    from influxdb_client.client.influxdb_client_async import (  # type: ignore[import]
        InfluxDBClientAsync,
    )

    return InfluxDBClientAsync(url=url, token=token, org=org)
