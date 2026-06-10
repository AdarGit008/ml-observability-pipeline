"""Hot-path Lambda handler — score + PSI + edge-triggered SNS alerts.

One invocation per IoT Rule trigger. Per-message flow:

1. Parse the IoT-Rule-delivered event (treated as the raw telemetry
   payload per ``context/_interfaces.md §Lambda scorer event envelope``).
2. Query the per-pump rolling window from DynamoDB — the last
   ``PSI_WINDOW_SAMPLES`` (1800) reading rows for the pump, filtered
   to exclude the STATE row (see §Schema below). A single Query
   serves BOTH windows: the last ``WINDOW_SAMPLES`` (150) slice
   feeds ``extract_features``; the full window feeds ``compute_psi``
   (PO decision 2026-06-02 PSI follow-on plan-step — one read per
   invocation beats two at ~15 pumps × ~0.5 RPS).
3. Append the new reading to the window (cap at ``PSI_WINDOW_SAMPLES``).
4. ``shared.features.extract_features(window[-WINDOW_SAMPLES:])``
   → 8-feature dict.
5. ``shared.score.score(features)`` → P(failure_48h) ∈ [0, 1].
6. ``shared.drift.compute_psi(window, reference=REFERENCE)`` → 4-key
   PSI dict per ADR 0009. Computed on EVERY invocation — the
   every-Nth-tick cadence in ``local_runtime`` (ADR 0007) exists to
   throttle InfluxDB writes, not because the computation is
   expensive (~2 ms of numpy on an 1800-sample window). Lambda has
   no per-tick write to throttle, and a stateless handler has no
   natural tick counter; every-invocation is the simpler shape
   (PO decision, same plan-step).
7. ``PutItem`` the reading row (``sk=<ts>``); ``GetItem`` the
   previous STATE row (edge-trigger input — see §Alerting below);
   overwrite the STATE row (``sk="STATE"``).
8. If the alert state flipped False → True this invocation, publish
   the ADR-0012 edge-triggered alert to SNS.

Mode-parity note (raw telemetry vs feature dicts): ``local_runtime``
feeds ``compute_psi`` a deque of *extracted-feature* dicts; this
handler feeds it the *raw-telemetry* reading rows. Equivalent on the
PSI surface — ``compute_psi`` reads only the four raw signals
(``PSI_FEATURE_NAMES``), and the feature dict's raw-signal entries
are exactly the latest raw reading — but the two modes pass
structurally different objects into the same shared function. See
the 2026-06-02 PSI follow-on session log for the long form.

Schema (locked by ADR 0010; STATE-row extension pre-authorized
there, landed this session):

    Reading row: {pump_id, sk=<ISO-8601 ts>, vibration_amp,
                  bearing_temp, motor_current, rpm, score}
    STATE row:   {pump_id, sk="STATE", latest_ts, latest_score,
                  latest_psi (4-key Map per ADR 0009), alert_flag,
                  last_alert_sent_at (absent until first publish)}

The reading-row sort key is the ISO-8601 timestamp string (which
sorts lexicographically equivalent to chronological). The STATE
row's reserved ``sk="STATE"`` puts it in the same partition as the
reading rows but outside any timestamp range query — the score
path filters it out via ``sk begins_with "2"`` (ISO timestamps
start with year digits; "STATE" starts with S).

Alerting (ADR 0012 — edge-trigger + two-attribute alert state):

- ``alert_flag`` on the STATE row is the CURRENT-invocation breach
  state: ``(psi_is_armed(window) AND max(psi.values()) > 0.25) OR
  score > 0.7`` per ``_interfaces.md §SNS alert payload`` + the
  ADR 0017 warmup gate. The PSI side only arms once the window holds
  ``PSI_MIN_SAMPLES`` (5 min at the 2 s tick); a sub-warmup window's
  max-PSI > 0.25 is a small-sample binning artifact, not drift
  (2026-06-07 cold-start storm). ``score > 0.7`` is ungated.
  Overwritten every invocation; the dashboards adapter reads it to
  light a panel red.
- ``last_alert_sent_at`` records the ``ts`` of the last SNS publish.
  Carried forward verbatim on non-publishing invocations; absent
  until the pump's first publish.
- The SNS publish fires only on the False → True flip of
  ``alert_flag`` (previous value read back via ``GetItem`` on the
  STATE row BEFORE the overwrite). A persisting breach publishes
  once, not 30×/min — protecting the SNS Always-Free 1000
  email-deliveries/month envelope (ADR 0012 §Decision).
- Publish ordering: AFTER the STATE-row write. A publish failure
  after the state landed surfaces as a loud Lambda invocation error
  in CloudWatch; the IoT-Rule retry re-runs the handler with
  ``prev alert_flag == True`` and does NOT re-publish (at-most-once
  per edge). ADR 0012 §Consequences carries the lost-vs-duplicate
  trade-off.

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
- ``boto3.resource("dynamodb").Table(...)`` and
  ``boto3.client("sns")`` are bound once and reused across
  invocations.

Environment variables (read at cold-start):

- ``DDB_TABLE_NAME`` — table to read/write. Defaults to
  ``pump_hot_state`` (Terraform module pins this).
- ``DDB_ENDPOINT_URL`` — optional, for moto-backed tests (sets
  ``endpoint_url`` on the boto3 resource).
- ``SNS_TOPIC_ARN`` — REQUIRED; ``KeyError`` at cold-start if
  unset. Matches the fail-fast posture of the reference eager-load:
  a Lambda deployed without its alert topic wired should fail at
  init in CloudWatch, not silently swallow alerts at invocation
  time.
- ``AWS_REGION`` — defaults to ``eu-central-1`` per the project-wide
  region lock (``context/_global.md`` §Hard constraints #5).

Parity boundary: ``extract_features`` + ``score`` + ``load_reference``
+ ``compute_psi`` are imported from ``shared/`` as peers (ADR 0005).
Structural-parity tests under ``lambda_scorer/tests/test_handler.py``
pin the load paths so a vendored fork in this directory fails loudly.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.conditions import Key

from shared.drift import (
    PSI_SIGNIFICANT_THRESHOLD,
    compute_psi,
    load_reference,
    psi_alert_should_fire,
    psi_is_armed,
)
from shared.features import RAW_SIGNAL_FIELDS, extract_features
from shared.score import score as score_fn


log = logging.getLogger(__name__)


# --- Cold-start state (module-level; runs once per container) ---

DDB_TABLE_NAME: str = os.environ.get("DDB_TABLE_NAME", "pump_hot_state")
DDB_ENDPOINT_URL: str | None = os.environ.get("DDB_ENDPOINT_URL")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-central-1")

# Required — KeyError at cold-start if unset (fail-fast; see module
# docstring §Environment variables).
SNS_TOPIC_ARN: str = os.environ["SNS_TOPIC_ARN"]

# Eager-load. Raises DriftError on missing reference or model_version
# desync (per ADR 0007); cold-start fails fast so a partial redeploy
# can't silently score against one model_version and PSI against
# another. The hot path passes this to compute_psi every invocation.
REFERENCE: Mapping[str, object] = load_reference()

# boto3 clients — module-level so connections are shared across warm
# invocations.
_DDB_KWARGS: dict[str, Any] = {"region_name": AWS_REGION}
if DDB_ENDPOINT_URL:
    _DDB_KWARGS["endpoint_url"] = DDB_ENDPOINT_URL
_DDB = boto3.resource("dynamodb", **_DDB_KWARGS)
TABLE = _DDB.Table(DDB_TABLE_NAME)
_SNS = boto3.client("sns", region_name=AWS_REGION)


# --- Constants ---

# 5-minute rolling window at 2 s tick = 150 samples. Matches
# ``local_runtime`` ``window_samples`` default; ``extract_features``
# uses the FULL window for the rolling mean/std features (only the
# last element supplies the "latest raw signal" fields).
WINDOW_SAMPLES: int = 150

# 1-hour PSI window at 2 s tick = 1800 samples. Matches
# ``local_runtime`` ``psi_window_samples``. The single hot-path Query
# reads this many rows; the scoring window is the trailing
# ``WINDOW_SAMPLES`` slice (ADR 0010 §Access patterns "PSI follow-on"
# row; single-Query shape per the 2026-06-02 plan-step decision).
PSI_WINDOW_SAMPLES: int = 1800

# Alert thresholds per ``_interfaces.md §SNS alert payload`` and
# §PSI parameters: PSI > 0.25 is "significant shift"; score > 0.7 is
# the high-failure-probability line. The PSI threshold's source of
# truth is ``shared.drift.PSI_SIGNIFICANT_THRESHOLD`` (ADR 0017 §1 --
# colocated with the warmup gate so alert sites can't diverge); this
# module-level alias preserves the existing name for readers/tests.
PSI_ALERT_THRESHOLD: float = PSI_SIGNIFICANT_THRESHOLD
SCORE_ALERT_THRESHOLD: float = 0.7

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
    that ``extract_features`` (and ``compute_psi``) consume.

    DynamoDB returns numeric attributes as ``Decimal``; the shared
    functions operate on ``float``. The cast happens here at the
    boundary so the rest of the pipeline doesn't have to know about
    the DynamoDB-side type.
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


def _alert_type(psi_breach: bool, score_breach: bool) -> str:
    """Map the two breach booleans to the ``_interfaces.md §SNS alert
    payload`` ``alert_type`` value. Caller guarantees at least one is
    True (the function is only reached when ``alert_flag`` is set).
    """
    if psi_breach and score_breach:
        return "both"
    if psi_breach:
        return "psi_breach"
    return "high_failure_prob"


# --- Hot path ---

def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Score + PSI one telemetry message per IoT Rule invocation.

    See module docstring for the full flow. Returns a small dict
    ``{pump_id, ts, score, alert_flag}`` for CloudWatch logging
    visibility; the IoT Rule trigger ignores the return value.

    Raises:
        EventParseError: ``event`` is missing one of the required
            fields (``pump_id``, ``ts``, or any of
            ``RAW_SIGNAL_FIELDS``).
        ScoreError: model artifact missing or feature-schema mismatch.
            Bubbles from ``shared.score.score``; the lazy-load
            triggers on first invocation per container.
        KeyError: a window entry is missing a PSI feature — bubbles
            from ``shared.drift.compute_psi``; can't happen for rows
            this handler wrote (reading rows always carry all four
            raw signals).
    """
    telemetry = _parse_event(event)
    pump_id = telemetry["pump_id"]
    ts = telemetry["ts"]

    # Read the rolling window — ONE Query serves both the 5-minute
    # scoring window and the 1-hour PSI window (Limit widened from
    # 150 to 1800 by the PSI follow-on; ADR 0010 forward commitment).
    # The STATE row is filtered out by the `sk begins_with "2"`
    # predicate (ISO-8601 ts starts with year digit; "STATE" starts
    # with S). This is part of the ``KeyConditionExpression`` -- a
    # sort-key range predicate applied at the index-scan level,
    # BEFORE the Limit. (DynamoDB's ``FilterExpression`` is the
    # post-Limit filter; we're not using that.) Confirmed by
    # ``test_handler_window_query_excludes_state_row``
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
    # excludes them. ``test_state_sk_outside_year_range_filter``
    # pins this convention.
    #
    # ScanIndexForward=False gets newest-first; we reverse to
    # oldest-first because extract_features wants chronological
    # order (the LAST element is the "latest reading" for the raw
    # signal fields).
    response = TABLE.query(
        KeyConditionExpression=(
            Key("pump_id").eq(pump_id) & Key("sk").begins_with("2")
        ),
        Limit=PSI_WINDOW_SAMPLES,
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
    # value itself, std=0") and compute_psi tolerates it too (one bin
    # holds all mass). On a cold window max-PSI can sit ABOVE the 0.25
    # band (the 2026-06-07 storm), but the ADR 0017 warmup gate
    # (psi_is_armed below) is what prevents that from arming an alert
    # — not the magnitude of the value here.
    window.append({name: float(telemetry[name]) for name in RAW_SIGNAL_FIELDS})
    window = window[-PSI_WINDOW_SAMPLES:]

    # Scoring uses the trailing 5-minute slice; PSI uses the full
    # window. Same list, two views — mirrors local_runtime's two
    # deques (FeatureWindow + _feature_history) without holding two
    # copies.
    features = extract_features(window[-WINDOW_SAMPLES:])
    score_value = score_fn(features)

    # PSI on every invocation (see module docstring step 6). The
    # window entries are raw-telemetry dicts, not feature dicts —
    # equivalent on the PSI surface; see the parity note in the
    # module docstring.
    psi = compute_psi(window, reference=REFERENCE)

    # Warmup gate (ADR 0017): a PSI breach may ARM an alert only once the
    # window holds >= PSI_MIN_SAMPLES (5 min at the 2 s tick). On a
    # sub-warmup window, max-PSI > 0.25 is a small-sample binning artifact,
    # not drift -- the 2026-06-07 first-live-apply storm fired 9/14 healthy
    # pumps within minute 1 (scores all <= 0.02). The warmup gate AND the
    # significant-shift threshold are colocated in
    # ``shared.drift.psi_alert_should_fire`` so the future fleet-PSI Lambda
    # can't diverge on either (ADR 0017 §1; DeepSeek review 2026-06-10).
    # compute_psi still ran and ``latest_psi`` is still written to the
    # STATE row below, so the dashboard shows PSI warming up; only the
    # ALERT is gated.
    psi_breach = psi_alert_should_fire(window, psi)
    # score_breach is intentionally NOT warmup-gated (ADR 0017 §3): a score
    # is a per-sample model output, not a distributional statistic that
    # needs bin population, so a high P(failure) even on a short window is a
    # legitimate signal we want surfaced -- and the observed storm was
    # PSI-only. The model's out-of-distribution behaviour on tiny windows
    # is a model-surface concern (ADR 0006 / context/model.md), monitored
    # post-deploy rather than papered over with a second gate here.
    score_breach = score_value > SCORE_ALERT_THRESHOLD
    alert_flag = psi_breach or score_breach

    # Reading row: append-only history. Single PutItem.
    reading_item: dict[str, Any] = {
        "pump_id": pump_id,
        "sk": ts,
        "score": _to_decimal(score_value),
    }
    for name in RAW_SIGNAL_FIELDS:
        reading_item[name] = _to_decimal(float(telemetry[name]))
    TABLE.put_item(Item=reading_item)

    # Edge-trigger input: the PREVIOUS invocation's alert_flag, read
    # back before the STATE overwrite (ADR 0012). One GetItem per
    # invocation — eventually-consistent read, half an RCU,
    # invisible at demo volume. Also carries last_alert_sent_at
    # forward so the PutItem overwrite below doesn't drop it.
    prev_state = TABLE.get_item(
        Key={"pump_id": pump_id, "sk": STATE_SK}
    ).get("Item") or {}
    prev_alert_flag = bool(prev_state.get("alert_flag", False))
    publish_alert = alert_flag and not prev_alert_flag

    # STATE row: overwrite-on-write per ADR 0010; latest_psi +
    # alert_flag landed by the PSI follow-on (extension
    # pre-authorized in ADR 0010 §Decision). latest_psi is a
    # DynamoDB Map keyed by PSI_FEATURE_NAMES (4 keys per ADR 0009).
    # last_alert_sent_at is omitted (not None) until the pump's
    # first publish — DynamoDB has no clean "absent" sentinel and
    # the dashboards adapter treats a missing attribute as "never
    # alerted".
    state_item: dict[str, Any] = {
        "pump_id": pump_id,
        "sk": STATE_SK,
        "latest_ts": ts,
        "latest_score": _to_decimal(score_value),
        "latest_psi": {name: _to_decimal(v) for name, v in psi.items()},
        "alert_flag": alert_flag,
    }
    if publish_alert:
        state_item["last_alert_sent_at"] = ts
    elif "last_alert_sent_at" in prev_state:
        state_item["last_alert_sent_at"] = prev_state["last_alert_sent_at"]
    TABLE.put_item(Item=state_item)

    # SNS publish AFTER the STATE write (ADR 0012 §Decision —
    # at-most-once per edge: if the publish raises after the state
    # landed, the invocation error is loud in CloudWatch and the
    # IoT-Rule retry sees prev alert_flag == True and does not
    # double-publish). Payload per _interfaces.md §SNS alert payload.
    if publish_alert:
        payload = {
            "pump_id": pump_id,
            "ts": ts,
            "alert_type": _alert_type(psi_breach, score_breach),
            "score": float(score_value),
            "psi": {name: float(v) for name, v in psi.items()},
        }
        _SNS.publish(TopicArn=SNS_TOPIC_ARN, Message=json.dumps(payload))
        log.info(
            "alert published pump=%s ts=%s type=%s",
            pump_id, ts, payload["alert_type"],
        )

    log.info(
        "scored pump=%s ts=%s score=%.4f max_psi=%.4f alert=%s",
        pump_id, ts, score_value, max(psi.values()), alert_flag,
    )
    return {
        "pump_id": pump_id,
        "ts": ts,
        "score": score_value,
        "alert_flag": alert_flag,
    }
