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
- ✅ `scripts/aws_teardown.sh` exists (2026-06-04, carried gap closed): `terraform destroy` wrapper + AWS CLI verification sweep over EVERY resource both sessions create (table, topic+subscription, both Lambdas, both log groups, Function URL, IoT rule, both IAM roles) + $1/$5 budget-alert posture check (budget absence = FAIL). `--verify-only` / `--destroy-only` modes. Unconfirmed SNS subscription = WARN (undeletable; AWS expires it ~3 days).
- [ ] Not yet: `s3_archive`, `glue_catalog`, `lambda_s3_batcher` modules; IoT Thing/cert provisioning.

## Run order (PO-side; terraform not in sandbox)
1. `.\scripts\build_lambda.ps1` — stages `.build/lambda_dist/` + Docker smoke-check (handler cold-start import from staged tree).
2. `.\scripts\build_adapter.ps1` — stages `.build/adapter_dist/` (copy + strip; seconds).
3. `cd infra && terraform init`
4. `terraform validate`
5. `terraform plan` — review resource list. **No apply outside demo day; `./scripts/aws_teardown.sh` after every demo.**

The `archive_file` data sources read the `.build/` trees at plan time — build before plan (validate works without them).

## Interfaces (in / out)
- **In:** `infra/terraform.tfvars` (gitignored; copy from `terraform.tfvars.example`) — project tag, alert email. Region is a locked-validated default. `fleet_size` (default 15) must match the simulator fleet.
- **Out (current):** `sns_topic_arn`, `ddb_table_name`/`arn`, `lambda_function_name`/`arn`, `iot_rule_arn`, `adapter_function_url` (→ Grafana Infinity datasource), `adapter_function_name`.
- **Out (later sessions):** S3 bucket name.

## Cost guardrails (CI-enforced — CI itself still TODO)
- CI plan-check must fail if any of these resource types appear: `aws_instance`, `aws_db_instance`, `aws_kinesis_firehose_delivery_stream`, `aws_glue_crawler`, `aws_grafana_workspace`.
- Region pin: `eu-central-1` — enforced today by variable validation; CI re-check later.
- ADR 0013 is the one documented non-$0 exception (~$0.10–0.20/demo DynamoDB on-demand). Adapter reads add ~$0.0003/demo on top (ADR 0014 §Consequences).
- `aws_teardown.sh` after every demo, no exceptions; it also re-verifies the $1/$5 budget alerts exist.

## Open questions
- mTLS provisioning flow for IoT Core Things — generate certs locally, upload via Terraform, or via separate `scripts/provision_certs.sh`? (Simulator-side; not in the hot-path modules.)
- IoT Rule has no `error_action` — a throttled/failed invoke is retried per IoT semantics then dropped silently. Reviewer (2026-06-04 cascade) recommends a republish-to-error-topic action; deferred from the 2026-06-04 dashboards session (stretch goal not reached), still tracked here.
- Glue Catalog table inline vs sub-module. Default: sub-module for reuse. (Lands with s3_archive session.)

## Related ADRs
- ADR 0005 §Addendum Q1 — build-script staging answer to multi-root packaging.
- ADR 0006 §Q4 — deploy-zip footprint baseline (~124 MB unzipped, 250 MB ceiling; build script enforces).
- ADR 0010 — DynamoDB schema the table implements.
- ADR 0012 — SNS topic contract + env-var fail-fast posture.
- ADR 0013 — on-demand billing decision (IaC session #1).
- ADR 0014 — adapter contract + Function URL auth mode (dashboards session #1).
