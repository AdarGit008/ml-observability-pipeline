# Next session brief — Fleet-PSI live verify + dashboards FLEET panel (demo-day rehearsal)

## Not a parity session
This session is **infra apply + dashboards** — `dashboards/` and
`dashboards_adapter/` are explicitly OUTSIDE the mode-parity set (ADR 0014,
inverse-import test pins it). No Tier 2b parity loads required. The fleet-PSI
*logic* shipped and is parity-clean already (ADR 0017/0018, committed
2026-06-11); this session does NOT touch `shared/`. If you find yourself
editing `shared/{features,score,drift}.py`, STOP — that's out of scope here.

## Goal
Prove the last component (Fleet-PSI Lambda, ADR 0018) end-to-end on live AWS
and make its output visible in Grafana:
1. Surface the `pump_id="FLEET"` STATE row on the AWS dashboard (new FLEET
   panel / row).
2. Live apply, run the `seasonal_drift` scenario, and verify the fleet path:
   the FLEET STATE row is written, edge-SNS fires **once** on the breach and
   does **not** re-publish on the next armed tick, and a healthy fleet stays
   quiet (no FLEET alert in the warmup window — ADR 0017 gate).
3. Fold in demo-day rehearsal: confirm `pumps_reporting == 15` live, close the
   Grafana Infinity relative-URL open item (`context/dashboards.md`), restore
   reserved concurrency if the quota bump has landed.

## The crux — the FLEET row is invisible to the adapter by design
ADR 0018 puts FLEET in a **separate DynamoDB partition**, deliberately
invisible to scorer/batcher/adapter. But the AWS dashboard's only read path is
the Grafana Infinity datasource -> the adapter Function URL (ADR 0014). So
surfacing FLEET in Grafana requires a decision — resolve with an ADR 0014
amendment or a session-log decision:
- **(A) Extend the adapter** to additionally `GetItem` the FLEET row and add it
  to the envelope (e.g. a top-level `fleet` object alongside `pumps[]`). Keeps
  one AWS read path; smallest Grafana change. **Leaning: A** — the adapter is
  already the single snapshot read surface; one extra GetItem is ~$0.0001/demo
  and keeps Grafana's datasource list unchanged.
- **(B) A second Infinity target** pointed at a new adapter route/field. More
  moving parts, two read shapes to keep in sync.
Either way: FLEET is **AWS-only** (EventBridge Lambda; local mode has no
fleet-PSI), so the panel is AWS-dashboard-only — same posture as the existing
alert-state / reporting panels (`dashboards/aws.json` only, not `local.json`).
The dashboard-vocabulary test (`dashboards/tests/`) will need a FLEET token +
the uid-pairing rule kept intact.

## Context — what's done as of 2026-06-11
- **Pipeline is feature-complete.** PSI warmup gate (ADR 0017) and Fleet-PSI
  Lambda + infra (ADR 0018) committed + pushed 2026-06-11. Stack is DOWN; $0.
- Fleet-PSI infra is built but **never applied live**: `infra/modules/fleet_psi`
  (own IAM role + log group + EventBridge `rate(5 minutes)`, S3 deploy-path
  upload), `scripts/build_fleet_psi.{ps1,sh}`, root wiring, teardown sweep.
  Last green `plan` = 130-add (fleet_psi = 7 resources).
- First live apply (2026-06-07) verified scorer->DynamoDB->adapter->Parquet and
  teardown-clean; cost ~$0.01/run (~10x under the ADR 0013 ceiling). Cold start
  4.8s / 43ms warm.
- One-time P-00 collision pre-step (delete the 2026-05-27 Console-provisioned
  Thing) was already done at the 2026-06-07 apply — should NOT recur, but
  confirm against the runbook before apply.

## In scope
- Adapter FLEET surfacing (decision A/B above) + the matching `dashboards/aws.json`
  panel + dashboard-vocabulary test update.
- Live apply: `terraform init` (new `local`/`http` providers from ADR 0016) ->
  all FOUR build scripts -> `validate` + `plan` -> apply -> run `seasonal_drift`
  -> verify -> teardown.
- Demo-day rehearsal checks: `pumps_reporting == 15`, Infinity relative-URL
  item, reserved-concurrency restore (adapter->5 / batcher->1) if Service Quotas
  bump is through (runbook section 0.5).
- Runbook update: add the fleet-PSI apply/verify steps to `aws-demo-day.md`.

## NOT in scope (later)
- CI cost guardrails (last item in `context/infra.md`).
- README / portfolio polish (cost table: ADR 0013 ceiling + $0.01 live actual).
- Any `shared/` / drift-logic change (parity surface — separate brief).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS (esp. section 5 tiers, section 7
  commit-after-review, section 8).
- Tier 2: `context/lambda_fleet_psi.md`, `context/infra.md`,
  `context/dashboards.md`.
- Tier 3 (crosses components): `context/_interfaces.md` (adapter envelope /
  ADR 0014 contract, SNS alert payload, FLEET partition).
- ADRs: **0018** (fleet-PSI), **0014** (adapter contract), 0016 (IoT fleet
  provisioning — providers/init), 0013 (DynamoDB billing/cost), 0012
  (edge-triggered SNS), 0017 (warmup gate — for the healthy-quiet check).
- Runbook: `docs/runbooks/aws-demo-day.md`.
- Memory: `ml-obs-pipeline-live-apply-2026-06-07`, `ml-obs-pipeline-fleet-psi`,
  `ml-obs-pipeline-infra-session1`, `ml-obs-pipeline-bash-scripts-path`,
  `ml-obs-pipeline-fuse-write-truncation`, `ml-obs-pipeline-git-on-windows`.

## Constraints
- **Apply discipline:** `terraform init` FIRST (fleet-PSI added `local ~>2.5` +
  `http ~>3.4` providers and the `aws_s3_object` deploy switch). Run all FOUR
  build scripts (`build_lambda`, `build_adapter`, `build_batcher`,
  `build_fleet_psi`) before `plan` — `archive_file`/`aws_s3_object` read
  `.build/*` at plan time.
- `.sh` scripts (teardown) need the aws+terraform dirs prepended to Git Bash
  PATH (memory: `ml-obs-pipeline-bash-scripts-path`).
- $0 posture: apply only for the live verify, then **teardown + absence sweep**;
  confirm budgets quiet. Expected ~$0.01/run; fleet-PSI adds one Lambda on a
  5-min rate + a few RCU/WCU — still well under ceiling.
- Git PO-side; FUSE rules (existing files bash-rewrite, NEW via Write, never
  `Edit` on D:); BOM-free commits (inline `-m`).
- Reviewer: `.\scripts\run_review.ps1 -Slug <slug>` (DeepSeek-only, ADR 0011
  Addendum).
- Commit AFTER review (DEV_NORMS section 7): tests green -> review packet -> PO
  cascade -> fold dispositions -> commit.

## Definition of done
- FLEET row visible on the AWS dashboard; A/B decision recorded (ADR 0014
  amendment or session-log); dashboard-vocabulary test green with a FLEET token.
- Live apply verified: FLEET STATE row written under `seasonal_drift`; edge-SNS
  fires once + no re-publish; healthy fleet stays quiet through the warmup
  window; `pumps_reporting == 15`; Infinity relative-URL item closed.
- Reserved concurrency restored (or explicitly deferred with the quota status
  noted).
- Teardown clean + absence sweep green + budgets quiet ($0 confirmed).
- Runbook updated with the fleet-PSI steps; session log + `context/*` closures;
  DeepSeek cascade + dispositions folded; commit.

## Open questions (leanings)
1. **Adapter FLEET surfacing — A or B?** Leaning **A** (extend the adapter
   envelope with a `fleet` object; one read path, trivial cost). Confirm with PO.
2. **FLEET panel form** — single stat/gauge (latest fleet PSI + alert state) vs
   a small row mirroring the per-pump PSI vocabulary? Leaning a compact
   stat+state pair, since FLEET is one pooled series, not 15.
3. **Reserved concurrency** — has the Service Quotas bump landed? If not, leave
   at -1 and note it; don't block the session on it.
4. **EventBridge `rate(5 minutes)` vs demo length** — a short demo run may only
   catch 1-2 fleet evaluations. Decide whether to temporarily tighten the rate
   for the rehearsal or run the scenario long enough to capture a breach.
