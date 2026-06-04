# Session 2026-06-04 — dashboards — adapter-contract (+ teardown catch-up)

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** reviewer model from the cascade (see response file footer for provider + model; ADR 0011)
- **Context loaded:** `_global`, `dashboards`, `_interfaces`, **Tier 2b** (`shared/{features,score,drift}.py` + full ADR 0005 + enforcement test names), ADRs 0009/0010/0012/0013, `context/infra.md`, HANDOFF §6
- **Duration:** ~1 session

## Intent
Resolve HANDOFF §6 Q1 (Grafana ↔ DynamoDB adapter contract — ADR 0014) and ship the adapter end-to-end (handler + moto tests + Terraform module + build script); close the carried `aws_teardown.sh` gap from infra #1 BEFORE any first apply; land the queued `shared/{score,drift}.py` present-tense docstring fix.

## What changed
- **NEW `scripts/aws_teardown.sh`** — `terraform destroy` wrapper + AWS CLI verification sweep asserting ABSENCE of every resource both infra sessions create (table, topic+subscription, both Lambdas, both log groups, Function URL, IoT rule, both IAM roles) + $1/$5 budget-alert posture check (absence = FAIL). `--verify-only`/`--destroy-only` modes. Syntax-verified; AWS-touching paths can only run PO-side.
- **NEW `docs/adr/0014-grafana-adapter-api-contract.md`** — the session's ADR (see Decisions).
- **NEW `dashboards_adapter/`** — `handler.py` (read-only fleet snapshot; one `BatchGetItem`; envelope + flat per-pump array; literal alert passthrough; FLEET_SIZE 1..99 fail-fast) + `tests/` (17 moto-backed tests mirroring the lambda_scorer conftest discipline: reload-inside-moto + credentials guard).
- **NEW `infra/modules/dashboards_adapter/`** — self-contained module: Lambda (128 MB/5 s, reserved concurrency 5 — post-review hardening, see dispositions), Function URL AuthType=NONE + public-invoke permission, own IAM role (`dynamodb:BatchGetItem` on table ARN ONLY; logs scoped to own group), Terraform-managed log group, archive_file packaging.
- **NEW `scripts/build_adapter.{sh,ps1}`** — staging sibling (copy + strip + no-shared-import smoke check). Sibling, not extension: adapter has zero third-party deps; coupling it to the scorer's pip/Docker build bought nothing.
- **Modified `infra/{main,variables,outputs}.tf`** — adapter module wiring, `adapter_function_name`/`fleet_size` vars, `adapter_function_url` output. Run order now includes build_adapter before plan.
- **Modified `shared/{score,drift}.py`** — three "the future ``lambda_scorer``" docstring references → present tense (parity-touching; bash-side python rewrite; structural-parity tests green).
- **Modified `requirements.txt`** — carried item closed: boto3 comment no longer claims it ships in the deploy zip (it's runtime-provided per infra #1); now also names the adapter as a consumer.
- **Modified `context/{dashboards,infra,_interfaces}.md`** — adapter contract from TBD → resolved; teardown documented; run order updated.

## Decisions
- **ADR 0014** (PO calls 2026-06-04, all four resolved via structured question): (1) response = JSON envelope `{fleet_size, pumps_reporting, as_of, pumps[]}` with FLAT per-pump objects whose `psi_<feature>` keys mirror the ADR 0005 §3 InfluxDB field names — panel mode-symmetry by construction; (2) plugin = **Infinity** (signed, maintained, native SigV4 → IAM upgrade is config-only) over the HANDOFF-sketched JSON API plugin; (3) Function URL **AuthType=NONE** — public-with-obscurity accepted for read-only synthetic data bounded by the apply→teardown lifecycle; (4) missing STATE rows **omitted** + `pumps_reporting` count (no null-filled placeholders).
- **Adapter stays OUTSIDE the ADR 0005 parity set** — no `shared/` import (it computes nothing); pinned by `test_adapter_does_not_import_shared` (the inverse of the structural-parity tests) and by the IAM policy granting BatchGetItem only.
- **Teardown failure semantics:** resource residue = FAIL (exit 1), unconfirmed SNS subscription = WARN (undeletable; AWS expires ~3 days), missing budget alert = FAIL (cost blind spot).

## Trade-offs surfaced
- **500-over-partial on persistent `UnprocessedKeys`:** a silently short pump list is indistinguishable from "pump not scored yet" — refusing to serve beats quietly lying. Cost: a theoretical availability dent at 15 keys ≈ 6 KB.
- **`FLEET_SIZE` env var duplicates the simulator fleet size** (ADR 0014 §Consequences) — accepted; `Scan`-discovery would obliterate the ADR 0013 cost math, hardcoding is worse.
- **Wire `null` vs storage-absent for `last_alert_sent_at`:** two representations of one fact, each idiomatic for its medium; stable key set wins for Grafana column inference.

## Reviewer feedback highlights
- Provenance: **groq (`llama-3.3-70b-versatile`)**, 2026-06-04 10:27 (footer on the response file). Per ADR 0011 weighting, Llama runs agreeable — points were pushed on, not rubber-stamped in reverse.
- **Q1 public-URL flood billing → code change:** `reserved_concurrent_executions = 5` added to the adapter module (new `reserved_concurrency` var) — caps worst-case anonymous-invocation spend + table read pressure. WAF/throttle-detection rejected (costs real money; concurrency cap + budget alerts + teardown lifecycle bound it).
- **Q6 Infinity null coercion → deferred with tracking:** verify `last_alert_sent_at` mixed null/ISO-string column inference when the panels exist — added to `context/dashboards.md` §Open questions.
- **Q2 (500-over-partial), Q4 (teardown coverage), Q5 (parity boundary) endorsed — no changes.** Q3 (grep threshold test) acknowledged as tripwire-not-proof; the IAM single-action policy is the structural guard. Full dispositions: packet §Resolution.
- Packet: `review_packets/2026-06-04-dashboards-adapter-contract.md`; response: `review_responses/2026-06-04-dashboards-adapter-contract.md`.

## State at end of session
- **Tests: 386 passed + 1 skipped** (baseline 369+1 + 17 new adapter tests). Structural-parity tests green post-docstring-fix. (Sandbox runs py3.10/sklearn-1.7.2 — version-skew warning on unpickle is environmental; PO-native 3.12 is canonical.)
- **Terraform validate + plan: PO-side, pending** — run `build_lambda.ps1` AND `build_adapter.ps1` before plan.
- **Stretch not reached:** IoT Rule `error_action` — stays in `context/infra.md` §Open questions.
- **Process findings for PO attention:**
  1. **Commit-subject BOM:** all three recent commit subjects begin with a UTF-8 BOM (`﻿infra:` …) — the DEV_NORMS §7 `Out-File -Encoding utf8` step writes a BOM under Windows PowerShell 5. Recommend amending §7 to `[IO.File]::WriteAllText(...)` (the build scripts already use this for exactly this reason). This session's commit sequence below uses the BOM-free form.
  2. **Branch naming:** work is landing on `drift/real-psi-and-cadence`, which now carries drift+scorer+housekeeping+infra+dashboards. Suggest merging to `main` (or renaming) before the cold-path session.
  3. **FUSE (memory updated):** Claude-side `Write`-tool overwrites of existing D:\ files land correctly on Windows but read stale-truncated in-sandbox — review-packet diffs must be generated PO-side whenever overwritten files are in the diff (this session qualifies).
- **Next session (PO call 2026-06-04): cold path** (`s3_archive` + `glue_catalog` + `lambda_s3_batcher`); brief prepared at `docs/next_session_brief.md`. IoT `error_action` promoted to in-scope there (deferred twice).

## Commit draft (stage AFTER cascade dispositions fold in — DEV_NORMS §7)

Subject: `dashboards: add fleet-snapshot adapter + aws_teardown.sh (ADR 0014)`

Body:
```
Resolve HANDOFF §6 Q1: read-only Lambda behind a public Function URL
serves the fleet's 15 STATE rows to Grafana's Infinity plugin via one
BatchGetItem per refresh. PSI keys flatten to the ADR 0005 InfluxDB
field names so panels are mode-symmetric; alert_flag +
last_alert_sent_at pass through literally (ADR 0012 2C). AuthType=NONE
is a recorded PO call (ADR 0014 §Alt 3) — synthetic read-only data,
URL dies at teardown; IAM/SigV4 upgrade is config-only.

Close the carried infra #1 gap: aws_teardown.sh destroys + PROVES
absence of every resource both infra sessions create, and re-checks
the $1/$5 budget alerts. Required before any first apply.

Also: shared/{score,drift}.py docstrings to present tense (parity
tests green; 386+1), stale boto3-in-zip comment fixed in
requirements.txt. Post-review hardening (groq cascade, dispositions
in the session log + packet §Resolution): reserved_concurrency=5 caps
the public URL's worst-case spend.
```

PowerShell sequence (BOM-free variant — note the WriteAllText swap vs DEV_NORMS §7):
```powershell
git status ; git diff --stat
git add -A
git status ; git diff --cached --name-status
$msg = @'
dashboards: add fleet-snapshot adapter + aws_teardown.sh (ADR 0014)

<body as above>
'@
[IO.File]::WriteAllText("$env:TEMP\commit-msg.txt", $msg)  # UTF-8 no BOM — fixes the ﻿-prefix on recent subjects
git commit -F "$env:TEMP\commit-msg.txt"
git log -1 --stat
```
