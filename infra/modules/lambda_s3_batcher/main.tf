# Cold-path batcher Lambda + EventBridge schedule (ADR 0015).
# Self-contained module — own IAM role, log group, packaging — same
# pattern as modules/dashboards_adapter.
#
# Packaging: scripts/build_batcher.{ps1,sh} stages lambda_s3_batcher/
# (tests stripped) + pyarrow (manylinux2014_x86_64) into
# .build/batcher_dist/ and enforces the ADR 0006 §Q4 footprint check;
# archive_file only zips it. boto3 is runtime-provided, never bundled
# (infra session #1 lock).
#
# IAM is the no-compute tripwire (ADR 0015 Principle, same trick as
# the adapter): Query + BatchGetItem + PutItem on the table, PutObject
# on the bucket's year=* prefix, logs on its own group. Nothing
# wildcarded; no Glue actions at all (partition projection needs
# none).

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

# Code upload VIA S3, same as the scorer (2026-06-04 measurement: the
# batcher zip is 47.6 MB — under the 50 MB direct-upload limit but
# with ~2 MB headroom; one upload mechanism for both Lambdas beats a
# limit surprise on the next pyarrow bump). deploy/ sits outside the
# Glue year=* projection paths; force_destroy sweeps it.
resource "aws_s3_object" "code" {
  bucket = var.bucket_name
  key    = "deploy/${var.function_name}.zip"
  source = data.archive_file.dist.output_path
  etag   = data.archive_file.dist.output_md5
}

resource "aws_cloudwatch_log_group" "batcher" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 7
}

resource "aws_iam_role" "batcher_exec" {
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

resource "aws_iam_role_policy" "batcher_exec" {
  name = "${var.function_name}-exec-policy"
  role = aws_iam_role.batcher_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Query  = the per-pump reading-window read (ADR 0015 §1).
        # BatchGetItem = the fleet watermark read.
        # PutItem      = the watermark advance.
        # No Scan, no UpdateItem, no DeleteItem — the policy is the
        # tripwire against the batcher growing access patterns.
        # KNOWN BREADTH (2026-06-04 cascade Q5): PutItem necessarily
        # covers every SK — IAM has dynamodb:LeadingKeys (partition
        # key) but no sort-key condition, so "WATERMARK rows only"
        # cannot be expressed in policy. A separate watermark table
        # was rejected for the same reasons as ADR 0010
        # §Alternatives 2B (doubles the IaC surface for no
        # operational gain); the discipline is held by code review +
        # the handler's single PutItem call site + its tests.
        Sid    = "DynamoDBWatermarkBatching"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:BatchGetItem",
          "dynamodb:PutItem",
        ]
        Resource = var.table_arn
      },
      {
        # Keys all start with "year=" (_interfaces.md §S3 archive
        # layout) — the grant is scoped to that prefix, not the bucket.
        Sid      = "S3ArchivePutOnly"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${var.bucket_arn}/year=*"
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

resource "aws_lambda_function" "batcher" {
  function_name = var.function_name
  role          = aws_iam_role.batcher_exec.arn
  runtime       = "python3.12"
  handler       = "lambda_s3_batcher.handler.handler"
  architectures = ["x86_64"]

  # 256 MB / 30 s: the pyarrow import dominates the cold start; the
  # warm path is 1 BatchGetItem + 15 Queries + 1 PutObject over
  # ~450 rows — seconds of headroom, not a tight budget.
  memory_size = var.memory_mb
  timeout     = var.timeout_s

  # EXACTLY ONE batcher at a time. Overlapping runs would race the
  # watermark read-advance cycle and double-archive the window —
  # at-least-once is the contract (ADR 0015), but concurrency-1 keeps
  # the duplicate path to genuine failures, not scheduling jitter.
  reserved_concurrent_executions = 1

  s3_bucket        = aws_s3_object.code.bucket
  s3_key           = aws_s3_object.code.key
  source_code_hash = data.archive_file.dist.output_base64sha256

  environment {
    variables = {
      DDB_TABLE_NAME     = var.table_name
      S3_BUCKET          = var.bucket_name
      FLEET_SIZE         = tostring(var.fleet_size)
      SAFETY_LAG_SECONDS = tostring(var.safety_lag_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.batcher]
}

# 60 s cadence (ADR 0015 §Decision 3; HANDOFF §6 Q6). Rule evaluation
# + scheduled invocations are free at this scale (verified 2026-06-04).
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.function_name}-schedule"
  description         = "Cold-path drain cadence (ADR 0015): one Parquet file per tick."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "batcher" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.batcher.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.batcher.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
