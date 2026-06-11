"""Cold-path batcher — EventBridge-scheduled DynamoDB → Parquet → S3.

Design locked by ADR 0015. Every invocation (60 s cadence):

1. ``cutoff = now − SAFETY_LAG_SECONDS`` — the batch's upper bound
   stays behind the scorer's write pipeline so a reading is never
   younger than the batch that should have carried it.
2. One ``BatchGetItem`` fetches the fleet's WATERMARK rows (the
   second reserved sort key, sibling of ADR 0010's ``"STATE"``).
   A missing row means "never archived" → epoch lower bound.
3. Per pump, ``Query(PK=pump_id, SK BETWEEN last_cutoff⁺ AND cutoff)``
   collects the reading rows written since the last batch.
   ``last_cutoff⁺`` (the watermark + ``"0"`` suffix) makes the
   inclusive BETWEEN lower bound effectively exclusive — the row
   keyed exactly at the previous cutoff was already archived.
4. All rows land in ONE Parquet file (pyarrow, snappy) at
   ``year=YYYY/month=MM/day=DD/hour=HH/<compact-cutoff>.parquet``
   (``_interfaces.md §S3 archive layout``).
5. After a successful put, EVERY pump's watermark advances to
   ``cutoff`` — including pumps that contributed no rows (the
   safety-lag contract is identical for all pumps). Watermarks never
   regress: a cutoff at-or-behind a pump's existing watermark skips
   both the query and the advance.
6. Zero rows fleet-wide → true no-op: no S3 put, no watermark write.

Failure semantics (ADR 0015 §Consequences): the S3 put and the
watermark writes are not transactional. Put-succeeded-watermark-
failed re-archives the overlap next round — duplicates across files,
never lost rows. Consumers dedupe on ``(pump_id, ts)``, unique by
construction (it was the DynamoDB primary key).

The batcher computes nothing — no ``shared/`` import (ADR 0014
§Decision 5 posture; the inverse-import test pins it).

Environment variables:

- ``DDB_TABLE_NAME`` — hot-state table (default ``pump_hot_state``).
- ``S3_BUCKET`` — archive bucket. REQUIRED; fail-fast at cold start
  (same posture as the scorer's ``SNS_TOPIC_ARN``, ADR 0012).
- ``FLEET_SIZE`` — pump count, expanded to ``P-00..P-(FLEET_SIZE-1)``; 1..99
  validated at cold start (pump-id format, ``_interfaces.md``).
- ``SAFETY_LAG_SECONDS`` — late-arrival guard (default 5, ≥ 0).
- ``DDB_ENDPOINT_URL`` / ``S3_ENDPOINT_URL`` — local-test affordances.
- ``AWS_REGION`` — set by the Lambda runtime; default is a local-test
  affordance.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from boto3.dynamodb.conditions import Key


log = logging.getLogger(__name__)


# --- Cold-start state (module-level; runs once per container) ---

DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "pump_hot_state")
DDB_ENDPOINT_URL: str | None = os.environ.get("DDB_ENDPOINT_URL")
S3_ENDPOINT_URL: str | None = os.environ.get("S3_ENDPOINT_URL")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-central-1")

S3_BUCKET: str = os.environ.get("S3_BUCKET", "")
if not S3_BUCKET:
    # Fail the cold start, not the first invocation — same env-var
    # posture as the scorer's SNS_TOPIC_ARN (ADR 0012).
    raise ValueError(
        "S3_BUCKET env var is required — the batcher has nowhere to "
        "archive. Set it in the Lambda environment (Terraform wires "
        "it from the s3_archive module output)."
    )

FLEET_SIZE: int = int(os.environ.get("FLEET_SIZE", "15"))
if not 1 <= FLEET_SIZE <= 99:
    # P-NN is two-digit zero-padded (_interfaces.md); same fail-fast
    # rationale as dashboards_adapter.handler.
    raise ValueError(
        f"FLEET_SIZE={FLEET_SIZE} outside 1..99 — the P-NN pump-id "
        "format is two-digit zero-padded (_interfaces.md). A larger "
        "fleet is a schema decision, not an env-var bump."
    )

SAFETY_LAG_SECONDS: float = float(os.environ.get("SAFETY_LAG_SECONDS", "5"))
if SAFETY_LAG_SECONDS < 0:
    raise ValueError(
        f"SAFETY_LAG_SECONDS={SAFETY_LAG_SECONDS} is negative — the "
        "cutoff must trail the wall clock, not lead it (ADR 0015 "
        "§Decision 1)."
    )

FLEET_PUMP_IDS: tuple[str, ...] = tuple(
    f"P-{i:02d}" for i in range(FLEET_SIZE)
)

_DDB_KWARGS: dict[str, Any] = {"region_name": AWS_REGION}
if DDB_ENDPOINT_URL:
    _DDB_KWARGS["endpoint_url"] = DDB_ENDPOINT_URL
_DDB = boto3.resource("dynamodb", **_DDB_KWARGS)
_TABLE = _DDB.Table(DDB_TABLE_NAME)

_S3_KWARGS: dict[str, Any] = {"region_name": AWS_REGION}
if S3_ENDPOINT_URL:
    _S3_KWARGS["endpoint_url"] = S3_ENDPOINT_URL
_S3 = boto3.client("s3", **_S3_KWARGS)


# --- Constants ---

# Second reserved sort key (ADR 0015 §Decision 1), sibling of the
# scorer's "STATE" (ADR 0010). Both start with a letter, so the hot
# path's `begins_with "2"` predicate and this module's BETWEEN range
# (timestamps on both ends) each exclude both reserved rows.
WATERMARK_SK: str = "WATERMARK"

# Lower bound for a pump that has never been archived.
EPOCH_TS: str = "1970-01-01T00:00:00.000Z"

# Mirrors the adapter's UnprocessedKeys posture: theoretical at 15
# keys, cheap insurance, hard failure beats a silent partial read
# (a missed watermark would re-archive from epoch — duplicates, not
# loss, but a needlessly fat file).
_BATCH_GET_ATTEMPTS: int = 3

# Parquet schema — the reading-row fields (ADR 0010) plus pump_id.
# `ts` stays an ISO-8601 string: lossless, lexicographically
# chronological, and identical to the DynamoDB sort key it came from.
PARQUET_SCHEMA: pa.Schema = pa.schema(
    [
        ("pump_id", pa.string()),
        ("ts", pa.string()),
        ("vibration_amp", pa.float64()),
        ("bearing_temp", pa.float64()),
        ("motor_current", pa.float64()),
        ("rpm", pa.float64()),
        ("score", pa.float64()),
    ]
)


# --- Helpers ---

def _iso(dt: datetime) -> str:
    """ISO-8601 UTC, millisecond precision, Z suffix (project-wide)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _cutoff_now() -> str:
    """The batch upper bound: now minus the late-arrival safety lag."""
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=SAFETY_LAG_SECONDS))


def _fetch_watermarks(pump_ids: tuple[str, ...]) -> dict[str, str]:
    """Map pump_id → last_cutoff for every pump with a WATERMARK row.

    ONE BatchGetItem (same shape as the adapter's STATE read), plus
    bounded retries for UnprocessedKeys spillover. Missing rows are
    simply absent — the caller defaults them to the epoch.

    Raises:
        RuntimeError: spillover survived ``_BATCH_GET_ATTEMPTS``
            passes. Failing the invocation beats silently treating an
            unread watermark as "never archived".
    """
    request: dict[str, Any] = {
        DDB_TABLE_NAME: {
            "Keys": [{"pump_id": pid, "sk": WATERMARK_SK} for pid in pump_ids],
        }
    }
    marks: dict[str, str] = {}
    for _ in range(_BATCH_GET_ATTEMPTS):
        resp = _DDB.batch_get_item(RequestItems=request)
        for item in resp.get("Responses", {}).get(DDB_TABLE_NAME, []):
            marks[item["pump_id"]] = item["last_cutoff"]
        request = resp.get("UnprocessedKeys") or {}
        if not request:
            return marks
    raise RuntimeError(
        f"BatchGetItem UnprocessedKeys persisted across "
        f"{_BATCH_GET_ATTEMPTS} attempts — refusing to batch against "
        "unread watermarks"
    )


def _query_new_rows(pump_id: str, last_cutoff: str, cutoff: str) -> list[dict]:
    """Reading rows for ``pump_id`` in ``(last_cutoff, cutoff]``.

    The ``+ "0"`` suffix makes BETWEEN's inclusive lower bound
    strictly greater than ``last_cutoff`` (any suffix sorts a string
    after its prefix) — the row keyed exactly at the previous cutoff
    is not re-archived. Both bounds are timestamps, so the reserved
    ``"STATE"``/``"WATERMARK"`` rows (letter-initial SKs) fall
    outside the range by construction.

    Pagination is honored; at ~30 rows/pump/minute it never triggers.
    """
    if not last_cutoff < cutoff:
        # Clock skew / rapid re-invoke: never query (or regress) a
        # window that ends at-or-before this pump's watermark.
        return []
    condition = Key("pump_id").eq(pump_id) & Key("sk").between(
        last_cutoff + "0", cutoff
    )
    rows: list[dict] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": condition}
    while True:
        resp = _TABLE.query(**kwargs)
        rows.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return rows
        kwargs["ExclusiveStartKey"] = last_key


def _to_arrow_table(rows: list[dict]) -> pa.Table:
    """Project reading rows onto the locked Parquet schema.

    Rows sort by ``(pump_id, ts)`` so file content is deterministic
    (DynamoDB returns per-pump order; the fleet concatenation order
    shouldn't depend on dict iteration). boto3's ``Decimal``s convert
    to float — same wire conversion the adapter performs.
    """
    rows = sorted(rows, key=lambda r: (r["pump_id"], r["sk"]))
    columns: dict[str, list] = {
        "pump_id": [r["pump_id"] for r in rows],
        "ts": [r["sk"] for r in rows],
    }
    for field in ("vibration_amp", "bearing_temp", "motor_current", "rpm", "score"):
        columns[field] = [float(r[field]) for r in rows]
    return pa.table(columns, schema=PARQUET_SCHEMA)


def _s3_key(cutoff: str) -> str:
    """Partitioned key per ``_interfaces.md §S3 archive layout``.

    Partition values derive from the batch cutoff (UTC). The filename
    is the cutoff with separators stripped — unique per batch (cutoffs
    strictly advance) and S3/Athena-friendly (no colons).
    """
    dt = datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%S.%f%z")
    compact = cutoff.replace("-", "").replace(":", "").replace(".", "")
    return (
        f"year={dt.year:04d}/month={dt.month:02d}/day={dt.day:02d}/"
        f"hour={dt.hour:02d}/{compact}.parquet"
    )


def _write_parquet(table: pa.Table) -> bytes:
    """Serialize to Parquet (snappy) in memory — files are ~tens of KB."""
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


def _advance_watermarks(
    watermarks: dict[str, str], cutoff: str, now_iso: str
) -> None:
    """Advance every pump's watermark to ``cutoff`` — never backward."""
    for pump_id in FLEET_PUMP_IDS:
        if watermarks.get(pump_id, EPOCH_TS) >= cutoff:
            continue  # never regress (clock skew / rapid re-invoke)
        _TABLE.put_item(
            Item={
                "pump_id": pump_id,
                "sk": WATERMARK_SK,
                "last_cutoff": cutoff,
                "updated_at": now_iso,
            }
        )


# --- Core (cutoff injected — the handler supplies the lagged clock) ---

def run_batch(cutoff: str) -> dict[str, Any]:
    """Drain ``(per-pump watermark, cutoff]`` to one Parquet file.

    Returns a summary dict (also the Lambda's return value — visible
    in CloudWatch for free observability).
    """
    watermarks = _fetch_watermarks(FLEET_PUMP_IDS)
    rows: list[dict] = []
    pumps_with_rows = 0
    for pump_id in FLEET_PUMP_IDS:
        pump_rows = _query_new_rows(
            pump_id, watermarks.get(pump_id, EPOCH_TS), cutoff
        )
        if pump_rows:
            pumps_with_rows += 1
            rows.extend(pump_rows)

    if not rows:
        # True no-op (ADR 0015 §Decision 1.5): no put, no watermark
        # write — nothing happened, nothing to clean up.
        log.info("empty batch at cutoff=%s — no-op", cutoff)
        return {
            "archived_rows": 0,
            "pumps_with_rows": 0,
            "cutoff": cutoff,
            "s3_key": None,
        }

    key = _s3_key(cutoff)
    _S3.put_object(
        Bucket=S3_BUCKET, Key=key, Body=_write_parquet(_to_arrow_table(rows))
    )
    # Watermarks advance only AFTER the put succeeds: put-failed →
    # next batch retries the same window (at-least-once, ADR 0015).
    _advance_watermarks(watermarks, cutoff, _iso(datetime.now(timezone.utc)))

    log.info(
        "archived %d rows from %d pumps to s3://%s/%s",
        len(rows), pumps_with_rows, S3_BUCKET, key,
    )
    return {
        "archived_rows": len(rows),
        "pumps_with_rows": pumps_with_rows,
        "cutoff": cutoff,
        "s3_key": key,
    }


# --- Entry point ---

def handler(event: dict, context: Any) -> dict[str, Any]:
    """EventBridge scheduled entry point — the event payload is unused
    (the schedule carries no information; the table holds the state)."""
    return run_batch(_cutoff_now())
