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

## DynamoDB schema
> **Resolved 2026-06-02 by ADR 0010.** Option A with a STATE-row sibling: one row per reading keyed `(pump_id, <ISO-8601 ts>)` for history, one row per pump keyed `(pump_id, "STATE")` for the latest snapshot. Two PutItems per scoring invocation; one BatchGetItem for fleet-wide latest snapshot.

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
# PSI follow-on adds:
#   latest_psi  = { vibration_amp: float, bearing_temp: float, motor_current: float, rpm: float }
#   alert_flag  = bool
```

### Access patterns

| Pattern | Operation |
|---|---|
| Hot path: read rolling window | `Query(PK=pump_id, SK begins_with "2", Limit=150, ScanIndexForward=False)` then reverse |
| Hot path: append reading | `PutItem` on the reading row |
| Hot path: overwrite STATE | `PutItem` on the STATE row |
| PSI follow-on: 1-hour window | Same `Query`, `Limit=1800` |
| Dashboards: fleet latest | `BatchGetItem` across 15 STATE keys |

The `SK begins_with "2"` predicate filters out the STATE row (ISO-8601 timestamps start with year digits; `"STATE"` starts with `S`). Reading PutItem + STATE PutItem are NOT issued via `TransactWriteItems` — see ADR 0010 §Item ordering for the rationale.

## Lambda scorer event envelope
From IoT Rule trigger. Standard IoT message + rule metadata. The handler should treat the inner payload as the MQTT body above.

## Lambda scorer DynamoDB writes
Per invocation, MVP scope (PSI + alert deferred):
- `PutItem` on the reading row `{PK=pump_id, SK=<ts>, telemetry…, score}`.
- `PutItem` on the STATE row `{PK=pump_id, SK="STATE", latest_ts, latest_score}`.

PSI follow-on extends the STATE row write with `latest_psi` + `alert_flag` and adds an SNS publish branch on threshold breach. Reading-row schema unchanged.

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
Published when PSI > 0.25 OR P(failure_48h) > 0.7.
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
> **Open: HANDOFF.md §6 Q1.** Lambda Function URL + JSON datasource plugin is the leader. API contract TBD. The adapter consumes STATE rows via `BatchGetItem` per `## DynamoDB schema` above — one call per panel refresh, fleet-wide latest snapshot.
