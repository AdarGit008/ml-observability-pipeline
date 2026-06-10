variable "function_name" {
  description = "Fleet-PSI Lambda name. The IAM role, log group, and EventBridge rule derive from it (<name>-exec, /aws/lambda/<name>, <name>-schedule) — aws_teardown.sh assumes this derivation."
  type        = string
}

variable "table_name" {
  description = "Hot-state table the fleet Lambda pools from (DDB_TABLE_NAME env)."
  type        = string
}

variable "table_arn" {
  description = "Table ARN the Query/GetItem/PutItem grant is scoped to."
  type        = string
}

variable "topic_arn" {
  description = "Alert topic ARN — the sns:Publish grant is scoped to it, and it is injected as SNS_TOPIC_ARN (cold-start required, ADR 0012). Reuses the scorer's topic (ADR 0018 §Decision 4)."
  type        = string
}

variable "aws_region" {
  description = "Region for the log-group ARN in the IAM policy."
  type        = string
}

variable "fleet_size" {
  description = "Pump count (P-01..P-NN) the fleet Lambda pools. Must match the simulator fleet. Injected as FLEET_SIZE; the handler validates 1..99."
  type        = number
}

variable "schedule_expression" {
  description = "EventBridge cadence (ADR 0018 §Decision 1 — the aggregated 5-minute fleet window of PLAN.md §2.7)."
  type        = string
  default     = "rate(5 minutes)"
}

variable "memory_mb" {
  description = "Fleet-PSI Lambda memory (MB). numpy-only cold start (drift-only zip, no sklearn) + PSI over ≤2250 pooled rows."
  type        = number
  default     = 256
}

variable "timeout_s" {
  description = "Fleet-PSI Lambda timeout (s). Covers the numpy cold start + 15 Queries + 1 GetItem + 1 PutItem + one pooled PSI."
  type        = number
  default     = 30
}

variable "dist_dir" {
  description = "Staged deploy tree (.build/fleet_psi_dist) produced by scripts/build_fleet_psi.{ps1,sh} — run it before terraform plan."
  type        = string
}

variable "zip_output_path" {
  description = "Where archive_file writes the deploy zip."
  type        = string
}

variable "code_bucket" {
  description = "Bucket the deploy zip is uploaded to (deploy/ prefix) — the archive bucket, same upload path as the scorer/batcher (DeepSeek review 2026-06-10 §2). Routing through S3 keeps all wheel-shipping Lambdas consistent and sidesteps the 50 MB direct-upload ceiling."
  type        = string
}
