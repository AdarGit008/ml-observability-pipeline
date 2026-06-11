# Session 2026-06-11 — fleet-PSI live verify (aborted) + pump-id off-by-one fix

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** DeepSeek (`deepseek-reasoner`) — `review_responses/2026-06-11-fleet-batcher-off-by-one.md` (ADR 0011 §Addendum 2026-06-10)
- **Context loaded:** `_global`, `lambda_fleet_psi`, `infra`, `dashboards`, `_interfaces`; ADRs 0010/0012/0013/0014/0016/0017/0018; runbook `aws-demo-day.md`. NOT a parity session (`shared/` untouched).
- **Duration:** ~1 session

## Intent
Prove the Fleet-PSI Lambda (ADR 0018) end-to-end on live AWS and surface the `FLEET` row in Grafana. The dashboards FLEET panel + adapter `fleet` object were already shipped (dashboards session #3, 2026-06-10), so this session was scoped to: live apply → run `seasonal_drift` → verify the fleet path → close the demo-day open items → teardown.

## What actually happened
Applied the full stack live (131 resources, clean). On the first adapter read during a `scenario: healthy` run, two issues surfaced and the verify was **aborted before the breach test**; stack torn down to $0; both issues handled offline.

### Finding A — fleet/batcher pump-id off-by-one (REAL bug, fixed)
The adapter envelope showed `fleet.pumps_pooled: 14` on a 15-pump fleet. Root cause: `lambda_fleet_psi/handler.py` enumerated `FLEET_PUMP_IDS` as `range(1, FLEET_SIZE + 1)` → `P-01..P-15`, dropping **P-00** and querying a phantom **P-15**. Inspection found `lambda_s3_batcher` had the **identical** bug (Finding C, folded in) — so **P-00's readings were never archived to S3** (silent cold-path data loss). `dashboards_adapter` was already correct (fixed live 2026-06-07; the same root pattern survived in two other copies). The fleet test `test_handler.py:84` had been **asserting the buggy form** (`range(1, 16)`), which is why it shipped green.

### Finding B — healthy fleet alerts (NOT a bug)
Every pump scored `~0.9999` with PSI ~1.5–2.0 on `scenario: healthy`. Investigated offline and reproduced the *correct* (low) behavior: with 300 steady-healthy samples, score `0.002` and PSI `< 0.06`. The live read was caught because **`demo_mode: true`** compresses HEALTHY `dwell_ticks` from 43,200 to 60 (`simulator/config.py:200-205`), so `scenario: healthy` runs the full HEALTHY→DEGRADING→FAILING→FAILED arc in ~15 min — the pumps had genuinely degraded by read time (offline: score crosses to 0.9999 at ~3 min). The 2026-06-07 "PSI warmup storm" was likely this same effect misattributed. **No code change.** Consequence: the "healthy fleet stays quiet" check and a clean `seasonal_drift` fleet demo both require **`demo_mode: false`** (steady HEALTHY baseline), else pump auto-degradation contaminates the signal.

## What changed (the fix — this commit)
- `lambda_fleet_psi/handler.py` + `lambda_s3_batcher/handler.py`: `range(1, FLEET_SIZE + 1)` → `range(FLEET_SIZE)` (now `P-00..P-(FLEET_SIZE-1)`). Docstrings updated.
- `lambda_fleet_psi/tests/test_handler.py`: corrected the assertion that enshrined the bug (`range(1,16)` → `range(15)`) + `P-00`/no-`P-15` checks.
- `lambda_s3_batcher/tests/test_batcher.py`: new `test_p00_is_archived_off_by_one_regression` (seeds ONLY P-00; the 18 prior tests all seed P-01/P-02, in-range either way).
- `lambda_fleet_psi/tests/test_fleet_id_consistency.py`: **new** cross-component guard — discovers every `*/handler.py` defining `FLEET_PUMP_IDS` and pins each to the canonical `range(FLEET_SIZE)` form (scoped to the assignment block; auto-fails if a 4th handler is added uncovered).
- Docs: `context/{_interfaces,lambda_fleet_psi,lambda_s3_batcher,dashboards}.md`; `_global.md` cross-component invariant (fleet-id enumeration + SSOT debt).
- Full suite: **454 passed, 1 skipped** (in-sandbox sklearn 1.7.2; prod runs matching 1.9.0).
- PR: _(fill in at commit)_

## DeepSeek dispositions (review approved, no blockers)
1. **Batcher first-run epoch drain** — P-00 has no WATERMARK, so its first post-fix batch drains from epoch. Benign under teardown-fresh tables (our normal flow); a note belongs in the commit message. Folded → commit message.
2. **Source-level guard robustness** — strengthened: scoped literal checks to the `FLEET_PUMP_IDS` assignment block (no comment false-positives) + a discovery assertion so a new handler can't silently escape `_HANDLERS`; limitations documented in the test docstring.
3. **SSOT debt** — "test, don't dedup" accepted for a $0/portfolio repo; recorded in `_global.md` to revisit if a 4th consumer appears. `shared/` is the wrong home (AWS-fleet concept, not a parity peer).
4. **Scope** — folding the batcher fix into a fleet session accepted (same bug family); noted in the commit message that they are separate hunks.
5. **Audit** — completed: no stray 1-indexed fleet enumerations remain. Terraform (`for i in range(var.fleet_size)`), teardown (`while i < FLEET_SIZE`, `printf 'P-%02d'`), simulator, and adapter are all 0-indexed. The only `range(1, FLEET_SIZE+1)` left is in `.build/{batcher,fleet_psi}_dist/` — **stale staged copies**; the build scripts must re-run before re-apply.

## Operational notes
- **$0 confirmed:** `terraform destroy` (131 destroyed) + PowerShell-native absence checks (DynamoDB `ResourceNotFound`, S3 `NoSuchBucket`, Lambda/IoT/SNS queries empty).
- **Teardown gotcha:** `& "…bash.exe" -lc '…aws_teardown.sh…'` produced **no stdout** in PO's PowerShell (both attempts). Run `aws_teardown.sh` from a native Git Bash terminal, or use PowerShell-native `aws` checks. (Runbook updated.)
- **Live apply confirmed working:** `pumps_reporting == 15` (the adapter's own 2026-06-07 off-by-one verified fixed live); adapter `fleet` object surfaces in the correct shape.

## NEXT (deferred to a re-apply verify session)
1. Rebuild (**all four** build scripts) → re-apply → run `seasonal_drift` with **`demo_mode: false`** → verify the FLEET path: `pumps_pooled == 15`, FLEET STATE row written, edge-SNS fires **once** on the breach + no re-publish next armed tick, healthy fleet quiet through the warmup window (ADR 0017) → teardown.
2. Close the demo-day open items in Grafana (Infinity relative-URL; `$.fleet` single-object render) — `context/dashboards.md §Open questions`.
3. Reserved concurrency still `-1` (Service Quotas bump not landed as of this session).
4. Add the fleet-PSI **verify** steps to the runbook once observed live (build step already added).
