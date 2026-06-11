# Next session brief — Fleet-PSI live FLEET-path verify (demo-day rehearsal)

Picks up the verify ABORTED on 2026-06-11. The pump-id off-by-one it
surfaced is FIXED + committed (77e2c61); see
`docs/sessions/2026-06-11-fleet-psi-live-verify.md` and memories
[[ml-obs-pipeline-fleet-psi]], [[ml-obs-pipeline-fleet-batcher-off-by-one]],
[[ml-obs-pipeline-demo-mode-degrades]].

Not a parity session — `shared/` untouched. dashboards/ + dashboards_adapter/
are outside the parity set (ADR 0014). No Tier 2b loads.

## Starting state
- Fix committed 77e2c61 (range(FLEET_SIZE) in fleet_psi + batcher; consistency
  guard; docs). Suite 454 passed / 1 skipped. Stack DOWN, $0.
- Dashboards FLEET panel + adapter `fleet` object already shipped (Option A).
- `pumps_reporting == 15` (adapter) already verified live 2026-06-11.

## Goal
Prove the FLEET path live and close the demo-day open items:
1. `pumps_pooled == 15` (the off-by-one fix, live — was 14).
2. FLEET STATE row written under `seasonal_drift`; edge-SNS fires ONCE on the
   False→True breach + does NOT re-publish on the next armed tick (ADR 0012).
3. Healthy fleet stays quiet through the warmup window (ADR 0017) — needs
   `demo_mode: false`.
4. Close Grafana open items: Infinity relative-URL vs datasource base, and
   `$.fleet` single-object → one-row render (`context/dashboards.md §Open`).

## ⚠️ Critical gotchas (these will bite)
- **REBUILD ALL FOUR before plan.** `.build/{batcher,fleet_psi}_dist/` still
  hold the OLD 1-indexed code — re-running `build_lambda` + `build_adapter` +
  `build_batcher` + **`build_fleet_psi`** is MANDATORY or you redeploy the bug.
- **`demo_mode: false`** in `simulator/config.aws-iot.yaml`. With `true`,
  HEALTHY dwell compresses to ~60 ticks and pumps degrade to failure in ~15min
  (Finding B 2026-06-11 — NOT a bug). False keeps a steady HEALTHY baseline so
  the fleet-PSI breach comes from the seasonal ambient shift, and a healthy run
  actually stays quiet.
- **SNS subscription** is destroyed by teardown → re-confirm the email each
  apply (check Spam) BEFORE the breach test, or no alert fires.
- **Teardown via NATIVE Git Bash**, not `& "…bash.exe" -lc '…'` from PowerShell
  (it swallowed all stdout twice, 2026-06-11). Or verify $0 with PowerShell
  `aws` absence checks.
- `rate(5 minutes)` — run `seasonal_drift` ~10–15 min to catch ≥1 fleet flip.
- Reserved concurrency stays `-1` (Service Quotas bump not landed as of
  2026-06-11) unless confirmed otherwise.

## Sequence
1. Pre-flight: `$0` confirmed, `infra/terraform.tfvars` present. P-00 Console
   pre-step NOT needed (teardown-fresh).
2. 4 builds → `terraform init` → `validate` → `plan` (~131 add) → `apply`.
3. Confirm SNS email. `terraform output -raw iot_endpoint` + `adapter_function_url`.
4. simulator `demo_mode: false`, `scenario: healthy` first → confirm
   `pumps_reporting == 15`, `fleet.pumps_pooled == 15`, fleet quiet (no FLEET
   alert through warmup). Then switch `scenario: seasonal_drift`, run ~10–15 min.
5. Verify the FLEET breach: FLEET STATE row, edge-SNS once (email + fleet-psi
   Live Tail), no re-publish next tick.
6. Grafana → live adapter URL → close the two Infinity open items.
7. Teardown (native Git Bash) + absence sweep + budgets quiet ($0).
8. Docs: add fleet-psi VERIFY steps to `docs/runbooks/aws-demo-day.md`; close
   `context/dashboards.md` open items + `context/infra.md` reserved-concurrency
   note; session log. Likely a docs-only commit (no code change expected).

## Loads
- Tier 1: `context/_global.md`, `DEV_NORMS.md`.
- Tier 2: `context/{lambda_fleet_psi,dashboards,infra,_interfaces}.md`.
- ADRs: 0018, 0014, 0017, 0012, 0013, 0016.
- Runbook: `docs/runbooks/aws-demo-day.md`.
- Memory: fleet-psi, demo-mode-degrades, fleet-batcher-off-by-one,
  bash-scripts-path, live-apply-2026-06-07, fuse-write-truncation, git-on-windows.
