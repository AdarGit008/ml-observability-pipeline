"""Tests for the fleet-snapshot adapter (ADR 0014; FLEET row ADR 0018).

Coverage map:

- §Envelope + projection — full-fleet snapshot, flattened PSI keys,
  Decimal→float, stable ordering.
- §Partial fleet — omit-don't-null-fill + ``pumps_reporting``;
  reading rows don't leak in (BatchGetItem is exact-key).
- §Alert passthrough — ADR 0012 literalism: flag + timestamp verbatim,
  absent → JSON null, and NO threshold logic anywhere in the module.
- §FLEET row (ADR 0018) — the pooled plant-wide aggregate surfaces as a
  separate ``fleet`` object (never inside ``pumps``); no ``latest_score``,
  carries ``pumps_pooled`` (renamed from the row's ``pumps_reporting``);
  absent → empty ``{}``; alert passthrough; malformed row → 500.
- §HTTP surface — 405 on non-GET, 500 on persistent UnprocessedKeys,
  generic error bodies (the URL is public).
- §Read efficiency — exactly ONE BatchGetItem per invocation
  (ADR 0010 "Dashboards: fleet latest" + ADR 0013 cost posture), now
  over 16 keys (15 pumps + FLEET).
- §Boundary — the adapter never imports ``shared/`` (ADR 0014
  §Decision 5: it stays OUTSIDE the ADR 0005 parity set; this is the
  inverse of the parity tests — the scorer MUST import shared, the
  adapter MUST NOT).
- §Cold start — FLEET_SIZE expansion + 1..99 fail-fast validation.
"""

from __future__ import annotations

import importlib
import json
import re
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from dashboards_adapter.tests.conftest import (
    get_event,
    put_fleet_state_row,
    put_state_row,
)


# --- §Envelope + projection ---

def test_full_fleet_snapshot(fresh_adapter):
    handler_mod, table = fresh_adapter
    for i in range(15):
        put_state_row(table, f"P-{i:02d}", score=(i + 1) / 100)

    resp = handler_mod.handler(get_event(), None)

    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    body = json.loads(resp["body"])
    assert body["fleet_size"] == 15
    assert body["pumps_reporting"] == 15
    assert len(body["pumps"]) == 15
    # ISO-8601 UTC millisecond format, project-wide convention.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body["as_of"])


def test_pump_entry_shape_and_flattened_psi(fresh_adapter):
    handler_mod, table = fresh_adapter
    put_state_row(
        table,
        "P-07",
        score=0.42,
        psi={"vibration_amp": 0.31, "bearing_temp": 0.08,
             "motor_current": 0.05, "rpm": 0.02},
    )

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    (pump,) = body["pumps"]

    # Exact key set — the ADR 0014 wire contract, including the
    # ADR 0005 §3 InfluxDB field names for PSI (panel mode-symmetry).
    assert set(pump) == {
        "pump_id", "latest_ts", "latest_score",
        "psi_vibration_amp", "psi_bearing_temp",
        "psi_motor_current", "psi_rpm",
        "alert_flag", "last_alert_sent_at",
    }
    # Decimal → float: json.dumps would raise on Decimal; also pin
    # the values round-trip numerically.
    assert pump["latest_score"] == pytest.approx(0.42)
    assert pump["psi_vibration_amp"] == pytest.approx(0.31)
    assert isinstance(pump["latest_score"], float)
    assert isinstance(pump["psi_rpm"], float)


def test_pumps_sorted_by_pump_id(fresh_adapter):
    handler_mod, table = fresh_adapter
    for pid in ("P-09", "P-00", "P-14", "P-03"):
        put_state_row(table, pid)

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    assert [p["pump_id"] for p in body["pumps"]] == ["P-00", "P-03", "P-09", "P-14"]


# --- §Partial fleet ---

def test_partial_fleet_omits_missing_pumps(fresh_adapter):
    """Unscored pumps are ABSENT — no null-filled placeholder rows
    (ADR 0014 §Decision 2); the envelope counts carry the gap."""
    handler_mod, table = fresh_adapter
    for i in range(13):  # 13 of 15 reporting
        put_state_row(table, f"P-{i:02d}")

    body = json.loads(handler_mod.handler(get_event(), None)["body"])

    assert body["fleet_size"] == 15
    assert body["pumps_reporting"] == 13
    reported = {p["pump_id"] for p in body["pumps"]}
    assert "P-13" not in reported and "P-14" not in reported


def test_reading_rows_never_leak_into_snapshot(fresh_adapter):
    """A pump with reading rows but no STATE row stays absent —
    BatchGetItem is exact-key (sk="STATE"), not a range read."""
    handler_mod, table = fresh_adapter
    put_state_row(table, "P-01")
    # P-02 has telemetry history but was never STATE-snapshotted.
    table.put_item(Item={
        "pump_id": "P-02", "sk": "2026-06-04T12:00:00.000Z",
        "vibration_amp": 1, "bearing_temp": 1, "motor_current": 1, "rpm": 1,
    })

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    assert body["pumps_reporting"] == 1
    assert [p["pump_id"] for p in body["pumps"]] == ["P-01"]


# --- §Alert passthrough (ADR 0012 literalism) ---

def test_alert_fields_literal_passthrough(fresh_adapter):
    handler_mod, table = fresh_adapter
    put_state_row(
        table, "P-05",
        alert_flag=True,
        last_alert_sent_at="2026-06-04T11:58:30.500Z",
    )

    (pump,) = json.loads(handler_mod.handler(get_event(), None)["body"])["pumps"]
    assert pump["alert_flag"] is True
    assert pump["last_alert_sent_at"] == "2026-06-04T11:58:30.500Z"


def test_never_alerted_maps_absent_attribute_to_null(fresh_adapter):
    """Storage has no null sentinel (ADR 0012); the WIRE carries an
    explicit null so the key set is stable (ADR 0014 §Decision 2)."""
    handler_mod, table = fresh_adapter
    put_state_row(table, "P-06", alert_flag=False, last_alert_sent_at=None)

    (pump,) = json.loads(handler_mod.handler(get_event(), None)["body"])["pumps"]
    assert pump["alert_flag"] is False
    assert "last_alert_sent_at" in pump
    assert pump["last_alert_sent_at"] is None


def test_no_threshold_logic_in_module():
    """The adapter never re-derives breach state (ADR 0012
    §Alternatives 2C; ADR 0014 Principle). Pin it structurally: the
    alert thresholds must not appear in the module source."""
    src = Path(importlib.import_module("dashboards_adapter.handler").__file__).read_text(
        encoding="utf-8"
    )
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and '"""' not in line
    )
    assert "0.25" not in code_only, "PSI threshold leaked into the adapter"
    assert "0.7" not in code_only, "score threshold leaked into the adapter"


# --- §FLEET row (ADR 0018 — pooled plant-wide PSI) ---

def test_fleet_row_surfaced_as_object(fresh_adapter):
    """The FLEET aggregate row surfaces under the envelope ``fleet`` key
    with the per-pump PSI/alert shape MINUS ``latest_score`` (no model on
    the fleet path) PLUS ``pumps_pooled`` (the row's ``pumps_reporting``
    attribute, renamed on the wire — DeepSeek review §2)."""
    handler_mod, table = fresh_adapter
    for i in range(15):
        put_state_row(table, f"P-{i:02d}")
    put_fleet_state_row(
        table,
        psi={"vibration_amp": 0.30, "bearing_temp": 0.12,
             "motor_current": 0.20, "rpm": 0.15},
        pumps_reporting=15,
    )

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    fleet = body["fleet"]

    assert set(fleet) == {
        "latest_ts",
        "psi_vibration_amp", "psi_bearing_temp",
        "psi_motor_current", "psi_rpm",
        "alert_flag", "last_alert_sent_at",
        "pumps_pooled",
    }
    # No model on the fleet path (ADR 0018 §5): never a latest_score.
    assert "latest_score" not in fleet
    assert "pump_id" not in fleet  # identity is the object, not a column
    # Wire rename — the storage attr name must NOT leak onto the wire.
    assert "pumps_reporting" not in fleet
    assert fleet["psi_vibration_amp"] == pytest.approx(0.30)
    assert isinstance(fleet["psi_rpm"], float)
    assert fleet["pumps_pooled"] == 15
    assert isinstance(fleet["pumps_pooled"], int)


def test_fleet_row_excluded_from_pumps_and_top_level_count(fresh_adapter):
    """FLEET is a separate partition (ADR 0018) — it must never appear in
    ``pumps`` nor inflate the envelope's top-level ``pumps_reporting``
    (which counts pumps with a STATE row, not the pooled-window count)."""
    handler_mod, table = fresh_adapter
    put_state_row(table, "P-00")
    put_fleet_state_row(table, pumps_reporting=1)

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    assert [p["pump_id"] for p in body["pumps"]] == ["P-00"]
    assert "FLEET" not in {p["pump_id"] for p in body["pumps"]}
    assert body["pumps_reporting"] == 1         # pumps with a STATE row
    assert body["fleet"]["pumps_pooled"] == 1   # pooled-window count (distinct)


def test_fleet_absent_is_empty_object(fresh_adapter):
    """No FLEET STATE row yet (fleet Lambda not run / empty no-op) →
    ``fleet`` is an empty object ``{}`` (the single-object analogue of
    omitting a missing pump; keeps a null off Infinity's ``$.fleet`` root
    selector — DeepSeek review §3); key still present."""
    handler_mod, table = fresh_adapter
    put_state_row(table, "P-00")

    body = json.loads(handler_mod.handler(get_event(), None)["body"])
    assert "fleet" in body
    assert body["fleet"] == {}


def test_fleet_alert_fields_literal_passthrough(fresh_adapter):
    """Same ADR 0012 literalism as per-pump — the FLEET edge-trigger
    state passes through verbatim, no re-derivation."""
    handler_mod, table = fresh_adapter
    put_fleet_state_row(
        table, alert_flag=True,
        last_alert_sent_at="2026-06-10T11:58:30.500Z",
    )

    fleet = json.loads(handler_mod.handler(get_event(), None)["body"])["fleet"]
    assert fleet["alert_flag"] is True
    assert fleet["last_alert_sent_at"] == "2026-06-10T11:58:30.500Z"


def test_fleet_never_alerted_maps_absent_to_null(fresh_adapter):
    handler_mod, table = fresh_adapter
    put_fleet_state_row(table, alert_flag=False, last_alert_sent_at=None)

    fleet = json.loads(handler_mod.handler(get_event(), None)["body"])["fleet"]
    assert fleet["alert_flag"] is False
    assert "last_alert_sent_at" in fleet
    assert fleet["last_alert_sent_at"] is None


def test_fleet_row_missing_required_field_is_500(fresh_adapter):
    """A FLEET row missing a required attribute (here ``pumps_reporting``,
    written by a DIFFERENT Lambda — ``lambda_fleet_psi``) hard-fails to a
    generic 500, never a silently malformed object. Symmetric with the
    per-pump hard reads in ``_pump_entry`` (DeepSeek review §4)."""
    handler_mod, table = fresh_adapter
    table.put_item(Item={
        "pump_id": "FLEET", "sk": "STATE",
        "latest_ts": "2026-06-10T12:00:00.000Z",
        "latest_psi": {"vibration_amp": Decimal("0.3"),
                       "bearing_temp": Decimal("0.1"),
                       "motor_current": Decimal("0.2"),
                       "rpm": Decimal("0.15")},
        "alert_flag": False,
        # pumps_reporting deliberately omitted — malformed FLEET row.
    })

    resp = handler_mod.handler(get_event(), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {"error": "internal error building fleet snapshot"}


# --- §HTTP surface ---

def test_non_get_method_rejected(fresh_adapter):
    handler_mod, _ = fresh_adapter
    resp = handler_mod.handler(get_event("POST"), None)
    assert resp["statusCode"] == 405


def test_unprocessed_keys_retried_then_complete(fresh_adapter):
    """UnprocessedKeys spillover is re-requested within the same
    invocation; the caller still sees one complete snapshot."""
    handler_mod, table = fresh_adapter
    put_state_row(table, "P-01")
    put_state_row(table, "P-02")

    real_resp = handler_mod._DDB.batch_get_item(
        RequestItems={
            handler_mod.DDB_TABLE_NAME: {
                "Keys": [{"pump_id": "P-01", "sk": "STATE"},
                         {"pump_id": "P-02", "sk": "STATE"}],
            }
        }
    )
    items = real_resp["Responses"][handler_mod.DDB_TABLE_NAME]
    first = {
        "Responses": {handler_mod.DDB_TABLE_NAME: items[:1]},
        "UnprocessedKeys": {
            handler_mod.DDB_TABLE_NAME: {
                "Keys": [{"pump_id": "P-02", "sk": "STATE"}],
            }
        },
    }
    second = {"Responses": {handler_mod.DDB_TABLE_NAME: items[1:]},
              "UnprocessedKeys": {}}

    fake_ddb = mock.Mock()
    fake_ddb.batch_get_item.side_effect = [first, second]
    with mock.patch.object(handler_mod, "_DDB", fake_ddb):
        body = json.loads(handler_mod.handler(get_event(), None)["body"])

    assert body["pumps_reporting"] == 2
    assert fake_ddb.batch_get_item.call_count == 2
    # The retry re-requested ONLY the spilled key.
    retry_keys = fake_ddb.batch_get_item.call_args_list[1].kwargs[
        "RequestItems"][handler_mod.DDB_TABLE_NAME]["Keys"]
    assert retry_keys == [{"pump_id": "P-02", "sk": "STATE"}]


def test_unprocessed_keys_exhausted_is_500_not_partial(fresh_adapter):
    """A snapshot that can't complete returns 500 with a generic body
    — never a silently short pump list (which would read as 'pump not
    scored yet'). Internals stay off the public wire."""
    handler_mod, _ = fresh_adapter
    stuck = {
        "Responses": {handler_mod.DDB_TABLE_NAME: []},
        "UnprocessedKeys": {
            handler_mod.DDB_TABLE_NAME: {
                "Keys": [{"pump_id": "P-01", "sk": "STATE"}],
            }
        },
    }
    fake_ddb = mock.Mock()
    fake_ddb.batch_get_item.return_value = stuck
    with mock.patch.object(handler_mod, "_DDB", fake_ddb):
        resp = handler_mod.handler(get_event(), None)

    assert resp["statusCode"] == 500
    assert fake_ddb.batch_get_item.call_count == handler_mod._BATCH_GET_ATTEMPTS
    body = json.loads(resp["body"])
    assert body == {"error": "internal error building fleet snapshot"}


# --- §Read efficiency ---

def test_single_batch_get_item_per_invocation(fresh_adapter):
    """ONE BatchGetItem per panel refresh (ADR 0010 access pattern;
    ADR 0013 cost posture) — not 16 GetItems, not a Query, not a Scan.
    The key set is the 15 pump STATE rows + the one FLEET aggregate
    row (ADR 0018), all sk="STATE"."""
    handler_mod, table = fresh_adapter
    for i in range(15):
        put_state_row(table, f"P-{i:02d}")
    put_fleet_state_row(table)

    with mock.patch.object(
        handler_mod._DDB, "batch_get_item",
        wraps=handler_mod._DDB.batch_get_item,
    ) as spy:
        resp = handler_mod.handler(get_event(), None)

    assert resp["statusCode"] == 200
    assert spy.call_count == 1
    keys = spy.call_args.kwargs["RequestItems"][handler_mod.DDB_TABLE_NAME]["Keys"]
    assert len(keys) == 16  # 15 pumps + FLEET
    assert all(k["sk"] == "STATE" for k in keys)
    assert {"pump_id": "FLEET", "sk": "STATE"} in keys


# --- §Boundary (inverse of the parity tests) ---

def test_adapter_does_not_import_shared():
    """ADR 0014 §Decision 5: the adapter stays OUTSIDE the ADR 0005
    parity set by never importing ``shared/``. The day this fails,
    the adapter joins the parity set and DEV_NORMS §5 Tier 2b applies
    — update the parity-set list in the same PR."""
    import dashboards_adapter.handler as handler_mod

    src = Path(handler_mod.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*(from|import)\s+shared\b", src, flags=re.MULTILINE
    ), "dashboards_adapter imports shared/ — it just joined the parity set"


# --- §Cold start ---

def test_fleet_size_env_expands_pump_ids(fresh_adapter, monkeypatch):
    handler_mod, _ = fresh_adapter
    monkeypatch.setenv("FLEET_SIZE", "3")
    importlib.reload(handler_mod)
    try:
        assert handler_mod.FLEET_PUMP_IDS == ("P-00", "P-01", "P-02")
        # The batch key set appends the FLEET aggregate id (ADR 0018).
        assert handler_mod._STATE_ROW_IDS == ("P-00", "P-01", "P-02", "FLEET")
    finally:
        monkeypatch.delenv("FLEET_SIZE")
        importlib.reload(handler_mod)


@pytest.mark.parametrize("bad_size", ["0", "100", "-3"])
def test_fleet_size_out_of_range_fails_cold_start(fresh_adapter, monkeypatch, bad_size):
    """P-NN is two-digit zero-padded; 1..99 enforced fail-fast at
    cold start so a misconfigured deploy never emits P-100."""
    handler_mod, _ = fresh_adapter
    monkeypatch.setenv("FLEET_SIZE", bad_size)
    try:
        with pytest.raises(ValueError, match="FLEET_SIZE"):
            importlib.reload(handler_mod)
    finally:
        monkeypatch.delenv("FLEET_SIZE")
        importlib.reload(handler_mod)
