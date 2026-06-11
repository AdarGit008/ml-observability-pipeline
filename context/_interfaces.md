# _interfaces.md — cross-component contracts

Load this when work crosses component boundaries (e.g., simulator → scorer, scorer → batcher). Keep schemas authoritative here; component files reference, don't duplicate.

> **Status:** All shapes below are resolved and live except the AWS-mode reference bundling location (HANDOFF §6 Q4 — open). Grafana adapter contract resolved 2026-06-04 by ADR 0014; cold-path batcher contract (WATERMARK row + archive layout mechanics) resolved 2026-06-04 by ADR 0015.

## MQTT topic pattern
```
factory/pumps/{pump_id}/telemetry
```
- `pump_id` format: `P-NN` zero-padded (e.g., `P-07`).
- One Thing per pump in AWS mode; one client per pump in local mode.
- **Error topic (ADR 0015 session):** `factory/errors` — the IoT Rule's `error_action` republishes messages whose scorer invoke failed past IoT's retries.

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
> **Resolved 2026-06-02 by ADR 0010.** Option A with a STATE-row sibling: one row per reading keyed `(pump_id, <ISO-8601 ts>)` for history, one row per pump keyed `(pump_id, "STATE")` for the latest snapshot. PSI follow-on (2026-06-02) landed the pre-authorized STATE-row extension: `latest_psi`, `alert_flag`, `last_alert_sent_at` (ADR 0012). **Cold path (2026-06-04, ADR 0015) added a second reserved SK: `(pump_id, "WATERMARK")` — the batcher's per-pump checkpoint.**

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

### WATERMARK row (ADR 0015)
```
PK = pump_id
SK = "WATERMARK"
last_cutoff = <ISO-8601 ts>   # hi bound of the last successful batch;
                              # the next batch drains (last_cutoff, cutoff]
updated_at  = <ISO-8601 ts>   # batcher invocation time
```
One row per pump, written by `lambda_s3_batcher` only — and only
after a successful S3 put (at-least-once; never regresses). ABSENT
until the pump's first archived batch (epoch lower bound applies).

### Reserved-SK coexistence (ADR 0010 rule, extended by ADR 0015)
ISO-8601 timestamps start with year digits; `"STATE"` and
`"WATERMARK"` start with letters. The hot path's `SK begins_with "2"`
predicate excludes BOTH reserved rows; the batcher's BETWEEN range
(timestamps on both ends) excludes them too. Any future SK convention
must coexist with both.

### Access patterns

| Pattern | Operation |
|---|---|
| Hot path: read rolling window | `Query(PK=pump_id, SK begins_with "2", Limit=1800, ScanIndexForward=False)` then reverse — ONE query serves both windows: the trailing 150-slice feeds `extract_features`, the full window feeds `compute_psi` (single-Query decision, 2026-06-02 PSI follow-on session log) |
| Hot path: append reading | `PutItem` on the reading row |
| Hot path: edge-trigger read | `GetItem(PK=pump_id, SK="STATE")` immediately before the STATE overwrite — previous `alert_flag` (publish gate) + `last_alert_sent_at` carry-forward (ADR 0012) |
| Hot path: overwrite STATE | `PutItem` on the STATE row |
| Dashboards: fleet latest | `BatchGetItem` across the `FLEET_SIZE` STATE keys — the adapter's ONLY operation (ADR 0014; IAM grants nothing else) |
| Cold path: read watermarks | `BatchGetItem` across the `FLEET_SIZE` WATERMARK keys — one call per batch (ADR 0015) |
| Cold path: drain new readings | Per pump, `Query(PK=pump_id, SK BETWEEN <last_cutoff + "0"> AND <cutoff>)` — the suffix makes the inclusive lower bound exclusive (ADR 0015 §Decision 1) |
| Cold path: advance watermark | `PutItem` on the WATERMARK row, after the S3 put succeeds |

Reading PutItem + STATE PutItem are NOT issued via `TransactWriteItems` — see ADR 0010 §Item ordering. The S3 put + WATERMARK PutItems are likewise non-transactional — see ADR 0015 §Consequences (duplicates possible, loss not).

## Lambda scorer event envelope
From IoT Rule trigger. Standard IoT message + rule metadata. The handler should treat the inner payload as the MQTT body above.

## Lambda scorer DynamoDB writes
Per invocation (PSI follow-on landed 2026-06-02):
- `Query` the 1800-row window (single read serving both scoring + PSI; see §Access patterns).
- `PutItem` on the reading row `{PK=pump_id, SK=<ts>, telemetry…, score}` — schema unchanged from MVP.
- `GetItem` on the STATE row (edge-trigger input, ADR 0012).
- `PutItem` on the STATE row `{PK=pump_id, SK="STATE", latest_ts, latest_score, latest_psi, alert_flag[, last_alert_sent_at]}`.
- SNS publish on the False → True `alert_flag` flip only (ADR 0012; payload per §SNS alert payload below).

## Fleet-PSI DynamoDB writes
Per invocation (EventBridge `rate(5 minutes)`; ADR 0018, `lambda_fleet_psi`):
- Per pump `P-01..P-NN`: `Query` the trailing 150-row window (`sk begins_with "2"`, `ScanIndexForward=False`) — same access pattern as the scorer; pooled across the fleet.
- `GetItem` the FLEET STATE row (edge-trigger input, ADR 0012).
- `PutItem` the FLEET STATE row `{pump_id="FLEET", sk="STATE", latest_ts, latest_psi (4-key Map), alert_flag, pumps_reporting[, last_alert_sent_at]}`. `FLEET` is a SEPARATE partition — invisible to the per-pump scorer/batcher iteration and the score-path query.
- SNS publish on the False → True `alert_flag` flip only (ADR 0012); PSI-only payload (see §SNS alert payload, FLEET-scope note).

## S3 archive layout
> **Mechanics locked 2026-06-04 by ADR 0015.**
```
s3://<bucket>/year=YYYY/month=MM/day=DD/hour=HH/<compact-cutoff>.parquet
```
- Bucket: `<project_tag>-pump-archive-<account-id>` (deterministic; `force_destroy = true`).
- The bucket also carries a **`deploy/` prefix** holding both Lambda zips (2026-06-04: the scorer zip measured 62 MB > the 50 MB direct-upload limit — ADR 0006 §Q4 fallback; the batcher rides the same mechanism). `deploy/` sits outside the Glue `year=*` projection paths, so Athena never reads it; `force_destroy` sweeps it.
- One Parquet file per non-empty batch (60 s EventBridge cadence). Partition values derive from the batch cutoff (UTC); the filename is the cutoff with `-`/`:`/`.` stripped (e.g. `20260604T143200123Z.parquet`) — unique because cutoffs strictly advance.
- Columns (= `lambda_s3_batcher.handler.PARQUET_SCHEMA` = the Glue table): `pump_id string, ts string, vibration_amp double, bearing_temp double, motor_current double, rpm double, score double`. `ts` is the reading row's sort key, verbatim.
- Glue Catalog table defined in `infra/modules/glue_catalog/`, **no Crawler** — partition projection computes partitions from query predicates; nothing registers them.
- At-least-once: duplicate `(pump_id, ts)` pairs may span two files after a put/watermark partial failure; consumers dedupe on that key.

## Reference distribution (PSI baseline)
- File: `model/artifacts/operational_reference_distribution.json` (ADR 0008 operational; 4-feature PSI surface per ADR 0009).
- Format: per-PSI-feature histograms with bin edges + bin counts.
- Loaded by both `local_runtime/scorer_service.py` and `lambda_scorer/handler.py`.
- Bundling location during AWS mode: open, see HANDOFF §6 Q4.

## PSI parameters
- Per-feature, rolling 1-hour window per pump.
- Bin count: 10 equal-frequency bins (ADR 0007).
- Smoothing: Laplace add-α, α = 1.0 (`shared.drift.LAPLACE_ALPHA`, ADR 0007).
- Thresholds: <0.1 stable, 0.1–0.25 warning, >0.25 significant shift.

## SNS alert payload
Published when PSI > 0.25 OR P(failure_48h) > 0.7. Publish is **edge-triggered** (ADR 0012): only on the False → True flip of `alert_flag` — a persisting breach publishes once, not per-invocation. Topic ARN supplied via the `SNS_TOPIC_ARN` env var (required at Lambda cold-start).

**FLEET-scope variant (ADR 0018, `lambda_fleet_psi`).** The fleet-PSI Lambda reuses this topic and edge-trigger but publishes a drift-only payload: `pump_id="FLEET"`, `scope="fleet"`, `alert_type="psi_breach"`, `score=null` (no model on the fleet path), the 4-key `psi` map, and `pumps_reporting`. Generic subscribers filter on `scope` (absent ⇒ per-pump); the per-pump payload is unchanged.
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
> **Resolved 2026-06-04 by ADR 0014.** Read-only Lambda (`pump-dashboard-adapter`) behind a Function URL (AuthType=NONE — recorded PO call), consumed by the Grafana **Infinity** plugin with root selector `$.pumps`. One `BatchGetItem` over the fleet's STATE keys per panel refresh.

One GET (path ignored; non-GET → 405) returns:

```json
{
  "fleet_size":      15,
  "pumps_reporting": 13,
  "as_of":           "<adapter invocation ts, ISO-8601 UTC ms>",
  "pumps": [
    {
      "pump_id":            "P-01",
      "latest_ts":          "2026-06-04T14:32:00.971Z",
      "latest_score":       0.04,
      "psi_vibration_amp":  0.02,
      "psi_bearing_temp":   0.01,
      "psi_motor_current":  0.03,
      "psi_rpm":            0.02,
      "alert_flag":         false,
      "last_alert_sent_at": null
    }
  ]
}
```

- `psi_<feature>` keys are the ADR 0005 §3 InfluxDB field names — AWS-mode and local-mode panels share one vocabulary, zero transforms.
- `alert_flag` (current breach) + `last_alert_sent_at` (last page) are **literal STATE-row passthroughs** — no client-side threshold re-derivation (ADR 0012 §Alternatives 2C). Storage's absent-until-first-publish maps to explicit JSON `null` on the wire (stable key set; ADR 0014 §Decision 2).
- Pumps without a STATE row are **omitted** (no null-filled placeholders); `pumps_reporting` vs `fleet_size` carries the gap.
- `pumps` is sorted by `pump_id`; failures return 500 with a generic body (the URL is public); persistent `UnprocessedKeys` is a 500, never a silently short list.

### FLEET object (ADR 0018 follow-up, 2026-06-10 — additive)

The adapter additionally requests the `pump_id="FLEET"` aggregate STATE
row (ADR 0018) in the SAME `BatchGetItem` (16 keys: 15 pumps + FLEET)
and surfaces it as a top-level **`fleet`** object beside `pumps` —
additive; `pumps[]` and all existing keys are unchanged. See ADR 0014
§Addendum 2026-06-10 for the full rationale.

```json
"fleet": {
  "latest_ts":          "2026-06-10T14:30:00.000Z",
  "psi_vibration_amp":  0.30,
  "psi_bearing_temp":   0.12,
  "psi_motor_current":  0.20,
  "psi_rpm":            0.15,
  "alert_flag":         true,
  "last_alert_sent_at": "2026-06-10T14:25:00.000Z",
  "pumps_pooled":       15
}
```

- Same projection as a pump entry (flattened `psi_<feature>`, Decimal→
  float, literal alert passthrough, absent `last_alert_sent_at` → JSON
  `null`) **minus `latest_score`** (no model on the fleet path, ADR 0018
  §5) **plus `pumps_pooled`** — the pooled-window pump count.
- **Wire rename:** the FLEET *row* stores the pooled count as
  `pumps_reporting`; the adapter projects it to **`pumps_pooled`** on the
  wire to disambiguate from the envelope's top-level `pumps_reporting`
  (pumps with a STATE row). The two counts can differ (throttled writes,
  partial window); the rename removes the name collision (DeepSeek review
  2026-06-11 §2).
- `fleet` is an empty object **`{}`** when no FLEET STATE row exists yet
  (fleet Lambda not run, or its empty-fleet no-op) — the single-object
  analogue of the per-pump omit-the-row rule; keeps a `null` off
  Infinity's `$.fleet` root selector and fabricates no `alert_flag`
  all-clear (DeepSeek review §3). The key is always present.
- `as_of` (adapter invocation time) vs `fleet.latest_ts` / pump
  `latest_ts` (row write times) can skew — the fleet Lambda's 5-min
  cadence and the scorer's per-reading writes run on different clocks
  (DeepSeek review §1).
- FLEET is **AWS-only** (local InfluxDB has no FLEET row / alert fields,
  ADR 0005 §3); the panel lives in `dashboards/aws.json` only.
