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
