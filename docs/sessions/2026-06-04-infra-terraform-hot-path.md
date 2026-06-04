# Session 2026-06-04 — infra — terraform-hot-path

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** reviewer model from the cascade (see response file footer; ADR 0011)
- **Context loaded:** `_global`, DEV_NORMS §7–8, `infra`, `_interfaces`, ADR 0010, ADR 0012, ADR 0006 §Q4, ADR 0005 §Addendum Q1, `lambda_scorer` (interfaces/env-vars only — infra is NOT in the parity set; the build script COPIES `shared/`, never imports it)
- **Duration:** ~1 session

## Intent
Stand up the Terraform that makes the scored hot path deployable: DynamoDB table, SNS topic + email subscription, IoT Rule → Lambda trigger, the Lambda resource, scoped IAM, and the deploy-zip build script. Gate: `terraform validate` + reviewed `plan`; NO apply.

## What changed
- `infra/` root module: local backend, aws provider `>= 5.30, < 7.0` + archive `~> 2.4`, region locked to `eu-central-1` via variable validation, `default_tags` Project tag.
- `infra/modules/dynamodb` — `pump_hot_state`, `PK=pump_id`/`SK=sk` (ADR 0010), PAY_PER_REQUEST (ADR 0013), no TTL/PITR/GSI, deletion protection off (teardown must succeed).
- `infra/modules/sns` — standard topic + email subscription to PO inbox (`terraform.tfvars`, gitignored; `.example` tracked).
- `infra/modules/iam` — execution role: `dynamodb:Query/GetItem/PutItem` on the table ARN, `sns:Publish` on the topic ARN, `logs:CreateLogStream/PutLogEvents` on this function's log group only. Log-group ARN built from strings (region + caller-identity account + function name) to break the iam↔lambda module cycle.
- `infra/modules/lambda_scorer` — python3.12 / 512 MB / 10 s / x86_64; `archive_file` zips the staged `.build/lambda_dist/`; env `DDB_TABLE_NAME` + `SNS_TOPIC_ARN`; Terraform-managed log group (7-day retention) ordered before the function.
- `infra/modules/iot_rule` — `SELECT * FROM 'factory/pumps/+/telemetry'` (raw-dict passthrough matching `_parse_event`) + `aws_lambda_permission` for `iot.amazonaws.com` scoped to the rule ARN.
- `scripts/build_lambda.{ps1,sh}` + `scripts/lambda_requirements.txt` — stage `shared/` + `lambda_scorer/` (tests stripped) + `model/artifacts/{model.pkl, operational_reference_distribution.json}` + manylinux2014_x86_64 cp312 wheels (numpy, scikit-learn, joblib; scipy transitively); strip `__pycache__`/tests; enforce 250 MB ceiling (warn ≥200); Docker (python:3.12-slim) smoke-check cold-start imports `lambda_scorer.handler` from the staged tree and asserts every `shared.*` module loads from INSIDE the dist.
- `docs/adr/0013-dynamodb-on-demand-billing.md` — new ADR.
- `context/infra.md` rewritten; `.gitignore` + `.build/`.

## Decisions
- **ADR 0013 — on-demand billing (PO call, 2026-06-04).** Provisioned-25-RCU Always-Free computed infeasible: 1800-row Query ≈ 30–35 RRU/invocation × 7.5 inv/s ≈ 220–260 RCU sustained (~9–10× over). On-demand ≈ $0.10–0.20 per 30-min demo — the project's first knowingly non-$0 line item, accepted with the literal-$0 alternative (Nth-tick PSI + ~10-pump fleet + parity-divergence ADR) evaluated and rejected.
- **boto3 NOT bundled** in the deploy zip — Lambda runtime provides it; bundling botocore would exceed the 50 MB zipped direct-upload limit. Supersedes the stale "ships in the deploy zip" comment in `requirements.txt` (fix queued for next session touching that file). Review packet Q1.
- **AWS_REGION not set on the Lambda** — reserved runtime env var; the runtime supplies `eu-central-1`. The handler default is a local-test affordance.
- **Local Terraform backend** — single PC, no S3 state-bucket cost/bootstrap.
- **No CreateLogGroup grant** — log group Terraform-managed; accidental out-of-band recreation fails loudly instead of resurrecting after teardown. Review packet Q3.

## Trade-offs surfaced
- Cents-per-demo vs redesign (ADR 0013 — the session's center of gravity).
- Smoke-check needs Docker on the PO machine (manylinux wheels can't import on Windows); structural warning + skip if absent.
- `archive_file` reads the dist at plan time → build-before-plan ordering documented in `infra/main.tf` header + `context/infra.md`.
- Provider ceiling `< 7.0` wide; `.terraform.lock.hcl` (committed) is the reproducibility anchor. Review packet Q6.

## Reviewer feedback highlights
Reviewer: **groq** (`llama-3.3-70b-versatile`), 2026-06-04 — advisory-weight response (Llama's posture is less adversarial than DeepSeek-R1's per ADR 0011 §Consequences); all six packet questions answered, no blockers, ADR 0013 math validated.

- **boto3 version skew** (monitor/pin): Rejected — runtime boto3 can't be pinned without bundling (precluded by the 50 MB limit); the four API calls used are years-stable; post-first-apply cold-start measurement doubles as the canary.
- **IAM CreateLogGroup for resilience**: Rejected — loud-failure-over-resurrection is deliberate; failure mode is lost logs, not lost scoring.
- **IoT Rule error_action**: Deferred — tracked in `context/infra.md` §Open questions for the dashboards/observability session.
- **Packaging smoke-vs-zip gap**: Addressed by construction — archive_file zips the same staged tree the smoke-check imported; build script is sole producer of both, no symlinks.
- **Provider pin tighter**: Rejected — committed `.terraform.lock.hcl` is the reproducibility pin; the range only governs explicit upgrades.

- Packet: `review_packets/2026-06-04-infra-terraform-hot-path.md`
- Response: `review_responses/2026-06-04-infra-terraform-hot-path.md` (provenance footer names provider + model)

## State at end of session
- Tests: `terraform validate` ✅ + `terraform plan` ✅ PO-side (9 resources to add, 0 change/destroy; no banned resource types; all eu-central-1). Python suite untouched: 369 passed + 1 skipped baseline carries.
- Open follow-ups: `aws_teardown.sh` does not exist in-repo (stretch not reached — next infra session, covering destroy + verification sweep); stale boto3 comment in `requirements.txt`; CI plan-check for banned resource types still TODO; cold-start latency measurement remains post-first-apply.
- `context/infra.md` updated? Yes — rewritten.

## Note for next session
Hot path is deployable on paper: build script → init → validate → plan all green; first apply is a demo-day act with teardown ready — except `aws_teardown.sh` doesn't exist yet; create it before any apply. Natural next session: dashboards adapter (consumes STATE rows via BatchGetItem; needs the Lambda Function URL module added here later) — it IS in the parity set, so Tier 2b loads apply, and it owns the queued `shared/{drift,score}.py` docstring fix ("the future lambda_scorer"). Prepared brief: `docs/next_session_brief.md` (PO call 2026-06-04 — dashboards adapter next, teardown folded in as the first-30-minutes item).
