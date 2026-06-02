"""Hot-path Lambda handler — MVP per ADR 0010 (DynamoDB schema).

One invocation per IoT Rule trigger. Per-message flow:

1. Parse the IoT-Rule-delivered event (treated as the raw telemetry
   payload per ``context/_interfaces.md §Lambda scorer event envelope``).
2. Query the per-pump rolling window from DynamoDB — the last
   ``WINDOW_SAMPLES`` reading rows for the pump, filtered to exclude
   the STATE row (see §Schema below).
3. Append the new reading to the window (cap at ``WINDOW_SAMPLES``).
4. ``shared.features.extract_features(window)`` → 8-feature dict.
5. ``shared.score.score(features)`` → P(failure_48h) ∈ [0, 1].
6. ``PutItem`` the reading row (``sk=<ts>``) and overwrite the
   STATE row (``sk="STATE"``).

PSI compute + SNS publish are deferred to a follow-on session per
the 2026-06-02 session brief. The reference distribution is loaded
at cold-start anyway so the follow-on adds a single ``compute_psi``
call against the same DynamoDB-backed window (query Limit widens
from 150 to 1800) plus the STATE-row extension for ``latest_psi`` +
``alert_flag`` — no schema migration.

Schema (locked by ADR 0010):

    Reading row: {pump_id, sk=<ISO-8601 ts>, vibration_amp,
                  bearing_temp, motor_current, rpm, score}
    STATE row:   {pump_id, sk="STATE", latest_ts, latest_score}

The reading-row sort key is the ISO-8601 timestamp string (which
sorts lexicographically equivalent to chronological). The STATE
row's reserved ``sk="STATE"`` puts it in the same partition as the
reading rows but outside any timestamp range query — the score
path filters it out via ``sk begins_with "2"`` (ISO timestamps
start with year digits; "STATE" starts with S).

Cold-start (module-level, runs once per container):

- ``shared.drift.load_reference()`` reads
  ``model/artifacts/operational_reference_distribution.json``,
  validates ``feature_names == PSI_FEATURE_NAMES`` (ADR 0009), and
  cross-checks ``model_version`` against ``model.pkl`` — raising
  ``DriftError`` on desync (ADR 0007). Eager-load so a partial
  redeploy fails at cold-start instead of silently scoring against
  one version and PSI-ing against another.
- ``shared.score.score`` itself lazy-loads the classifier on the
  first call (see ``shared/score.py`` for rationale); we don't
  pre-warm here because that's the existing contract.
- ``boto3.resource("dynamodb").Table(...)`` is bound once and reused
  across invocations.

Environment variables (read at cold-start):

- ``DDB_TABLE_NAME`` — table to read/write. Defaults to
  ``pump_hot_state`` (Terraform module pins this).
- ``DDB_ENDPOINT_URL`` — optional, for moto-backed tests (sets
  ``endpoint_url`` on the boto3 resource).
- ``AWS_REGION`` — defaults to ``eu-central-1`` per the project-wide
  region lock (``context/_global.md`` §Hard constraints #5).

Parity boundary: ``extract_features`` + ``score`` + ``load_reference``
are imported from ``shared/`` as peers (ADR 0005). Structural-parity
tests under ``lambda_scorer/tests/test_handler.py`` pin the load
paths so a vendored fork in this directory fails loudly.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.conditions import Key

from shared.drift import load_reference
from shared.features import RAW_SIGNAL_FIELDS, extract_features
from shared.score import score as score_fn


log = logging.getLogger(__name__)


# --- Cold-start state (module-level; runs once per container) ---

DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "pump_hot_state")
DDB_ENDPOINT_URL: str | None = os.environ.get("DDB_ENDPOINT_URL")

# Eager-load. Raises DriftError on missing reference or model_version
# desync (per ADR 0007); cold-start fails fast so a partial redeploy
# can't silently score against one model_version and PSI-ban against
# another. Stored at module scope for the PSI follow-on to read; the
# MVP hot path doesn't use it directly.
REFERENCE: Mapping[str, object] = load_reference()

# boto3 resource client — module-level so the connection is shared
# across warm invocations.
_DDB_KWARGS: dict[str, Any] = {
    "region_name": os.environ.get("AWS_REGION", "eu-central-1"),
}
if DDB_ENDPOINT_URL:
    _DDB_KWARGS["endpoint_url"] = DDB_ENDPOINT_URL
_DDB = boto3.resource("dynamodb", **_DDB_KWARGS)
TABLE = _DDB.Table(DDB_TABLE_NAME)


# --- Constants ---

# 5-minute rolling window at 2 s tick = 150 samples. Matches
# ``local_runtime`` ``window_samples`` default; ``extract_features``
# uses the FULL window for the rolling mean/std features (only the
# last element supplies the "latest raw signal" fields).
WINDOW_SAMPLES: int = 150

# Reserved sort-key value for the per-pump STATE row. ISO-8601
# timestamps start with year digits, so a ``sk begins_with "2"``
# predicate cleanly excludes this row from the score-path window
# query. Any future reserved-SK rows ("META", etc.) must coexist
# with this filter rule (ADR 0010 §Reserved SK literal "STATE").
STATE_SK: str = "STATE"


class EventParseError(ValueError):
    """Raised when the IoT Rule event is missing required fields.

    Subclass of ``ValueError`` so caller code that catches ``ValueError``
    (or doesn't) behaves predictably. The handler converts this to a
    Lambda-visible error so CloudWatch surfaces the malformed payload
    rather than silently dropping it.
    """


# --- Helpers ---

def _to_decimal(value: float) -> Decimal:
    """Float → Decimal for DynamoDB writes.

    boto3's DynamoDB resource rejects native ``float`` (TypeError:
    "Float types are not supported. Use Decimal types instead.") We
    pass through ``str()`` rather than ``Decimal(value)`` directly to
    avoid the IEEE-754 representation surprise where ``Decimal(0.1)``
    becomes ``0.10000000000000000555...``. ``Decimal(str(0.1))``
    is exactly ``0.1``.
    """
    return Decimal(str(value))


def _reading_to_telemetry(item: Mapping[str, Any]) -> dict[str, float]:
    """Project a DynamoDB reading-row item to the raw-signal dict
    that ``extract_features`` consumes.

    DynamoDB returns numeric attributes as ``Decimal``; ``extract_features``
    operates on ``float``. The cast happens here at the boundary so
    the rest of the pipeline doesn't have to know about the
    DynamoDB-side type.
    """
    return {name: float(item[name]) for name in RAW_SIGNAL_FIELDS}


def _parse_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the IoT Rule event has the fields the handler needs.

    Treats ``event`` as the raw telemetry payload — matching the
    default IoT Rule SQL ``SELECT * FROM 'factory/pumps/+/telemetry'``.
    If a future rule wraps the payload in an envelope (e.g.,
    ``{"body": {...}, "topic": "..."}``), this is the function to
    update; the rest of the handler stays unchanged.
    """
    required = ("pump_id", "ts", *RAW_SIGNAL_FIELDS)
    missing = [name for name in required if name not in event]
    if missing:
        raise EventParseError(
            f"event missing required fields: {sorted(missing)}. "
            f"Got keys: {sorted(event.keys()) if event else []}"
        )
    return dict(event)


# --- Hot path ---

def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Score one telemetry message per IoT Rule invocation.

    See module docstring for the full flow. Returns a small dict
    ``{pump_id, ts, score}`` for CloudWatch logging visibility; the
    IoT Rule trigger ignores the return value.

    Raises:
        EventParseError: ``event`` is missing one of the required
            fields (``pump_id``, ``ts``, or any of
            ``RAW_SIGNAL_FIELDS``).
        ScoreError: model artifact missing or feature-schema mismatch.
            Bubbles from ``shared.score.score``; the lazy-load
            triggers on first invocation per container.
    """
    telemetry = _parse_event(event)
    pump_id = telemetry["pump_id"]
    ts = telemetry["ts"]

    # Read the rolling window. The STATE row is filtered out by the
    # `sk begins_with "2"` predicate (ISO-8601 ts starts with year
    # digit; "STATE" starts with S). This is part of the
    # ``KeyConditionExpression`` -- a sort-key range predicate
    # applied at the index-scan level, BEFORE the Limit. (DynamoDB's
    # ``FilterExpression`` is the post-Limit filter; we're not using
    # that.) Confirmed by ``test_handler_window_query_excludes_state_row``
    # + ``test_state_sk_outside_year_range_filter`` -- both pin the
    # invariant.
    #
    # **Year-2xxx assumption (ADR 0010 §Reserved SK literal "STATE",
    # Groq review 2026-06-02 Q2).** The predicate works for any
    # year 2000-2999 because ISO-8601 ts strings start with the
    # year digit. Year 3000+ would need a wider predicate (or
    # the schema refactor to a `row_type` GSI flagged in ADR 0010's
    # §Negative consequences). Future reserved-SK rows (e.g.
    # "META") MUST not start with "2" so the same filter still
    # excludes them. The test below pins this convention.
    #
    # ScanIndexForward=False gets newest-first; we reverse to
    # oldest-first because extract_features wants chronological
    # order (the LAST element is the "latest reading" for the raw
    # signal fields).
    response = TABLE.query(
        KeyConditionExpression=(
            Key("pump_id").eq(pump_id) & Key("sk").begins_with("2")
        ),
        Limit=WINDOW_SAMPLES,
        ScanIndexForward=False,
    )
    items = response.get("Items", [])
    window: list[dict[str, float]] = [
        _reading_to_telemetry(item) for item in reversed(items)
    ]
    # Append the new reading; cap the window length. On the very
    # first invocation for a pump the DynamoDB read returns zero
    # items and the window has just the new reading — extract_features
    # tolerates a 1-element window (rolling stats degenerate to "the
    # value itself, std=0"). Documented in shared/features.py.
    window.append({name: float(telemetry[name]) for name in RAW_SIGNAL_FIELDS})
    window = window[-WINDOW_SAMPLES:]

    features = extract_features(window)
    score_value = score_fn(features)

    # Reading row: append-only history. Single PutItem.
    reading_item: dict[str, Any] = {
        "pump_id": pump_id,
        "sk": ts,
        "score": _to_decimal(score_value),
    }
    for name in RAW_SIGNAL_FIELDS:
        reading_item[name] = _to_decimal(float(telemetry[name]))
    TABLE.put_item(Item=reading_item)

    # STATE row: overwrite-on-write per ADR 0010. The PSI follow-on
    # adds `latest_psi` and `alert_flag` here; the dashboards adapter
    # consumes this row via BatchGetItem (one call per panel refresh
    # across the 15 STATE rows).
    TABLE.put_item(Item={
        "pump_id": pump_id,
        "sk": STATE_SK,
        "latest_ts": ts,
        "latest_score": _to_decimal(score_value),
    })

    log.info("scored pump=%s ts=%s score=%.4f", pump_id, ts, score_value)
    return {"pump_id": pump_id, "ts": ts, "score": score_value}
