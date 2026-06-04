variable "rule_name" {
  description = "IoT topic rule name — alphanumeric + underscore only (AWS constraint). The error-republish role name derives from it."
  type        = string
}

variable "lambda_arn" {
  type = string
}

variable "function_name" {
  type = string
}

variable "aws_region" {
  description = "Region for the error-topic ARN in the republish role policy."
  type        = string
}

variable "error_topic" {
  description = "MQTT topic that receives messages whose scorer invoke failed past IoT's retries (rule error_action)."
  type        = string
  default     = "factory/errors"
}
