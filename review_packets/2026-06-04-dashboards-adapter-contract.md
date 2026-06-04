# Review Packet 2026-06-04 — dashboards — adapter-contract

> Run via: `.\scripts\gemini_review.ps1 -Slug dashboards-adapter-contract`

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

(Per ADR 0011, this packet may be reviewed by any model in the cascade: Gemini, DeepSeek R1 via OpenRouter, Llama 3.3 70B via Groq, or Llama 3.3 70B via Cerebras. The role is identical across providers; the response file's footer records which one actually wrote the response.)

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost (one documented exception: ADR 0013, ~$0.15/demo).
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Decision under review: `docs/adr/0014-grafana-adapter-api-contract.md`.

## Summary of the change
This session resolves HANDOFF §6 Q1 — the Grafana ↔ DynamoDB adapter contract (ADR 0014) — and ships it: a read-only Lambda (`dashboards_adapter/handler.py`) behind a public Function URL serving the fleet's 15 STATE rows as a JSON envelope (`{fleet_size, pumps_reporting, as_of, pumps[]}`) via ONE `BatchGetItem` per Grafana panel refresh, consumed by the Infinity datasource plugin. Per-pump PSI keys flatten to the ADR 0005 §3 InfluxDB field names (`psi_vibration_amp`, …) so AWS-mode and local-mode panels share one vocabulary; `alert_flag` + `last_alert_sent_at` are literal STATE-row passthroughs (ADR 0012 §Alternatives 2C — no client-side threshold re-derivation). The session also closes the carried infra-#1 gap (`scripts/aws_teardown.sh` — destroy + CLI verification sweep + budget-alert check), adds the Terraform module (`infra/modules/dashboards_adapter` — scoped IAM: `dynamodb:BatchGetItem` on the table ARN only), a staging sibling (`scripts/build_adapter.{sh,ps1}`), and lands the queued `shared/{score,drift}.py` present-tense docstring fix (parity-touching; structural-parity tests green).

## Changed files
> Full diff available PO-side via `git diff` (sandbox FUSE caching makes in-sandbox diffs unreliable for overwritten files this session — see session log §Process findings #3).

**New:**
- `scripts/aws_teardown.sh` — destroy wrapper + absence-asserting sweep (table, topic+subscription, 2 Lambdas, 2 log groups, Function URL, IoT rule, 2 IAM roles) + $1/$5 budget posture (missing budget = FAIL; unconfirmed SNS sub = WARN).
- `docs/adr/0014-grafana-adapter-api-contract.md` — contract, plugin, auth mode, fleet-membership source; 4 alternatives sections.
- `dashboards_adapter/__init__.py`, `dashboards_adapter/handler.py` — handler: GET-only (405 otherwise), `FLEET_SIZE` env (1..99 fail-fast at cold start → `P-01..P-NN` key set), BatchGetItem with ≤3-pass UnprocessedKeys retry then 500 (never a silently short snapshot), Decimal→float, absent `last_alert_sent_at` → JSON `null`, pumps sorted by id, generic 500 bodies (public URL — internals to CloudWatch only). NO `shared/` import.
- `dashboards_adapter/tests/{conftest.py,test_adapter.py}` — 17 moto tests: envelope/projection, omit-don't-null-fill, literal alert passthrough, `test_no_threshold_logic_in_module` (asserts "0.25"/"0.7" absent from code), single-BatchGetItem spy, UnprocessedKeys retry + exhaustion, `test_adapter_does_not_import_shared` (inverse parity tripwire), FLEET_SIZE expansion + fail-fast. Conftest mirrors lambda_scorer's reload-inside-moto + credentials-guard discipline.
- `infra/modules/dashboards_adapter/{main,variables,outputs}.tf` — self-contained: own role `<fn>-exec` (BatchGetItem on table ARN ONLY; logs scoped to own group), Terraform-managed log group, Lambda 128 MB/5 s py3.12, `aws_lambda_function_url` AuthType=NONE + `aws_lambda_permission` (principal `*`, `function_url_auth_type=NONE`), archive_file on `.build/adapter_dist/`.
- `scripts/build_adapter.{sh,ps1}` — copy + strip tests/__pycache__ + smoke check (handler staged; no shared/ import). Sibling of build_lambda, not extension (zero third-party deps — boto3 runtime-provided).

**Modified:**
- `infra/main.tf` (+module wiring, run order doc), `infra/variables.tf` (+`adapter_function_name`, `fleet_size` w/ 1..99 validation), `infra/outputs.tf` (+`adapter_function_url`, `adapter_function_name`).
- `shared/score.py` (1 docstring ref), `shared/drift.py` (2 docstring refs) — "the future ``lambda_scorer``" → present tense. No code change.
- `requirements.txt` — boto3 comment: was "ships in the Lambda deploy zip" (false since infra #1); now "NOT bundled — runtime-provided" + names the adapter.
- `context/{dashboards,infra,_interfaces}.md` — TBD → resolved contract; teardown + run-order docs.

**Tests: 386 passed + 1 skipped** (baseline 369+1 + 17 new). Structural-parity tests green. `terraform validate`/`plan` pending PO-side.

## Specific questions for the reviewer
1. **Public Function URL (AuthType=NONE).** ADR 0014 §Alt 3 argues bounded blast radius: read-only synthetic data, URL exists only apply→teardown, budget alerts as backstop, IAM/SigV4 upgrade config-only. Is there an attack/abuse vector this rationalizes past (e.g., request-flood billing on Lambda invocations themselves — first 1 M/month free, but is the math actually safe under sustained abuse)?
2. **500-over-partial on persistent UnprocessedKeys.** For a dashboard, is refusing to serve better than a partial snapshot with a flag? The argument: short list is indistinguishable from "pump not scored yet". Counter-argument welcome.
3. **`test_no_threshold_logic_in_module`** greps code lines for "0.25"/"0.7". Is a string-grep structural test too brittle/too weak to pin "the adapter never re-derives thresholds", and is there a better invariant?
4. **Teardown sweep completeness.** It checks: table, topic, subscriptions, both functions, both log groups, Function URL config, IoT rule, both IAM roles, budgets. Inline IAM role policies die with their roles; archive zips are local. Does anything either Terraform root creates survive `terraform destroy` + this sweep unverified (provider default_tags artifacts, CloudWatch metrics are free/eventual, anything else)?
5. **Adapter outside the parity set** (ADR 0014 §Decision 5): no `shared/` import means no Tier 2b load for adapter-only sessions, enforced by an inverse structural test + the single-action IAM policy. Sound boundary, or does the flattening of `latest_psi` keys constitute "drift logic" that belongs behind the parity contract?
6. **Wire format:** absent-until-first-publish (storage, ADR 0012) → explicit JSON `null` (wire, ADR 0014 §Decision 2). Any Grafana/Infinity column-inference or type-coercion trap with mixed `null`/timestamp strings in one column?

## What I'm NOT looking for in this review
- Grafana dashboard JSON / panel design — later session.
- IoT Rule `error_action` — known open question, tracked in `context/infra.md`.
- Style/formatting; the docstring fix is comment-only by design.

## Resolution (2026-06-04, post-cascade)

Response: `review_responses/2026-06-04-dashboards-adapter-contract.md` — **groq (`llama-3.3-70b-versatile`)**. Weighting note per ADR 0011: Llama's posture runs agreeable; points were pushed on rather than taken at face value.

1. **Public URL / flood billing — ACCEPTED IN PART (code change).** The valid kernel: unbounded anonymous invocations. Fix landed: `reserved_concurrent_executions = 5` on the adapter (new `reserved_concurrency` variable) — caps worst-case Lambda spend AND table read pressure at five containers; one Grafana instance needs ~1. Request-level throttle/WAF detection REJECTED for the demo: the concurrency cap + $1/$5 budget alerts + apply→teardown lifecycle already bound the exposure; WAF costs real money (north star #1).
2. **500-over-partial — ACCEPTED AS-STATED (no change).** Reviewer concurred with caveats. The ≤3-pass retry handles transients; persistent UnprocessedKeys on a 6 KB read means something is genuinely wrong — serving stale-looking partial data to an ops dashboard is the worse failure.
3. **Grep-based threshold test — PARTIALLY ACCEPTED (no code change).** Agreed it's a tripwire, not a proof; the STRUCTURAL guard is the IAM policy (BatchGetItem only — the adapter can't even read what it would need to re-derive trends) plus `test_adapter_does_not_import_shared`. The reviewer's "check operation types" alternative is heavier than the property it pins. Documented as a known limitation here.
4. **Teardown completeness — ADDRESSED AS-NOTED (no change).** CloudWatch log GROUPS are swept (both functions). CloudWatch METRICS are not deletable by anyone — they age out; free at this volume. No VPC config → no orphaned ENIs. Inline role policies die with their roles (`aws iam get-role` absence covers both).
5. **Parity boundary — ENDORSED (no change).** Reviewer agreed key-flattening is projection, not drift logic.
6. **Infinity null coercion — ACCEPTED, DEFERRED with tracking.** Verify-don't-assume landed as an open question in `context/dashboards.md` for the Grafana session (the panels don't exist yet to test against).
