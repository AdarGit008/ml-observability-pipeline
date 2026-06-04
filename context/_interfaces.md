# _interfaces.md — cross-component contracts

Load this when work crosses component boundaries (e.g., simulator → scorer, scorer → batcher). Keep schemas authoritative here; component files reference, don't duplicate.

> **Status:** Many shapes below are TBD pending resolution of HANDOFF.md §6 open questions. Filled in as decisions are made.

## MQTT topic pattern
```
factory/pumps/{pump_id}/telemetry
```
- `pump_id` format: `P-NN` zero-padded (e.g., `P-07`).
- One Thing per pump in AWS mode; one client per pump in local mode.

## Telemetry payload (JSON)
Published by simulator on the topic above.

```json
{
  "pump_id":       "P-07",
  "ts":            "2026-05-24T14:32:01.123Z",
  "vibration_amp": 0.42,
  "bearing_temp":  68.3,
  "motor_current": 4.7,
  "rpm":           1798
}
```
- `ts` is ISO-8601 UTC with millisecond precision.
- Numeric fields are floats. RPM is float not int to preserve noise model.
- The numeric values above are illustrative only — they do NOT necessarily fall inside the operational reference distribution's per-feature ranges (ADR 0008 demo-paced HEALTHY baseline). Tests that need PSI-healthy windows must sample the reference's own bin ranges, not these example values (see `lambda_scorer/tests/test_handler.py §PSI window mechanics`).

## DynamoDB schema
> **Resolved 2026-06-02 by ADR 0010.** Option A with a STATE-row sibling: one row per reading keyed `(pump_id, <ISO-8601 ts>)` for history, one row per pump keyed `(pump_id, "STATE")` for the latest snapshot. PSI follow-on (2026-06-02) landed the pre-authorized STATE-row extension: `latest_psi`, `alert_flag`, `last_alert_sent_at` (ADR 0012).

### Reading row
```
PK = pump_id            # "P-07"
SK = <ISO-8601 ts>      # "2026-06-02T14:32:01.123Z"
vibration_amp = <float>
bearing_temp  = <float>
motor_current = <float>
rpm           = <float>
score         = <float>  # P(failure_48h) ∈ [0, 1]
```

### STATE row
```
PK = pump_id
SK = "STATE"
latest_ts    = <ISO-8601 ts>
latest_score = <float>
latest_psi   = { vibration_amp: float, bearing_temp: float,
                 motor_current: float, rpm: float }
                          # DynamoDB Map, 4 keys per ADR 0009
alert_flag   = <bool>     # CURRENT-invocation breach state:
                          # max(psi.values()) > 0.25 OR score > 0.7.
                          # Overwritten every invocation (ADR 0012).
last_alert_sent_at = <ISO-8601 ts>
                          # ts of the last SNS publish; carried
                          # forward on non-publishing invocations;
                          # ABSENT until the pump's first publish
                          # (ADR 0012 — no null sentinel).
```

### Access patterns

| Pattern | Operation |
|---|---|
| Hot path: read rolling window | `Query(PK=pump_id, SK begins_with "2", Limit=1800, ScanIndexForward=False)` then reverse — ONE query serves both windows: the trailing 150-slice feeds `extract_features`, the full window feeds `compute_psi` (single-Query decision, 2026-06-02 PSI follow-on session log) |
| Hot path: append reading | `PutItem` on the reading row |
| Hot path: edge-trigger read | `GetItem(PK=pump_id, SK="STATE")` immediately before the STATE overwrite — previous `alert_flag` (publish gate) + `last_alert_sent_at` carry-forward (ADR 0012) |
| Hot path: overwrite STATE | `PutItem` on the STATE row |
| Dashboards: fleet latest | `BatchGetItem` across 15 STATE keys |

The `SK begins_with "2"` predicate filters out the STATE row (ISO-8601 timestamps start with year digits; `"STATE"` starts with `S`). Reading PutItem + STATE PutItem are NOT issued via `TransactWriteItems` — see ADR 0010 §Item ordering for the rationale.

## Lambda scorer event envelope
From IoT Rule trigger. Standard IoT message + rule metadata. The handler should treat the inner payload as the MQTT body above.

## Lambda scorer DynamoDB writes
Per invocation (PSI follow-on landed 2026-06-02):
- `Query` the 1800-row window (single read serving both scoring + PSI; see §Access patterns).
- `PutItem` on the reading row `{PK=pump_id, SK=<ts>, telemetry…, score}` — schema unchanged from MVP.
- `GetItem` on the STATE row (edge-trigger input, ADR 0012).
- `PutItem` on the STATE row `{PK=pump_id, SK="STATE", latest_ts, latest_score, latest_psi, alert_flag[, last_alert_sent_at]}`.
- SNS publish on the False → True `alert_flag` flip only (ADR 0012; payload per §SNS alert payload below).

## S3 archive layout
```
s3://<bucket>/year=YYYY/month=MM/day=DD/hour=HH/<batch>.parquet
```
- One Parquet file per minute (from EventBridge-triggered batcher).
- Glue Catalog table defined in `infra/modules/glue_catalog/`, no Crawler.

## Reference distribution (PSI baseline)
- File: `model/artifacts/operational_reference_distribution.json` (ADR 0008 operational; 4-feature PSI surface per ADR 0009).
- Format: per-PSI-feature histograms with bin edges + bin counts.
- Loaded by both `local_runtime/scorer_service.py` and `lambda_scorer/handler.py`.
- Bundling location during AWS mode: open, see HANDOFF §6 Q4.

## PSI parameters
- Per-feature, rolling 1-hour window per pump.
- Bin count: TBD (likely 10 equal-frequency).
- Smoothing: TBD (Laplace + epsilon to avoid div/0).
- Thresholds: <0.1 stable, 0.1–0.25 warning, >0.25 significant shift.

## SNS alert payload
Published when PSI > 0.25 OR P(failure_48h) > 0.7. Publish is **edge-triggered** (ADR 0012): only on the False → True flip of `alert_flag` — a persisting breach publishes once, not per-invocation. Topic ARN supplied via the `SNS_TOPIC_ARN` env var (required at Lambda cold-start).
```json
{
  "pump_id":    "P-07",
  "ts":         "...",
  "alert_type": "psi_breach" | "high_failure_prob" | "both",
  "score":      0.82,
  "psi":        { "vibration_amp": 0.31, "bearing_temp": 0.08, ... }
}
```

## Grafana → DynamoDB adapter
> **Open: HANDOFF.md §6 Q1.** Lambda Function URL + JSON datasource plugin is the leader. API contract TBD. The adapter consumes STATE rows via `BatchGetItem` per `## DynamoDB schema` above — one call per panel refresh, fleet-wide latest snapshot. Alert surfacing reads `alert_flag` (current breach) + `last_alert_sent_at` (last page) directly — no client-side threshold re-derivation (ADR 0012 §Alternatives 2C).
