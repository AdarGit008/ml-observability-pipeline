# Next session brief — first live AWS end-to-end apply (+ cold-start canary)

## Goal
Run the full AWS demo path for real, once: `terraform apply` → simulator
aws-mode (15 pumps) → hot path scores → Grafana `aws.json` renders →
cold path archives → `aws_teardown.sh` proves absence. Close every
"verify at first apply" item the dry sessions accumulated, measure the
scorer's cold start, and record what reality disagreed with.

## NOT a parity-set session
Live verification + fixes. If any fix reaches into `shared/`, STOP and
re-brief per DEV_NORMS §5 (Tier 2b loads).

## Hard preconditions (before any AWS call)
1. **Dashboards #2 commit landed FIRST** (explicit-path staging — its
   draft is in `docs/sessions/2026-06-04-dashboards-grafana-json-pair.md`),
   then the iot-fleet commit (draft in
   `docs/sessions/2026-06-07-simulator-iot-fleet-provisioning.md`).
   Both cascades run + dispositions folded pre-commit (DEV_NORMS §7).
2. iot-fleet PO-side checks green: `terraform init` (new local/http
   providers), all THREE build scripts, `terraform validate` + `plan`.
3. One-time: Console-provisioned P-00 deleted (runbook §0).

## In-scope (in order)
1. Walk `docs/runbooks/aws-demo-day.md` end-to-end, PO driving, Claude
   navigating + debugging. Sandbox makes NO live AWS calls — PO pastes
   outputs/errors.
2. Close the verify-don't-assume items where they live:
   `context/dashboards.md` §Open questions (Infinity relative-URL vs
   base; `null last_alert_sent_at` rendering) + `pumps_reporting` → 15.
3. Cold-start canary: first-invocation vs warm duration from the
   scorer's log group; record in `context/lambda_scorer.md` open items.
4. Watch the cost surfaces live: DynamoDB consumed units vs ADR 0013's
   math, one Parquet/min in S3 (ADR 0015), IoT free-tier meter (ADR
   0016). Record actuals vs predictions in the session log.
5. `aws_teardown.sh` full run — sweep must exit 0 (first exercise of
   the iot-fleet sweep against real residue).
6. Session log + any fix diffs → cascade → dispositions → commit
   (normal §7 sequence; no deferral expected this time).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §7 + §8.
- Tier 2: `context/infra.md`.
- Tier 3: `context/_interfaces.md` (only if a wire shape misbehaves),
  `context/dashboards.md` (§Open questions), `context/simulator.md`
  (§AWS-mode), `docs/runbooks/aws-demo-day.md` (the script).
- ADRs: 0013/0015/0016 (cost predictions to check against actuals),
  0014 (adapter contract if panels misrender).
- Memory: fuse-write-truncation, git-on-windows, infra-sessions.

## Constraints
- $0 posture: this demo SPENDS the ADR 0013 dimes — bound the run
  (~30 min target), teardown immediately, no second apply without
  reason. Budget alerts must stay green.
- Terraform/AWS CLI/git all PO-side. Bash 45 s cap. FUSE rules
  (existing files bash-rewrite; NEW via Write; never Edit on D:\).
  BOM-free commit sequence.

## Definition of done
- End-to-end observed: pumps publish → scores in DynamoDB → aws.json
  panels live → Parquet accumulating → teardown sweep exits 0.
- Dashboards + cold-start open items closed in their context files;
  actual-vs-predicted costs logged.
- Session log + cascade + dispositions + commit landed.
- Close with AskUserQuestion: next focus (candidates: CI cost
  guardrails; README/portfolio polish; demo-day rehearsal script).

## Carried context
- Suite baseline: **427 passed + 1 skipped** (iot-fleet session).
- Fixes discovered live are in-scope if small; anything structural
  becomes its own brief.
- WATERMARK + STATE reserved-SK coexistence stands (`_interfaces.md`).
