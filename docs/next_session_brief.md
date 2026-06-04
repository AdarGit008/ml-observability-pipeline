# Next session brief — infra: Terraform for the AWS hot path (IaC session #1)

## Goal

Stand up the Terraform that makes the scored hot path deployable: DynamoDB table
(ADR 0010 schema), SNS alert topic + PO email subscription, IoT Rule →
lambda_scorer trigger, the Lambda function resource itself, scoped IAM, and the
deploy-zip build script. `terraform validate` (+ a reviewed `plan`) is the test
gate — NO `apply` during the session; first real deploy is a PO-side demo-day
act with `aws_teardown.sh` ready.

## How to start — plain-language walkthrough FIRST

Same rule as always: walk PO through the planned modules + open Qs in plain
language, one paragraph each, BEFORE any code. AskUserQuestion for the PO calls.

## In-scope (in order)

1. **Repo layout + skeleton.** `infra/` root module + per-resource modules
   (`dynamodb`, `sns`, `iot_rule`, `lambda_scorer`, `iam`). Pin provider +
   region `eu-central-1` (hard constraint #5). Remote state: local backend
   (single PC — no S3 state bucket cost surface).
2. **DynamoDB table.** `pump_hot_state` per ADR 0010 (`PK=pump_id`, `SK=sk`).
   PO call at plan-step: PAY_PER_REQUEST vs provisioned-within-Always-Free
   (25 RCU/WCU free FOREVER applies to provisioned; on-demand bills per
   request — at 13.5K invocations/demo the math matters). Likely ADR.
3. **SNS topic + email subscription.** Topic + `SNS_TOPIC_ARN` output wired to
   the Lambda env (required at cold-start — KeyError fail-fast posture).
4. **Lambda + packaging.** `scripts/build_lambda.ps1` (+ `.sh`) staging
   `shared/` + `lambda_scorer/` into `.build/lambda_dist/` before
   `archive_file` (ADR 0005 Addendum Q1 — the committed answer to Gemini's
   multi-root packaging point). 512 MB, Python 3.12, `DDB_TABLE_NAME` +
   `SNS_TOPIC_ARN` + region env. Zip footprint sanity vs ADR 0006 §Q4
   (~124 MB unzipped baseline, 250 MB ceiling).
5. **IoT Rule.** `SELECT * FROM 'factory/pumps/+/telemetry'` → Lambda
   permission + trigger (handler treats event as raw telemetry dict —
   `context/lambda_scorer.md` §Interfaces).
6. **IAM.** Execution role: `dynamodb:Query/GetItem/PutItem` on the table ARN,
   `sns:Publish` on the topic ARN, CloudWatch logs. Nothing wildcarded.
7. **(Stretch) Teardown alignment.** `aws_teardown.sh` covers everything the
   modules create; budget-alert posture re-checked ($1 / $5 alerts).

## Loads

- Tier 1: `context/_global.md`, DEV_NORMS §7 (review-before-commit ordering +
  staging sequence) + §8.
- Tier 2: `context/infra.md`.
- Tier 3: `context/_interfaces.md` (DynamoDB schema, SNS payload, env vars).
- ADRs: 0010 (schema), 0012 (SNS topic contract), 0006 §Q4 (zip footprint),
  0005 §Addendum Q1 (packaging decision — load the Addendum, not the full
  parity load: this session imports no `shared/` logic; it only COPIES the
  directory at build time. infra is NOT in the parity set).
- Memory: fuse-write-truncation (single Write + cp + verify; NEW-filename rule
  for any PO-side mid-session rewrites), git-on-windows, lambda_scorer
  carry-forward.

## Constraints

- **$0:** Always-Free services only; `terraform validate`/`plan` in-session,
  never `apply`. No real AWS calls from the sandbox.
- FUSE: single complete Write → cp → verify per file; bash-side heredoc for
  post-Write changes; rm on D:\ blocked — overwrite instead.
- Bash 45 s cap. Terraform binary availability in sandbox to be checked at
  session start (else validate runs PO-side like git).
- Git PO-side; commit AFTER the review cascade per DEV_NORMS §7.

## Definition of done

- `terraform validate` green on the root module (sandbox or PO-side).
- `terraform plan` output reviewed by PO — resource list matches scope, no
  cost-bearing surprises.
- Build script produces a zip from which `lambda_scorer.handler` imports
  `shared.*` cleanly (smoke-check scripted).
- DynamoDB billing-mode decision recorded (ADR if provisioned-free-tier wins).
- `context/infra.md` updated; session log + review packet → cascade →
  dispositions → commit draft, in that order.
- Close with AskUserQuestion: next-session focus (dashboards adapter is the
  natural follow-on) + prepared brief.

## Carried context

- Suite baseline entering this session: **369 passed + 1 skipped**.
- Committed `model/artifacts/*` are the PO-native 30-pump canonical build
  (commit-canonical-only policy — README §Commit policy). Don't rebuild here.
- Cold-start latency measurement remains post-deploy (after this session's
  output first gets applied).
- `shared/{drift,score}.py` docstrings still say "the future lambda_scorer" —
  fix belongs to the next parity-touching session (dashboards), not this one.
