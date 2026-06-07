# Review Packet 2026-06-04 — dashboards — Grafana JSON pair + provisioning

Session log: `docs/sessions/2026-06-04-dashboards-grafana-json-pair.md`

## Role for the reviewer model

You are reviewing the Grafana dashboard layer of an ML observability pipeline. Focus on dashboard-JSON correctness (Flux queries, Infinity datasource queries, Grafana provisioning), contract adherence to the ADRs quoted below, and demo-day failure modes. The dashboards are the last component of the local/AWS mode-parity story.

## Project north stars (constraint anchors)

- $0 lifetime cost — Grafana OSS in local Docker only; Amazon Managed Grafana is a named anti-pattern.
- Mode parity — local (InfluxDB) and AWS (DynamoDB-via-adapter) modes share one panel-level field vocabulary: `score`/`latest_score` + `psi_vibration_amp`, `psi_bearing_temp`, `psi_motor_current`, `psi_rpm` (ADR 0005 §3, ADR 0009).
- ADR 0014: the adapter is a snapshot contract (one GET → `{fleet_size, pumps_reporting, as_of, pumps[]}`); Infinity plugin, root selector `$.pumps`.
- ADR 0012 §2C: `alert_flag` + `last_alert_sent_at` are literal passthroughs — consumers NEVER re-derive breach state from thresholds.
- PSI bands (display only): <0.10 stable / 0.10–0.25 warning / >0.25 significant.

## Summary of the change

Two committed dashboard JSONs (`dashboards/local.json`, `dashboards/aws.json`) rendering the same panel concepts per mode; Grafana datasource + dashboard provisioning YAML with pinned uids (`influxdb-local`, `infinity-aws`); a `grafana` service added to docker-compose (`grafana-oss:11.6.0`, `GF_INSTALL_PLUGINS: yesoreyeram-infinity-datasource`, `ADAPTER_FUNCTION_URL` env expansion into the Infinity datasource base URL, anonymous-admin demo auth); 7 structural tests pinning the panel vocabulary to `shared.features.PSI_FEATURE_NAMES` (derived, not hand-copied). Suite 404+1 → 411+1.

## Changed files

- `dashboards/local.json` (new) — fleet score table, per-pump score timeseries (`pump` template var), 4× PSI timeseries with band thresholds, `spanNulls` for the every-30th-tick PSI cadence, 5 s refresh.
- `dashboards/aws.json` (new) — fleet snapshot table, latest-score bar gauge, 4× PSI bar gauges, alert-state table (value-mapped OK/ALERT, null→"never"), reporting stat; Infinity queries with relative URL `""`, `last_alert_sent_at` pinned `type: string`.
- `grafana/provisioning/datasources/datasources.yml` (new) — uids pinned; Flux datasource with committed local token (same posture as ADR 0005 §Negative); Infinity base URL from `$ADAPTER_FUNCTION_URL`.
- `grafana/provisioning/dashboards/dashboards.yml` (new) — file provider, ro mount.
- `docker-compose.yml` (edited) — grafana service + `grafana_data` volume.
- `dashboards/tests/test_dashboard_vocabulary.py` + `__init__.py` (new) — structural vocabulary/uid/refresh pins.

## Specific questions for the reviewer

1. **Flux query correctness.** Fleet table uses `last() |> group() |> keep([pump_id, _time, _value])`; timeseries use `pump_id =~ /^${pump:regex}$/`. Any footguns with multi-select + include-all template values in Grafana 11 Flux panels?
2. **Infinity relative-URL pattern.** Datasource carries the base URL (env-expanded at provisioning); every query uses `url: ""`. Is relying on datasource-base + empty relative path solid across Infinity versions, or should queries carry `${ADAPTER_FUNCTION_URL}` — which dashboard JSON does NOT env-expand — making the datasource-base approach the only viable one?
3. **`last_alert_sent_at` null handling.** Column pinned `type: string`; table override maps special-null to "never". Does the Infinity frontend parser deliver JSON `null` as something Grafana's special-value mapping actually catches (vs. empty string)?
4. **Snapshot-pair asymmetry.** AWS mode renders current-state-only panels (bar gauge/table/stat) — accepted consequence of the ADR 0014 snapshot contract. Any cheap way to get score-over-time in AWS mode WITHOUT violating the "adapter computes nothing, stores nothing" principle? (We believe no, and don't want one that adds infra.)
5. **Vocabulary test scope.** The psi-token scan is text-level over the whole JSON (catches titles/selectors/queries); Flux fields and Infinity selectors are parsed structurally. Blind spots?
6. **Anonymous-admin Grafana** on localhost:3000 for demo ergonomics — acceptable at this threat posture, or should it be viewer-role?

## What I'm NOT looking for in this review

- Re-litigating ADR 0014 (adapter contract, AuthType=NONE, Infinity choice) or ADR 0009 (4-key PSI surface) — locked.
- Panel aesthetics / layout taste.
- Managed-Grafana or cloud-dashboard alternatives ($0 constraint, anti-pattern list).

## Resolution (post-cascade)

Cascade ran 2026-06-07 (groq / llama-3.3-70b-versatile). All six points ACCEPTED as
shipped — no code changes requested. Dispositions:

| Point | Disposition | Notes |
|---|---|---|
| 1. Flux multi-select | ACCEPT + live check | Reviewer asked for a live multi-select/include-all check; performed against the running local stack during the 2026-06-07 soak close (see session log §Reviewer feedback). |
| 2. Infinity relative-URL | ACCEPT | Reviewer concurs datasource-base + empty relative path is the only viable pattern (JSON does not env-expand). |
| 3. `last_alert_sent_at` null | ACCEPT, deferred verify | Already a verify-don't-assume item in `context/dashboards.md` §Open questions — closes at first AWS apply. |
| 4. Snapshot asymmetry | ACCEPT | Reviewer concurs: no cheap score-over-time in AWS mode without violating ADR 0014; none added. |
| 5. Vocabulary test scope | ACCEPT | No blind spots identified beyond the text-level scan's known shape. |
| 6. Anonymous-admin | ACCEPT | Acceptable at this threat posture (localhost-only, demo ergonomics). |
