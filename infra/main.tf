# Root module — AWS hot path (IaC session #1, 2026-06-04) + Grafana
# adapter (dashboards session, 2026-06-04) + cold path (this session,
# 2026-06-04: ADR 0015).
#
# Scope: DynamoDB hot-state table (ADR 0010/0013), SNS alert topic
# (ADR 0012), scorer Lambda + packaging, IoT Rule trigger (+ republish
# error_action), scoped IAM, fleet-snapshot adapter Lambda + Function
# URL (ADR 0014), S3 archive bucket + Glue Catalog table + batcher
# Lambda on an EventBridge schedule (ADR 0015).
# IoT Thing/cert provisioning: modules/iot_fleet (simulator identity,
# ADR 0016, 2026-06-07). Out of scope (later sessions): CI.
#
# Run order (PO-side):
#   1. .\scripts\build_lambda.ps1        # stages .build/lambda_dist/
#   2. .\scripts\build_adapter.ps1       # stages .build/adapter_dist/
#   3. .\scripts\build_batcher.ps1       # stages .build/batcher_dist/
#   4. cd infra && terraform init
#   5. terraform validate
#   6. terraform plan                    # review resource list — no apply in-session
#
# The archive_file data sources read the staged .build/ trees at plan
# time, so ALL THREE build scripts MUST run before terraform plan
# (validate works without them).

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
  code_bucket     = module.s3_archive.bucket_name # 62 MB zip > 50 MB direct-upload limit (2026-06-04)
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
  aws_region    = var.aws_region
}

module "dashboards_adapter" {
  source          = "./modules/dashboards_adapter"
  function_name   = var.adapter_function_name
  table_name      = var.ddb_table_name
  table_arn       = module.dynamodb.table_arn
  aws_region      = var.aws_region
  fleet_size      = var.fleet_size
  dist_dir        = abspath("${path.root}/../.build/adapter_dist")
  zip_output_path = abspath("${path.root}/../.build/dashboards_adapter.zip")
}

# --- Cold path (ADR 0015) ---

module "s3_archive" {
  source      = "./modules/s3_archive"
  name_prefix = var.project_tag
}

module "glue_catalog" {
  source        = "./modules/glue_catalog"
  database_name = var.glue_database_name
  bucket_name   = module.s3_archive.bucket_name
}

module "lambda_s3_batcher" {
  source              = "./modules/lambda_s3_batcher"
  function_name       = var.batcher_function_name
  table_name          = var.ddb_table_name
  table_arn           = module.dynamodb.table_arn
  bucket_name         = module.s3_archive.bucket_name
  bucket_arn          = module.s3_archive.bucket_arn
  aws_region          = var.aws_region
  fleet_size          = var.fleet_size
  safety_lag_seconds  = var.batcher_safety_lag_seconds
  schedule_expression = var.batcher_schedule_expression
  dist_dir            = abspath("${path.root}/../.build/batcher_dist")
  zip_output_path     = abspath("${path.root}/../.build/lambda_s3_batcher.zip")
}

# --- IoT fleet identity (ADR 0016) ---

module "iot_fleet" {
  source      = "./modules/iot_fleet"
  fleet_size  = var.fleet_size
  policy_name = var.iot_policy_name
  cert_dir    = abspath("${path.root}/../simulator/.secrets")
  aws_region  = var.aws_region
}
