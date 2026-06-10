"""Fleet-snapshot adapter — Grafana's AWS-mode datasource backend.

Contract locked by ADR 0014. One GET on the Lambda Function URL
returns the fleet's latest snapshot as a JSON envelope around a flat
per-pump array:

    {
      "fleet_size":      15,
      "pumps_reporting": 13,
      "as_of":           "<adapter invocation ts, ISO-8601 UTC>",
      "pumps": [
        {"pump_id", "latest_ts", "latest_score",
         "psi_vibration_amp", "psi_bearing_temp",
         "psi_motor_current", "psi_rpm",
         "alert_flag", "last_alert_sent_at"},
        ...
      ]
    }

Design rules (the ADR's Principle: a projection, not a brain):

- **One ``BatchGetItem`` per invocation** over the fleet's STATE keys
  (ADR 0010 §Access patterns "Dashboards: fleet latest"). Eventually
  consistent reads — dashboards are the textbook EC-tolerant consumer.
- **``alert_flag`` + ``last_alert_sent_at`` pass through literally**
  (ADR 0012 §Alternatives 2C). No threshold is evaluated here, ever.
  Storage's "absent until first publish" maps to JSON ``null`` on the
  wire (stable key set for Grafana's column inference; ADR 0014
  §Decision 2).
- **``latest_psi`` flattens to ``psi_<feature>``** — the InfluxDB
  field names from ADR 0005 §3, so AWS-mode panels share the
  local-mode vocabulary. The map's own keys drive the flattening; the
  adapter embeds no copy of ``PSI_FEATURE_NAMES``.
- **Pumps without a STATE row are omitted** (not null-filled);
  ``pumps_reporting`` vs ``fleet_size`` carries the gap.
- **No ``shared/`` import.** The adapter extracts no features, scores
  nothing, computes no PSI — it stays outside the ADR 0005 parity
  test surface (ADR 0014 §Decision 5 is the tripwire; the test
  ``test_adapter_does_not_import_shared`` enforces it).

Environment variables:

- ``DDB_TABLE_NAME`` — hot-state table (default ``pump_hot_state``).
- ``FLEET_SIZE`` — pump count, expanded to ``P-00..P-{NN-1}`` per the
  ``_interfaces.md`` pump-id format. Must be 1..99 (the format is
  two-digit zero-padded); validated at cold start, fail-fast.
- ``DDB_ENDPOINT_URL`` — local-test affordance, same as the scorer.
- ``AWS_REGION`` — set by the Lambda runtime; the default is a
  local-test affordance.

Function URL events arrive in payload format 2.0; the method lives at
``requestContext.http.method``. Non-GET gets 405. Unexpected failures
get a 500 with a generic body (details go to CloudWatch, not the
public wire — the URL is AuthType=NONE per ADR 0014 §Alternatives 3).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import boto3


log = logging.getLogger(__name__)


# --- Cold-start state (module-level; runs once per container) ---

DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "pump_hot_state")
DDB_ENDPOINT_URL: str | None = os.environ.get("DDB_ENDPOINT_URL")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-central-1")

FLEET_SIZE: int = int(os.environ.get("FLEET_SIZE", "15"))
if not 1 <= FLEET_SIZE <= 99:
    # The pump-id wire format is "P-NN" two-digit zero-padded
    # (_interfaces.md §MQTT topic pattern). A 100-pump fleet needs a
    # format change (and per ADR 0010 §Consequences, a partition-shape
    # re-evaluation) — fail the cold start rather than emit "P-100".
    raise ValueError(
        f"FLEET_SIZE={FLEET_SIZE} outside 1..99 — the P-NN pump-id "
        "format is two-digit zero-padded (_interfaces.md). A larger "
        "fleet is a schema decision, not an env-var bump."
    )

# The exact BatchGetItem key set. Generated, not configured per-pump:
# the simulator names pumps P-00..P-{NN-1} (0-indexed — terraform
# aws_iot_thing.pump[count.index] and the scorer key STATE rows by the
# same ids, ADR 0010/0016). Fixed 2026-06-07 (live apply): was 1-indexed
# P-01..P-NN, which queried a nonexistent P-15 and never asked for P-00.
FLEET_PUMP_IDS: tuple[str, ...] = tuple(
    f"P-{i:02d}" for i in range(FLEET_SIZE)
)

# boto3 resource — module-level so the connection is shared across
# warm invocations (same posture as lambda_scorer.handler).
_DDB_KWARGS: dict[str, Any] = {"region_name": AWS_REGION}
if DDB_ENDPOINT_URL:
    _DDB_KWARGS["endpoint_url"] = DDB_ENDPOINT_URL
_DDB = boto3.resource("dynamodb", **_DDB_KWARGS)


# --- Constants ---

# Reserved STATE sort key (ADR 0010). String literal duplicated from
# lambda_scorer.handler.STATE_SK on purpose: importing it from there
# would drag the scorer's cold-start (model + reference load) into
# this read-only function for one constant.
STATE_SK: str = "STATE"

# BatchGetItem can return UnprocessedKeys under throttling. At 15
# keys ≈ 6 KB this is theoretical, but the loop is cheap insurance.
# Exhausting the attempts is a 500 — a partial snapshot silently
# missing pumps would be indistinguishable from "pump not scored yet".
_BATCH_GET_ATTEMPTS: int = 3


# --- Helpers ---

def _iso_now() -> str:
    """ISO-8601 UTC with millisecond precision + Z suffix.

    Matches the project-wide timestamp format
    (``_interfaces.md §Telemetry payload``).
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _batch_get_state_rows(pump_ids: tuple[str, ...]) -> list[dict]:
    """Fetch the STATE rows for ``pump_ids`` — ONE BatchGetItem, plus
    bounded retries for any UnprocessedKeys spillover.

    Missing keys (pump not yet scored) are simply absent from the
    response — DynamoDB omits them rather than erroring, which is
    exactly the ADR 0014 omit-don't-null-fill semantic.

    Raises:
        RuntimeError: UnprocessedKeys survived ``_BATCH_GET_ATTEMPTS``
            passes. The handler converts this to a 500 — never a
            silently short snapshot.
    """
    request: dict[str, Any] = {
        DDB_TABLE_NAME: {
            "Keys": [{"pump_id": pid, "sk": STATE_SK} for pid in pump_ids],
        }
    }
    items: list[dict] = []
    for _ in range(_BATCH_GET_ATTEMPTS):
        resp = _DDB.batch_get_item(RequestItems=request)
        items.extend(resp.get("Responses", {}).get(DDB_TABLE_NAME, []))
        request = resp.get("UnprocessedKeys") or {}
        if not request:
            return items
    raise RuntimeError(
        f"BatchGetItem UnprocessedKeys persisted across "
        f"{_BATCH_GET_ATTEMPTS} attempts — refusing to serve a "
        "partial snapshot"
    )


def _pump_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    """Project one STATE row to the ADR 0014 per-pump wire object.

    - ``latest_psi`` (DynamoDB Map) flattens to ``psi_<feature>``
      keys — the map's own keys drive the rename; no feature-name
      constant is vendored here.
    - ``Decimal`` (boto3's number type) converts to ``float`` for
      JSON. Wire precision: PSI and scores are display values with
      PLAN.md §2.7 thresholds at 2 decimal places; float round-trip
      is far inside tolerance.
    - ``last_alert_sent_at``: absent attribute → explicit JSON
      ``null`` (ADR 0014 §Decision 2 wire-vs-storage mapping).
    """
    entry: dict[str, Any] = {
        "pump_id": item["pump_id"],
        "latest_ts": item["latest_ts"],
        "latest_score": float(item["latest_score"]),
    }
    for feature, value in item["latest_psi"].items():
        entry[f"psi_{feature}"] = float(value)
    entry["alert_flag"] = bool(item["alert_flag"])
    entry["last_alert_sent_at"] = item.get("last_alert_sent_at")
    return entry


def _snapshot(items: list[dict]) -> dict[str, Any]:
    """Assemble the response envelope from the fetched STATE rows.

    Pumps sort by ``pump_id`` so the wire order is stable across
    refreshes (BatchGetItem responses are unordered).
    """
    pumps = sorted(
        (_pump_entry(item) for item in items),
        key=lambda p: p["pump_id"],
    )
    return {
        "fleet_size": FLEET_SIZE,
        "pumps_reporting": len(pumps),
        "as_of": _iso_now(),
        "pumps": pumps,
    }


def _response(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Function-URL-shaped response (payload format 2.0)."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


# --- Entry point ---

def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """GET → fleet snapshot. Anything else → 405. Failures → 500.

    The path is deliberately ignored (ADR 0014 §Decision 1): the
    Function URL serves exactly one resource.
    """
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", "GET")
    ).upper()
    if method != "GET":
        return _response(405, {"error": "method not allowed — GET only"})

    try:
        items = _batch_get_state_rows(FLEET_PUMP_IDS)
        return _response(200, _snapshot(items))
    except Exception:
        # Generic body on purpose — the URL is public (AuthType=NONE,
        # ADR 0014); internals go to CloudWatch, not the wire.
        log.exception("fleet snapshot failed")
        return _response(500, {"error": "internal error building fleet snapshot"})
