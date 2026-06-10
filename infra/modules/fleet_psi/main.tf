# Fleet-PSI Lambda + EventBridge schedule (ADR 0018).
# Self-contained module — own IAM role, log group, packaging — same
# pattern as modules/lambda_s3_batcher (the EventBridge-Lambda sibling)
# and modules/dashboards_adapter.
#
# Packaging: scripts/build_fleet_psi.{ps1,sh} stages lambda_fleet_psi/
# (tests stripped) + shared/{features,drift}.py + the operational
# reference JSON + numpy (manylinux x86_64) into .build/fleet_psi_dist/
# and enforces the ADR 0006 §Q4 footprint check; archive_file only zips
# it. This is the DRIFT-ONLY layer (ADR 0018 §Decision 6): NO model.pkl,
# NO sklearn — load_reference skips the version check when model.pkl is
# absent (ADR 0007 §4). boto3 is runtime-provided, never bundled
# (infra session #1 lock).
#
# Code upload goes VIA S3 (deploy/ prefix on the archive bucket), the
# same mechanism as the scorer and batcher — the three wheel-shipping
# Lambdas share one upload path (the pure-python adapter is the lone
# direct-filename case). The drift-only zip is small today, but routing
# it through S3 keeps the codebase consistent (north star #5), sidesteps
# the 50 MB direct-upload ceiling on any future numpy bump, and would
# already be required if sklearn were ever added (DeepSeek review
# 2026-06-10 §2). deploy/ sits outside the Glue year=* projection paths;
# the bucket's force_destroy sweeps it at teardown. source_code_hash
# still drives change detection.
#
# IAM is the no-extra-access tripwire (ADR 0018 §Follow-ups, same trick
# as the batcher/adapter): Query + GetItem + PutItem on the table ARN,
# sns:Publish on the topic ARN, logs on its own group. Nothing
# wildcarded; no Scan, no model/score actions (there is no score path).

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

# Code upload VIA S3 (deploy/ prefix), same as the scorer/batcher
# (DeepSeek review 2026-06-10 §2). deploy/ is outside the Glue year=*
# projection paths; force_destroy sweeps it at teardown.
resource "aws_s3_object" "code" {
  bucket      = var.code_bucket
  key         = "deploy/${var.function_name}.zip"
  source      = data.archive_file.dist.output_path
  source_hash = data.archive_file.dist.output_md5 # multipart-safe (not etag): avoids phantom diff on >5MB zips (2026-06-07 live-apply lesson)
}

# Terraform-managed log group: bounded retention, and teardown removes
# it (an auto-created group would survive destroy and linger as
# un-tagged residue). The only-this-log-group IAM scoping below is
# sufficient without logs:CreateLogGroup because this exists first.
resource "aws_cloudwatch_log_group" "fleet_psi" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 7
}

resource "aws_iam_role" "fleet_psi_exec" {
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

resource "aws_iam_role_policy" "fleet_psi_exec" {
  name = "${var.function_name}-exec-policy"
  role = aws_iam_role.fleet_psi_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Query   = the per-pump trailing-window read, P-01..P-NN (the
        #           scorer's access pattern, ADR 0010 §Access patterns).
        # GetItem = the previous FLEET STATE row (edge-trigger input,
        #           ADR 0012).
        # PutItem = the FLEET STATE row overwrite.
        # No Scan, no BatchGetItem, no UpdateItem, no DeleteItem — the
        # policy is the tripwire against the fleet Lambda growing access
        # patterns. ADR 0018 §Follow-ups abbreviated this as "Query" (amended in the
        # 2026-06-10 Addendum (infra) to list all three);
        # the handler's edge-trigger read + STATE write necessarily add
        # GetItem + PutItem (lambda_fleet_psi/handler.py: TABLE.query /
        # TABLE.get_item / TABLE.put_item).
        # KNOWN BREADTH (mirrors the batcher, 2026-06-04 cascade Q5):
        # GetItem/PutItem cover every SK — IAM has dynamodb:LeadingKeys
        # (partition key) but no sort-key condition, so "FLEET STATE row
        # only" cannot be expressed in policy. The discipline is held by
        # the handler's single get_item/put_item call sites + their
        # tests.
        Sid    = "DynamoDBFleetPsiReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
        ]
        Resource = var.table_arn
      },
      {
        # Reuses the scorer's alert topic (ADR 0018 §Decision 4);
        # pump_id="FLEET" / scope:"fleet" marks the scope on the shared
        # topic. Publish only — no Subscribe, no topic management.
        Sid      = "SNSPublishAlertsOnly"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.topic_arn
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

resource "aws_lambda_function" "fleet_psi" {
  function_name = var.function_name
  role          = aws_iam_role.fleet_psi_exec.arn
  runtime       = "python3.12"
  handler       = "lambda_fleet_psi.handler.handler"
  architectures = ["x86_64"] # build script pulls manylinux x86_64 numpy

  # 256 MB / 30 s: numpy-only cold start (no sklearn/model.pkl — the
  # drift-only zip), then 15 Queries + 1 GetItem + 1 PutItem + one PSI
  # over ≤2250 pooled rows. Lighter than the scorer (sklearn, 512 MB)
  # and the batcher (pyarrow); seconds of headroom, not a tight budget.
  memory_size = var.memory_mb
  timeout     = var.timeout_s

  # EXACTLY ONE fleet run at a time would be the intent, but account
  # concurrency quota is at the new-account floor; any reservation
  # violates min-10-unreserved (2026-06-07 batcher lesson). -1 = no
  # reservation. Overlap is benign here: the FLEET STATE row is
  # idempotent-overwrite and the edge-trigger GetItem→PutItem is the
  # same at-most-once-per-edge posture as the scorer/batcher. Restore
  # to 1 after a quota bump.
  reserved_concurrent_executions = -1

  s3_bucket        = aws_s3_object.code.bucket
  s3_key           = aws_s3_object.code.key
  source_code_hash = data.archive_file.dist.output_base64sha256

  environment {
    variables = {
      DDB_TABLE_NAME = var.table_name
      SNS_TOPIC_ARN  = var.topic_arn # required at cold-start — KeyError fail-fast (ADR 0012)
      FLEET_SIZE     = tostring(var.fleet_size)
    }
  }

  depends_on = [aws_cloudwatch_log_group.fleet_psi]
}

# 5-minute cadence (ADR 0018 §Decision 1 — the aggregated 5-minute
# fleet window of PLAN.md §2.7). Rule evaluation + scheduled
# invocations are free at this scale (verified 2026-06-04, batcher).
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.function_name}-schedule"
  description         = "Fleet-PSI cadence (ADR 0018): pool the trailing 5-minute fleet window, one plant-wide PSI per tick."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "fleet_psi" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.fleet_psi.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fleet_psi.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
