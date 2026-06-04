# IoT Rule: telemetry topic -> scorer Lambda.
#
# SELECT * forwards the raw telemetry JSON unmodified, which is
# exactly the shape _parse_event expects (context/lambda_scorer.md
# §Interfaces — "the handler treats event as the raw telemetry
# dict"). No envelope, no SQL functions; if a future rule wraps the
# payload, _parse_event is the single place to update.
#
# Device identity (Things/certs for the simulator publisher) is
# deliberately NOT here — it belongs to the simulator side and is
# tracked as an open question in context/infra.md.

resource "aws_iot_topic_rule" "telemetry_to_scorer" {
  name        = var.rule_name # IoT rule names: [a-zA-Z0-9_]+ only
  enabled     = true
  sql         = "SELECT * FROM 'factory/pumps/+/telemetry'"
  sql_version = "2016-03-23"

  lambda {
    function_arn = var.lambda_arn
  }
}

resource "aws_lambda_permission" "allow_iot" {
  statement_id  = "AllowIoTRuleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.function_name
  principal     = "iot.amazonaws.com"
  source_arn    = aws_iot_topic_rule.telemetry_to_scorer.arn
}
