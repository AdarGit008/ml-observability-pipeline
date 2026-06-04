# IoT Rule: telemetry topic -> scorer Lambda.
#
# SELECT * forwards the raw telemetry JSON unmodified, which is
# exactly the shape _parse_event expects (context/lambda_scorer.md
# §Interfaces — "the handler treats event as the raw telemetry
# dict"). No envelope, no SQL functions; if a future rule wraps the
# payload, _parse_event is the single place to update.
#
# error_action (cold-path session, 2026-06-04 — closes the
# 2026-06-04 dashboards-cascade finding after two deferrals): when
# the rule's Lambda invoke fails past IoT's retries, the message
# republishes to var.error_topic instead of dropping silently. A
# demo-day subscriber (`mosquitto_sub`-equivalent in the IoT console)
# on that topic turns "pumps went quiet" from a mystery into a
# message. The republish role is scoped to that ONE topic.
#
# Device identity (Things/certs for the simulator publisher) is
# deliberately NOT here — it belongs to the simulator side and is
# tracked as an open question in context/infra.md.

data "aws_caller_identity" "current" {}

resource "aws_iot_topic_rule" "telemetry_to_scorer" {
  name        = var.rule_name # IoT rule names: [a-zA-Z0-9_]+ only
  enabled     = true
  sql         = "SELECT * FROM 'factory/pumps/+/telemetry'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = var.lambda_arn
  }

  error_action {
    republish {
      role_arn = aws_iam_role.error_republish.arn
      topic    = var.error_topic
      qos      = 0
    }
  }
}

resource "aws_lambda_permission" "allow_iot" {
  statement_id  = "AllowIoTRuleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.function_name
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.telemetry_to_scorer.arn
}

# Republish role — iot:Publish on the error topic ONLY.
resource "aws_iam_role" "error_republish" {
  name = "${var.rule_name}_error_republish"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "error_republish" {
  name = "${var.rule_name}_error_republish_policy"
  role = aws_iam_role.error_republish.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "RepublishToErrorTopicOnly"
      Effect   = "Allow"
      Action   = ["iot:Publish"]
      Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/${var.error_topic}"
    }]
  })
}
