variable "function_name" {
  description = "Batcher Lambda name. The IAM role, log group, and EventBridge rule derive from it (<name>-exec, /aws/lambda/<name>, <name>-schedule) — aws_teardown.sh assumes this derivation."
  type        = string
}

variable "table_name" {
  description = "Hot-state table the batcher drains (DDB_TABLE_NAME env)."
  type        = string
}

variable "table_arn" {
  description = "Table ARN the Query/BatchGetItem/PutItem grant is scoped to."
  type        = string
}

variable "bucket_name" {
  description = "Archive bucket (S3_BUCKET env)."
  type        = string
}

variable "bucket_arn" {
  description = "Bucket ARN the PutObject grant's year=* prefix is scoped under."
  type        = string
}

variable "aws_region" {
  description = "Region for the log-group ARN in the IAM policy."
  type        = string
}

variable "fleet_size" {
  description = "Pump count (P-01..P-NN) whose watermarks the batcher tracks. Must match the simulator fleet."
  type        = number
}

variable "safety_lag_seconds" {
  description = "Late-arrival guard: the batch cutoff trails the wall clock by this much (ADR 0015 §Decision 1)."
  type        = number
  default     = 5
}

variable "schedule_expression" {
  description = "EventBridge cadence (ADR 0015 §Decision 3 — 60 s default, HANDOFF §6 Q6)."
  type        = string
  default     = "rate(1 minute)"
}

variable "memory_mb" {
  description = "Batcher Lambda memory (MB). pyarrow import is the sizing driver."
  type        = number
  default     = 256
}

variable "timeout_s" {
  description = "Batcher Lambda timeout (s). Covers the pyarrow cold start + 15 Queries + 1 PutObject."
  type        = number
  default     = 30
}

variable "dist_dir" {
  description = "Staged deploy tree (.build/batcher_dist) produced by scripts/build_batcher.{ps1,sh} — run it before terraform plan."
  type        = string
}

variable "zip_output_path" {
  description = "Where archive_file writes the deploy zip."
  type        = string
}
