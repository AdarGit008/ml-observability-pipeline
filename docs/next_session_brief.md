# Next session brief — PSI warmup gate (parity-aware): stop the minute-1 false-alert storm

## ⚠ PARITY-SET SESSION — Tier 2b loads are MANDATORY
This session touches the **drift surface** (`shared.drift` / PSI alert arming).
Per DEV_NORMS §5 Tier 2b and the locked mode-parity contract (ADR 0005), you
MUST load — IN ADDITION to Tier 2 — `shared/{features,score,drift}.py` plus
ADR 0005 / 0007 / 0009 / 0012. **If this brief is ever loaded WITHOUT those
Tier 2b loads, STOP and alert the PO before any work** (memory:
`ml-obs-pipeline-parity-load-check`).

## Goal
Add a **minimum-sample warmup gate** so PSI-based alerts don't arm until a
pump's window holds enough data for PSI to be meaningful. Fixes the
2026-06-07 live finding: on a `healthy` fleet, 9/14 pumps fired
`alert_flag: true` within minute 1 — max-PSI > 0.25 on sub-minute sample
windows (scores all ≤ 0.02, far below the 0.7 score threshold). Edge-triggered
SNS fired fleet-wide on PSI noise from tiny windows.

## The crux — this is a parity problem, not a lambda_scorer-only fix
PSI is computed by `shared.drift.compute_psi`, called identically in BOTH modes
(lambda_scorer AND local_runtime). The gate must behave identically in both or
it's a north-star-#6 parity break. Two candidate shapes — decide with an ADR:
- **(A) Gate inside `shared/`** — e.g. a `psi_is_armed(window)` helper (or a
  not-armed signal from `compute_psi`) both modes consume. Keeps the logic in
  the parity contract; likely the right home.
- **(B) Gate in the alert-arming logic** (lambda_scorer SNS edge-trigger +
  local_runtime equivalent) — both call sites must apply the SAME threshold via
  a shared constant + a structural-parity test, or they drift.
The threshold itself (require the full reference-window count? ≥ X readings?)
needs justification tied to ADR 0007's PSI cadence/formula. Evidence: the
2026-06-07 data (scores ≤ 0.02 but PSI > 0.25 on sub-minute windows).

## Context — what the 2026-06-10 wrap-up left
- Live apply (2026-06-07) verified end-to-end; teardown clean; cost ~$0.01/run
  (~10× under the ADR 0013 ceiling). Stack is DOWN; $0 posture.
- Two commits landed 2026-06-10: **Commit A** (review tooling — DeepSeek-only
  `run_review.{ps1,sh}`, ADR 0011 §Addendum) and **Commit B** (live-apply
  wrap-up — adapter off-by-one fix, reserved concurrency -1, `source_hash`
  swap, context closures, cost actuals, runbook §0.5 guards).
- Reviewer is now **DeepSeek-only**: `.\scripts\run_review.ps1 -Slug <slug>`,
  key in gitignored `scripts/review_keys.local.ps1` (ADR 0011 §Addendum
  2026-06-10).
- The PSI warmup storm was deliberately deferred from the wrap-up to THIS
  brief because it touches the parity surface.

## NOT in scope (other deferred items — pick later)
- Demo-day rehearsal: redeploy the adapter + verify `pumps_reporting == 15`
  live; open Grafana vs the live URL to close the Infinity relative-URL open
  item (`context/dashboards.md`).
- Restore reserved concurrency (adapter→5 / batcher→1) after a Service Quotas
  bump — runbook §0.5 checklist.
- CI cost guardrails (last `[ ]` in `context/infra.md`).
- README / portfolio polish (cost table citing ADR 0013 + the $0.01 live actual).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §5 (Tier 2b) + §8.
- Tier 2: `context/lambda_scorer.md`, `context/drift.md`.
- **Tier 2b (MANDATORY — parity):** `shared/{features,score,drift}.py`;
  ADR 0005, ADR 0007, ADR 0009, ADR 0012.
- Tier 3: `context/_interfaces.md` (§SNS alert payload, §PSI parameters).
- Memory: `ml-obs-pipeline-parity-load-check`,
  `ml-obs-pipeline-live-apply-2026-06-07`,
  `ml-obs-pipeline-fuse-write-truncation`, `ml-obs-pipeline-git-on-windows`.

## Constraints
- Parity-aware: any drift/alert-surface change keeps
  `test_structural_parity_no_vendoring` (+ siblings) green and applies
  identically in both modes.
- Likely needs an ADR (where the gate lives + the threshold) — north star #6:
  local/AWS divergence is a bug or an ADR.
- Git PO-side; FUSE rules (existing files bash-rewrite, NEW via Write, never
  Edit on D:); BOM-free commits (inline `-m`).
- Reviewer: `run_review.ps1 -Slug <slug>` (DeepSeek).
- No `terraform apply` unless a live re-verify is explicitly in scope.

## Definition of done
- Gate implemented in the parity-correct location; threshold justified against
  ADR 0007; ADR written for the decision.
- Both modes proven equivalent: structural-parity tests green + a test that a
  sub-minute window does NOT arm an alert while a full window with real drift
  DOES.
- Session log + context closures (`lambda_scorer.md` PSI-storm open item
  closed, `drift.md` updated); DeepSeek cascade + dispositions + commit.
