variable "role_name" {
  type = string
}

variable "function_name" {
  description = "Lambda function name — used to construct the log-group ARN (string, not resource ref, to avoid an iam<->lambda module cycle)."
  type        = string
}

variable "aws_region" {
  type = string
}

variable "table_arn" {
  type = string
}

variable "topic_arn" {
  type = string
}
