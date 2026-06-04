variable "function_name" {
  type = string
}

variable "role_arn" {
  type = string
}

variable "table_name" {
  description = "Injected as DDB_TABLE_NAME."
  type        = string
}

variable "topic_arn" {
  description = "Injected as SNS_TOPIC_ARN (cold-start required, ADR 0012)."
  type        = string
}

variable "memory_mb" {
  type    = number
  default = 512
}

variable "timeout_s" {
  type    = number
  default = 10
}

variable "dist_dir" {
  description = "Staged deploy tree produced by scripts/build_lambda — must exist before terraform plan."
  type        = string
}

variable "zip_output_path" {
  description = "Where archive_file writes the deploy zip."
  type        = string
}

variable "code_bucket" {
  description = "Bucket the deploy zip is uploaded to (deploy/ prefix) — the 62 MB zip exceeds the 50 MB direct-upload limit (2026-06-04 measurement; ADR 0006 §Q4 fallback)."
  type        = string
}
