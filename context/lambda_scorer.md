# lambda_scorer

## Purpose
Hot-path Lambda. One invocation per MQTT message via IoT Rule. Reads recent feature window from DynamoDB, computes rolling features, scores, updates per-pump PSI, writes back, fires SNS on threshold breach.

## Current state
- ✅ MVP shipped 2026-06-02 (cold-start + per-pump score path). DynamoDB schema locked by ADR 0010 (Option A: `PK=pump_id, SK=sk` where `sk` is the ISO-8601 timestamp for reading rows or the literal `"STATE"` for the per-pump snapshot row).
- Cold-start: `shared.drift.load_reference()` + `shared.score`-bound model + `boto3.resource("dynamodb").Table(...)`. Reference + model version-match validated at cold-start per ADR 0007.
- Hot path: parse event → `Query` last 150 reading rows (filtering STATE row via `sk begins_with "2"`) → append latest → `extract_features` → `score` → `PutItem` reading row + `PutItem` STATE row overwrite.
- 10 tests under `lambda_scorer/tests/` (3 structural-parity + 2 cold-start + 5 hot-path). Full sandbox suite: 360 passed + 1 skipped.
- ⏳ PSI compute + SNS publish deferred to a follow-on session. The reference is loaded at cold-start so the follow-on adds a `compute_psi` call on a wider `Query` (Limit 150 → 1800) plus a STATE-row extension for `latest_psi` + `alert_flag` — no schema migration.

## Interfaces (in / out)
- **In:** IoT Rule event envelope wrapping the telemetry JSON (`_interfaces.md §Telemetry payload`). The handler treats `event` as the raw telemetry dict — matches the default IoT Rule SQL `SELECT * FROM 'factory/pumps/+/telemetry'`. If a future rule wraps the payload, only `_parse_event` needs updating.
- **Out:** Two `PutItem`s per invocation per ADR 0010 — reading row `{pump_id, sk=<ts>, vibration_amp, bearing_temp, motor_current, rpm, score}` and STATE row overwrite `{pump_id, sk="STATE", latest_ts, latest_score}`. PSI follow-on extends the STATE row + adds SNS publish.
- **Shared logic:** `shared.features.extract_features`, `shared.score.score`, `shared.drift.load_reference` imported as peers (ADR 0005). Structural-parity tests under `lambda_scorer/tests/test_handler.py::test_structural_parity_*` pin the load paths.

## Resource sizing
- 512 MB memory.
- Bundled model pickle in deployment package (HANDOFF §6 Q3 default; ADR 0006 §Q4 measured ~124 MB unzipped, ~50% headroom against Lambda's 250 MB ceiling).
- Cold-start latency target: <2 s. Reference + model + boto3 client warm at module import; classifier lazy-loads on first `score()` call. Measure (not assume) post-deploy.
- Volume: 15 pumps × 30 msg/min × 30-min demo ≈ 13.5 K invocations per demo. Two `PutItem`s per invocation = 27 K writes per demo. Well inside Always-Free 1M Lambda req/mo and 25 RCU/WCU equivalent on-demand DynamoDB.

## Environment variables
- `DDB_TABLE_NAME` — defaults to `pump_hot_state`. Terraform pins the actual name.
- `DDB_ENDPOINT_URL` — optional. Tests set this to a moto endpoint; production leaves it unset (boto3 uses the AWS default endpoint).
- `AWS_REGION` — defaults to `eu-central-1` per `_global.md` Hard constraint #5.

## Open questions
- Cold-start latency — to be measured post-deploy. Bundle-vs-S3-cold-load decision per ADR 0006 §Q4 (fall-back to S3 cold-load pre-authorized without an ADR amendment if measurement exceeds <2 s target).

## Related ADRs
- ADR 0005 — parity boundary (`shared/{features,score,drift}`). Structural-parity tests in `lambda_scorer/tests/test_handler.py` enforce.
- ADR 0006 §Q4 — Lambda deploy zip footprint baseline (~124 MB unzipped).
- ADR 0007 — `load_reference` contract: model/reference `model_version` cross-check raises `DriftError` on desync.
- ADR 0008 — operational reference source. Loaded at cold-start; not mutated by hot path.
- ADR 0009 — PSI surface = 4 raw features. PSI follow-on writes `latest_psi` as a 4-key dict to the STATE row.
- ADR 0010 — DynamoDB schema (this component's load-bearing schema decision). Option A + STATE-row sibling + DynamoDB-backed PSI window (forward commitment).
