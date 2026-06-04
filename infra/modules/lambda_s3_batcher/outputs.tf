output "function_name" {
  value = aws_lambda_function.batcher.function_name
}

output "function_arn" {
  value = aws_lambda_function.batcher.arn
}

output "schedule_rule_name" {
  description = "EventBridge rule name — aws_teardown.sh checks its absence."
  value       = aws_cloudwatch_event_rule.schedule.name
}
