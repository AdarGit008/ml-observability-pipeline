# Session 2026-06-11 (part 2) — fleet-PSI live FLEET-path verify (COMPLETE)

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** n/a (verification + docs session; no code change shipped)
- **Context loaded:** `_global`, `lambda_fleet_psi`, `dashboards`, `infra`, `_interfaces`; ADRs 0012/0013/0014/0016/0017/0018; runbook `aws-demo-day.md`. NOT a parity session (`shared/` untouched; `dashboards*` outside the parity set, ADR 0014).
- **Picks up:** `2026-06-11-fleet-psi-live-verify.md` (part 1, aborted before the breach test). The pump-id off-by-one it surfaced was fixed + committed `77e2c61` before this session.

## Intent
Complete the live FLEET-path verify that part 1 deferred: prove the off-by-one fix live (`pumps_pooled == 15`, was 14), the ADR 0012 edge-triggered SNS (fires once on the False→True breach, no re-publish on the next armed tick), and the healthy-fleet-stays-quiet behaviour (ADR 0017); close the Grafana demo-day items; teardown to $0.

## Outcome — all primary goals PROVEN live
- **Off-by-one fixed live:** healthy `scenario: healthy`, `demo_mode: false` run → adapter `fleet.pumps_pooled == 15` (part 1 read this as 14). `pumps_reporting == 15`. All 15 pumps `P-00..P-14` connected clean, no publisher-retry loops.
- **Healthy fleet quiet:** `fleet` object showed `psi_* ≈ 0.002–0.005` (threshold 0.25), `alert_flag: false`, `last_alert_sent_at: null`, `pumps_pooled: 15`. Clean quiet baseline.
- **FLEET breach + ADR 0012 edge-trigger:** switched to `scenario: seasonal_drift` (kept `demo_mode: false`). `psi_bearing_temp` climbed 0.0025 → 0.176 → **3.69**, crossing 0.25. At the 08:27:19 tick `alert_flag` flipped `true` and `last_alert_sent_at` populated to **T1 = 2026-06-11T08:27:19.674Z** (written only on the edge). At the next tick (08:32:19) `alert_flag` stayed `true` but `last_alert_sent_at` **held at T1** — armed, did **not** re-publish. ADR 0012 proven.
- **Stack converged:** `terraform plan` → `No changes` (131 resources). IoT things enumerated `P-00..P-14` in the plan outputs (fix visible at the infra layer).

## Verification method note (important)
The fleet edge-publish was verified via the **FLEET STATE row's `last_alert_sent_at`** (adapter `fleet` object), NOT via logs or the SNS topic metric — both of those are blind here:
- **CloudWatch logs** only showed the per-pump WARNING lines, never the handler's INFO `fleet alert published` / per-run summary lines → see Finding F1.
- **SNS `NumberOfMessagesPublished`** read 24 over ~10 min because the topic is **shared** with the per-pump scorer (ADR 0018); under all-pump `seasonal_drift` the scorer floods per-pump alerts onto the same topic → the metric can't isolate the fleet's single publish → see Finding F3.

`last_alert_sent_at` is fleet-scoped (the `pump_id="FLEET"` STATE row), written only when `publish_alert` is true and carried forward unchanged otherwise — so "same timestamp on a later armed tick" is a clean, fleet-specific edge-trigger proof.

## New findings (no fix shipped this session — logged for follow-up)
- **F1 — fleet-PSI / scorer INFO logs are suppressed in CloudWatch.** Both handlers do `log = logging.getLogger(__name__)` with no `setLevel`, and terraform sets no `logging_config { application_log_level }`. The module logger inherits the Lambda root level (WARNING), so `log.info(...)` — including the `fleet alert published` edge confirmation and the per-run `fleet-psi ts=… alert=…` summary — never reaches the log group; only the per-pump WARNING lines surface. **Not a behaviour bug** (the alert fired correctly), but it blinds log-based observability/debugging. Fix: `log.setLevel(logging.INFO)` at module load, or a terraform `logging_config { application_log_level = "INFO" }` on the fleet (and scorer) Lambda. Follow-up candidate.
- **F2 — 5-min window paginates under `demo_mode: false`.** Every pump logged `window query returned LastEvaluatedKey (>150 rows or >1MB in 5 min) — trailing window may be truncated`. At the real-time tick rate each pump produces >150 rows / >1 MB per 5-min window, so `_read_pump_window`'s single-page query (`Limit=FLEET_WINDOW_SAMPLES`, `ScanIndexForward=False`) drops the overflow. **Benign:** descending sort keeps the **newest** 150 readings (the drifted ones) — the pooled PSI is computed on the freshest window, which favours breach detection. Worth a one-line note in `lambda_fleet_psi` context; not a correctness issue.
- **F3 — shared SNS topic conflates publishes** (see method note above). Consequence: any future "did the fleet publish?" check must read the FLEET STATE row, not the topic-level `NumberOfMessagesPublished` metric. The payload `scope: "fleet"` field is the only subscriber-side discriminator.
- **F4 — SNS confirmation email never delivered.** The `aws_sns_topic_subscription.po_email` sat at `PendingConfirmation`; two `aws sns subscribe` attempts to `adar008@gmail.com` produced no email (Gmail connector confirmed the mailbox is live and receiving other mail; nothing from `no-reply@sns.amazonaws.com` in any folder). Likely SNS-side suppression from repeated subscribe/confirm/teardown cycles across sessions. The breach was verified without it (adapter + STATE row). An unconfirmed email sub can't be deleted by TF/CLI and AWS expires it in ~3 days (already noted in `aws_teardown.sh`).

## Operational lessons (build / creds / network — Windows toolchain)
- **Build the dists in NATIVE Git Bash with the venv Python, never `bash scripts/*.sh` from PowerShell.** From PowerShell, `bash` resolves to **WSL** (`/mnt/d…`), whose system `python3` has no `pip` (heavy builds fail) and which can't delete Windows-owned `.build/**/__pycache__/*.pyc` (`rm -rf` → Permission denied, half-cleaned trees). Fix: open a real Git Bash terminal and `export PYTHON="$(pwd)/.venv/Scripts/python.exe"` (has pip; `--platform manylinux_2_28 --python-version 3.12 --only-binary` makes host Python version irrelevant). If a WSL run already polluted `.build/`, clear it from PowerShell (`Remove-Item -Recurse -Force .build\*_dist`) before rebuilding.
- **Docker smoke-check under Git Bash:** the `docker run … -w /work` arg gets MSYS-mangled to `C:/Program Files/Git/work`. Fix: prefix `MSYS_NO_PATHCONV=1` and pass the host path as `$(pwd -W)` (Windows form Docker accepts). Inside the container the import can also hang on botocore's IMDS credential probe — add `-e AWS_EC2_METADATA_DISABLED=true -e AWS_ACCESS_KEY_ID=x -e AWS_SECRET_ACCESS_KEY=y`.
- **`terraform apply` hit transient DNS drops + connection resets** (`no such host`, `wsarecv: forcibly closed`) mid-multipart on the large zips. Stabilised by quitting Docker Desktop + dropping VPN and re-running with **`-parallelism=1`** (one resource at a time → far fewer concurrent sockets). Applied cleanly across the partial+final passes (17 added on the final pass; 131 total; `plan` → No changes confirmed convergence). Failed passes leave orphaned multipart uploads — `force_destroy` aborted them at teardown (no BucketNotEmpty).
- **AWS creds are PowerShell-only** (env-var creds; no shared `~/.aws` profile — `terraform` in Git Bash fell through to IMDS and failed). So **terraform AND teardown run from PowerShell**, not Git Bash. The Git Bash–only steps this session were just the four dist builds + the Docker smoke-check.

## Teardown — $0 confirmed
- `terraform destroy -auto-approve` from PowerShell (creds + clean stdout). The native-Git-Bash `aws_teardown.sh` route was skipped — it needs creds on the Git Bash PATH, which this setup lacks.
- PowerShell-native absence sweep all-empty: no `pump-*` Lambdas, no `pump_hot_state` table, no `P-*` IoT things, **0 ACTIVE certs**, no `ml-obs-pipeline-pump-alerts` topic, no `pump_archive` Glue DB, no `pump-*` EventBridge rules, no `…pump-archive…` S3 bucket.
- Local cert material cleaned: 0 `*.private.key`, 0 `*.cert.pem`, no `AmazonRootCA1.pem` under `simulator/.secrets/`. Residue is 15 **empty** `P-*` directories (cosmetic — TF manages the files, not the dirs; teardown WARNs only on `*.private.key`, which is 0).

## Still open (carried forward)
- **Grafana demo-day items — STILL OPEN (deferred, not closed).** Grafana would not open this session (local Docker), so neither could be observed against the live adapter:
  - Infinity relative-URL-against-datasource-base joining (`dashboards/aws.json` panels use empty query `url` + the `$ADAPTER_FUNCTION_URL` datasource base).
  - `$.fleet` single-object → one-row-table render.
  Both still require a live Function URL → carry to the next re-apply. (The `null` `last_alert_sent_at` render was already contract-closed 2026-06-07; the live quiet baseline this session showed `last_alert_sent_at: null` clean in the JSON envelope, consistent with that closure.)
- **F1 INFO-log suppression** — follow-up (small: logger level or TF `application_log_level`).
- **F2 window pagination note** — doc note in `lambda_fleet_psi` context.
- Reserved concurrency still `-1` (no Service Quotas bump this session) — unchanged from 2026-06-07.

## Commit
Likely **docs-only** (no code change). Files: this session log; runbook `aws-demo-day.md` (fleet-PSI VERIFY steps); `context/dashboards.md` (Grafana items remain open — note the part-2 rehearsal attempt + Grafana-wouldn't-open); `context/lambda_fleet_psi.md` (F1 + F2 notes). PR: _(fill in at commit)_
