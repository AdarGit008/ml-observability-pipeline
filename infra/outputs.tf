# Outputs promised by context/infra.md §Interfaces. The Grafana
# adapter Function URL + S3 bucket name land in later sessions.

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
