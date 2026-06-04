variable "database_name" {
  description = "Glue database name. Glue names: lowercase letters, numbers, underscore."
  type        = string
}

variable "table_name" {
  description = "Glue table name for the readings archive."
  type        = string
  default     = "pump_readings"
}

variable "bucket_name" {
  description = "Archive bucket the table location + projection template point at."
  type        = string
}
