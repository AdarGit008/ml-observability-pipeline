# Next session brief — fleet-PSI infra: make `lambda_fleet_psi` deployable

## Paste-block (per templates/session_brief_template.md)

```
Component: infra
Intent:    Write infra/modules/fleet_psi + scripts/build_fleet_psi.{ps1,sh} + root wiring so the
           ADR 0018 fleet-PSI Lambda is deployable. NO apply.
Loads:     _global, infra, lambda_fleet_psi, _interfaces; Tier 3: lambda_s3_batcher, ADR 0018, ADR 0015,
           ADR 0006 §Q4, ADR 0013
Reference: ADR 0018 §Follow-ups (the charter for this session); modules/lambda_s3_batcher/*.tf +
           scripts/build_batcher.* as the EventBridge-Lambda template; scripts/build_lambda.* for
           reference-JSON bundling.
Constraints: NO terraform apply (build + validate only; stack stays down, $0). terraform not in sandbox.
Definition of done: module + build script + root instantiation + outputs + teardown sweep land; terraform
           validate green + build-script static smoke-check pass PO-side; DeepSeek-reviewed; committed.
```

## NOT parity-touching — infra only
This session does **not** modify `shared/` or any scoring/drift logic, so it is **not** in the
parity set (no Tier 2b loads required). The one nuance: `scripts/build_fleet_psi.*` must **stage
`shared/{__init__,features,drift}.py` verbatim** into the zip — copy, never edit. If you find
yourself changing anything under `shared/`, stop: that's a different (parity) session.

## Goal
The ADR 0018 handler (`lambda_fleet_psi/handler.py`) is built, tested (9 moto tests green), and
committed — but it has no Terraform and no build script, so it can't deploy. Land all of:

1. **`infra/modules/fleet_psi/`** — `aws_lambda_function` (python3.12, x86_64) + an EventBridge
   `aws_cloudwatch_event_rule` (`rate(5 minutes)`) + target + `aws_lambda_permission` (events
   invoke) + a scoped IAM role. **Self-contained module** (own role + log group + packaging
   reference), the dashboards_adapter / batcher posture.
2. **`scripts/build_fleet_psi.{ps1,sh}` + `scripts/fleet_psi_requirements.txt`** — the **drift-only**
   staging: `numpy` (manylinux2014_x86_64 wheel) + `shared/{__init__,features,drift}.py` +
   `model/artifacts/operational_reference_distribution.json` + `lambda_fleet_psi/{__init__,handler}.py`,
   tests stripped, **boto3 NOT bundled** (runtime-provided), **NO sklearn / NO model.pkl**. Enforce
   the ADR 0006 §Q4 footprint check; static smoke-check (manylinux numpy can't import on Windows —
   same as the batcher).
3. **Root module wiring** — instantiate `modules/fleet_psi`, pass the table name/ARN, the SNS topic
   ARN, and `fleet_size`; add outputs (`fleet_psi_function_name`, `fleet_psi_schedule_rule_name`); add
   a `fleet_psi_schedule_expression` tfvar (default `rate(5 minutes)`, mirroring
   `batcher_schedule_expression`) + the `terraform.tfvars.example` line.
4. **`scripts/aws_teardown.sh`** — sweep the new function + its log group + the EventBridge rule
   (the FLEET STATE row is swept by the existing table teardown; note it, don't special-case).

## Crux — three decisions to make (with recommendations)
- **Zip upload path.** The fleet zip is numpy-only (~23 MB unzipped per ADR 0006 §Q4's numpy line) —
  likely **under** the 50 MB direct-upload limit, unlike the scorer (62 MB → S3). Decide: direct
  `archive_file` upload (simpler) vs the S3 `deploy/` + `source_hash` path the scorer/batcher use
  (consistency; the 2026-06-10 F3 multipart-safe fix). **Recommend:** measure the staged zip; if
  comfortably < 50 MB, direct-upload and note why it diverges from the other two; else reuse the S3
  pattern. This divergence is the one thing that may deserve an ADR note (or a fold into ADR 0018).
- **Sizing.** **Recommend 256 MB / 30 s** (mirror the batcher — numpy import dominates the cold
  start; the warm path is 15 Queries + 1 GetItem + 1 PutItem + PSI on ≤2250 rows).
- **`reserved_concurrent_executions`.** The new-account floor forces **-1** (infra.md open item) —
  `rate(5 minutes)` is never concurrent anyway, so -1 is fine; document it (restore to 1 after the
  Service Quotas bump, same note as the batcher/adapter).

## IAM (scoped — the no-extra-access tripwire)
`dynamodb:Query` + `dynamodb:GetItem` + `dynamodb:PutItem` on the **table ARN only** (Query for the
per-pump windows, GetItem for the FLEET edge-trigger read, PutItem for the FLEET STATE write — **no
`BatchGetItem`**, the fleet doesn't use it); `sns:Publish` on the **topic ARN only**; logs scoped to
this function's own group (no CreateLogGroup — Terraform-managed). The scoped policy is the tripwire
against the Lambda growing access it shouldn't.

## Reference-JSON layout gotcha
`shared.drift.load_reference()` resolves `_DEFAULT_REF_PATH =
<…>/shared/../model/artifacts/operational_reference_distribution.json`, so in the zip `shared/` and
`model/artifacts/` must be **siblings at the zip root** — `scripts/build_lambda.*` already stages the
reference for the scorer; copy that layout. `model.pkl` is intentionally absent (drift-only;
`load_reference` skips the version check, ADR 0007 §4).

## NOT in scope
- **No `terraform apply`.** Build + `terraform validate` + `terraform plan` review only; the stack
  stays down ($0). Live verify is the demo-day rehearsal (separate session).
- **Dashboards FLEET panel** — separate dashboards session (adapter reads the 15 pump STATE keys;
  surfacing FLEET is an additive `BatchGetItem` + ADR 0014 contract change).
- **`seasonal_drift` live verify** — rolls into the demo-day rehearsal once this is deployable.
- The other carry-overs (reserved-concurrency restore, CI cost guardrails, README polish) — untouched.

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §5–§8.
- Tier 2: `context/infra.md`, `context/lambda_fleet_psi.md`.
- Tier 3 (templates + contracts): `context/lambda_s3_batcher.md`, `context/_interfaces.md`
  (§Fleet-PSI DynamoDB writes, §SNS FLEET-scope); the actual `infra/modules/lambda_s3_batcher/*.tf`
  + `infra/modules/dashboards_adapter/*.tf` (self-contained module pattern) +
  `scripts/build_batcher.{ps1,sh}` + `scripts/build_lambda.{ps1,sh}` (staging templates);
  ADR 0018 (charter + §Follow-ups), ADR 0015 (batcher infra), ADR 0006 §Q4 (zip footprint/upload),
  ADR 0013 (cost), ADR 0014 (self-contained adapter module).
- Memory: `ml-obs-pipeline-fleet-psi`, `ml-obs-pipeline-infra-session1`,
  `ml-obs-pipeline-live-apply-2026-06-07` (reserved-concurrency -1 + `source_hash` lessons),
  `ml-obs-pipeline-git-on-windows`, `ml-obs-pipeline-bash-scripts-path`,
  `ml-obs-pipeline-fuse-write-truncation`.

## Constraints
- $0; **no apply** this session. `terraform` not in the sandbox → `validate`/`plan` + the build-script
  static smoke-check are the PO-side testable surface (manylinux numpy can't import on Windows).
- CI cost guardrails: the new module must add **no** forbidden resource types (no EC2/RDS/Firehose/
  Crawler/Managed-Grafana); region stays `eu-central-1`.
- Git PO-side; FUSE rules (NEW files via Write; existing-file edits — `aws_teardown.sh`, root `*.tf`,
  `terraform.tfvars.example`, `context/*` — via bash-python rewrite); BOM-free commits (inline `-m`).
- Reviewer: `.\scripts\run_review.ps1 -Slug fleet-psi-infra` (DeepSeek).

## Definition of done
`infra/modules/fleet_psi/` + `scripts/build_fleet_psi.{ps1,sh}` + `scripts/fleet_psi_requirements.txt`
+ root instantiation + outputs + `terraform.tfvars.example` knob + `aws_teardown.sh` sweep all land;
`terraform validate` green and the build script's static smoke-check passes (PO-side, Windows);
`context/infra.md` (flip the `[ ] modules/fleet_psi` item) + `context/lambda_fleet_psi.md` (flip the
Terraform open question) updated; upload-path decision recorded (ADR note or ADR 0018 fold); session
log + DeepSeek review packet written; committed. **No apply** — the Lambda is deployable, deployment
waits for the demo-day rehearsal.
```
