# Outputs promised by context/infra.md §Interfaces. The S3 bucket
# name lands with the cold-path session.

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
