# dashboards

## Purpose
Single local Grafana instance with two datasources. Renders fleet health, per-pump detail, and drift panels. Switches between local InfluxDB and AWS DynamoDB-via-Lambda-URL via datasource selection — no panel rewrites.

## Current state
- ✅ AWS-mode adapter shipped 2026-06-04 (dashboards session #1): contract locked by **ADR 0014** (HANDOFF §6 Q1 resolved), `dashboards_adapter/handler.py` + moto tests, `infra/modules/dashboards_adapter` (Function URL AuthType=NONE, reserved concurrency 5 per the 2026-06-04 review Q1), `scripts/build_adapter.{ps1,sh}`.
  - One GET → JSON envelope `{fleet_size, pumps_reporting, as_of, pumps[]}`; per-pump objects carry flattened `psi_<feature>` keys matching the ADR 0005 §3 InfluxDB field names — panels are mode-symmetric by construction.
  - `alert_flag` + `last_alert_sent_at` literal passthrough (ADR 0012 §Alternatives 2C); absent-until-first-publish maps to JSON `null` on the wire.
  - Adapter does NOT import `shared/` — outside the ADR 0005 parity set; `test_adapter_does_not_import_shared` is the tripwire (joins the set in the same PR if that ever changes).
  - Datasource: **Infinity plugin**, root selector `$.pumps` (PO call 2026-06-04; SigV4/IAM upgrade is config-only).
- ✅ Dashboard JSON pair shipped 2026-06-06 (dashboards session #2): `dashboards/local.json` (InfluxDB) + `dashboards/aws.json` (Infinity/adapter) with Grafana provisioning for zero-manual-import startup.
  - Both dashboards render the same panel concepts: fleet score timeseries, fleet PSI timeseries, alert state table, per-pump detail, pumps reporting stat, max fleet score gauge, max fleet PSI gauge, alerts active stat.
  - Thresholds: score > 0.7 (red), PSI > 0.25 (red), PSI 0.1–0.25 (yellow) — per `_interfaces.md §PSI parameters`.
  - Grafana service added to `docker-compose.yml` with Infinity plugin auto-install and provisioning mounts.
  - `infra/grafana/provisioning/datasources/datasources.yml` — pre-wires InfluxDB-local (default) + Infinity-AWS.
  - `infra/grafana/provisioning/dashboards/dashboards.yml` — loads both JSON files from `/var/lib/grafana/dashboards`.

## Interfaces (in / out)
- **In (local mode):** InfluxDB at `localhost:8086`.
- **In (AWS mode):** adapter Function URL (Terraform output `adapter_function_url`) serving the ADR 0014 snapshot contract — see `_interfaces.md §Grafana → DynamoDB adapter`.
- **Out:** Fleet heatmap of P(failure_48h), per-pump filtered detail, drift PSI panel (4 surviving `psi_*` fields per ADR 0009), alert-state table, pumps reporting stat.

## Open questions
- ~~Dashboard JSON pair~~ — resolved 2026-06-06.
- ~~Provisioning method~~ — resolved: provisioning-as-code via Grafana provisioning dirs.
- ~~Infinity plugin install~~ — resolved: `GF_INSTALL_PLUGINS` env in docker-compose.
- `FLEET_SIZE` (Terraform var → adapter env) duplicates the simulator fleet size — drift means silently short snapshots (`pumps_reporting` exposes it). Single-source-of-truth fix if it ever bites.
- Infinity null-handling: verify column inference / type coercion on the `last_alert_sent_at` column (mixed `null` + ISO strings) when building the panels — 2026-06-04 review Q6 (groq) flagged it as verify-don't-assume.

## Related ADRs
- ADR 0014 — adapter API contract + Infinity + AuthType=NONE (this component's founding decision).
- ADR 0005 §3 — InfluxDB field names the flattened PSI keys mirror.
- ADR 0009 — 4-key PSI surface the panels bind to.
- ADR 0010 — STATE row + BatchGetItem pattern the adapter consumes.
- ADR 0012 — two-attribute alert state the panels surface literally.
