# infra

## Purpose
Terraform that stands up the AWS demo stack in `eu-central-1`. Apply → 30-min demo → destroy. All Always-Free or near-zero-cost services.

## Current state
- ✅ Hot path shipped 2026-06-04 (IaC session #1): root module + `modules/{dynamodb, sns, iam, lambda_scorer, iot_rule}` + `scripts/build_lambda.{ps1,sh}` + `scripts/lambda_requirements.txt`.
  - `pump_hot_state` table per ADR 0010; billing PAY_PER_REQUEST per **ADR 0013** (~$0.10–0.20/demo; provisioned-25-RCU computed infeasible at 9–10× over read capacity).
  - SNS topic + PO email subscription (confirmation click required at first apply); topic ARN → Lambda env `SNS_TOPIC_ARN`.
  - Lambda: python3.12, 512 MB, timeout 10 s, x86_64; deploy tree staged by the build script (ADR 0005 Addendum Q1), zipped by `archive_file`. **boto3 NOT bundled** (runtime-provided; bundling botocore would blow the 50 MB direct-upload zip limit).
  - IoT Rule `SELECT * FROM 'factory/pumps/+/telemetry'` → Lambda + invoke permission. Raw-dict passthrough matches `_parse_event`.
  - IAM: Query/GetItem/PutItem on the table ARN, sns:Publish on the topic ARN, logs scoped to this function's log group only (no CreateLogGroup — group is Terraform-managed).
  - Backend: local state (single PC; no S3 state-bucket cost surface). Region locked by variable validation.
- ✅ Dashboards adapter shipped 2026-06-04 (dashboards session #1, **ADR 0014**): `modules/dashboards_adapter` — self-contained (own IAM role `pump-dashboard-adapter-exec`, log group, packaging). Function URL AuthType=NONE (recorded PO call; IAM/SigV4 upgrade is config-only). IAM: `dynamodb:BatchGetItem` on the table ARN ONLY — the scoped policy doubles as the tripwire against the adapter growing read patterns. 128 MB / 5 s. Staged by `scripts/build_adapter.{ps1,sh}` (sibling, not extension — adapter has zero third-party deps; no pip, no Docker).
- ✅ Cold path shipped 2026-06-04 (cold-path session, **ADR 0015**): `modules/{s3_archive, glue_catalog, lambda_s3_batcher}`.
  - `s3_archive`: bucket `<project_tag>-pump-archive-<account-id>` (deterministic — teardown derives it), `force_destroy = true` (recorded PO call, ADR 0015 §Decision 5), public-access fully blocked, SSE-S3.
  - `glue_catalog` (sub-module per the recorded default): database `pump_archive` + table `pump_readings`, schema declared in Terraform, **partition projection** on year/month/day/hour — no Crawler, no CreatePartition, catalog stays at 2 objects (Always-Free forever).
  - `lambda_s3_batcher`: 256 MB / 30 s, `reserved_concurrent_executions = 1` (watermark race guard), EventBridge `rate(1 minute)` + invoke permission. IAM: Query/BatchGetItem/PutItem on the table ARN + PutObject on `<bucket>/year=*` + scoped logs — the no-compute tripwire. Staged by `scripts/build_batcher.{ps1,sh}` + `scripts/batcher_requirements.txt` (pyarrow only; footprint check per ADR 0006 §Q4).
  - **Code upload via S3** (both Lambdas): first measured zips (2026-06-04) came out 62 MB (scorer — OVER the 50 MB direct-upload limit; ADR 0006 §Q4 anticipated fallback) and 47.6 MB (batcher — 2 MB headroom). `aws_s3_object` pushes both zips to the archive bucket under `deploy/`; functions reference `s3_bucket`/`s3_key`; `source_code_hash` unchanged. Root cause of the growth: real wheels (sklearn 1.9.0 pinned to the training env + numpy tests exemption) vs the ~124 MB paper estimate.
  - IoT Rule gained `error_action` → republish to `factory/errors` with a single-topic-scoped role (`pump_telemetry_to_scorer_error_republish`) — closes the 2026-06-04 cascade finding after two deferrals.
- ✅ `scripts/aws_teardown.sh` covers BOTH paths: destroy wrapper + absence sweep (table, topic+subscription, all THREE Lambdas, three log groups, Function URL, IoT rule, four IAM roles, bucket, Glue database+table, EventBridge rule) + $1/$5 budget-alert posture check. `--verify-only` / `--destroy-only` modes.
- [ ] Not yet: IoT Thing/cert provisioning; CI cost guardrails.

## Run order (PO-side; terraform not in sandbox)
1. `.\scripts\build_lambda.ps1` — stages `.build/lambda_dist/` + Docker smoke-check.
2. `.\scripts\build_adapter.ps1` — stages `.build/adapter_dist/` (copy + strip; seconds).
3. `.\scripts\build_batcher.ps1` — stages `.build/batcher_dist/` (pyarrow wheel + handler; static smoke-check — manylinux pyarrow can't import on Windows).
4. `cd infra && terraform init`
5. `terraform validate`
6. `terraform plan` — review resource list. **No apply outside demo day; `./scripts/aws_teardown.sh` after every demo.**

The `archive_file` data sources read the `.build/` trees at plan time — ALL THREE builds before plan (validate works without them).

## Interfaces (in / out)
- **In:** `infra/terraform.tfvars` (gitignored; copy from `terraform.tfvars.example`) — project tag, alert email. Region is a locked-validated default. `fleet_size` (default 15) must match the simulator fleet. Cold-path knobs: `batcher_schedule_expression` (default `rate(1 minute)`), `batcher_safety_lag_seconds` (default 5), `glue_database_name`.
- **Out:** `sns_topic_arn`, `ddb_table_name`/`arn`, `lambda_function_name`/`arn`, `iot_rule_arn`, `adapter_function_url` (→ Grafana Infinity datasource), `adapter_function_name`, `s3_bucket_name`, `glue_database_name`, `glue_table_name`, `batcher_function_name`, `batcher_schedule_rule_name`.

## Cost guardrails (CI-enforced — CI itself still TODO)
- CI plan-check must fail if any of these resource types appear: `aws_instance`, `aws_db_instance`, `aws_kinesis_firehose_delivery_stream`, `aws_glue_crawler`, `aws_grafana_workspace`.
- Region pin: `eu-central-1` — enforced today by variable validation; CI re-check later.
- ADR 0013 is the one documented non-$0 exception (~$0.10–0.20/demo DynamoDB on-demand). Adapter reads add ~$0.0003/demo (ADR 0014); batcher reads + S3 PUTs add ~$0.0005/demo combined (ADR 0015 — S3 storage/Glue/EventBridge verified Always-Free 2026-06-04).
- `aws_teardown.sh` after every demo, no exceptions; it also re-verifies the $1/$5 budget alerts exist.

## Open questions
- mTLS provisioning flow for IoT Core Things — generate certs locally, upload via Terraform, or via separate `scripts/provision_certs.sh`? (Simulator-side; not in the hot-path modules.)

## Related ADRs
- ADR 0005 §Addendum Q1 — build-script staging answer to multi-root packaging.
- ADR 0006 §Q4 — deploy-zip footprint method (~124 MB scorer baseline, 250 MB ceiling; all build scripts enforce).
- ADR 0010 — DynamoDB schema the table implements (+ reserved-SK coexistence the WATERMARK row extends).
- ADR 0012 — SNS topic contract + env-var fail-fast posture.
- ADR 0013 — on-demand billing decision (IaC session #1).
- ADR 0014 — adapter contract + Function URL auth mode (dashboards session #1).
- ADR 0015 — cold-path batcher: watermark read pattern, pyarrow, cadence, partition projection, force_destroy (cold-path session).
