# Review Packet 2026-06-04 — infra — terraform-hot-path

> Run via: `.\scripts\gemini_review.ps1 -Slug infra-terraform-hot-path`

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

(Per ADR 0011, this packet may be reviewed by any model in the cascade: Gemini, DeepSeek R1 via OpenRouter, Llama 3.3 70B via Groq, or Llama 3.3 70B via Cerebras. The role is identical across providers; the response file's footer records which one actually wrote the response.)

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`.

## Summary of the change
IaC session #1: Terraform for the scored hot path. New `infra/` root module (local backend, provider pinned `>= 5.30, < 7.0`, region locked to `eu-central-1` by variable validation) wiring five modules: `dynamodb` (`pump_hot_state` per ADR 0010, billing PAY_PER_REQUEST per new ADR 0013), `sns` (alert topic + PO email subscription, ARN → Lambda env), `iam` (execution role scoped to exactly the four hot-path DynamoDB ops on the table ARN, `sns:Publish` on the topic ARN, logs on this function's log group only — no CreateLogGroup), `lambda_scorer` (python3.12 / 512 MB / 10 s / x86_64, `archive_file` over a build-script-staged tree per ADR 0005 Addendum Q1, Terraform-managed log group with 7-day retention), `iot_rule` (`SELECT * FROM 'factory/pumps/+/telemetry'` → Lambda + invoke permission). Companion `scripts/build_lambda.{ps1,sh}` + `scripts/lambda_requirements.txt` stage `shared/` + `lambda_scorer/` (tests stripped) + `model/artifacts/` + manylinux2014_x86_64 cp312 wheels into `.build/lambda_dist/`, enforce the ADR 0006 §Q4 footprint ceiling, and smoke-check the staged tree in Docker (python:3.12-slim cold-start import of `lambda_scorer.handler`, asserting every `shared.*` module physically loads from inside the dist). `terraform validate` + `plan` green PO-side (9 resources, no banned types). No apply — first deploy is a demo-day act.

## Changed files (no diff — all files are new; read them directly)
- `infra/{versions,variables,main,outputs}.tf`, `infra/terraform.tfvars.example` (real tfvars gitignored)
- `infra/modules/{dynamodb,sns,iam,lambda_scorer,iot_rule}/{main,variables,outputs}.tf`
- `scripts/build_lambda.ps1`, `scripts/build_lambda.sh`, `scripts/lambda_requirements.txt`
- `docs/adr/0013-dynamodb-on-demand-billing.md`
- `context/infra.md` (rewritten), `.gitignore` (+`.build/`)

## Decisions already PO-ratified (challenge the execution, not the call)
- **ADR 0013, on-demand billing:** provisioned-25-RCU computed infeasible (1800-row Query ≈ 30–35 RRU × 7.5 inv/s ≈ 220–260 RCU sustained, 9–10× over Always-Free). ~$0.10–0.20/demo accepted by PO with the literal-$0 redesign chain (Nth-tick PSI + ~10-pump fleet) evaluated and rejected. The math is in the ADR — check it.
- **Walkthrough-first, validate/plan-only session; teardown stretch not reached** (recorded in `context/infra.md` open questions).

## Specific questions for the reviewer
1. **boto3 exclusion.** The zip relies on the Lambda-runtime-provided boto3 (bundling botocore would exceed the 50 MB zipped direct-upload limit), accepting runtime-version skew risk against `boto3>=1.34`. A passing comment in `requirements.txt` ("ships in the Lambda deploy zip") implied the opposite; that comment is now stale. Is runtime-provided boto3 the right call here, and is version skew a real risk for `Table.query/put_item/get_item` + `sns.publish`?
2. **ADR 0013 read math.** Reading row ~130–150 B → 1800-row eventually-consistent Query ≈ 30–35 RRU. Spot any error (item-size estimate, RRU rounding semantics per 4 KB block, Query vs per-item accounting) that would change the provisioned-infeasible conclusion or the per-demo cost band?
3. **IAM minimality vs fragility.** No CreateLogGroup (group is Terraform-managed; `depends_on` orders it before the function). Failure mode if the group is deleted out-of-band mid-demo: log writes fail (function still runs?). Acceptable, or grant CreateLogGroup for resilience?
4. **IoT Rule error handling.** No `error_action` on the rule. A throttled/failed Lambda invoke at demo scale is retried per IoT semantics and then dropped silently. Worth a republish-to-error-topic action now, or YAGNI until the dashboards session?
5. **Packaging integrity.** The smoke-check imports the handler from the staged tree (cold-start eager-load incl. reference/model version match) but the *zip* itself is produced separately by `archive_file` at plan time. Any gap where dist passes smoke but the zip diverges (file modes, symlinks, archive_file determinism)?
6. **Provider ceiling `< 7.0`.** Wide pin to avoid 6.x-only syntax assumptions; `.terraform.lock.hcl` (committed per .gitignore policy) pins the actual resolved version. Sane, or should the floor/ceiling be tighter for reproducibility?

## What I'm NOT looking for in this review
- The billing-mode *choice* itself — PO-ratified with the math (ADR 0013); execution/math errors in it ARE in scope.
- s3_archive / glue_catalog / batcher modules — later sessions.
- Style/formatting; `terraform fmt` conventions.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. boto3 version skew (monitor / pin minor) | Rejected — no change | The runtime's boto3 cannot be pinned without bundling, which the 50 MB direct-upload limit precludes (the very reason for exclusion). The four calls used (`Table.query/put_item/get_item`, `sns.publish`) have been API-stable across boto3 1.x for years. Post-first-apply cold-start measurement (already a tracked follow-up) doubles as the compatibility canary. |
| 2. ADR 0013 math | Validated | "No apparent errors found." No change. |
| 3. IAM: grant CreateLogGroup for resilience | Rejected — keep minimal | Single-operator project; out-of-band log-group deletion mid-demo is a self-inflicted act. Failure mode is lost logs, not lost scoring (invocations succeed; log delivery errors). Loud-failure-over-resurrection posture is deliberate (module comment documents it). |
| 4. IoT Rule error_action | Deferred — tracked | Real at production scale; at demo scale a dropped invoke surfaces as a gap in the Grafana panel within seconds. Added to `context/infra.md` §Open questions for the dashboards/observability session. |
| 5. Packaging integrity (smoke vs zip gap) | Addressed by construction — documented | `archive_file` zips the *same staged tree* the smoke-check imported; the build script is the sole producer of both and produces no symlinks (cp -r + pip --target write real files). Residual risk is rebuilding dist between smoke and plan — the script runs both, in order, every time. Noted in session log. |
| 6. Provider pin tighter | Rejected — lockfile is the pin | `.terraform.lock.hcl` (committed per .gitignore policy) pins the exact resolved provider version; the wide range only governs *upgrades*, which are explicit `terraform init -upgrade` acts. Tightening the range duplicates the lockfile's job. |

---
Reviewed by **groq** (`llama-3.3-70b-versatile`), 2026-06-04 — see response file footer.
