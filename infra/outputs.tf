# Outputs promised by context/infra.md §Interfaces.

output "sns_topic_arn" {
  description = "Alert topic ARN — also injected into the Lambda env as SNS_TOPIC_ARN."
  value       = module.sns.topic_arn
}

output "ddb_table_name" {
  description = "Hot-state table name (pump_hot_state per ADR 0010)."
  value       = module.dynamodb.table_name
}

output "ddb_table_arn" {
  value = module.dynamodb.table_arn
}

output "lambda_function_name" {
  value = module.lambda_scorer.function_name
}

output "lambda_function_arn" {
  value = module.lambda_scorer.function_arn
}

output "iot_rule_arn" {
  value = module.iot_rule.rule_arn
}

output "adapter_function_url" {
  description = "Public fleet-snapshot endpoint (ADR 0014) — paste into the Grafana Infinity datasource as the base URL."
  value       = module.dashboards_adapter.function_url
}

output "adapter_function_name" {
  value = module.dashboards_adapter.function_name
}

output "s3_bucket_name" {
  description = "Cold-path archive bucket (ADR 0015) — also the batcher's S3_BUCKET env."
  value       = module.s3_archive.bucket_name
}

output "glue_database_name" {
  value = module.glue_catalog.database_name
}

output "glue_table_name" {
  description = "Athena-queryable readings table (partition projection — no Crawler, ever)."
  value       = module.glue_catalog.table_name
}

output "batcher_function_name" {
  value = module.lambda_s3_batcher.function_name
}

output "batcher_schedule_rule_name" {
  value = module.lambda_s3_batcher.schedule_rule_name
}

output "iot_endpoint" {
  description = "IoT Core ATS data endpoint (ADR 0016) — paste into simulator config broker.url for target: aws-iot."
  value       = module.iot_fleet.iot_endpoint
}

output "iot_thing_names" {
  value = module.iot_fleet.thing_names
}

output "iot_policy_name" {
  description = "Shared fleet policy name — aws_teardown.sh sweeps by it."
  value       = module.iot_fleet.policy_name
}

output "fleet_psi_function_name" {
  value = module.fleet_psi.function_name
}

output "fleet_psi_schedule_rule_name" {
  description = "EventBridge rule name for the fleet-PSI Lambda — aws_teardown.sh checks its absence."
  value       = module.fleet_psi.schedule_rule_name
}
