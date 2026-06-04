# Scorer Lambda execution role. Nothing wildcarded:
#   - dynamodb:Query/GetItem/PutItem on the table ARN only — the
#     exact four hot-path operations (ADR 0010 §Access patterns;
#     Query x1, GetItem x1, PutItem x2 per invocation).
#   - sns:Publish on the topic ARN only (ADR 0012).
#   - logs:CreateLogStream/PutLogEvents on this function's log group
#     only. The group itself is Terraform-managed (modules/
#     lambda_scorer), so CreateLogGroup is deliberately NOT granted —
#     an accidental log-group recreation outside Terraform would
#     fail loudly instead of resurrecting after teardown.
#
# The log-group ARN is constructed from strings (region + account +
# function name) rather than referencing the log-group resource,
# breaking what would otherwise be an iam <-> lambda module cycle.

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "lambda_exec" {
  name = var.role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_exec" {
  name = "${var.role_name}-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBHotPath"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
        ]
        Resource = var.table_arn
      },
      {
        Sid      = "SnsAlertPublish"
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
