# iot_fleet — Things, certificates, policy, attachments + on-disk cert
# material for the simulator fleet (ADR 0016, 2026-06-07).
#
# One Thing per pump, named bare P-NN to satisfy the ADR 0003 lock
# (ThingName == client_id == pump_id) that the shared policy's
# thing-policy variables depend on. AWS mints each keypair
# (aws_iot_certificate with no CSR — ADR 0016 §Decision 1: private
# keys live in the LOCAL-ONLY gitignored tfstate, accepted for the
# apply → demo → teardown lifecycle); local_sensitive_file writes
# cert+key under var.cert_dir so `terraform apply` IS the "cert pull"
# step of the demo runbook. `terraform destroy` deletes the files too
# — teardown sweeps disk as well as cloud.
#
# The ONE policy uses ${iot:Connection.Thing.ThingName} so each
# connection may only CONNECT as itself and PUBLISH to its own
# telemetry topic — the per-pump identity tripwire, same
# scoped-policy-as-tripwire trick as ADR 0014/0015. No
# Subscribe/Receive: the simulator only publishes.
#
# NOTE (runbook pre-step): the Console-provisioned P-00 Thing from the
# 2026-05-27 smoke test collides with aws_iot_thing.pump[0]. Delete it
# (Thing, cert, policy) once before the first apply.

data "aws_caller_identity" "current" {}

data "aws_iot_endpoint" "ats" {
  endpoint_type = "iot:Data-ATS"
}

# Amazon Root CA 1 is PUBLIC trust material, fetched at plan/apply
# rather than vendored — committing a .pem would need a gitignore
# negation through the blanket *.pem rule (ADR 0016 §Decision 3).
data "http" "amazon_root_ca1" {
  url = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Fetching Amazon Root CA 1 returned HTTP ${self.status_code} — check network reachability to amazontrust.com."
    }
  }
}

locals {
  # P-00 .. P-NN — must match simulator.runner.pump_id_for (zero-padded
  # two digits) and the MQTT contract (_interfaces.md §MQTT).
  pump_ids = [for i in range(var.fleet_size) : format("P-%02d", i)]
}

resource "aws_iot_thing" "pump" {
  count = var.fleet_size
  name  = local.pump_ids[count.index]
}

resource "aws_iot_certificate" "pump" {
  count  = var.fleet_size
  active = true
}

resource "aws_iot_policy" "fleet" {
  name = var.policy_name

  # $${...} escapes Terraform interpolation — the literal
  # ${iot:Connection.Thing.ThingName} must reach IoT Core, which
  # resolves it per-connection from the attached Thing.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConnectOnlyAsOwnThingName"
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:client/$${iot:Connection.Thing.ThingName}"
        Condition = {
          Bool = {
            "iot:Connection.Thing.IsAttached" = "true"
          }
        }
      },
      {
        Sid      = "PublishOnlyToOwnTelemetryTopic"
        Effect   = "Allow"
        Action   = ["iot:Publish"]
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/factory/pumps/$${iot:Connection.Thing.ThingName}/telemetry"
      }
    ]
  })
}

resource "aws_iot_policy_attachment" "pump" {
  count  = var.fleet_size
  policy = aws_iot_policy.fleet.name
  target = aws_iot_certificate.pump[count.index].arn
}

resource "aws_iot_thing_principal_attachment" "pump" {
  count     = var.fleet_size
  thing     = aws_iot_thing.pump[count.index].name
  principal = aws_iot_certificate.pump[count.index].arn
}

# --- On-disk material (simulator/.secrets/, gitignored) ---
# Layout per ADR 0016 §Decision 3 / config.example.yaml:
#   <cert_dir>/AmazonRootCA1.pem            (shared, public)
#   <cert_dir>/P-NN/P-NN.cert.pem
#   <cert_dir>/P-NN/P-NN.private.key
# file_permission is best-effort on Windows (single-PC posture); the
# gitignored directory is the custody boundary.

resource "local_file" "amazon_root_ca1" {
  content         = data.http.amazon_root_ca1.response_body
  filename        = "${var.cert_dir}/AmazonRootCA1.pem"
  file_permission = "0644"
}

resource "local_sensitive_file" "cert" {
  count           = var.fleet_size
  content         = aws_iot_certificate.pump[count.index].certificate_pem
  filename        = "${var.cert_dir}/${local.pump_ids[count.index]}/${local.pump_ids[count.index]}.cert.pem"
  file_permission = "0600"
}

resource "local_sensitive_file" "key" {
  count           = var.fleet_size
  content         = aws_iot_certificate.pump[count.index].private_key
  filename        = "${var.cert_dir}/${local.pump_ids[count.index]}/${local.pump_ids[count.index]}.private.key"
  file_permission = "0600"
}
