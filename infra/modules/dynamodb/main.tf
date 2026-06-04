# pump_hot_state — hot-path table per ADR 0010 (schema) + ADR 0013
# (billing mode).
#
# Two item shapes share the table: reading rows (sk = ISO-8601 ts)
# and one STATE row per pump (sk = "STATE"). Only the two key
# attributes are declared — DynamoDB is schemaless beyond the key;
# the item shapes are documented in context/_interfaces.md
# §DynamoDB schema and pinned by lambda_scorer/tests.
#
# Billing: PAY_PER_REQUEST (ADR 0013). The 1800-row PSI window query
# at fleet rate (~7.5 inv/s) sustains ~220-260 RCU — ~9-10x over the
# 25 RCU Always-Free provisioned ceiling, which would throttle
# continuously. On-demand costs ~$0.10-0.20 per 30-min demo; the
# table exists only between apply and teardown.
#
# No TTL: demo lifetime is bounded by aws_teardown; a retention sweep
# is the production-shaped alternative (ADR 0010 §Access patterns).
# No PITR, no GSIs.

resource "aws_dynamodb_table" "pump_hot_state" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pump_id"
  range_key    = "sk"

  attribute {
    name = "pump_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  deletion_protection_enabled = false # teardown must always succeed
}
