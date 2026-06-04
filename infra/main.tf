# Root module — AWS hot path (IaC session #1, 2026-06-04).
#
# Scope: DynamoDB hot-state table (ADR 0010/0013), SNS alert topic
# (ADR 0012), scorer Lambda + packaging, IoT Rule trigger, scoped IAM.
# Out of scope (later sessions): s3_archive, glue_catalog,
# lambda_s3_batcher, IoT Thing/cert provisioning (simulator-side).
#
# Run order (PO-side):
#   1. .\scripts\build_lambda.ps1        # stages .build/lambda_dist/
#   2. cd infra && terraform init
#   3. terraform validate
#   4. terraform plan                    # review resource list — no apply in-session
#
# The archive_file data source in modules/lambda_scorer reads
# .build/lambda_dist/ at plan time, so the build script MUST run
# before terraform plan (validate works without it).

module "dynamodb" {
  source     = "./modules/dynamodb"
  table_name = var.ddb_table_name
}

module "sns" {
  source      = "./modules/sns"
  topic_name  = "${var.project_tag}-pump-alerts"
  alert_email = var.alert_email
}

module "iam" {
  source        = "./modules/iam"
  role_name     = "${var.lambda_function_name}-exec"
  function_name = var.lambda_function_name
  aws_region    = var.aws_region
  table_arn     = module.dynamodb.table_arn
  topic_arn     = module.sns.topic_arn
}

module "lambda_scorer" {
  source          = "./modules/lambda_scorer"
  function_name   = var.lambda_function_name
  role_arn        = module.iam.role_arn
  table_name      = var.ddb_table_name
  topic_arn       = module.sns.topic_arn
  memory_mb       = var.lambda_memory_mb
  timeout_s       = var.lambda_timeout_s
  dist_dir        = abspath("${path.root}/../.build/lambda_dist")
  zip_output_path = abspath("${path.root}/../.build/lambda_scorer.zip")
}

module "iot_rule" {
  source        = "./modules/iot_rule"
  rule_name     = "pump_telemetry_to_scorer"
  lambda_arn    = module.lambda_scorer.function_arn
  function_name = module.lambda_scorer.function_name
}
