# Review Packet 2026-06-10 — infra — live-apply wrap-up

> Run via: `.\scripts\run_review.ps1 -Slug infra-live-apply-wrapup`

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

(Per ADR 0011, this packet may be reviewed by any model in the cascade: Gemini, DeepSeek R1 via OpenRouter, Llama 3.3 70B via Groq, or Llama 3.3 70B via Cerebras. The role is identical across providers; the response file's footer records which one actually wrote the response.)

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change
Wrap-up commit for the **2026-06-07 first live AWS end-to-end apply** (run + clean teardown already done; this lands the fixes + bookkeeping the live run produced). Bookkeeping-heavy; two real code fixes:

1. **Adapter off-by-one** (`dashboards_adapter/handler.py`). `FLEET_PUMP_IDS` was 1-indexed `P-01..P-15`; the real fleet is 0-indexed `P-00..P-14` (terraform `count.index`, simulator, scorer STATE keys). Live, the adapter queried a nonexistent `P-15` and never asked for `P-00` (which had 186 scored rows) → the `pumps_reporting: 14` mystery. Fix: `tuple(f"P-{i:02d}" for i in range(FLEET_SIZE))`. 7 test patches in `tests/test_adapter.py`; 17/17 adapter tests pass. **Fixed-not-redeployed before teardown** — `pumps_reporting == 15` is a redeploy-verify at the next apply.
2. **Reserved concurrency → -1** on both the adapter (`infra/modules/dashboards_adapter/variables.tf` default 5 → -1) and batcher (`infra/modules/lambda_s3_batcher/main.tf` 1 → -1). The new-account Lambda concurrency quota sits at the floor (min-10-unreserved), which rejects ANY reservation. Both carry inline comments + "restore after Service Quotas bump" notes.

Plus bookkeeping: the session log (`docs/sessions/2026-06-07-infra-first-live-apply.md`), the teardown Git-Bash PATH gotcha in the runbook, context-file closures (cold-start numbers, off-by-one disposition, quota + etag/multipart lessons, 15/15 live-connect confirm), and CloudWatch cost actuals folded into the session log. No new ADR. `shared/` untouched — NOT a parity-set change.

## Diff
Full diff is in the working tree (uncommitted; reviewer sees files below). Changed surface:

**New:** `docs/sessions/2026-06-07-infra-first-live-apply.md`.
**Modified (code):** `dashboards_adapter/handler.py`, `dashboards_adapter/tests/test_adapter.py`, `infra/modules/dashboards_adapter/variables.tf`, `infra/modules/lambda_s3_batcher/main.tf`.
**Modified (docs/context):** `context/{dashboards,infra,lambda_scorer,simulator}.md`, `docs/runbooks/aws-demo-day.md`, `docs/next_session_brief.md`.

Key excerpts:

```python
# dashboards_adapter/handler.py — the fix
# the simulator names pumps P-00..P-{NN-1} (0-indexed — terraform
# aws_iot_thing.pump[count.index] and the scorer key STATE rows by the
# same ids, ADR 0010/0016). Fixed 2026-06-07 (live apply): was 1-indexed
# P-01..P-NN, which queried a nonexistent P-15 and never asked for P-00.
FLEET_PUMP_IDS: tuple[str, ...] = tuple(
    f"P-{i:02d}" for i in range(FLEET_SIZE)
)
```

```hcl
# infra/modules/dashboards_adapter/variables.tf
variable "reserved_concurrency" {
  # ...caps worst-case spend + table read pressure from the public URL (2026-06-04 review Q1).
  # Set to -1 (no reservation) 2026-06-07: account Lambda concurrency quota is at the
  # new-account floor, so ANY reservation violates min-10-unreserved. Restore to 5 after bump.
  default = -1
}

# infra/modules/lambda_s3_batcher/main.tf
  # 2026-06-07: -1 (no reservation) — account concurrency quota at the new-account floor.
  # Overlap needs a >60s stuck invocation (timeout 30s); duplicates stay covered by the
  # ADR 0015 at-least-once contract. Restore to 1 after quota bump.
  reserved_concurrent_executions = -1
```

## Specific questions for the reviewer

1. **Off-by-one fix completeness.** The fix is `range(FLEET_SIZE)` → `P-00..P-{N-1}`. Is there any OTHER place a 1-indexed `P-01..P-N` assumption could still lurk (teardown sweep, any test fixture, any doc that drives behavior), and do the 7 test patches pin the right invariant — `P-00` present, `P-14` present, no `P-15`, `pumps_reporting == FLEET_SIZE`?
2. **Reserved concurrency -1 on a PUBLIC (AuthType=NONE) adapter Function URL.** Going from `reserved=5` (a hard cap on spend + table-read pressure from the public URL — the 2026-06-04 review Q1 rationale) to `-1` (no reservation; shares the account unreserved pool, effectively uncapped to the account limit) is forced by the quota floor. Standing exposure is zero (teardown destroys the URL after each demo), but during a demo the URL is uncapped. Is "comment says restore to 5 after bump" a sufficient control, or does the uncapped public URL warrant a compensating guard now?
3. **`source_hash` swap — should it ride in THIS commit?** `aws_s3_object` currently uses `etag = filemd5`, which re-uploads >5 MB zips on every apply (provider does multipart → ETag `<hash>-N` ≠ MD5 → phantom diff; cost it four applies live, compounded by flaky DNS). Recorded durable fix: switch both s3-object modules to `source_hash` (multipart-safe). It forces exactly one re-upload on the next apply — which is the rehearsal anyway. Land it now, or keep it a tracked open item? Any `source_hash` downside being missed?
4. **Restore-after-quota-bump traceability.** The "restore reserved concurrency to 5 / 1" items live only in code comments + `context/infra.md` open-questions (no issue tracker on this single-PC project). Adequate, or should these be an explicit pre-demo checklist item in the runbook?

## What I'm NOT looking for in this review
- **PSI warmup alert storm / min-sample gate** — separate parity-aware brief (touches `shared.drift`, the ADR 0005 parity set); explicitly out of scope here.
- terraform fmt / style (PO-side).
- Re-litigating on-demand billing (ADR 0013) or adapter AuthType=NONE (ADR 0014) — locked decisions.
- Cost-actuals arithmetic — recorded for the portfolio cost story, not a design decision.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Off-by-one completeness | ACCEPT — verified, no change | Repo-wide grep for `P-01`/`P-15` outside tests = clean; invariant pinned in `test_adapter.py` (`pumps_reporting==15`, `P-00`/`P-14` present, `FLEET_PUMP_IDS==("P-00","P-01","P-02")` at FLEET_SIZE=3). |
| 2. Uncapped public adapter URL | ACCEPT — runbook guard added | Reviewer right that the `-1` comment is "a wish, not a control"; reservation is impossible under the quota floor (Option B ruled out). Added §0.5 guard to `aws-demo-day.md`: watch `ConcurrentExecutions` for `pump-dashboard-adapter`, tear down on spike. Standing exposure already zero (teardown each demo). |
| 3. source_hash swap | ACCEPT — landed this commit | Provider pinned `>=5.30` (source_hash since v4.39 — version worry N/A). Both modules now `source_hash = ...output_md5`; `etag` removed (avoids conflicting-args). Forces one re-upload next apply = the rehearsal. `context/infra.md` TODO flipped to LANDED. |
| 4. Restore-after-quota-bump traceability | ACCEPT — elevated to runbook | Added §0.5 checklist (restore adapter→5 / batcher→1 if quota raised) to `aws-demo-day.md`; also noted for the next-session brief. Code comments + `context/infra.md` retained as the why. |
