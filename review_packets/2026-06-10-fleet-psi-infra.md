# Review Packet 2026-06-10 — infra/modules/fleet_psi — fleet-psi-infra

> Run via: `.\scripts\run_review.ps1 -Slug fleet-psi-infra` (DeepSeek, ADR 0011 §Addendum 2026-06-10)

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.
6. (Operational corollary) Any local/AWS divergence in scoring/drift is a bug or an ADR.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

The deferred infra half of ADR 0018 (§Follow-ups): makes `lambda_fleet_psi` deployable. The handler + tests shipped in the prior session (packet `2026-06-10-fleet-psi-lambda.md`); this packet covers **only** the Terraform module, the build script, the root wiring, and the teardown sweep. **Build + validate only — NO `terraform apply` this session** (the stack stays down, $0).

New self-contained module `infra/modules/fleet_psi` (own IAM role, log group, EventBridge rule — the `lambda_s3_batcher` pattern). EventBridge `rate(5 minutes)` → Lambda; scoped IAM; reuses the scorer's SNS topic. Drift-only build script stages numpy + `shared/{features,drift}.py` + the reference JSON (no sklearn/model.pkl) with a footprint check + a Docker cold-start smoke check. `aws_teardown.sh` extended to sweep the new function/role/log-group/rule.

`terraform validate` is green and `terraform plan` expands the module to exactly 7 resources (Lambda + role + role-policy + log group + event rule + event target + invoke permission), `130 to add, 0 to change, 0 to destroy` for the full from-scratch stack.

## Changed / new files

- `infra/modules/fleet_psi/main.tf` — new. archive_file → Lambda (direct `filename` upload), own IAM role + scoped inline policy, log group, EventBridge rule + target + `lambda:InvokeFunction` permission.
- `infra/modules/fleet_psi/variables.tf`, `outputs.tf` — new.
- `scripts/build_fleet_psi.{ps1,sh}` + `scripts/fleet_psi_requirements.txt` — new. Drift-only staging, ADR 0006 §Q4 footprint check, Docker cold-start smoke check (also asserts no `model.pkl`/`sklearn` in the tree).
- `infra/main.tf` — `module "fleet_psi"` instantiation + run-order header (now FOUR build scripts).
- `infra/variables.tf` — `fleet_psi_function_name`, `fleet_psi_schedule_expression`.
- `infra/outputs.tf` — `fleet_psi_function_name`, `fleet_psi_schedule_rule_name`.
- `scripts/aws_teardown.sh` — `FLEET_FN` var; added to the Lambda+log-group loop, the `-exec` role list, and a new `pump-fleet-psi-schedule` EventBridge-rule absence check; coverage comment.

## Core hunk — the scoped IAM policy

```hcl
# IAM is the no-extra-access tripwire (ADR 0018 §Follow-ups).
Statement = [
  {
    # Query   = the per-pump trailing-window read, P-01..P-NN (ADR 0010).
    # GetItem = the previous FLEET STATE row (edge-trigger input, ADR 0012).
    # PutItem = the FLEET STATE row overwrite.
    # ADR 0018 §Follow-ups abbreviates this as "Query"; the handler's
    # edge-trigger read + STATE write necessarily add GetItem + PutItem
    # (handler.py: TABLE.query / TABLE.get_item / TABLE.put_item).
    Sid    = "DynamoDBFleetPsiReadWrite"
    Effect = "Allow"
    Action = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"]
    Resource = var.table_arn
  },
  {
    Sid      = "SNSPublishAlertsOnly"   # reuses the scorer's topic (ADR 0018 §4)
    Effect   = "Allow"
    Action   = ["sns:Publish"]
    Resource = var.topic_arn
  },
  {
    Sid    = "CloudWatchLogsThisFunctionOnly"
    Effect = "Allow"
    Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
    Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.function_name}:*"
  },
]
```

## Specific questions for the reviewer

1. **IAM scope vs the ADR's wording.** ADR 0018 §Follow-ups says "IAM = Query on the table ARN + sns:Publish + scoped logs." I granted `Query`+`GetItem`+`PutItem` because the handler's edge-trigger read (`get_item`) and STATE write (`put_item`) require them — "Query" was an abbreviation. Is granting all three (still scoped to the single table ARN, no `Scan`/`BatchGetItem`/`UpdateItem`/`DeleteItem`) the right least-privilege call, or should the ADR be amended to match? Note the known-breadth caveat carried from the batcher: IAM `LeadingKeys` can't restrict to the `FLEET` partition / `STATE` sort key, so the grant covers every key — held by code review + single call sites + tests.

2. **Direct `filename` upload vs the S3 deploy path.** The scorer (62 MB) and batcher (47.6 MB) upload via `aws_s3_object` to the archive bucket's `deploy/` prefix. The fleet zip is drift-only (numpy, no sklearn) — measured 54 MB unzipped, well under the 50 MB *direct-upload* limit once zipped — so I followed the adapter's direct `filename` pattern instead, avoiding a dependency on `module.s3_archive`. Is decoupling from the bucket worth diverging from the two heavier Lambdas' mechanism? Is there a risk the zipped artifact creeps over 50 MB on a numpy bump (the footprint check warns at 200 MB *unzipped*, which wouldn't catch a 48→52 MB *zipped* crossing)?

3. **`reserved_concurrent_executions = -1`.** Copied the batcher's posture (account concurrency quota at the new-account floor; any reservation violates min-10-unreserved). For a 5-minute cadence with a 30 s timeout, overlap is implausible, and the FLEET STATE row is idempotent-overwrite with the same at-most-once-per-edge GetItem→PutItem. Acceptable, or does the edge-trigger read-modify-write want a concurrency=1 guard the way the batcher's watermark did (deferred there only for the quota)?

4. **`depends_on` / log-group race.** The function `depends_on = [aws_cloudwatch_log_group.fleet_psi]` so the only-this-log-group IAM scoping is sufficient without `logs:CreateLogGroup` (mirrors the scorer/batcher). Is there any first-invocation race where Lambda tries to create the group before the EventBridge rule fires? (The rule + permission have no explicit dependency on the log group.)

5. **`env { variables = (known after apply) }` in the plan.** The fleet Lambda's env shows opaque in `plan` because `SNS_TOPIC_ARN` = `module.sns.topic_arn` (known after apply) — same as the scorer. Confirm this is benign and not masking a missing `DDB_TABLE_NAME`/`FLEET_SIZE` (both are static and *should* be plan-visible — is hiding the whole block expected Terraform behavior when one value is unknown?).

6. **Teardown completeness.** The sweep now asserts absence of `pump-fleet-psi` (function + log group), `pump-fleet-psi-exec` (role), and `pump-fleet-psi-schedule` (rule). The FLEET STATE row is swept implicitly with the table (no separate delete). Any fleet-PSI residue that could survive `terraform destroy` + this sweep and accrue cost (e.g. the EventBridge target, the Lambda permission)?

## What I'm NOT looking for
- The handler logic / pooling statistics / tests — reviewed and folded in packet `2026-06-10-fleet-psi-lambda.md` (ADR 0018 §Addendum); unchanged here.
- Re-litigating ADR 0013/0015/0006 §Q4 — accepted; this module reuses their proven patterns.
- Style / formatting — `terraform fmt` is clean.
- The `Â§` console mojibake on the `§` byte in the build script's `Write-Host` — cosmetic, identical to `build_batcher.ps1`.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. IAM Query+GetItem+PutItem vs ADR "Query" | **Accepted (doc fix)** | Grant is correct for the handler's actual calls; amended ADR 0018 §Follow-ups wording to "Query + GetItem + PutItem" + new infra §Addendum; main.tf comment points to it. Known LeadingKeys breadth already documented inline (batcher precedent). |
| 2. Direct filename upload vs S3 deploy path | **Accepted (design change)** | Switched module to the S3 `deploy/` path (added `aws_s3_object.code` + `code_bucket` var; root passes `module.s3_archive.bucket_name`). Matches scorer/batcher, removes the 50 MB direct-upload ceiling entirely (so no zipped-size check needed), future-proofs sklearn. |
| 3. reserved_concurrent_executions = -1 | **Accept (comment already present)** | Idempotent-overwrite + at-most-once-per-edge rationale already annotated in main.tf; quota floor blocks a reservation (batcher precedent). |
| 4. depends_on / log-group race | **Accept, no change** | Reviewer confirmed creation order is correct. |
| 5. env known-after-apply in plan | **Accept, no change** | Standard Terraform behavior (SNS ARN known-after-apply); not masking the static vars; identical to scorer. |
| 6. Teardown completeness | **Accept, no change (by design)** | Sweep is verify-don't-delete: `terraform destroy` removes rule+target+permission; sweep FAILS loudly on residue. Manual remove-targets/delete-rule declined — would diverge from the batcher rule's identical assert-absence handling. |
| extras: numpy pin / mem+timeout / sns dep / retention | **Verified, no change** | numpy==2.4.6 matches the scorer's lambda_requirements.txt; 256MB/30s defaults present + plan-confirmed; module.sns dep tracked via topic_arn ref; retention_in_days=7 set. |
