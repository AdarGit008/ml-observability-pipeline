output "iot_endpoint" {
  description = "Account-specific IoT Core ATS data endpoint — paste into simulator config broker.url (port 8883 is the AwsIotPublisher default)."
  value       = data.aws_iot_endpoint.ats.endpoint_address
}

output "thing_names" {
  description = "Thing names P-00..P-NN (== client_ids == pump_ids, ADR 0003)."
  value       = aws_iot_thing.pump[*].name
}

output "certificate_arns" {
  description = "Per-pump certificate ARNs (index-aligned with thing_names)."
  value       = aws_iot_certificate.pump[*].arn
}

output "policy_name" {
  value = aws_iot_policy.fleet.name
}

output "cert_dir" {
  description = "Directory the cert/key material was written under."
  value       = var.cert_dir
}
