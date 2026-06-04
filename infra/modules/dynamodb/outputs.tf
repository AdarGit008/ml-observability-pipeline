output "table_name" {
  value = aws_dynamodb_table.pump_hot_state.name
}

output "table_arn" {
  value = aws_dynamodb_table.pump_hot_state.arn
}
