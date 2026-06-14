# Next session brief — close the Grafana Infinity open items

Closes the **last verification gap** in the project. The pipeline is
feature-complete and end-to-end live-verified (fleet-PSI FLEET path proven
2026-06-11 part-2, `docs/sessions/2026-06-11-fleet-psi-live-verify-part2.md`,
memory [[ml-obs-pipeline-fleet-psi]]). Two Grafana/Infinity rendering
questions remain open in `context/dashboards.md §Open questions`.

Not a parity session — `dashboards*` are outside the parity set (ADR 0014).
No Tier 2b loads.

## Goal
Close the two open items in `context/dashboards.md §Open questions`:
1. **Infinity relative-URL-against-datasource-base joining.** `dashboards/aws.json`
   panels use an empty query `url: ""` and rely on the `infinity-aws` datasource
   base URL (`$ADAPTER_FUNCTION_URL`). Unverified that Infinity joins empty/relative
   panel URLs to the base — "No data / 404 on every panel" means the assumption broke.
2. **`$.fleet` single-object → one-row render.** The 4 fleet PSI gauges + the fleet
   alert-state table bind `root_selector: "$.fleet"` (a single JSON object, not an
   array). Verify it renders as one clean row.
   - Bonus (already contract-closed 2026-06-07): `null` `last_alert_sent_at` renders
     as "never"/blank, no `Invalid date`.

## ⚠️ Key insight — this can be done $0, no re-apply
Both are **Grafana/Infinity-side rendering** questions; Infinity behaves identically
whether the datasource base URL points at a live Lambda or a local server. So the
**recommended path is a LOCAL MOCK adapter** — no AWS, no cost, no flaky-DNS apply.
A future live demo just re-confirms.

### Path A (recommended, $0) — local mock adapter
- Serve the real envelope shape on the host. Shape (`context/_interfaces.md`,
  ADR 0014/0018): `{fleet_size, pumps_reporting, as_of, pumps[], fleet}`.
  - `pumps[]`: each `{pump_id, latest_ts, latest_score, psi_rpm, psi_bearing_temp,
    psi_vibration_amp, psi_motor_current, alert_flag, last_alert_sent_at}`.
  - `fleet`: `{latest_ts, psi_rpm, psi_bearing_temp, psi_vibration_amp,
    psi_motor_current, alert_flag, last_alert_sent_at, pumps_pooled}`.
  - Make the mock fleet **breaching** (`alert_flag: true`, `psi_bearing_temp > 0.25`,
    `pumps_pooled: 15`, `last_alert_sent_at` set) AND keep one pump with
    `last_alert_sent_at: null` so the null-render path is exercised in the same view.
  - ~15-line `python -m http.server`-style server or a static `mock_envelope.json`
    served over HTTP. Claude will write it at session start.
- **Grafana runs IN Docker → it cannot reach the host as `localhost`.** Set the base
  URL to `http://host.docker.internal:<port>/` so the container reaches the host mock.
- `ADAPTER_FUNCTION_URL=http://host.docker.internal:<port>/`, then
  `docker compose up -d --force-recreate grafana` from the **repo root**.

### Path B (alternative) — live re-apply
Full cycle if a live Function URL is wanted: 4 builds in NATIVE Git Bash w/ venv
python, `terraform apply -parallelism=1` from PowerShell, simulator
`demo_mode:false` `seasonal_drift` to write a breaching FLEET row,
`ADAPTER_FUNCTION_URL=(terraform -chdir=infra output -raw adapter_function_url)`.
See [[ml-obs-pipeline-windows-live-apply-toolchain]]. More expensive; only needed
to exercise the real HTTPS Function URL.

## ⚠️ Critical gotchas (these bit 2026-06-11)
- **Grafana wouldn't open last time — blocker #1.** Diagnose Docker FIRST:
  `docker ps` (expect `ml-obs-grafana`, `0.0.0.0:3000->3000`), `docker compose logs
  grafana --tail 40`. First boot pulls the Infinity plugin (needs internet; cached in
  the `grafana_data` volume after). Confirm Docker Desktop is actually running.
- **`ADAPTER_FUNCTION_URL` must be set in the shell BEFORE `docker compose up`, from
  the REPO ROOT.** Setting it from inside `infra/` makes `terraform -chdir=infra
  output` look for `infra/infra` → empty → blank datasource → "No data" (this WAS the
  first No-data cause 2026-06-11). Always `--force-recreate grafana` so a stale
  container picks up the new URL. Verify with `echo $env:ADAPTER_FUNCTION_URL`.
- Datasource is provisioned **read-only** (`editable:false`, uid `infinity-aws`,
  `access: proxy` → Grafana backend fetches, no browser CORS). Edits go in
  `grafana/provisioning/datasources/datasources.yml`, not the UI.
- Existing-file edits (aws.json / datasources.yml / context) via **bash-python
  rewrite**, never the Edit tool — [[ml-obs-pipeline-fuse-write-truncation]].
- Git is PO-side — [[ml-obs-pipeline-git-on-windows]].

## What "closed" looks like
- Every panel populates (no "No data"/404) → relative-URL-against-base join works →
  **item 1 CLOSED**.
- The 4 fleet gauges + fleet alert table show ONE row from `$.fleet` → **item 2
  CLOSED**.
- If panels still 404/No data WITH the base URL confirmed set → the relative-URL
  assumption is genuinely broken → fix the panel `url` (relative path) or datasource
  config, document the fix, then close.

## Sequence
1. Pre-flight: confirm $0 (stack down — should already be). Pick Path A (mock) or B.
2. Get Grafana open: Docker diagnosis (`docker ps` / logs) → fix.
3. (Path A) Claude writes the mock + a serve command; start it. (Path B) re-apply +
   simulator per the toolchain memory.
4. Set `ADAPTER_FUNCTION_URL` (mock `host.docker.internal` URL, or terraform output);
   `docker compose up -d --force-recreate grafana` from repo root; `echo` to verify.
5. Open `localhost:3000` (anonymous admin, no login), open the **Pump Fleet — AWS**
   dashboard. Observe both behaviors; screenshot.
6. If broken: fix `dashboards/aws.json` / `datasources.yml` (bash-python), recreate,
   re-observe.
7. Close both items in `context/dashboards.md §Open questions`; session log; commit
   (docs + any dashboard-JSON fix). If Path B: teardown to $0 + absence sweep.

## Loads
- Tier 1: `context/_global.md`, `DEV_NORMS.md`.
- Tier 2: `context/{dashboards,_interfaces}.md` (envelope shape), `context/infra.md`
  (adapter module) if Path B.
- ADRs: 0014 (adapter snapshot contract + Infinity datasource), 0018 (`fleet`
  object), 0012 (alert-state passthrough).
- Files: `dashboards/aws.json`, `grafana/provisioning/datasources/datasources.yml`,
  `docker-compose.yml`.
- Memory: fleet-psi, windows-live-apply-toolchain (Path B), fuse-write-truncation,
  git-on-windows.

## Out of scope (separate follow-ups)
- F1 INFO-log suppression fix (code → review → redeploy).
- Reserved concurrency `-1` (Service Quotas bump).
- SSOT dedup of `FLEET_PUMP_IDS` (3 copies).
