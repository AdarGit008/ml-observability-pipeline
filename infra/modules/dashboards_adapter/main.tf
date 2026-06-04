# Grafana fleet-snapshot adapter (ADR 0014): read-only Lambda behind
# a public Function URL. Self-contained module — its own IAM role,
# log group, packaging — because the adapter shares nothing with the
# scorer except the table it reads.
#
# Packaging: scripts/build_adapter.{ps1,sh} stages dashboards_adapter/
# (tests stripped) into .build/adapter_dist/; archive_file only zips
# it. No third-party deps — boto3 is runtime-provided (same posture
# as the scorer: infra session #1) — so the zip is a few KB.
#
# AuthType NONE is a recorded PO call (ADR 0014 §Alternatives 3):
# read-only synthetic data, URL dies at every teardown, budget alerts
# as backstop. The IAM-auth upgrade is config-only: flip
# authorization_type to AWS_IAM, drop the public permission, add a
# SigV4 credential to the Grafana Infinity datasource.

terraform {
  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

data "aws_caller_identity" "current" {}

data "archive_file" "dist" {
  type        = "zip"
  source_dir  = var.dist_dir
  output_path = var.zip_output_path
}

# Terraform-managed log group (same rationale as the scorer module:
# bounded retention; teardown removes it; no CreateLogGroup grant).
resource "aws_cloudwatch_log_group" "adapter" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 7
}

# Execution role — nothing wildcarded:
#   - dynamodb:BatchGetItem on the table ARN only. The adapter's ONE
#     access pattern (ADR 0010 "Dashboards: fleet latest"). No Query,
#     no GetItem, no Scan — the policy is the cheapest tripwire
#     against the adapter growing read patterns ADR 0014 forbids.
#   - logs scoped to this function's log group only.
resource "aws_iam_role" "adapter_exec" {
  name = "${var.function_name}-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "adapter_exec" {
  name = "${var.function_name}-exec-policy"
  role = aws_iam_role.adapter_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBFleetSnapshotReadOnly"
        Effect   = "Allow"
        Action   = ["dynamodb:BatchGetItem"]
        Resource = var.table_arn
      },
      {
        Sid    = "CloudWatchLogsThisFunctionOnly"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.function_name}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "adapter" {
  function_name = var.function_name
  role          = aws_iam_role.adapter_exec.arn
  runtime       = "python3.12"
  handler       = "dashboards_adapter.handler.handler"
  architectures = ["x86_64"]

  # 128 MB / 5 s: the handler is one BatchGetItem (~6 KB) + a JSON
  # reshape — nothing like the scorer's model-unpickle cold start.
  memory_size = var.memory_mb
  timeout     = var.timeout_s

  # Cap on the public URL's blast radius (2026-06-04 review Q1, groq):
  # a request flood against the AuthType=NONE URL can at worst run
  # var.reserved_concurrency containers — bounding both worst-case
  # Lambda spend and DynamoDB read pressure. One Grafana instance
  # refreshing panels needs ~1; 5 leaves headroom without donating
  # meaningful account-pool concurrency.
  reserved_concurrent_executions = var.reserved_concurrency

  filename         = data.archive_file.dist.output_path
  source_code_hash = data.archive_file.dist.output_base64sha256

  environment {
    variables = {
      DDB_TABLE_NAME = var.table_name
      FLEET_SIZE     = tostring(var.fleet_size)
    }
  }

  depends_on = [aws_cloudwatch_log_group.adapter]
}

resource "aws_lambda_function_url" "adapter" {
  function_name      = aws_lambda_function.adapter.function_name
  authorization_type = "NONE" # ADR 0014 §Alternatives 3 — PO call 2026-06-04
}

# AuthType NONE still requires an explicit public invoke permission —
# without it the URL returns 403 to anonymous callers.
resource "aws_lambda_permission" "public_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.adapter.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
