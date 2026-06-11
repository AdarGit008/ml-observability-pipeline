# lambda_fleet_psi

## Purpose
Plant-wide drift detector. EventBridge wakes it every 5 minutes; it pools the trailing 5-minute reading window from ALL pumps into ONE window, runs `shared.drift.compute_psi` against the operational reference, and writes a single fleet PSI to a `pump_id="FLEET"` STATE row — edge-triggering an SNS alert on the False→True flip via the shared `psi_alert_should_fire`. Catches a fleet-wide shift (e.g. `seasonal_drift`, ADR 0004) too subtle to flag per-pump. The last pipeline component (PLAN.md §2.7). Pools the live fleet window against the **15-pump pooled** operational reference (ADR 0008) — apples-to-apples. It is a **systemic-shift** detector and a **late indicator** for any single pump (≈1/N of pooled mass; the per-pump alert fires first), NOT a substitute for per-pump drift (DeepSeek review 2026-06-10 §1).

## Current state
- ✅ **Handler + tests shipped 2026-06-10 (ADR 0018):** `lambda_fleet_psi/handler.py` + 9 moto tests (3 structural-parity + cold-start + pooling/healthy + empty no-op + drifting edge-publish + warmup-gate). In the parity set (imports `compute_psi`).
- ⏳ **Terraform + build script + teardown DEFERRED to an infra session** (PO scope call 2026-06-10). Not yet deployable.
- Reads the hot table via the scorer's per-pump `Query` (ADR 0010); pools across `P-00..P-(N-1)`; one `compute_psi` on the pooled window. Arms via the shared composite `psi_alert_should_fire` (ADR 0017) — same warmup gate (`PSI_MIN_SAMPLES`) + 0.25 threshold as the per-pump scorer. No score path (drift-only).
- FLEET is a separate DynamoDB partition → invisible to the scorer/batcher per-pump iteration and the score-path query; disturbs no existing access pattern.

## Interfaces (in / out)
- **In:** EventBridge scheduled event (payload unused — the table holds the state). Cadence `rate(5 minutes)` (ADR 0018; Terraform-wired in the infra session).
- **Out (DynamoDB):** FLEET STATE row overwrite `{pump_id="FLEET", sk="STATE", latest_ts, latest_psi (4-key Map), alert_flag, pumps_reporting[, last_alert_sent_at]}` (`_interfaces.md §DynamoDB schema`). One `GetItem` on the FLEET row per run (edge-trigger input).
- **Out (SNS):** fleet alert payload, published only on the False→True `alert_flag` flip (ADR 0012). PSI-only: `{pump_id:"FLEET", scope:"fleet", ts, alert_type:"psi_breach", score:null, psi:{…}, pumps_reporting}` (`_interfaces.md §SNS alert payload`, FLEET-scope note). Reuses the scorer's topic.
- **Shared logic:** `shared.drift.compute_psi`, `shared.drift.psi_alert_should_fire`, `shared.drift.load_reference` imported as peers (ADR 0005). Three structural-parity guards in `test_handler.py` pin the load paths.

## Environment variables
- `DDB_TABLE_NAME` — default `pump_hot_state`.
- `SNS_TOPIC_ARN` — **required**; `KeyError` at cold start if unset (ADR 0012 fail-fast). Reuses the scorer's topic.
- `FLEET_SIZE` — default 15; 1..99 validated; expands to `P-00..P-(N-1)`.
- `DDB_ENDPOINT_URL` — local-test affordance. `AWS_REGION` — default `eu-central-1`.

## Resource sizing (target — Terraform deferred)
- Drift-only zip: numpy + `shared/{features,drift}.py` + `operational_reference_distribution.json`. **No sklearn, no model.pkl** (`load_reference` skips the version check when `model.pkl` is absent, ADR 0007 §4). Much lighter than the scorer zip.
- Per run: ≤15 Queries (≤150 rows each) + 1 GetItem + 1 PutItem + ~numpy PSI on ≤2250 pooled rows. Well inside the envelope the batcher validated (ADR 0013/0015). SNS bounded by incident count (edge-trigger).

## Open questions
- **Terraform module + build script + teardown** — deferred to the infra session (ADR 0018 §Follow-ups): `infra/modules/fleet_psi` (EventBridge `rate(5 minutes)` + Lambda + scoped IAM: Query on table ARN + `sns:Publish` on topic ARN + scoped logs), `scripts/build_fleet_psi.{ps1,sh}` (drift-only staging + ADR 0006 §Q4 footprint check), env wiring, teardown sweep.
- **Dashboards FLEET panel** — deferred to a dashboards session: the adapter reads the 15 pump STATE keys; surfacing FLEET is a small additive `BatchGetItem` + contract change (ADR 0014).
- **Live verify** — confirm `seasonal_drift` arms the FLEET alert post-warm-up (rolls into the deferred demo-day rehearsal).

## Related ADRs
- **ADR 0018** — this component's charter: pooled fleet PSI, FLEET STATE row + edge-triggered SNS, drift-only deploy, hot-table per-pump read. **Accepted** 2026-06-10 (DeepSeek review pending).
- ADR 0017 — the shared `psi_alert_should_fire` composite (warmup gate + threshold) this Lambda arms through.
- ADR 0012 — edge-triggered SNS + two-attribute alert state (mirrored on the FLEET row).
- ADR 0010 — DynamoDB schema; FLEET separate-partition cleanliness + the per-pump Query reused here.
- ADR 0015 — the EventBridge-Lambda sibling (per-pump fan-out, empty no-op, FLEET_SIZE→P-NN).
- ADR 0007 §4 — `load_reference` skips the version check without `model.pkl` → the drift-only layer.
- ADR 0004 — `seasonal_drift`, the motivating fleet-wide case.
- ADR 0005 — parity boundary; the three structural-parity guards.
