# Session log — 2026-06-07 — infra: first live AWS end-to-end apply

## Outcome
End-to-end OBSERVED on real AWS for the first time: 15 pumps (mTLS,
per-Thing identity) → IoT rule → pump-scorer → DynamoDB `pump_hot_state`
→ adapter Function URL snapshot → cold path draining one Parquet/min to
S3. Teardown run at session end — **clean (sweep exit 0, all clear)**.
Session split across two days by PO call: live run + teardown today;
context-file closures, cascade, dispositions, commit tomorrow.

## Pre-flight surprises (before any AWS call)
1. **main was wrong.** The two 2026-06-07 commits (dashboards #2
   `9463f82`, iot-fleet `4f402c9`) sat unmerged on
   `dashboards/grafana-json-pair`; main instead carried `87192dc` — a
   parallel dashboards implementation authored `root@AdarHermes.VPS`,
   pulled onto main by fast-forward, unintended (PO confirmed: own VPS,
   push not meant to land). Resolution: revert `7396246` + merge
   `704575a` (merge tree byte-identical to branch tree). The newer
   uncommitted session brief was lost in a botched stash restore and
   reconstructed byte-identically (4,221 bytes) from the session-start
   read.
2. **Console-P-00 residue was real** — `describe-thing` found the
   2026-05-27 smoke Thing; deleted via CLI (cert detach/deactivate/
   delete, `pump-P-00-policy` delete, thing delete) to NotFound.
3. Both PO branch commit messages carry a leading BOM (U+FEFF) —
   BOM-free rule violated at the `-m`-from-file step; cosmetic, in
   history now. Type `-m` inline going forward.

## Live-apply lessons (the dry runs couldn't catch these)
1. **Lambda reserved concurrency vs new-account quota.** Account
   concurrency floor (min 10 unreserved) rejects ANY reservation:
   adapter `reserved_concurrency` 5 → -1 (module default), batcher
   hardcoded 1 → -1 (comments updated in both modules; restore after a
   Service Quotas bump — open item).
2. **`aws_s3_object` + `etag = filemd5` is broken for zips >5 MB.**
   Provider uploads multipart → S3 ETag is `<hash>-N` ≠ MD5 → phantom
   diff → re-upload on EVERY apply. Combined with flaky local DNS
   (repeated `no such host` on the bucket host mid-multipart), this
   looped four applies. Unblock: `aws s3api put-object` (always single
   PUT, ETag = MD5) for both zips, `terraform import` for the scorer
   object, refresh reconciled the batcher. **Open item: switch both
   modules to `source_hash` (multipart-safe) — forces one re-upload, do
   it next session, not mid-demo.**
3. **Adapter off-by-one (the `pumps_reporting: 14` mystery).**
   `FLEET_PUMP_IDS` enumerated `P-01..P-15` (1-indexed assumption from
   the 2026-06-04 adapter session); the real fleet is `P-00..P-14`
   (terraform `count.index`, simulator, scorer). Adapter queried a
   nonexistent P-15 and never asked for P-00 — while P-00 had 186
   scored rows in DynamoDB. Fixed in `dashboards_adapter/handler.py` +
   7 test patches; **17/17 adapter tests pass** (sandbox). NOT
   redeployed before teardown — `pumps_reporting == 15` live check
   carries to the next apply (rehearsal).
4. **PSI warmup alert storm.** On `healthy`, 9/14 pumps showed
   `alert_flag: true` within the first minute — exact correlation with
   max-PSI > 0.25 on sub-minute sample windows (scores all ≤0.02, far
   below the 0.7 score threshold). Every pump had `last_alert_sent_at`
   set ≈ immediately. Edge-triggered SNS fired fleet-wide on PSI noise.
   **Open item (drift/lambda_scorer): min-sample warmup gate before PSI
   alerts arm.** Side effect: the `null last_alert_sent_at` Grafana
   rendering question could not be observed live (nothing stayed null);
   close from the ADR 0014 contract instead.

## Verified live (good news)
- 15/15 pumps connected first try (~0.2 s); per-pump mTLS + Connect/
  Publish-own-topic policy scoping works as designed (ADR 0016).
- Hot-path freshness ~2 s (adapter `as_of` vs `latest_ts`).
- Cold path: exactly one Parquet per minute on the :32 tick, 15 files,
  Hive partitions `year=/month=/day=/hour=` correct (ADR 0015).
- **Cold-start canary: Init ~4.76–4.83 s, warm ~43 ms, 272 MB / 512 MB**
  (two cold containers from the 15-way first-publish fan-out; well
  inside the 10 s timeout). → close into `context/lambda_scorer.md`.
- Adapter envelope matches ADR 0014: fleet_size / pumps_reporting /
  as_of / pumps[], absent pumps omitted (no null rows), stable sort.

## Footprint drift (record vs baselines)
- scorer staged tree 175 MB (ADR 0006 §Q4 baseline ~124 MB; ceiling 250).
- batcher staged tree 144 MB (ADR 0015 estimate ~100 MB).

## Teardown
- `aws_teardown.sh` came back clean (exit 0, all clear) — stack down,
  $0 posture restored. Invocation gotcha: the script needs `aws`+
  `terraform` dirs prepended to Git Bash PATH (they are not on bash's
  PATH though they work in PowerShell); exact one-liner now in the
  runbook §4 and memory `ml-obs-pipeline-bash-scripts-path`.

## Cost actuals (pulled 2026-06-10 from CloudWatch; 2026-06-07 09:00–11:00 UTC window)
- **DynamoDB `pump_hot_state`:** 18,468.5 consumed read units + 10,333
  consumed write units for the run ≈ **~$0.01** (about a cent) at
  eu-central-1 on-demand rates — ~10× UNDER ADR 0013's ~$0.10–0.20/demo
  estimate, and the gap is explainable, not a measurement error. ADR
  0013 assumed the steady-state full 1800-row PSI window (~30–35
  RCU/invocation); a 14-min run never fills it (1800 rows @ 2 s cadence
  = 60 min to accumulate), so actual reads were ~3.7 RCU/invocation
  (18,468.5 ÷ 5,054). ADR 0013 stands as a conservative per-demo
  CEILING; a short cold-start run costs a fraction of it.
- **Lambda → $0 (Always-Free).** scorer 5,054 invocations (avg 65.5 ms,
  max 403.5 ms — the max is the first real invocation's lazy classifier
  load on first `score()`; the ~4.8 s INIT phase is reported separately,
  see cold-start canary above). batcher 58 invocations (avg 388.8 ms,
  max 988.2 ms — pyarrow Parquet write; 58 ≈ stack-up minutes on the
  rate(1 min) schedule, only ~15 of which had data to drain). Combined
  ≈ 171 GB-s, well inside the 400 K GB-s/mo free tier.
- **IoT Core → $0** inside the account's 12-month free tier (ADR 0016 §Cost).
- **Run total ≈ ~$0.01** — essentially the DynamoDB cent. The live run
  validates ADR 0013's model as a ceiling: portfolio cost line is "cents
  per demo, and the first live run came in ~10× under the documented
  worst case."
- Run length: simulator ~12:36–~12:50 local (~14 min publishing,
  ~2 s cadence, 15 pumps ≈ 6.3k readings → 5,054 scorer invocations
  actual); teardown same session.
- Stack-gone re-confirmed 2026-06-10 (3-day-gap check): `iot
  list-things` [], `dynamodb list-tables` [], no `pump-*` Lambdas. One
  unrelated pre-existing `onboarding-test` function remains — not part
  of this stack; idle Lambda = $0 standing.

## Carried to next session (tomorrow)
1. Teardown result + cost actuals into this log; context closures:
   `dashboards.md` (Infinity URL — UNOBSERVED, Grafana was never
   opened live this run; `null last_alert_sent_at` → close from
   contract; `pumps_reporting` → fixed-not-redeployed),
   `lambda_scorer.md` (cold start), `infra.md` (quota + etag lessons).
2. Cascade + dispositions + commit (BOM-free, inline -m): adapter fix,
   concurrency -1s, session artifacts.
3. Open items to triage: source_hash swap (both s3-object modules);
   PSI warmup gate (parity-relevant? touches drift surface — likely
   needs its own brief per DEV_NORMS §5); restore reserved
   concurrency after quota increase; SNS subscription confirm flow
   (marketing mail confusion — runbook §0 wording).

## Review dispositions + wrap-up (2026-06-10)

Reviewer cascade trimmed to DeepSeek-only (PO call; ADR 0011 §Addendum
2026-06-10). Response by **deepseek** (`deepseek-reasoner`) at
`review_responses/2026-06-10-infra-live-apply-wrapup.md`. Four findings,
all ACCEPTED (full table in the packet):

1. Off-by-one completeness — verified clean (repo grep + pinned tests); no change.
2. Uncapped public adapter URL (`reserved=-1`, AuthType=NONE) — added a §0.5
   pre-demo guard to `docs/runbooks/aws-demo-day.md` (watch
   `ConcurrentExecutions`, tear down on spike). Reservation impossible under
   the quota floor.
3. `source_hash` swap — LANDED this commit: both s3-object modules use
   `source_hash` (not `etag`); forces one re-upload next apply.
4. Restore-after-quota-bump — elevated to a §0.5 runbook checklist
   (restore adapter->5 / batcher->1 once quota is raised).

Commit split (PO call 2026-06-10): **Commit A** = review tooling
(DeepSeek-only `run_review.{ps1,sh}` rename, ADR 0011 addendum, DEV_NORMS +
dev_workflow ref updates, gitignore); **Commit B** = this live-apply wrap-up
(fixes + closures + cost actuals + session log + source_hash + runbook guards).
