# Next session brief — cold path: s3_archive + glue_catalog + lambda_s3_batcher

## Goal
Complete the AWS data plane's cold path: an EventBridge-scheduled batcher Lambda drains recent reading rows to Parquet in S3 (layout per `_interfaces.md §S3 archive layout`), with the Glue Catalog table defined in Terraform (NO Crawler — anti-pattern list). Resolve the session's ADR-worthy decisions: batch read pattern, Parquet engine (footprint!), and EventBridge cadence (HANDOFF §6 Q6 — 60 s default).

## NOT a parity-set session
`lambda_s3_batcher` is not in the ADR 0005 parity set and must NOT import `shared/` (it moves rows; it computes nothing — same posture as the dashboards adapter, ADR 0014 §Decision 5). If the design drifts toward calling `extract_features`/`score`/`compute_psi`, STOP: that's a parity-set change requiring Tier 2b loads + DEV_NORMS §5 list update.

## How to start — plain-language walkthrough FIRST
Same rule as always: walk PO through the planned pieces + open Qs in plain language, one paragraph each, BEFORE any code. AskUserQuestion for the PO calls.

## Open questions to resolve (the ADR surface)
1. **Batch read pattern.** How does the batcher find "rows since last batch"? Options: per-pump `Query(SK between last_watermark and now)` ×15 (cheap, needs a watermark row — a second reserved SK joins `"STATE"`); DynamoDB Streams → buffer (more moving parts, but no re-read); full recent-window Scan (cost math per ADR 0013 likely kills it — compute it). Watermark + per-pump Query is the expected leader; ADR it.
2. **Parquet engine.** pyarrow is ~100 MB unzipped — check against the batcher's OWN zip (it doesn't carry sklearn, so likely fine under 250 MB; ADR 0006 §Q4 method). Alternatives: awswrangler (heavier), fastparquet (+pandas), CSV-instead (weaker portfolio signal — Parquet is named in PLAN).
3. **EventBridge cadence** (HANDOFF §6 Q6): 60 s default (~7.5 K records/file) vs 5 min. Data-loss window vs invocation count (both free at this scale — decide on demo-story grounds).
4. **Glue table:** sub-module default (per `context/infra.md` open question).
5. **S3 bucket teardown:** `terraform destroy` fails on non-empty buckets — `force_destroy = true` (demo posture) vs sweep-side `aws s3 rm --recursive`. Decide + extend `aws_teardown.sh` EITHER way (bucket + table absence checks).

## In-scope (in order)
1. Walkthrough + PO calls (above).
2. ADR: batch read pattern + Parquet engine + cadence (one ADR, three locked knobs — or split if the trade-offs sprawl).
3. `lambda_s3_batcher/` handler + moto tests (S3 + DynamoDB mocked; watermark mechanics; empty-batch no-op; Parquet round-trip read-back assert).
4. Terraform: `infra/modules/{s3_archive,glue_catalog,lambda_s3_batcher}` + root wiring + EventBridge schedule + scoped IAM (Query on table, PutObject on bucket prefix — nothing wildcarded). Build-script sibling if pyarrow bundling is needed.
5. Extend `scripts/aws_teardown.sh`: bucket (empty-then-absent), Glue database/table, batcher Lambda + log group + role + EventBridge rule.
6. **IoT Rule `error_action`** (republish-to-error-topic) — IN-SCOPE this time, not stretch: deferred twice (infra #1 → dashboards), tracked in `context/infra.md`.
7. Update `context/{lambda_s3_batcher,infra}.md` + `_interfaces.md` (watermark row if adopted joins §DynamoDB schema; reserved-SK coexistence note per ADR 0010).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §7 + §8.
- Tier 2: `context/lambda_s3_batcher.md`.
- Tier 3: `context/_interfaces.md` (§DynamoDB schema incl. reserved SK rule, §S3 archive layout).
- ADRs: 0010 (schema + reserved-SK coexistence for any watermark row), 0013 (cost math method for the read pattern), 0014 (the "projection not brain" posture + outside-parity-set precedent), 0006 §Q4 (zip footprint method).
- Memory: fuse-write-truncation (NOTE: Write-tool overwrites of existing files read stale in-sandbox — bash-side python rewrite for anything the sandbox re-reads; review-packet diffs PO-side), git-on-windows, infra-session1.

## Constraints
- $0: S3 Always-Free 5 GB / Glue free tier / EventBridge free — verify each in-session; any new exception needs an ADR like 0013.
- FUSE: NEW files via Write are safe; existing-file changes bash-side python rewrite; `rm` on D:\ blocked; verify every file (`grep -c ''` sandbox + `Read` Windows-side).
- Bash 45 s cap. Terraform + git PO-side. No apply in-session.
- Commit AFTER the review cascade per DEV_NORMS §7 — use the BOM-free `[IO.File]::WriteAllText` commit sequence (2026-06-04 session log; pending DEV_NORMS §7 amendment).

## Definition of done
- ADR accepted; batcher tests green; full suite ≥ 386+1.
- `terraform validate` + reviewed `plan` green (all three new modules + error_action).
- `aws_teardown.sh` covers every cold-path resource.
- Session log + review packet → cascade → dispositions → commit draft, in that order.
- Close with AskUserQuestion: next-session focus (Grafana dashboards JSON pair is the natural follow-on — it closes the dashboards component; alternatives: simulator IoT Thing/cert provisioning, CI) + prepared brief.

## Carried context
- Suite baseline: 386 passed + 1 skipped. Committed `model/artifacts/*` = PO-native canonical; don't rebuild.
- Verify the dashboards commit (`dashboards: add fleet-snapshot adapter…`) landed before starting (`git log --oneline -3`) — and check its subject has NO BOM (first post-fix commit).
- Branch posture: PO was advised to merge `drift/real-psi-and-cadence` → `main` (or rename); check where HEAD sits.
- PO-side validate+plan for the dashboards session may still be pending — if so, run both build scripts + validate + plan for the EXISTING stack before adding cold-path modules.
- Cold-start latency measurement remains post-first-apply (boto3-runtime-version canary).
