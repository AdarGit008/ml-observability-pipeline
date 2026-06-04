# Next session brief — dashboards: Grafana → DynamoDB adapter (+ teardown catch-up)

## Goal
Resolve HANDOFF §6 Q1 (the Grafana adapter API contract — the session's ADR-worthy decision) and ship the adapter: a read-only Lambda behind a Function URL that serves the fleet's latest snapshot to a Grafana JSON datasource, consuming the 15 STATE rows via one `BatchGetItem` per panel refresh. Alert surfacing reads `alert_flag` + `last_alert_sent_at` literally — NO client-side threshold re-derivation (ADR 0012 §Alternatives 2C).

## ⚠ Parity-set session — Tier 2b loads are MANDATORY
`dashboards` is in the ADR 0005 parity set, and this session also owns the queued `shared/{drift,score}.py` docstring fix ("the future lambda_scorer" → present tense). Per DEV_NORMS §5 Tier 2b the brief MUST load: `shared/features.py` + `shared/score.py` + `shared/drift.py` (read, don't re-derive), the FULL ADR 0005, and cite the enforcement tests (`local_runtime/tests/test_service.py::test_structural_parity_no_vendoring` + siblings; `lambda_scorer/tests/test_handler.py::test_structural_parity_*`). Claude: refuse to start if these aren't loaded.

## How to start — plain-language walkthrough FIRST
Same rule as always: walk PO through the planned pieces + open Qs in plain language, one paragraph each, BEFORE any code. AskUserQuestion for the PO calls (the API contract shape is the big one).

## In-scope (in order)
1. **`aws_teardown.sh` (first 30 minutes — carried gap from infra #1).** Must exist before ANY first apply. `terraform destroy` wrapper + verification sweep covering: DynamoDB table, SNS topic+subscription, Lambda(s), log groups, IoT rule, IAM roles/policies — and the adapter resources this session adds. Budget-alert posture re-check ($1/$5).
2. **Adapter API contract (ADR).** Resolve HANDOFF §6 Q1: endpoint shape, response JSON for the fleet snapshot (15 × STATE row projection), how `alert_flag`/`last_alert_sent_at` surface. PO call on the Grafana datasource plugin (JSON API plugin is the leader).
3. **Adapter Lambda.** New small handler (separate function from the scorer — read-only, no shared/ imports expected; if it ends up importing shared/, it joins the structural-parity test surface). moto-backed tests for the BatchGetItem path.
4. **Terraform: `infra/modules/dashboards_adapter`.** Function URL (auth mode: PO call — public-with-obscurity vs IAM), scoped IAM (`dynamodb:BatchGetItem` on the table ARN only), log group, build-script extension or sibling. Validate+plan PO-side, NO apply.
5. **`shared/{drift,score}.py` docstring fix** (parity-touching; tests must stay green — 369+1 baseline).
6. **(Stretch) IoT Rule `error_action`** — republish-to-error-topic, deferred here by the 2026-06-04 cascade (tracked in `context/infra.md`).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §7 + §8.
- Tier 2: `context/dashboards.md`.
- **Tier 2b (parity):** `shared/{features,score,drift}.py` + ADR 0005 (full) + enforcement test names above.
- Tier 3: `context/_interfaces.md` (§DynamoDB schema STATE row, §Grafana → DynamoDB adapter).
- ADRs: 0009 (4-key PSI surface in `latest_psi`), 0010 (BatchGetItem access pattern), 0012 (two-attribute alert state — the adapter is its second consumer), 0013 (cost posture for added reads: 15-key BatchGetItem per refresh is noise).
- Memory: fuse-write-truncation, git-on-windows, infra-session1 (terraform PO-side; boto3-not-bundled; build-before-plan).

## Constraints
- $0 posture per ADR 0013: Function URL is free; adapter reads are ~7 RRU per panel refresh — negligible. No apply in-session.
- FUSE: single complete write → verify per file; bash-side heredoc/python full-rewrite for changes; rm on D:\ blocked.
- Bash 45 s cap. Terraform + git PO-side.
- Commit AFTER the review cascade per DEV_NORMS §7.

## Definition of done
- `aws_teardown.sh` exists, covers every resource both infra sessions create.
- Adapter contract ADR accepted; `context/dashboards.md` + `_interfaces.md` §Grafana adapter updated from TBD.
- Adapter tests green; full suite ≥ 369+1 with parity tests passing post-docstring-fix.
- `terraform validate` + reviewed `plan` green (adapter module included).
- Session log + review packet → cascade → dispositions → commit draft, in that order.
- Close with AskUserQuestion: next-session focus (cold path: s3_archive + glue_catalog + batcher is the natural follow-on) + prepared brief.

## Carried context
- Suite baseline: 369 passed + 1 skipped. Committed `model/artifacts/*` = PO-native canonical; don't rebuild.
- infra #1 commit (`infra-terraform-hot-path`) assumed landed — verify `git log` shows it before starting.
- Cold-start latency measurement remains post-first-apply (now doubles as the boto3-runtime-version canary per the 2026-06-04 review disposition).
- `requirements.txt` stale comment ("boto3 ships in the deploy zip") — fix opportunistically in any session that touches that file.
