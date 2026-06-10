variable "function_name" {
  description = "Adapter Lambda name. The IAM role and log group derive from it (<name>-exec, /aws/lambda/<name>) — aws_teardown.sh assumes this derivation."
  type        = string
}

variable "table_name" {
  description = "Hot-state table the adapter reads (DDB_TABLE_NAME env)."
  type        = string
}

variable "table_arn" {
  description = "Table ARN the BatchGetItem grant is scoped to."
  type        = string
}

variable "aws_region" {
  description = "Region for the log-group ARN in the IAM policy."
  type        = string
}

variable "fleet_size" {
  description = "Pump count -> P-01..P-NN BatchGetItem key set (ADR 0014 §Decision 3). Must match the simulator fleet size; 1..99 enforced by the handler at cold start."
  type        = number
  default     = 15

  validation {
    condition     = var.fleet_size >= 1 && var.fleet_size <= 99
    error_message = "fleet_size must be 1..99 — the P-NN pump-id format is two-digit zero-padded (ADR 0014)."
  }
}

variable "memory_mb" {
  description = "Adapter Lambda memory. 128 MB — one BatchGetItem + JSON reshape."
  type        = number
  default     = 128
}

variable "timeout_s" {
  description = "Adapter Lambda timeout. 5 s covers BatchGetItem retries with margin."
  type        = number
  default     = 5
}

variable "reserved_concurrency" {
  description = "Reserved concurrent executions — caps worst-case spend + table read pressure from the public URL (2026-06-04 review Q1). One Grafana instance needs ~1. Set to -1 (no reservation) 2026-06-07: the account's Lambda concurrency quota sits at the new-account floor, so ANY reservation violates the min-10-unreserved rule. Restore to 5 after a Service Quotas increase."
  type        = number
  default     = -1
}

variable "dist_dir" {
  description = "Staged deploy tree (scripts/build_adapter stages dashboards_adapter/ here)."
  type        = string
}

variable "zip_output_path" {
  description = "Where archive_file writes the deploy zip."
  type        = string
}
