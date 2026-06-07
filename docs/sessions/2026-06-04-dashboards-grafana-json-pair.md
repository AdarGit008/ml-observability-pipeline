# Session 2026-06-04 — dashboards — Grafana JSON pair + provisioning

- **Component:** dashboards (#2 — closes the component)
- **Parity-set session (Tier 2b):** yes. Loaded `shared/{features,score,drift}.py` (read, not re-derived), ADR 0005 + Addendum 2026-06-03, and cited the enforcement tests by name: `local_runtime/tests/test_service.py::test_structural_parity_no_vendoring`, `::test_structural_parity_score_loads_from_shared`, `::test_structural_parity_compute_psi_loads_from_shared`.
- **Pre-flight:** cold-path commit verified on `main` (`3bd7c8a`, ff of `infra: add cold path — S3 archive, Glue catalog, batcher (ADR 0015…)`); PO confirmed the cold-path `terraform validate` + `plan` with all three build scripts ran green.

## Intent

Close the dashboards component: committed dashboard JSON pair — `dashboards/local.json` (InfluxDB/Flux) and `dashboards/aws.json` (Infinity → adapter Function URL) — rendering the same panel concepts from the shared field vocabulary (ADR 0005 §3 / ADR 0009 / ADR 0014), plus Grafana provisioning so `docker compose up` loads both with zero manual steps.

## What changed

- **NEW `dashboards/local.json`** — uid `pump-fleet-local`, datasource uid `influxdb-local`. Panels: fleet latest-score table (color-background at 0.7), per-pump score timeseries (`pump` template variable via `schema.tagValues`), 4× PSI timeseries (`psi_vibration_amp|bearing_temp|motor_current|rpm`) with `_interfaces.md §PSI parameters` band thresholds (green / yellow 0.10 / red 0.25), `spanNulls` on for the every-30th-tick PSI cadence. Refresh 5 s.
- **NEW `dashboards/aws.json`** — uid `pump-fleet-aws`, datasource uid `infinity-aws`. Same concepts as current-state views (the ADR 0014 adapter is a snapshot contract — no history): fleet snapshot table (score + 4 PSI + alert columns, same band colors), latest-score bar gauge, 4× PSI bar gauges, alert-state table (`alert_flag` + `last_alert_sent_at` literal passthrough, value-mapped OK/ALERT and null→"never" — display mapping only, NO threshold re-derivation per ADR 0012 §2C), `pumps_reporting`/`fleet_size` stat. All Infinity queries use relative URL `""` against the datasource base URL; `last_alert_sent_at` columns pin `type: string` explicitly (mixed null + ISO — 2026-06-04 review Q6 verify-don't-assume).
- **NEW `grafana/provisioning/datasources/datasources.yml`** — pins uids `influxdb-local` (Flux, org `ml-obs`, bucket `pump_telemetry`, same plaintext-local-token posture as ADR 0005 §Negative) and `infinity-aws` (base URL from `$ADAPTER_FUNCTION_URL` env expansion).
- **NEW `grafana/provisioning/dashboards/dashboards.yml`** — file provider over `/var/lib/grafana/dashboards` (read-only mount of `dashboards/`).
- **EDITED `docker-compose.yml`** (bash-side rewrite per FUSE rules, both views verified) — new `grafana` service: `grafana/grafana-oss:11.6.0` pinned, `GF_INSTALL_PLUGINS: yesoreyeram-infinity-datasource`, anonymous-admin demo auth, `ADAPTER_FUNCTION_URL: ${ADAPTER_FUNCTION_URL:-}` passthrough, provisioning + dashboards mounted ro, `grafana_data` named volume (plugin cache), `depends_on: influxdb`.
- **NEW `dashboards/tests/test_dashboard_vocabulary.py`** (+ `__init__.py`) — 7 structural tests: both JSONs parse; every `psi_*` token ∈ `{psi_<n> for n in shared.features.PSI_FEATURE_NAMES}` (allowed set DERIVED from the parity boundary, not hand-copied — catches retired rolling-feature PSI names); local Flux `_field` references ⊆ {`score`} ∪ PSI set; AWS Infinity selectors ⊆ ADR 0014 wire-contract keys; datasource uid pairing pinned against the provisioning YAML; 5 s refresh pinned.

## Decisions (session-log level — none ADR-worthy; all inside contracts locked by ADR 0005/0014)

1. **Provisioning-as-code** over manual import (PO call). DoD's "zero manual steps" is literal.
2. **Panel concept set** as proposed (PO call): snapshot-vs-history asymmetry handled by panel *type* (timeseries locally, table/bar/stat in AWS mode); field *vocabulary* identical across modes. Alert-state table and reporting stat are AWS-only — local mode has no alert fields (InfluxDB schema is 13 fields, ADR 0005 §3; ADR 0012's two attributes live on the DynamoDB STATE row).
3. **`GF_INSTALL_PLUGINS` + `${ADAPTER_FUNCTION_URL}` env expansion, 5 s refresh** (PO call). No baked image; Grafana provisioning natively expands env vars; empty default keeps local-only `docker compose up` clean.
4. **Datasource UIDs pinned**: `influxdb-local`, `infinity-aws` — referenced deterministically by the JSON pair, enforced by `test_datasource_uids_pinned_and_paired`.
5. **Anonymous-admin demo auth** on local Grafana (no login form). Local-only on `localhost:3000`, same threat posture as the committed InfluxDB token.

## Trade-offs surfaced

- Infinity relative-URL-against-base behavior and `last_alert_sent_at` column inference are **flagged verify-don't-assume** — AWS-mode eyes-on is post-first-apply by nature; explicit `type: string` pins the riskiest column.
- `grafana-oss:11.6.0` pin trades freshness for reproducibility (same posture as `influxdb:2.7`).
- `GF_INSTALL_PLUGINS` needs internet on first container boot; cached in `grafana_data` after.

## Verification

- Sandbox: full suite **411 passed + 1 skipped** (baseline 404+1 + 7 new). Structural parity tests green. `docker-compose.yml` parses (yaml.safe_load); both dashboard JSONs parse.
- PO-side eyes-on (DoD): `docker compose up` → `localhost:3000` renders the local dashboard with live data, zero manual steps. AWS dashboard renders panel skeletons (datasource URL empty until next apply).
- FUSE incident note: one `Edit`-tool truncation of the new test file mid-session (known failure mode); recovered via bash-side full rewrite, both views verified.

## State at end of session

- Dashboards component: **closed** pending PO eyes-on + cascade dispositions.
- Suite baseline moves 404+1 → **411+1**.
- Open (carried): Infinity null/relative-URL behavior verification at first AWS apply; `FLEET_SIZE` single-source-of-truth if it ever bites; cold-start latency canary post-first-apply.

## Reviewer feedback (cascade 2026-06-07)

Response: `review_responses/2026-06-04-dashboards-grafana-json-pair.md`
(groq / llama-3.3-70b-versatile). All six packet questions ACCEPTED as
shipped; no code changes. Full disposition table in the packet's
§Resolution. Reviewer's two "verify" asks map onto existing items:

- Flux multi-select / include-all behavior — checked live against the
  local stack at soak close (Pump variable multi-select + All render
  correctly on the timeseries panels).
- Infinity null delivery for `last_alert_sent_at` — already in
  `context/dashboards.md` §Open questions; closes at first AWS apply.

Eyes-on soak (2026-06-07, multi-hour): panels populated within one
refresh of `local_runtime` start; PSI panels STABLE on all four raw
signals across the full 1-hour window fill; no gaps. Component closure
criteria met.

## Commit draft (stage AFTER cascade dispositions fold in — DEV_NORMS §7)

```
dashboards: add Grafana JSON pair + provisioning (closes component)

Local (InfluxDB/Flux) and AWS (Infinity -> adapter Function URL)
dashboards render the same panel concepts from the ADR 0005 s3 /
ADR 0009 field vocabulary; the ADR 0014 snapshot contract makes the
AWS pair current-state views of the same fields. Provisioning-as-code
(datasources with pinned uids + file provider) and a grafana service
in docker-compose mean `docker compose up` renders the local
dashboard with zero manual steps. Alert columns are literal
passthroughs (ADR 0012 s2C) - display mapping only, no re-derived
thresholds. 7 new structural tests pin the panel vocabulary to
shared.features.PSI_FEATURE_NAMES; suite 411+1.
```

PowerShell staging sequence (canonical, DEV_NORMS §7):

```powershell
git status; git diff --stat
git add -A
git status; git diff --cached --name-status
@'
dashboards: add Grafana JSON pair + provisioning (closes component)

Local (InfluxDB/Flux) and AWS (Infinity -> adapter Function URL)
dashboards render the same panel concepts from the ADR 0005 s3 /
ADR 0009 field vocabulary; the ADR 0014 snapshot contract makes the
AWS pair current-state views of the same fields. Provisioning-as-code
(datasources with pinned uids + file provider) and a grafana service
in docker-compose mean `docker compose up` renders the local
dashboard with zero manual steps. Alert columns are literal
passthroughs (ADR 0012 s2C) - display mapping only, no re-derived
thresholds. 7 new structural tests pin the panel vocabulary to
shared.features.PSI_FEATURE_NAMES; suite 411+1.
'@ | Out-File -Encoding utf8 -NoNewline $env:TEMP\commit-msg.txt
git commit -F $env:TEMP\commit-msg.txt
git log -1 --stat
```
