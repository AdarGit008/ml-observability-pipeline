# dashboards

## Purpose
Single local Grafana instance with two datasources. Renders fleet health, per-pump detail, and drift panels. Switches between local InfluxDB and AWS DynamoDB-via-Lambda-URL via a second dashboard JSON — no panel rewrites.

## Current state
- ✅ AWS-mode adapter shipped 2026-06-04 (dashboards session #1): contract locked by **ADR 0014** (HANDOFF §6 Q1 resolved), `dashboards_adapter/handler.py` + moto tests, `infra/modules/dashboards_adapter` (Function URL AuthType=NONE, reserved concurrency 5 per the 2026-06-04 review Q1), `scripts/build_adapter.{ps1,sh}`.
  - One GET → JSON envelope `{fleet_size, pumps_reporting, as_of, pumps[]}`; per-pump objects carry flattened `psi_<feature>` keys matching the ADR 0005 §3 InfluxDB field names — panels are mode-symmetric by construction.
  - `alert_flag` + `last_alert_sent_at` literal passthrough (ADR 0012 §Alternatives 2C); absent-until-first-publish maps to JSON `null` on the wire.
  - Adapter does NOT import `shared/` — outside the ADR 0005 parity set; `test_adapter_does_not_import_shared` is the tripwire (joins the set in the same PR if that ever changes).
  - Datasource: **Infinity plugin**, root selector `$.pumps` (PO call 2026-06-04; SigV4/IAM upgrade is config-only).
- ✅ Dashboard JSON pair + provisioning shipped 2026-06-04 (dashboards session #2 — **component closed**):
  - `dashboards/local.json` (uid `pump-fleet-local` → datasource `influxdb-local`, Flux): fleet latest-score table, per-pump score timeseries (`pump` template var), 4× PSI timeseries with band colors per `_interfaces.md §PSI parameters`; `spanNulls` for the every-30th-tick PSI cadence.
  - `dashboards/aws.json` (uid `pump-fleet-aws` → datasource `infinity-aws`): same concepts as current-state views (ADR 0014 is a snapshot contract — no history): fleet snapshot table, latest-score bar gauge, 4× PSI bar gauges, alert-state table (display-mapped OK/ALERT + null→"never", NO threshold re-derivation), `pumps_reporting / fleet_size` stat. Alert-state + reporting panels are AWS-only — local InfluxDB has no alert fields (13-field schema, ADR 0005 §3).
  - Provisioning-as-code: `grafana/provisioning/{datasources,dashboards}` mounted ro in docker-compose; datasource uids PINNED (`influxdb-local`, `infinity-aws`); Infinity base URL from `$ADAPTER_FUNCTION_URL` env expansion (set from `terraform output -raw adapter_function_url` before `docker compose up` for AWS demos; empty default keeps local-only boots clean).
  - docker-compose `grafana` service: `grafana-oss:11.6.0` pinned, `GF_INSTALL_PLUGINS: yesoreyeram-infinity-datasource`, anonymous-admin demo auth, `grafana_data` volume (plugin cache).
  - Structural tests: `dashboards/tests/test_dashboard_vocabulary.py` (7 tests) — psi tokens ⊆ derived-from-`shared.features.PSI_FEATURE_NAMES` set, Flux fields ⊆ ADR 0005 §3 panel vocabulary, Infinity selectors ⊆ ADR 0014 contract keys, uid pairing, 5 s refresh. Dashboard-side siblings of `test_structural_parity_no_vendoring` + siblings.

## Interfaces (in / out)
- **In (local mode):** InfluxDB at `localhost:8086` (in-compose: `http://influxdb:8086`).
- **In (AWS mode):** adapter Function URL (Terraform output `adapter_function_url`) serving the ADR 0014 snapshot contract — see `_interfaces.md §Grafana → DynamoDB adapter`.
- **Out:** Grafana at `localhost:3000`, both dashboards provisioned at boot.

## Open questions
- Infinity verify-don't-assume items, checkable only at first AWS apply: relative-URL-against-datasource-base behavior, and `null` `last_alert_sent_at` through the frontend parser's special-value mapping (2026-06-04 review Q6; column pinned `type: string` defensively).
- `FLEET_SIZE` (Terraform var → adapter env) duplicates the simulator fleet size — drift means silently short snapshots (`pumps_reporting` exposes it). Single-source-of-truth fix if it ever bites.

## Related ADRs
- ADR 0014 — adapter API contract + Infinity + AuthType=NONE (this component's founding decision).
- ADR 0005 §3 — InfluxDB field names the flattened PSI keys mirror; parity set membership.
- ADR 0009 — 4-key PSI surface the panels bind to.
- ADR 0010 — STATE row + BatchGetItem pattern the adapter consumes.
- ADR 0012 — two-attribute alert state the panels surface literally.
