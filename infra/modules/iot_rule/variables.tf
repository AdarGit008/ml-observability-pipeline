variable "rule_name" {
  description = "IoT topic rule name — alphanumeric + underscore only (AWS constraint)."
  type        = string
}

variable "lambda_arn" {
  type = string
}

variable "function_name" {
  type = string
}
