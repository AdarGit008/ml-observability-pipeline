# Session 2026-06-04 — infra / lambda_s3_batcher — cold path

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** reviewer model from the cascade (see response file footer; ADR 0011)
- **Context loaded:** `_global`, DEV_NORMS §7+§8, `lambda_s3_batcher`, `_interfaces`, ADRs 0010/0013/0014/0006 §Q4, memory (fuse-write-truncation, git-on-windows, infra-session1)
- **NOT a parity-set session** — the batcher imports no `shared/` code (inverse-import test pins it).

## Intent
Complete the AWS data plane's cold path: EventBridge-scheduled batcher Lambda drains recent reading rows to Parquet in S3; Glue Catalog table in Terraform (no Crawler); resolve the read-pattern / Parquet-engine / cadence decisions; land the twice-deferred IoT Rule `error_action`; extend the teardown sweep.

## What changed
- **ADR 0015** — watermark + per-pump Query read pattern; pyarrow (no pandas); 60 s cadence; Glue partition projection; `force_destroy = true`. Free-tier posture verified in-session (S3 5 GB / Glue 1 M objects / EventBridge — all Always-Free at our scale; ~$0.0002/demo S3 PUT residue recorded in the ADR, not a new ADR-0013-style exception).
- **`lambda_s3_batcher/`** — handler + 18 moto tests (S3 + DynamoDB mocked; watermark mechanics; empty-batch no-op; Parquet round-trip read-back; put-failure at-least-once; reserved-row exclusion; cold-start validation; inverse parity test).
- **Terraform** — `modules/{s3_archive, glue_catalog, lambda_s3_batcher}` + root wiring (`main/variables/outputs.tf`) + EventBridge schedule + scoped IAM (Query/BatchGetItem/PutItem on table; PutObject on `<bucket>/year=*`; nothing wildcarded; `reserved_concurrent_executions = 1`).
- **IoT Rule `error_action`** — republish failed-invoke messages to `factory/errors` with a single-topic-scoped role. Closes the 2026-06-04 dashboards-cascade finding after two deferrals.
- **`scripts/build_batcher.{ps1,sh}`** + `batcher_requirements.txt` — pyarrow-only staging, ADR 0006 §Q4 footprint check.
- **`scripts/aws_teardown.sh`** — absence checks for bucket, Glue db+table, batcher Lambda + log group + role, EventBridge rule, error-republish role.
- **Docs** — `context/{lambda_s3_batcher, infra}.md` rewritten; `_interfaces.md` gains the WATERMARK row, reserved-SK coexistence note, cold-path access patterns, and archive-layout mechanics. `requirements.txt` +pyarrow (justified line).

## Decisions
- All in **ADR 0015** (PO sign-off in-session via the four-option call: watermark+Query / pyarrow / 60 s / force_destroy; Glue sub-module default confirmed by non-objection).

## Trade-offs surfaced
- **At-least-once over exactly-once:** S3 put + watermark writes are non-transactional; duplicates across files possible, loss not. Dedupe key `(pump_id, ts)`.
- **Late rows beyond the 5 s safety lag are skipped permanently** — accepted at demo scale; Streams is the recorded production upgrade path (ADR 0015 §Alternatives 1B).
- **pyarrow (~100 MB unzipped) for ~450-row files** — portfolio signal over footprint; CSV is the recorded fallback if the build-script check ever fails.
- **`dynamodb:PutItem` on the table ARN** technically exceeds the watermark-only need (IAM can't scope to an SK) — flagged to the reviewer (packet Q5).

## Reviewer feedback highlights
- Cascade ran 2026-06-04; response by **groq** (`llama-3.3-70b-versatile`) — provenance footer on `review_responses/2026-06-04-infra-cold-path.md` (ADR 0011: weight accordingly — Llama 3.3's posture runs validating, not adversarial; it confirmed 2/7 and asked for things 3/7 of which already existed).
- **Q4 (projection ranges)** → addressed: out-of-range Athena behavior (empty results, not errors) now documented at the projection params in `glue_catalog/main.tf`.
- **Q5 (PutItem breadth)** → rejected with documentation: IAM cannot condition on sort keys (`dynamodb:LeadingKeys` is PK-only), and a separate watermark table re-loses ADR 0010 §Alternatives 2B. KNOWN BREADTH comment added at the grant.
- **Q1 (boundary collision)** → rejected as a misreading: a cutoff-keyed row is archived in its own batch (inclusive hi bound); the suffix only prevents re-archiving. `test_boundary_row_at_cutoff_archived_once` pins it.
- **Q2/Q3** → already covered (ADR 0015 §Consequences; `test_watermarks_advance_for_all_pumps_including_rowless`). **Q6 (stuck invocation + concurrency=1)** → rejected: missed ticks are self-healing under the watermark pattern. **Q7 (cost math)** → verified.
- Full dispositions table: `review_packets/2026-06-04-infra-cold-path.md` §Resolution.

## State at end of session
- Tests: **404 passed + 1 skipped** (baseline 386+1 + 18 new). Run sandbox-side from `/tmp` copy per the FUSE norm.
- `terraform validate` + `plan`: **pending PO-side** (terraform not in sandbox). Run ALL THREE build scripts first — `archive_file` reads `.build/{lambda_dist,adapter_dist,batcher_dist}` at plan time.
- **Build-script fix (post-cascade, PO-side verification step):** the Docker smoke-check's FIRST real run caught a latent bug in `scripts/build_lambda.{ps1,sh}` — the tests-strip deleted `numpy/_core/tests`, but numpy 2.4.x's `numpy.testing` imports `numpy._core.tests._natype` at module level, and scipy's `array_api_compat` `from numpy import *` triggers it during the handler's cold-start import. Fix: numpy is now EXEMPT from the strip (a few MB vs the 250 MB ceiling). Verified sandbox-side: the find/Where-Object exemption keeps only numpy's tests; a stripped pyarrow tree (batcher dist shape) still imports + Parquet-round-trips, so `build_batcher.*` needs no exemption. This is exactly the failure class the smoke-check exists for — it had been Docker-skipped until today. **Second catch, same run:** the smoke output showed `InconsistentVersionWarning` — model.pkl was pickled by sklearn 1.9.0 (PO venv) but the dist resolved sklearn 1.7.2, because the `manylinux2014` platform cap excluded 1.9.0's wheels (manylinux_2_28-only). Newer-pickle-into-older-sklearn is the unsupported direction. Fix: `lambda_requirements.txt` now PINS the training versions (numpy==2.4.6, scikit-learn==1.9.0, joblib==1.5.3 — retraining bumps the pins in the same commit as the new model.pkl), and both build scripts request `manylinux_2_28_x86_64` + `manylinux2014_x86_64` (Lambda python3.12 = AL2023, glibc 2.34 — compatible). First REAL footprint measurement: 181 MB unzipped with sklearn 1.7.2 (ADR 0006 §Q4's ~124 was a paper estimate); expect similar ±10 with the pins. Ceiling 250. **Third catch:** `build_batcher` hit the same platform cap — pyarrow 21+ is manylinux_2_28-only, so the ==24.0.0 pin was unresolvable; both batcher scripts now accept manylinux_2_28 like the scorer's. **Fourth catch:** measured zips: scorer **62.1 MB** (> 50 MB direct-upload limit — apply would have failed at CreateFunction), batcher 47.6 MB (~2 MB headroom), adapter ~KBs. Both Lambdas switched to the ADR 0006 §Q4 fallback: `aws_s3_object` uploads the zips to the archive bucket under `deploy/` (outside the Glue projection paths; force_destroy sweeps it) and the functions reference `s3_bucket`/`s3_key`. Scorer module gained a `code_bucket` variable (wired from `module.s3_archive` — the hot path now depends on the cold-path bucket at deploy time, an acceptable coupling recorded here).
- File integrity: every touched file verified in BOTH views (sandbox `grep -c ''` + Windows-side Read). Root `infra/*.tf` + `context/{infra,_interfaces}.md` were rebuilt via bash write-through (their sandbox views had gone stale after the dashboards session's Write-tool overwrites) — both views now fresh.
- `context/lambda_s3_batcher.md` updated: yes (full rewrite — component shipped).
- Open follow-ups: IoT Thing/cert provisioning (simulator-side); CI cost guardrails; Grafana dashboard JSON pair; README cost-table line citing ADR 0015.

## Note for next session
Cold path is code-complete and cascade-reviewed (dispositions above; two comment-level changes, no code changes — suite unchanged at 404+1). The natural follow-on is the Grafana dashboards JSON pair (closes the dashboards component: local InfluxDB panels + AWS Infinity panels sharing the ADR 0005 §3 field vocabulary). The WATERMARK reserved SK now coexists with STATE — any session touching SK conventions must check both (`_interfaces.md §Reserved-SK coexistence`). Batcher sizing knobs (`batcher_schedule_expression`, `batcher_safety_lag_seconds`) are tfvars-level if demo rehearsal wants different pacing.
