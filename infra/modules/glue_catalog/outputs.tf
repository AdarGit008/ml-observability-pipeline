output "database_name" {
  value = aws_glue_catalog_database.archive.name
}

output "table_name" {
  value = aws_glue_catalog_table.readings.name
}
