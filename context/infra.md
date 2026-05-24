# infra

## Purpose
Terraform that stands up the AWS demo stack in `eu-central-1`. Apply → 30-min demo → destroy. All Always-Free or near-zero-cost services.

## Current state
- [ ] Not started.
- Module list defined in `PLAN.md §1`: `iot_core`, `lambda_scorer`, `lambda_s3_batcher`, `dynamodb`, `s3_archive`, `glue_catalog`, `sns_alerts`.

## Interfaces (in / out)
- **In:** `infra/environments/dev/terraform.tfvars` (region, project tag, alert email).
- **Out:** AWS resources. Outputs: Lambda Function URL for the Grafana adapter, S3 bucket name, SNS topic ARN.

## Cost guardrails (CI-enforced)
- CI plan-check must fail if any of these resource types appear: `aws_instance`, `aws_db_instance`, `aws_kinesis_firehose_delivery_stream`, `aws_glue_crawler`, `aws_grafana_workspace`.
- Region pin: `eu-central-1`. CI rejects any provider/resource with a different region.

## Open questions
- mTLS provisioning flow for IoT Core Things — generate certs locally, upload via Terraform, or via separate `scripts/provision_certs.sh`?
- Whether to define the Glue Catalog table inline in `infra/main.tf` or as a sub-module. Default: sub-module for reuse.

## Related ADRs
- ADR 0001 (planned): IaC = Terraform
- ADR 0003 (planned): DynamoDB instead of Timestream
- ADR 0004 (planned): Lambda batching instead of Firehose
