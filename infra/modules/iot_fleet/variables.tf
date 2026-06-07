variable "fleet_size" {
  description = "Pump count — one Thing/cert per pump, names P-00..P-NN. Must match the simulator's fleet.pump_count and the adapter/batcher FLEET_SIZE (root var.fleet_size is the single source)."
  type        = number

  validation {
    condition     = var.fleet_size >= 1 && var.fleet_size <= 100
    error_message = "fleet_size must be in [1, 100] — P-NN ids are two-digit (simulator.runner.pump_id_for)."
  }
}

variable "policy_name" {
  description = "Name of the single shared IoT policy (thing-policy variables scope it per-Thing — ADR 0016 §Decision 2). aws_teardown.sh checks absence by this name."
  type        = string
}

variable "cert_dir" {
  description = "Absolute path the cert/key material is written under (gitignored simulator/.secrets — ADR 0016 §Decision 3). Pass abspath() from the root module."
  type        = string
}

variable "aws_region" {
  description = "Region for the client/topic ARNs in the fleet policy."
  type        = string
}
