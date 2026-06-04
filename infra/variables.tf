variable "aws_region" {
  description = "Deployment region. Locked to eu-central-1 by hard constraint #5 (context/_global.md) — cross-region transfer adds cost."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "Region is locked to eu-central-1 (context/_global.md hard constraint #5). Changing it requires an ADR."
  }
}

variable "project_tag" {
  description = "Project tag applied to every resource via provider default_tags. Also prefixes resource names."
  type        = string
  default     = "ml-obs-pipeline"
}

variable "alert_email" {
  description = "Email endpoint for the SNS alert subscription (PO inbox). AWS sends a confirmation email on first apply — alerts flow only after it is clicked."
  type        = string
}

variable "ddb_table_name" {
  description = "DynamoDB hot-state table name. Matches the lambda_scorer handler default (context/lambda_scorer.md §Environment variables)."
  type        = string
  default     = "pump_hot_state"
}

variable "lambda_function_name" {
  description = "Name of the scorer Lambda function."
  type        = string
  default     = "pump-scorer"
}

variable "lambda_memory_mb" {
  description = "Scorer Lambda memory (MB). 512 per context/lambda_scorer.md §Resource sizing."
  type        = number
  default     = 512
}

variable "lambda_timeout_s" {
  description = "Scorer Lambda timeout (s). Warm path is single-digit ms; 10 s covers the cold-start eager-load (reference + model unpickle) with margin."
  type        = number
  default     = 10
}

variable "adapter_function_name" {
  description = "Name of the Grafana fleet-snapshot adapter Lambda (ADR 0014). aws_teardown.sh derives the role + log-group names from it."
  type        = string
  default     = "pump-dashboard-adapter"
}

variable "fleet_size" {
  description = "Pump count the adapter snapshots and the batcher tracks (P-01..P-NN). Must match the simulator fleet size — drift here means silently short snapshots and stale watermarks."
  type        = number
  default     = 15
}

variable "batcher_function_name" {
  description = "Name of the cold-path batcher Lambda (ADR 0015). aws_teardown.sh derives the role, log-group, and EventBridge-rule names from it."
  type        = string
  default     = "pump-s3-batcher"
}

variable "batcher_schedule_expression" {
  description = "Batcher cadence (ADR 0015 §Decision 3 — 60 s, HANDOFF §6 Q6). One Parquet file per tick."
  type        = string
  default     = "rate(1 minute)"
}

variable "batcher_safety_lag_seconds" {
  description = "Batch-cutoff lag behind the wall clock (ADR 0015 §Decision 1) — covers the scorer write pipeline's worst case."
  type        = number
  default     = 5
}

variable "glue_database_name" {
  description = "Glue Catalog database for the Parquet archive (ADR 0015 §Decision 4). Lowercase + underscores per Glue naming."
  type        = string
  default     = "pump_archive"
}
