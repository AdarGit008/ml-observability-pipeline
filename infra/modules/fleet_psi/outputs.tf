output "function_name" {
  value = aws_lambda_function.fleet_psi.function_name
}

output "function_arn" {
  value = aws_lambda_function.fleet_psi.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.fleet_psi.name
}

output "schedule_rule_name" {
  description = "EventBridge rule name — aws_teardown.sh checks its absence."
  value       = aws_cloudwatch_event_rule.schedule.name
}
