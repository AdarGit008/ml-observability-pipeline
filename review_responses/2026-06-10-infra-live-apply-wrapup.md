## Adversarial Review â File-by-File, Risk-First

You've cleaned up two real bugs and gathered hard-won operational data. The off-by-one fix is correct; the concurrency -1 is correct for now but exposes a gap; the `source_hash` question is a judgment call with a shallow edge case; and traceability should be elevated to the runbook. Here's the detail.

### 1. Off-by-one fix completeness

**Verdict: Complete, but one latent risk remains in test coverage.**

The fix in `dashboards_adapter/handler.py` changes the tuple generation from a hardcoded 1-indexed list to `range(FLEET_SIZE)`, producing `P-00..P-14`. The seven test patches in `tests/test_adapter.py` now presumably assert:

- `P-00` is present in the generated list  
- `P-14` is present  
- `P-15` is absent  
- `pumps_reporting` equals `FLEET_SIZE` when all 15 pumps have rows  

Iâd need to see the actual test diffs to be 100% sure, but the summary claims 17/17 tests pass, and the off-by-one was the only cause of the `14` mystery, so the fix is sound for the adapter.

**However**, there is a residual risk: **is `FLEET_SIZE` imported correctly in every module that references pump IDs?** The constant lives in `dashboards_adapter/handler.py` (defined as `FLEET_SIZE = 15`?). If any other part of the adapter (e.g., `_build_asset_property_value` or the dashboard template generator) also hardcodes `P-01..P-15`, they would remain broken. I scanned the summary and diff excerpts: no such code was changed. Given the fix was triggered by a live-run symptom, itâs likely that *only* the ID generation was wrong. *But* Iâd advise a quick grep of the entire `dashboards_adapter/` tree (including templates) for `P-01` or `P-15` to confirm no other stale literal. The same grep across `infra/` and `docs/` (e.g., runbook example commands) would be cheap insurance.

On the test side: the seven patches presumably cover the core handler scan-and-query flow. They ought to include a test that passes a simulated DynamoDB response that has rows for `P-00` through `P-14` and verifies `pumps_reporting == 15`. If that test exists, the invariant is pinned. If not, add it.

### 2. Reserved concurrency `-1` on a public Function URL

**Verdict: The risk is real and the comment alone is insufficient control.**

Youâre aware of the tension: the `reserved=5` cap protected the DynamoDB table from runaway reads (and limited Lambda spend). The quota floor forced you to go to `-1`, which means the adapter can scale to the account-level concurrent execution limit (often 1000 for new accounts, sometimes lower). During a demo, the URL is liveâand **unauthenticated** (AuthType=NONE per ADR 0014). An accidental discovery or a burst of refresh clicks could trigger hundreds of concurrent invocations, each doing DynamoDB `Query` on potentially many pump keys.

**Cost exposure is low** (tiny table, single-page application with low row count), and the demo is ephemeral (teardown after each session). But the *principle* of an uncapped public endpoint contradicts the $0 cost north star. The comment says ârestore to 5 after bumpâââbut thatâs an indefinite placeholder.

**Recommendation**: Add a compensating control now, not later.

- **Option A (runbook guard)**: Add a step in `docs/runbooks/aws-demo-day.md`: *âBefore demo: verify that Lambda concurrent executions cannot exceed 10 by monitoring CloudWatch `ConcurrentExecutions` for the adapter. If you see any spike, immediately disable the Function URL via Terraform (`privatize_url = true` toggle).â*  
- **Option B (better, if possible)**: Set a **function-level reserved concurrency** that is allowed under the quota floor. Waitâyou said any reservation violates min-10-unreserved. Thatâs true if the *sum of all reservations* leaves less than 10 unreserved. But if your account has exactly one function (the adapter) and you reserve 5, the unreserved pool drops from 10 to 5, which is below 10 â error. So reservation is impossible.  

- **Option C (operational mitigation)**: Store the cost risk explicitly in the session log and the demo check list: *âAdapter is uncapped during demo; no cost expected, but be ready to terminate the URL if something looks off.â*  

- **Option D (architectural)**: Give the adapter a private API Gateway with API key throttling. That adds cost (!) and violates `$0`. Not recommended.  

**For now**, the comment is not a controlâitâs a wish. Write the runbook guard and move on.

### 3. `source_hash` swap â should it ride in THIS commit?

**Verdict: Land it now, but verify three preconditions.**

The `etag = filemd5` phantom-diff bug cost you four wasted applies and flaky DNS. The fix (`source_hash`) is cheap, deterministic, and multipart-safe. Delaying it means the *next* apply will also trigger a re-upload (because the source hash has changed from the old state), which is exactly the same cost as landing it nowâexcept you will have to remember to do it later or risk forgetting.

**Downsides I see:**

1. **Provider version dependency**: `source_hash` was introduced in the AWS provider v4.?? (I think v4.67+). Ensure your `required_providers` block in the root module (or in the S3 object modules) uses a version `>= 4.67`. If youâre on an older version, the attribute doesnât exist and Terraform will error. Check `infra/provider.tf` or wherever the AWS provider constraint lives.  
2. **One-time re-upload on apply** (you already acknowledged this). Thatâs fineâand itâs a rehearsal.  
3. **If you use `source_hash` together with `etag`** the provider will complain about conflicting arguments. You must remove the `etag` line entirely. Make sure the diff includes that deletion.  
4. **`source_hash` updates require the file to be re-read each apply** (which can slow down large zips). Thatâs the same as `filemd5`. No net change.

Given the commit is already a âwrap-upâ containing two functional fixes and bookkeeping, adding a third unrelated change is questionable from a git history hygiene standpoint. But this is a single-PC portfolio projectâperfect commit messages are a nice-to-have. I recommend **squashing the two S3 object module edits** into this commit anyway, with a clear commit message body mentioning the `etagâsource_hash` change as a drive-by fix. The single re-upload will happen on the next apply regardless of when you merge it.

**Do it now.**

### 4. Restore-after-quota-bump traceability

**Verdict: Inadequate. Elevate to runbook checklist item.**

Code comments are the lowest form of traceabilityâeasy to miss, never surfaced by automation, and often stale. Open-questions in `context/infra.md` are better but still buried. The next developer (or you in 3 months) will not scan random context files before a demo.

**Recommendation**: Add a **dedicated pre-demo checklist item** in `docs/runbooks/aws-demo-day.md`. Something like:

```
## Before demo (if AWS quota has changed)
- [ ] If account Lambda concurrency quota > 10, restore reserved_concurrency:
    - `dashboards_adapter` â 5 (variable default)
    - `lambda_s3_batcher` â 1 (direct attribute)
- [ ] Re-apply terraform to update reservations
- [ ] Verify no deployment errors from quota violation
```

Also, in `docs/next_session_brief.md`, include a note: *âRestore adapter/batcher reserved concurrency after quota increase (see runbook).â*

This makes the action a concrete, checkable step rather than an ambient comment buried in a `.tf` file. The project has no issue tracker; the runbook is your single source of procedural truth. Use it.

---

### Summary of risks and recommendations

| Question | Risk | Recommendation |
|----------|------|----------------|
| 1 | Stale hardcoded `P-01` elsewhere | Grep entire repo for `P-01`/`P-15`; add assertion test for `pumps_reporting == FLEET_SIZE` |
| 2 | Uncapped public URL during demo | Add runbook guard step: monitor/disable if spike; store cost risk explicitly |
| 3 | Provider version compatibility for `source_hash` | Verify `>= 4.67`; include in this commit, remove `etag` |
| 4 | Missing restore reminder | Add explicit checkbox to runbook pre-demo checklist |

None of these are blockers to landing the commitâbut address the runbook and grep before the next apply. Your off-by-one fix is clean and the concurrency -1 is the only possible move given the quota; just donât rely on hope for the rest.

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-10 12:58:54._

