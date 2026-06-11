"""Fleet-PSI Lambda — EventBridge-scheduled plant-wide drift detector.

Design locked by ADR 0018. One invocation every 5 minutes (the
EventBridge cadence; the event payload is unused — the table holds the
state). Per invocation:

1. For each pump ``P-00..P-(FLEET_SIZE-1)`` (``FLEET_SIZE``), read the trailing
   ``FLEET_WINDOW_SAMPLES`` reading rows from DynamoDB — the same
   ``Query(PK=pump_id, sk begins_with "2", ScanIndexForward=False)``
   the scorer uses (ADR 0010 §Access patterns), reversed to
   oldest-first.
2. **Pool** every pump's readings into ONE combined window and run
   ``shared.drift.compute_psi`` against the operational reference. The
   result is a single 4-key fleet PSI dict — a plant-wide
   "is the whole fleet drifting" gauge that catches a fleet-wide shift
   (e.g. the ``seasonal_drift`` scenario, ADR 0004: ambient temp climbs
   across the plant) that is too subtle to flag on any single pump and
   that no per-pump computation provides.
3. ``shared.drift.psi_alert_should_fire(pooled, psi)`` — the SAME shared
   alert decision the per-pump scorer uses (ADR 0017): the warmup gate
   (``PSI_MIN_SAMPLES``) AND the 0.25 significant-shift threshold. At
   fleet scale the pooled window is large, so warmup is satisfied
   trivially except at a cold deploy — but using the shared decision is
   what keeps the two AWS alert sites from diverging (north star #6).
4. Overwrite the FLEET STATE row (``pump_id="FLEET"``, ``sk="STATE"``)
   with the latest fleet PSI + ``alert_flag`` + ``pumps_reporting``.
5. Edge-triggered SNS (ADR 0012): publish only on the False → True flip
   of ``alert_flag`` (previous value read back via ``GetItem`` before
   the overwrite). Publish AFTER the STATE write — at-most-once per
   edge, loud-on-failure.

This is a **drift-only** deployment (the drift.py docstring's
"drift without sklearn" layer): the deploy zip ships
``shared/{features,drift}.py`` + numpy + the operational reference
JSON, but NOT ``model.pkl`` and NOT sklearn. ``load_reference`` skips
the model/reference version check when ``model.pkl`` is absent
(ADR 0007 §4), so the fleet-PSI cold start needs only the reference.
No scoring happens here — there is no score path and no
``score > 0.7`` branch; fleet alerts are PSI-only (``alert_type
= "psi_breach"``).

The reference ``compute_psi`` pools against is the SINGLE operational
reference (``model/artifacts/operational_reference_distribution.json``,
ADR 0008) — itself built by pooling 15 pumps x 1800 HEALTHY ticks
(``model.train.OPERATIONAL_REFERENCE_PUMPS = 15``). So pooling the
fleet's live windows and comparing against it is apples-to-apples: the
live pooled window is the runtime analogue of how the reference was
built. ``load_reference`` takes no pump_id — there is no per-pump
reference. Consequence (DeepSeek review 2026-06-10 §1): fleet PSI is a
**systemic-shift** detector and a **late indicator** for any single
pump (one drifting pump is ~1/N of the pooled mass) — NOT a substitute
for the per-pump alerts, which catch single-pump drift first.

Parity: ``compute_psi`` + ``psi_alert_should_fire`` + ``load_reference``
are imported from ``shared/`` as peers (ADR 0005). Structural-parity
tests under ``lambda_fleet_psi/tests/test_handler.py`` pin the load
paths so a vendored fork here fails loudly — and so this Lambda and the
per-pump scorer share one definition of "fleet drift is meaningful and
breaching."

The FLEET partition is isolated from the per-pump partitions: the
scorer and batcher iterate ``P-00..P-(FLEET_SIZE-1)`` and never touch ``"FLEET"``,
and the dashboards adapter's ``BatchGetItem`` reads the 15 pump STATE
keys (a FLEET panel is a small follow-on adapter change — ADR 0018
§Follow-ups). So this row adds a plant-level view without disturbing
any existing access pattern (ADR 0010).

Environment variables (read at cold start):

- ``DDB_TABLE_NAME`` — hot-state table (default ``pump_hot_state``).
- ``DDB_ENDPOINT_URL`` — optional, for moto-backed tests.
- ``SNS_TOPIC_ARN`` — REQUIRED; ``KeyError`` at cold start if unset
  (same fail-fast posture as the scorer, ADR 0012). The fleet reuses
  the scorer's alert topic; ``pump_id="FLEET"`` in the payload marks
  the scope.
- ``FLEET_SIZE`` — pump count, expanded to ``P-00..P-(FLEET_SIZE-1)``; 1..99
  validated (two-digit zero-padded pump-id format, ``_interfaces.md``).
- ``AWS_REGION`` — defaults to ``eu-central-1`` (``_global.md`` #5).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.conditions import Key

from shared.drift import compute_psi, load_reference, psi_alert_should_fire
from shared.features import RAW_SIGNAL_FIELDS


log = logging.getLogger(__name__)


# --- Cold-start state (module-level; runs once per container) ---

DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "pump_hot_state")
DDB_ENDPOINT_URL: str | None = os.environ.get("DDB_ENDPOINT_URL")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-central-1")

# Required — KeyError at cold-start if unset (fail-fast; same posture as
# the scorer's SNS_TOPIC_ARN, ADR 0012). The fleet reuses the scorer's
# topic; pump_id="FLEET" in the payload marks the scope.
SNS_TOPIC_ARN: str = os.environ["SNS_TOPIC_ARN"]

FLEET_SIZE: int = int(os.environ.get("FLEET_SIZE", "15"))
if not 1 <= FLEET_SIZE <= 99:
    # P-NN is two-digit zero-padded (_interfaces.md); same fail-fast
    # rationale as lambda_s3_batcher / dashboards_adapter.
    raise ValueError(
        f"FLEET_SIZE={FLEET_SIZE} outside 1..99 — the P-NN pump-id "
        "format is two-digit zero-padded (_interfaces.md). A larger "
        "fleet is a schema decision, not an env-var bump."
    )

FLEET_PUMP_IDS: tuple[str, ...] = tuple(
    f"P-{i:02d}" for i in range(FLEET_SIZE)
)

# Eager-load the operational reference (the 15-pump pooled HEALTHY
# distribution, ADR 0008). Raises DriftError on a missing
# reference (per ADR 0007); cold-start fails fast. model.pkl is NOT
# bundled in the fleet-PSI (drift-only) zip, so the model/reference
# version check is skipped (ADR 0007 §4) — the reference is all this
# Lambda needs. The hot path passes REFERENCE to compute_psi each run.
REFERENCE: Mapping[str, object] = load_reference()

_DDB_KWARGS: dict[str, Any] = {"region_name": AWS_REGION}
if DDB_ENDPOINT_URL:
    _DDB_KWARGS["endpoint_url"] = DDB_ENDPOINT_URL
_DDB = boto3.resource("dynamodb", **_DDB_KWARGS)
TABLE = _DDB.Table(DDB_TABLE_NAME)
_SNS = boto3.client("sns", region_name=AWS_REGION)


# --- Constants ---

# Per-pump pull depth = the trailing 5-minute window at the 2 s tick
# (150 samples) — the "aggregated 5-minute fleet window" of ADR 0007.
# Pooled across the fleet this is up to FLEET_SIZE * 150 readings.
FLEET_WINDOW_SAMPLES: int = 150

# Reserved partition + sort key for the fleet aggregate row. A SEPARATE
# partition (PK="FLEET") from the per-pump rows, so it is invisible to
# the scorer/batcher per-pump iteration and the score-path query
# (ADR 0010 §Reserved SK). sk="STATE" mirrors the per-pump snapshot row.
FLEET_PK: str = "FLEET"
STATE_SK: str = "STATE"


# --- Helpers ---

def _now_iso() -> str:
    """ISO-8601 UTC, millisecond precision, Z suffix (project-wide)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _to_decimal(value: float) -> Decimal:
    """Float → Decimal for DynamoDB (via str() to avoid IEEE-754 noise).

    Same conversion the scorer performs at the write boundary.
    """
    return Decimal(str(value))


def _reading_to_telemetry(item: Mapping[str, Any]) -> dict[str, float]:
    """Project a DynamoDB reading row to the raw-signal dict ``compute_psi``
    consumes. DynamoDB returns numerics as ``Decimal``; cast at the
    boundary so the shared function sees ``float`` (ADR 0010).
    """
    return {name: float(item[name]) for name in RAW_SIGNAL_FIELDS}


def _read_pump_window(pump_id: str) -> list[dict[str, float]]:
    """The trailing ``FLEET_WINDOW_SAMPLES`` readings for one pump.

    Same access pattern as the scorer (ADR 0010): ``begins_with "2"``
    filters out the pump's STATE row (ISO timestamps start with a year
    digit; "STATE" starts with S); ``ScanIndexForward=False`` returns
    newest-first; we reverse to oldest-first. Pagination is irrelevant
    at Limit=150 (one page).

    A pump with no reading rows yet returns an empty list — it simply
    contributes nothing to the pooled window.
    """
    resp = TABLE.query(
        KeyConditionExpression=(
            Key("pump_id").eq(pump_id) & Key("sk").begins_with("2")
        ),
        Limit=FLEET_WINDOW_SAMPLES,
        ScanIndexForward=False,
    )
    items = resp.get("Items", [])
    if resp.get("LastEvaluatedKey"):
        # Defensive (DeepSeek review §4): at 0.5 Hz a pump writes ~150
        # rows / 5 min, and 150 small rows sit far under DynamoDB's 1 MB
        # Query page, so the trailing-150 single page is complete. If
        # this ever trips the pooled window is silently truncated — warn
        # loudly rather than hard-fail (a benign burst should not take
        # the whole fleet run down; matches the scorer's single-Query
        # posture).
        log.warning(
            "fleet-psi: pump=%s window query returned LastEvaluatedKey "
            "(>150 rows or >1MB in 5 min) — trailing window may be truncated",
            pump_id,
        )
    return [_reading_to_telemetry(item) for item in reversed(items)]


# --- Core (clock injected — the handler supplies wall-clock now) ---

def compute_fleet_psi(now_iso: str) -> dict[str, Any]:
    """Pool the fleet's recent readings, PSI them, write the FLEET row.

    Returns a summary dict (also the Lambda return value — visible in
    CloudWatch for free observability).

    Empty fleet (no reading rows on ANY pump) is a true no-op: nothing
    to compute, no STATE write, no alert. Mirrors the batcher's
    empty-batch no-op (ADR 0015).
    """
    pooled: list[dict[str, float]] = []
    pumps_reporting = 0
    for pump_id in FLEET_PUMP_IDS:
        window = _read_pump_window(pump_id)
        if window:
            pumps_reporting += 1
            pooled.extend(window)

    if not pooled:
        log.info("fleet-psi: no readings fleet-wide at %s — no-op", now_iso)
        return {
            "fleet_max_psi": None,
            "pumps_reporting": 0,
            "alert_flag": False,
            "published": False,
        }

    # One pooled PSI for the whole fleet (ADR 0018). compute_psi reads
    # only the four raw signals (PSI_FEATURE_NAMES); order across the
    # pooled samples is irrelevant — PSI is distributional.
    psi = compute_psi(pooled, reference=REFERENCE)
    # The warmup gate inside psi_alert_should_fire (ADR 0017) is trivially
    # satisfied at fleet scale — any single pump's 150-row window already
    # clears PSI_MIN_SAMPLES once pooled — but we arm through the SAME
    # shared decision as the per-pump scorer so the two AWS alert sites
    # cannot diverge (ADR 0018 §4; DeepSeek review §6).
    alert_flag = psi_alert_should_fire(pooled, psi)

    # Edge-trigger input: the PREVIOUS invocation's alert_flag, read back
    # before the overwrite (ADR 0012). Also carries last_alert_sent_at
    # forward so the PutItem doesn't drop it.
    prev_state = TABLE.get_item(
        Key={"pump_id": FLEET_PK, "sk": STATE_SK}
    ).get("Item") or {}
    prev_alert_flag = bool(prev_state.get("alert_flag", False))
    publish_alert = alert_flag and not prev_alert_flag

    state_item: dict[str, Any] = {
        "pump_id": FLEET_PK,
        "sk": STATE_SK,
        "latest_ts": now_iso,
        "latest_psi": {name: _to_decimal(v) for name, v in psi.items()},
        "alert_flag": alert_flag,
        "pumps_reporting": pumps_reporting,
    }
    if publish_alert:
        state_item["last_alert_sent_at"] = now_iso
    elif "last_alert_sent_at" in prev_state:
        state_item["last_alert_sent_at"] = prev_state["last_alert_sent_at"]
    TABLE.put_item(Item=state_item)

    # Publish AFTER the STATE write (ADR 0012 — at-most-once per edge).
    # Fleet alerts are PSI-only: no score field, alert_type "psi_breach".
    # pump_id="FLEET" marks the scope on the shared topic.
    if publish_alert:
        payload = {
            "pump_id": FLEET_PK,
            "scope": "fleet",          # generic subscribers filter on this
            "ts": now_iso,
            "alert_type": "psi_breach",
            "score": None,             # drift-only path — no model score (review §2)
            "psi": {name: float(v) for name, v in psi.items()},
            "pumps_reporting": pumps_reporting,
        }
        _SNS.publish(TopicArn=SNS_TOPIC_ARN, Message=json.dumps(payload))
        log.info(
            "fleet alert published ts=%s max_psi=%.4f pumps=%d",
            now_iso, max(psi.values()), pumps_reporting,
        )

    log.info(
        "fleet-psi ts=%s max_psi=%.4f pumps_reporting=%d alert=%s",
        now_iso, max(psi.values()), pumps_reporting, alert_flag,
    )
    return {
        "fleet_max_psi": max(psi.values()),
        "pumps_reporting": pumps_reporting,
        "alert_flag": alert_flag,
        "published": publish_alert,
    }


# --- Entry point ---

def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """EventBridge scheduled entry point — the event payload is unused
    (the schedule carries no information; the table holds the state)."""
    return compute_fleet_psi(_now_iso())
