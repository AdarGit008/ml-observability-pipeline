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
- [ ] Not yet: `s3_archive`, `glue_catalog`, `lambda_s3_batcher` modules; Grafana-adapter Function URL; IoT Thing/cert provisioning.

## Run order (PO-side; terraform not in sandbox)
1. `.\scripts\build_lambda.ps1` — stages `.build/lambda_dist/` + Docker smoke-check (handler cold-start import from staged tree).
2. `cd infra && terraform init`
3. `terraform validate`
4. `terraform plan` — review resource list. **No apply outside demo day; `aws_teardown.sh` after every demo.**

The `archive_file` data source reads `.build/lambda_dist/` at plan time — build before plan (validate works without it).

## Interfaces (in / out)
- **In:** `infra/terraform.tfvars` (gitignored; copy from `terraform.tfvars.example`) — project tag, alert email. Region is a locked-validated default.
- **Out (current):** `sns_topic_arn`, `ddb_table_name`/`arn`, `lambda_function_name`/`arn`, `iot_rule_arn`.
- **Out (later sessions):** Lambda Function URL for the Grafana adapter, S3 bucket name.

## Cost guardrails (CI-enforced — CI itself still TODO)
- CI plan-check must fail if any of these resource types appear: `aws_instance`, `aws_db_instance`, `aws_kinesis_firehose_delivery_stream`, `aws_glue_crawler`, `aws_grafana_workspace`.
- Region pin: `eu-central-1` — enforced today by variable validation; CI re-check later.
- ADR 0013 is the one documented non-$0 exception (~$0.10–0.20/demo DynamoDB on-demand).

## Open questions
- mTLS provisioning flow for IoT Core Things — generate certs locally, upload via Terraform, or via separate `scripts/provision_certs.sh`? (Simulator-side; not in the hot-path modules.)
- IoT Rule has no `error_action` — a throttled/failed invoke is retried per IoT semantics then dropped silently. Reviewer (2026-06-04 cascade) recommends a republish-to-error-topic action; deferred to the dashboards/observability session.
- Glue Catalog table inline vs sub-module. Default: sub-module for reuse. (Lands with s3_archive session.)
- `aws_teardown.sh` does not exist in-repo yet — teardown alignment was this session's stretch goal, not reached; next infra session should create it covering: DynamoDB table, SNS topic+subscription, Lambda, log group, IoT rule, IAM role/policy (i.e., `terraform destroy` + verification sweep).

## Related ADRs
- ADR 0005 §Addendum Q1 — build-script staging answer to multi-root packaging.
- ADR 0006 §Q4 — deploy-zip footprint baseline (~124 MB unzipped, 250 MB ceiling; build script enforces).
- ADR 0010 — DynamoDB schema the table implements.
- ADR 0012 — SNS topic contract + env-var fail-fast posture.
- ADR 0013 — on-demand billing decision (this session).
