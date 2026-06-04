# Scorer Lambda + packaging (ADR 0005 Addendum Q1 — the committed
# answer to the multi-root packaging point).
#
# Terraform does NOT assemble the zip contents. scripts/build_lambda
# stages shared/ + lambda_scorer/ + model/artifacts/ + pip deps into
# .build/lambda_dist/; the archive_file data source below only zips
# that staged tree. Run the build script before terraform plan.
#
# boto3 is intentionally NOT bundled — the Lambda Python runtime
# provides it, and bundling botocore would push the zip past the
# 50 MB direct-upload limit (footprint discussion: session log
# 2026-06-04 + ADR 0006 §Q4 baseline ~124 MB unzipped).
#
# AWS_REGION is a reserved Lambda env var set by the runtime
# (eu-central-1 here) — Terraform must not (and cannot) set it; the
# handler's eu-central-1 default is a local-test affordance.

terraform {
  required_providers {
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

data "archive_file" "dist" {
  type        = "zip"
  source_dir  = var.dist_dir
  output_path = var.zip_output_path
}

# Terraform-managed log group: bounded retention, and teardown
# removes it (an auto-created group would survive destroy and linger
# as un-tagged residue).
resource "aws_cloudwatch_log_group" "scorer" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 7
}

resource "aws_lambda_function" "scorer" {
  function_name = var.function_name
  role          = var.role_arn
  runtime       = "python3.12"
  handler       = "lambda_scorer.handler.handler"
  architectures = ["x86_64"] # build script pulls manylinux2014_x86_64 wheels

  memory_size = var.memory_mb
  timeout     = var.timeout_s

  filename         = data.archive_file.dist.output_path
  source_code_hash = data.archive_file.dist.output_base64sha256

  environment {
    variables = {
      DDB_TABLE_NAME = var.table_name
      SNS_TOPIC_ARN  = var.topic_arn # required at cold-start — KeyError fail-fast (ADR 0012)
    }
  }

  # Group exists before first invocation so the only-this-log-group
  # IAM scoping (modules/iam) is sufficient without CreateLogGroup.
  depends_on = [aws_cloudwatch_log_group.scorer]
}
