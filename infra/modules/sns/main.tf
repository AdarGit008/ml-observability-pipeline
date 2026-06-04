# Alert topic per ADR 0012. Standard topic (email protocol; FIFO has
# no email support). Publishes are edge-triggered handler-side, so
# volume is bounded by incident count — well inside the 1,000
# Always-Free email deliveries/month.
#
# NOTE: the email subscription lands in "pending confirmation" until
# the PO clicks the link AWS emails at apply time. Terraform cannot
# confirm it and cannot destroy an unconfirmed subscription (it
# expires on its own after 3 days).

resource "aws_sns_topic" "pump_alerts" {
  name = var.topic_name
}

resource "aws_sns_topic_subscription" "po_email" {
  topic_arn = aws_sns_topic.pump_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
