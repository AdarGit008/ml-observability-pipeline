# lambda_scorer

## Purpose
Hot-path Lambda. One invocation per MQTT message via IoT Rule. Reads recent feature window from DynamoDB, computes rolling features, scores, updates per-pump PSI, writes back, fires SNS on threshold breach.

## Current state
- ✅ MVP shipped 2026-06-02 (cold-start + per-pump score path). DynamoDB schema locked by ADR 0010 (Option A: `PK=pump_id, SK=sk` where `sk` is the ISO-8601 timestamp for reading rows or the literal `"STATE"` for the per-pump snapshot row).
- ✅ PSI + SNS follow-on shipped 2026-06-02: single hot-path `Query` widened to `Limit=1800` (trailing 150-slice feeds scoring, full window feeds PSI); `shared.drift.compute_psi` called on EVERY invocation; STATE row extended with `latest_psi` (4-key Map per ADR 0009) + `alert_flag` + `last_alert_sent_at`; SNS publish edge-triggered on the False → True `alert_flag` flip (ADR 0012).
- Cold-start: `shared.drift.load_reference()` + `shared.score`-bound model + `boto3.resource("dynamodb").Table(...)` + `boto3.client("sns")`. Reference + model version-match validated at cold-start per ADR 0007. `SNS_TOPIC_ARN` required — `KeyError` at init if unset.
- Hot path: parse event → `Query` last 1800 reading rows (filtering STATE row via `sk begins_with "2"`) → append latest → `extract_features` on the trailing 150 → `score` → `compute_psi` on the full window → `PutItem` reading row → `GetItem` previous STATE (edge-trigger) → `PutItem` STATE row → SNS publish on alert edge.
- 19 tests under `lambda_scorer/tests/` (4 structural-parity + 3 cold-start + 8 hot-path + 4 SNS/PSI alert-path, incl. the ADR 0012 publish-failure at-most-once pin; see test file docstring). Tests get an autouse fake-credentials guard in `conftest.py` (2026-06-04) so a moto-less AWS call fails loudly. Full sandbox suite: 369 passed + 1 skipped.
- Mode-parity nuance: Lambda feeds `compute_psi` raw-telemetry reading rows; local mode feeds it extracted-feature dicts. Equivalent on the PSI surface (only the 4 raw signals are read; identical values in both representations) — documented in the handler module docstring + the 2026-06-02 PSI follow-on session log.

## Interfaces (in / out)
- **In:** IoT Rule event envelope wrapping the telemetry JSON (`_interfaces.md §Telemetry payload`). The handler treats `event` as the raw telemetry dict — matches the default IoT Rule SQL `SELECT * FROM 'factory/pumps/+/telemetry'`. If a future rule wraps the payload, only `_parse_event` needs updating.
- **Out (DynamoDB):** reading row `{pump_id, sk=<ts>, vibration_amp, bearing_temp, motor_current, rpm, score}` + STATE row overwrite `{pump_id, sk="STATE", latest_ts, latest_score, latest_psi, alert_flag[, last_alert_sent_at]}` per ADR 0010 + ADR 0012. One `GetItem` on the STATE row per invocation (edge-trigger input).
- **Out (SNS):** alert payload per `_interfaces.md §SNS alert payload`, published only on the False → True `alert_flag` flip (ADR 0012). Thresholds: `shared.drift.psi_alert_should_fire(window, psi) OR score > 0.7` — the PSI side is the shared composite (warmup floor `PSI_MIN_SAMPLES = 150` AND threshold `PSI_SIGNIFICANT_THRESHOLD = 0.25`, ADR 0017 §1); `score > 0.7` ungated.
- **Shared logic:** `shared.features.extract_features`, `shared.score.score`, `shared.drift.load_reference`, `shared.drift.compute_psi` imported as peers (ADR 0005). Four structural-parity tests under `lambda_scorer/tests/test_handler.py::test_structural_parity_*` pin the load paths.

## Resource sizing
- 512 MB memory.
- Bundled model pickle in deployment package (HANDOFF §6 Q3 default; ADR 0006 §Q4 measured ~124 MB unzipped, ~50% headroom against Lambda's 250 MB ceiling). SNS client is the same boto3 already in the zip — no new dep.
- Cold-start latency target: <2 s (aspirational). **Measured 2026-06-07 first live apply: Init ~4.76–4.83 s, warm ~43 ms, 272 MB / 512 MB peak** — see Open questions. Reference + model + boto3 clients warm at module import; classifier lazy-loads on first `score()` call.
- Volume: 15 pumps × 30 msg/min × 30-min demo ≈ 13.5 K invocations per demo. Per invocation: one `Query` (≤1800 rows), one `GetItem`, two `PutItem`s, ~2 ms of PSI numpy. SNS volume bounded by incident count, not invocation count (edge-trigger) — well inside Always-Free 1M Lambda req/mo, 25 RCU/WCU-equivalent DynamoDB, and 1 K SNS email deliveries/mo.

## Environment variables
- `DDB_TABLE_NAME` — defaults to `pump_hot_state`. Terraform pins the actual name.
- `DDB_ENDPOINT_URL` — optional. Tests set this to a moto endpoint; production leaves it unset (boto3 uses the AWS default endpoint).
- `SNS_TOPIC_ARN` — **required**; `KeyError` at cold-start if unset. Matches the reference eager-load's fail-fast posture: a Lambda deployed without its alert topic wired fails at init in CloudWatch, not silently at publish time. Terraform supplies the topic ARN (IaC session).
- `AWS_REGION` — defaults to `eu-central-1` per `_global.md` Hard constraint #5.

## Open questions
- ~~Cold-start latency (measure post-deploy)~~ — **MEASURED 2026-06-07:** Init ~4.76–4.83 s, warm ~43 ms, 272 MB / 512 MB peak (two cold containers from the 15-way first-publish fan-out). Init exceeds the <2 s aspirational target but sits well inside the 10 s timeout; the hot path is async (IoT→Lambda, no user waiting) so only the first 1–2 invocations of a fan-out pay it. ADR 0006 §Q4's pre-authorized S3-cold-load fallback is therefore not triggered — recorded as informed-by-data; reopen only if a change pushes Init toward the timeout.
- ~~**PSI warmup alert storm (2026-06-07).**~~ **CLOSED 2026-06-10 by ADR 0017.** The storm (9/14 healthy pumps firing `alert_flag: true` within minute 1 — max-PSI > 0.25 on sub-minute windows; scores all ≤ 0.02) is fixed by a min-sample warmup gate: `psi_breach = psi_is_armed(window) and max(psi) > 0.25`, where `psi_is_armed` + `PSI_MIN_SAMPLES = 150` (5 min) live in `shared.drift` (parity-correct single source; binds the future fleet-PSI Lambda). `score > 0.7` stays ungated (the storm was PSI-only). `compute_psi` is unchanged and `latest_psi` is still written on cold windows, so the dashboard shows PSI warming up — only the alert is gated.

## Related ADRs
- ADR 0005 — parity boundary (`shared/{features,score,drift}`). Four structural-parity tests in `lambda_scorer/tests/test_handler.py` enforce (`compute_psi` guard added by the PSI follow-on).
- ADR 0006 §Q4 — Lambda deploy zip footprint baseline (~124 MB unzipped).
- ADR 0007 — `load_reference` contract + PSI formula/cadence. Lambda computes PSI every invocation — the every-Nth-tick cadence is a local-mode InfluxDB-write throttle, not a compute constraint (handler docstring step 6).
- ADR 0008 — operational reference source. Loaded at cold-start; not mutated by hot path.
- ADR 0009 — PSI surface = 4 raw features. `latest_psi` is a 4-key Map; pinned by `test_psi_surface_pinned_at_four_keys`.
- ADR 0010 — DynamoDB schema (load-bearing schema decision). The PSI follow-on exercised the pre-authorized STATE-row extension + the Limit=1800 forward commitment.
- ADR 0012 — edge-triggered SNS alerts + two-attribute alert state (this component's alert-path decision).
- ADR 0017 — PSI warmup gate. `psi_is_armed(window)` + `PSI_MIN_SAMPLES = 150` from `shared.drift` gate the PSI side of `alert_flag` at the arming site; `score > 0.7` ungated. Closes the 2026-06-07 warmup-storm open item. Pinned by `test_structural_parity_psi_is_armed_loads_from_shared` + `test_psi_alert_gated_below_warmup` / `test_psi_alert_arms_when_warm`.
