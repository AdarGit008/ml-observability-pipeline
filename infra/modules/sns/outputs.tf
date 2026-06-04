output "topic_arn" {
  description = "Wired into the Lambda env as SNS_TOPIC_ARN (required at cold-start, ADR 0012)."
  value       = aws_sns_topic.pump_alerts.arn
}
