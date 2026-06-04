"""Tests for the cold-path batcher (ADR 0015).

Coverage map:

- §First batch — full drain, Parquet round-trip read-back (values,
  schema, deterministic order), partitioned key layout.
- §Watermark mechanics — advance-for-all-pumps (including rowless),
  exclusive lower bound (no re-archive of the boundary row),
  no regression on clock skew, missing watermark → epoch.
- §Empty batch — true no-op: no put, no watermark write.
- §Safety lag — the handler's cutoff trails the wall clock; rows
  younger than the lag wait for the next batch.
- §Reserved rows — STATE + WATERMARK rows never leak into a file.
- §Failure semantics — put-failed leaves watermarks untouched
  (at-least-once: the window is retried, never skipped).
- §Read efficiency — ONE BatchGetItem for the fleet's watermarks.
- §Boundary — the batcher never imports ``shared/`` (ADR 0015
  Principle via ADR 0014 §Decision 5 — outside the parity set).
- §Cold start — required S3_BUCKET, FLEET_SIZE 1..99, non-negative
  SAFETY_LAG_SECONDS, all fail-fast.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from lambda_s3_batcher.tests.conftest import (
    BUCKET_NAME,
    list_archive_keys,
    put_reading_row,
    read_parquet,
)


CUTOFF_1 = "2026-06-04T12:01:00.000Z"
CUTOFF_2 = "2026-06-04T12:02:00.000Z"


def _iso_ago(seconds: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- §First batch ---

def test_first_batch_drains_all_rows_with_parquet_roundtrip(fresh_batcher):
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:01.000Z", score=0.11)
    put_reading_row(table, "P-01", "2026-06-04T12:00:03.000Z", score=0.12)
    put_reading_row(table, "P-02", "2026-06-04T12:00:02.000Z",
                    vibration_amp=0.9, rpm=1650.0, score=0.73)

    summary = handler_mod.run_batch(CUTOFF_1)

    assert summary["archived_rows"] == 3
    assert summary["pumps_with_rows"] == 2
    (key,) = list_archive_keys(s3)
    assert key == summary["s3_key"]

    tbl = read_parquet(s3, key)
    assert tbl.num_rows == 3
    rows = tbl.to_pylist()
    # Deterministic (pump_id, ts) order — file content must not
    # depend on fleet-dict iteration.
    assert [(r["pump_id"], r["ts"]) for r in rows] == [
        ("P-01", "2026-06-04T12:00:01.000Z"),
        ("P-01", "2026-06-04T12:00:03.000Z"),
        ("P-02", "2026-06-04T12:00:02.000Z"),
    ]
    # Decimal → float survived the round trip numerically.
    assert rows[2]["vibration_amp"] == pytest.approx(0.9)
    assert rows[2]["rpm"] == pytest.approx(1650.0)
    assert rows[2]["score"] == pytest.approx(0.73)


def test_partitioned_key_layout(fresh_batcher):
    """`_interfaces.md §S3 archive layout` — partitions derive from
    the batch cutoff (UTC); the filename is the compacted cutoff."""
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T14:31:59.500Z")

    summary = handler_mod.run_batch("2026-06-04T14:32:00.123Z")

    assert summary["s3_key"] == (
        "year=2026/month=06/day=04/hour=14/20260604T143200123Z.parquet"
    )


def test_parquet_schema_locked(fresh_batcher):
    """Exact column names + types — this is the Glue table contract
    (the Terraform-declared schema reads these files; no Crawler will
    ever paper over a drift)."""
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:01.000Z")

    handler_mod.run_batch(CUTOFF_1)

    tbl = read_parquet(s3, list_archive_keys(s3)[0])
    assert tbl.schema.equals(handler_mod.PARQUET_SCHEMA)
    assert tbl.schema.names == [
        "pump_id", "ts",
        "vibration_amp", "bearing_temp", "motor_current", "rpm", "score",
    ]


# --- §Watermark mechanics ---

def test_watermarks_advance_for_all_pumps_including_rowless(fresh_batcher):
    """A successful batch advances EVERY pump's watermark to the
    cutoff — pumps with no rows included (their next window simply
    starts later; ADR 0015 §Decision 1.4)."""
    handler_mod, table, _ = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:01.000Z")

    handler_mod.run_batch(CUTOFF_1)

    for pid in handler_mod.FLEET_PUMP_IDS:
        item = table.get_item(
            Key={"pump_id": pid, "sk": handler_mod.WATERMARK_SK}
        ).get("Item")
        assert item is not None, f"{pid} has no watermark row"
        assert item["last_cutoff"] == CUTOFF_1


def test_boundary_row_at_cutoff_archived_once(fresh_batcher):
    """BETWEEN's upper bound is inclusive, so a row keyed exactly at
    the cutoff rides THAT batch — and the exclusive-lower-bound
    suffix trick keeps the next batch from re-archiving it."""
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", CUTOFF_1)  # exactly at cutoff

    first = handler_mod.run_batch(CUTOFF_1)
    second = handler_mod.run_batch(CUTOFF_2)

    assert first["archived_rows"] == 1
    assert second["archived_rows"] == 0  # no-op, no second file
    assert len(list_archive_keys(s3)) == 1


def test_incremental_batch_carries_only_new_rows(fresh_batcher):
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:30.000Z")
    handler_mod.run_batch(CUTOFF_1)

    put_reading_row(table, "P-01", "2026-06-04T12:01:30.000Z")
    put_reading_row(table, "P-03", "2026-06-04T12:01:45.000Z")
    summary = handler_mod.run_batch(CUTOFF_2)

    assert summary["archived_rows"] == 2
    keys = list_archive_keys(s3)
    assert len(keys) == 2
    second_file = read_parquet(s3, summary["s3_key"]).to_pylist()
    assert {(r["pump_id"], r["ts"]) for r in second_file} == {
        ("P-01", "2026-06-04T12:01:30.000Z"),
        ("P-03", "2026-06-04T12:01:45.000Z"),
    }


def test_watermark_never_regresses(fresh_batcher):
    """A pump whose watermark is already past the cutoff (clock skew,
    rapid re-invoke) is neither queried nor regressed."""
    handler_mod, table, _ = fresh_batcher
    future_mark = "2026-06-04T12:05:00.000Z"
    table.put_item(Item={
        "pump_id": "P-01", "sk": handler_mod.WATERMARK_SK,
        "last_cutoff": future_mark, "updated_at": future_mark,
    })
    # P-01 has a row inside (cutoff, future_mark) — already archived
    # territory from this batch's perspective; it must NOT reappear.
    put_reading_row(table, "P-01", "2026-06-04T12:00:30.000Z")
    put_reading_row(table, "P-02", "2026-06-04T12:00:30.000Z")

    summary = handler_mod.run_batch(CUTOFF_1)

    assert summary["archived_rows"] == 1  # P-02 only
    item = table.get_item(
        Key={"pump_id": "P-01", "sk": handler_mod.WATERMARK_SK}
    )["Item"]
    assert item["last_cutoff"] == future_mark  # untouched


# --- §Empty batch ---

def test_empty_batch_is_true_noop(fresh_batcher):
    handler_mod, table, s3 = fresh_batcher

    summary = handler_mod.run_batch(CUTOFF_1)

    assert summary == {
        "archived_rows": 0, "pumps_with_rows": 0,
        "cutoff": CUTOFF_1, "s3_key": None,
    }
    assert list_archive_keys(s3) == []
    # No watermark rows were written either.
    for pid in handler_mod.FLEET_PUMP_IDS:
        assert "Item" not in table.get_item(
            Key={"pump_id": pid, "sk": handler_mod.WATERMARK_SK}
        )


# --- §Safety lag (handler end-to-end, real clock) ---

def test_handler_cutoff_trails_wall_clock(fresh_batcher):
    """A reading younger than SAFETY_LAG_SECONDS waits for the next
    batch — the scorer's write pipeline gets time to land it."""
    handler_mod, table, s3 = fresh_batcher
    old_ts = _iso_ago(60)          # safely behind the lag
    fresh_ts = _iso_ago(0)         # inside the lag window
    put_reading_row(table, "P-01", old_ts)
    put_reading_row(table, "P-01", fresh_ts)

    summary = handler_mod.handler({}, None)

    assert summary["archived_rows"] == 1
    rows = read_parquet(s3, summary["s3_key"]).to_pylist()
    assert [r["ts"] for r in rows] == [old_ts]


# --- §Reserved rows ---

def test_reserved_rows_never_leak_into_archive(fresh_batcher):
    """STATE and WATERMARK rows sort outside the timestamp BETWEEN
    range by construction (letter-initial SKs) — only reading rows
    reach Parquet."""
    handler_mod, table, s3 = fresh_batcher
    table.put_item(Item={
        "pump_id": "P-01", "sk": "STATE",
        "latest_ts": "2026-06-04T12:00:05.000Z", "latest_score": 1,
    })
    table.put_item(Item={
        "pump_id": "P-01", "sk": handler_mod.WATERMARK_SK,
        "last_cutoff": "2026-06-04T12:00:00.000Z",
        "updated_at": "2026-06-04T12:00:00.000Z",
    })
    put_reading_row(table, "P-01", "2026-06-04T12:00:30.000Z")

    summary = handler_mod.run_batch(CUTOFF_1)

    assert summary["archived_rows"] == 1
    rows = read_parquet(s3, summary["s3_key"]).to_pylist()
    assert [(r["pump_id"], r["ts"]) for r in rows] == [
        ("P-01", "2026-06-04T12:00:30.000Z"),
    ]


# --- §Failure semantics ---

def test_put_failure_leaves_watermarks_untouched(fresh_batcher):
    """At-least-once: if the S3 put dies, no watermark advances and
    the next batch retries the same window (ADR 0015 §Consequences —
    duplicates are possible, loss is not)."""
    handler_mod, table, s3 = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:30.000Z")

    with mock.patch.object(
        handler_mod._S3, "put_object",
        side_effect=RuntimeError("S3 unavailable"),
    ):
        with pytest.raises(RuntimeError, match="S3 unavailable"):
            handler_mod.run_batch(CUTOFF_1)

    for pid in handler_mod.FLEET_PUMP_IDS:
        assert "Item" not in table.get_item(
            Key={"pump_id": pid, "sk": handler_mod.WATERMARK_SK}
        )
    # And the retry actually drains the window.
    assert handler_mod.run_batch(CUTOFF_1)["archived_rows"] == 1


# --- §Read efficiency ---

def test_single_batch_get_for_fleet_watermarks(fresh_batcher):
    """ONE BatchGetItem for all 15 watermarks (the adapter's STATE-read
    shape, ADR 0014) — not 15 GetItems."""
    handler_mod, table, _ = fresh_batcher
    put_reading_row(table, "P-01", "2026-06-04T12:00:30.000Z")

    with mock.patch.object(
        handler_mod._DDB, "batch_get_item",
        wraps=handler_mod._DDB.batch_get_item,
    ) as spy:
        handler_mod.run_batch(CUTOFF_1)

    assert spy.call_count == 1
    keys = spy.call_args.kwargs["RequestItems"][handler_mod.DDB_TABLE_NAME]["Keys"]
    assert len(keys) == 15
    assert all(k["sk"] == handler_mod.WATERMARK_SK for k in keys)


# --- §Boundary (inverse of the parity tests) ---

def test_batcher_does_not_import_shared():
    """ADR 0015 Principle (via ADR 0014 §Decision 5): the batcher
    moves rows, computes nothing, and stays OUTSIDE the ADR 0005
    parity set. The day this fails, the batcher joins the set and
    DEV_NORMS §5 Tier 2b applies — update the list in the same PR."""
    import lambda_s3_batcher.handler as handler_mod

    src = Path(handler_mod.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*(from|import)\s+shared\b", src, flags=re.MULTILINE
    ), "lambda_s3_batcher imports shared/ — it just joined the parity set"


# --- §Cold start ---

def test_missing_bucket_fails_cold_start(fresh_batcher, monkeypatch):
    handler_mod, _, _ = fresh_batcher
    monkeypatch.delenv("S3_BUCKET")
    try:
        with pytest.raises(ValueError, match="S3_BUCKET"):
            importlib.reload(handler_mod)
    finally:
        monkeypatch.setenv("S3_BUCKET", BUCKET_NAME)
        importlib.reload(handler_mod)


@pytest.mark.parametrize("bad_size", ["0", "100", "-3"])
def test_fleet_size_out_of_range_fails_cold_start(fresh_batcher, monkeypatch, bad_size):
    handler_mod, _, _ = fresh_batcher
    monkeypatch.setenv("FLEET_SIZE", bad_size)
    try:
        with pytest.raises(ValueError, match="FLEET_SIZE"):
            importlib.reload(handler_mod)
    finally:
        monkeypatch.delenv("FLEET_SIZE")
        importlib.reload(handler_mod)


def test_negative_safety_lag_fails_cold_start(fresh_batcher, monkeypatch):
    handler_mod, _, _ = fresh_batcher
    monkeypatch.setenv("SAFETY_LAG_SECONDS", "-1")
    try:
        with pytest.raises(ValueError, match="SAFETY_LAG_SECONDS"):
            importlib.reload(handler_mod)
    finally:
        monkeypatch.delenv("SAFETY_LAG_SECONDS")
        importlib.reload(handler_mod)
