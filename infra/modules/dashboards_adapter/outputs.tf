output "function_url" {
  description = "Public Function URL — the Grafana Infinity datasource's base URL (ADR 0014)."
  value       = aws_lambda_function_url.adapter.function_url
}

output "function_name" {
  value = aws_lambda_function.adapter.function_name
}

output "function_arn" {
  value = aws_lambda_function.adapter.arn
}
