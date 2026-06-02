Next session brief — lambda_scorer PSI follow-on (compute_psi + SNS publish + STATE-row extension)

Goal
Pick up the lambda_scorer thread where the 2026-06-02 MVP session closed. Add the deferred PSI compute + SNS publish path: widen the score-path DynamoDB read to the 1-hour PSI window, call `shared.drift.compute_psi(window, REFERENCE)` against the cold-start-loaded reference, extend the STATE-row PutItem with `latest_psi` (4-key dict per ADR 0009) + `alert_flag`, and publish to SNS on threshold breach per `_interfaces.md §SNS alert payload`. Schema is additive only — no DynamoDB migration; the ADR 0010 contract carries this session unchanged.

After this session, the AWS-mode hot path is feature-complete except for the dashboards adapter (Grafana → DynamoDB) and the IaC (Terraform module for the table, SNS topic, IoT Rule, IAM).

How to start this session — plain-language walkthrough FIRST
Same rule as 2026-06-02. Claude walks PO through the open Qs + the MVP-to-PSI scope delta + the SNS-topic-ARN env-var addition in plain language BEFORE any code. ONE paragraph each.

If anything in the in-scope items below has changed meaning since the brief was written (e.g., the STATE-row attribute names need adjustment, a Q resolution turned out wrong), say so before greenlight.

In-scope items (in order)

Item 1 — Resolve the PSI-window query shape
What changes: the hot-path DynamoDB read goes from `Limit=150` (5-minute scoring window) to needing BOTH a scoring window AND a 1-hour PSI window. Two shapes on the table per ADR 0010's forward commitment:

* (A) Single `Query` with `Limit=1800`; slice the last 150 in-handler for `extract_features` and pass the full 1800 to `compute_psi`. Cheapest: one DynamoDB read per invocation. Pays for the 1800-row payload on every call even though scoring only needs the most recent 150.
* (B) Two `Query` calls (`Limit=150` for scoring, `Limit=1800` for PSI). Double the DynamoDB read count per invocation. Conceptually cleaner; aligns with how `local_runtime/service.py` keeps the two windows distinct (5-min `FeatureWindow` deque + 1-hour feature-history deque).

Claude's leaning at plan-step: **(A) single query**. At ~15 pumps × ~0.5 RPS, the marginal read-unit cost is invisible; the simpler control flow + halved DynamoDB latency contribution to cold-start-after-reload wins. PO call.

PSI cadence: ADR 0007 specifies every-Nth-tick (default N=30 ticks = once per minute at 2 s tick). Lambda hot path doesn't have a tick counter the same way `local_runtime/service.py` does (one process holding state) — each invocation is independent. Two options:

* (a) Compute PSI on every invocation (~30/min/pump = 450 PSI computes/min across the fleet). Same `compute_psi` call shape as `local_runtime`, no cadence logic in the handler.
* (b) Use the DynamoDB-stored `latest_ts` on the STATE row as a poor man's cadence — only compute PSI if `(new_ts - state.latest_ts) > 60 s`. Reduces PSI compute volume at the cost of one extra GetItem + an `if` branch.

Claude's leaning: **(a) every-invocation compute**. PSI on a 1800-sample window is ~2 ms of numpy work; running it 450 times/min is free CPU. The cadence in `local_runtime` exists to throttle InfluxDB writes (PSI value lands as fields on a point), NOT because the computation is expensive. In Lambda the throttle isn't needed.

Item 2 — STATE-row schema extension
New attributes on the STATE row (per ADR 0010's pre-authorized extension):

```
PK = pump_id
SK = "STATE"
latest_ts    = <ISO-8601 ts>      # unchanged from MVP
latest_score = <Decimal>          # unchanged from MVP
latest_psi   = {                  # NEW: 4-key dict per ADR 0009
  "vibration_amp": <Decimal>,
  "bearing_temp":  <Decimal>,
  "motor_current": <Decimal>,
  "rpm":           <Decimal>
}
alert_flag   = <bool>             # NEW: convenience flag for the dashboards adapter
```

`latest_psi` lands as a DynamoDB Map (`M`). The `_to_decimal` helper from MVP applies per-value. `alert_flag` is the OR of (`max(psi.values()) > 0.25`) and (`latest_score > 0.7`) per `_interfaces.md §SNS alert payload`.

Update `context/_interfaces.md §DynamoDB schema` STATE row block: remove the "(PSI follow-on adds)" comment, land the attributes as committed. ADR 0010 stays unedited (it's the load-bearing schema decision; the extension was pre-authorized in §Decision #1 §Reading row + §STATE row blocks).

Item 3 — SNS publish branch
New module-level cold-start state:

```python
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]    # required; KeyError on cold-start if unset
_SNS = boto3.client("sns", region_name=AWS_REGION)
```

Hot path adds an SNS publish AFTER the STATE-row write, gated on `alert_flag`. Payload shape per `_interfaces.md §SNS alert payload`:

```python
{
  "pump_id":    pump_id,
  "ts":         ts,
  "alert_type": "both" | "psi_breach" | "high_failure_prob",
  "score":      float(score_value),
  "psi":        {name: float(v) for name, v in psi.items()}
}
```

Open question — alert dedupe: if PSI > 0.25 persists, do we publish on every invocation (= 30 alerts/min/pump = noise) or use the previous `alert_flag` to suppress duplicates? Two options:
* Edge-trigger: only publish when `alert_flag` flips from False → True. Requires reading the previous STATE row's `alert_flag` (one extra GetItem OR fold it into the existing window Query).
* Always-publish: every invocation that breaches publishes. Downstream (SNS email subscription) handles dedupe via per-day-per-pump throttling rules at the SNS topic level.

Claude's leaning: **edge-trigger** because SNS Always-Free is 1000 email deliveries/month and 13.5K invocations/demo × any reasonable demo scenario would burn through that fast. PO call.

Item 4 — Tests with `moto`
Add 3-4 new tests to `lambda_scorer/tests/test_handler.py`. Coverage:

* PSI-on-warm-window happy path: seed 1800 readings, invoke, verify `latest_psi` lands on STATE row + value set is `PSI_FEATURE_NAMES` (4 keys).
* PSI surface stays at 4 keys: pin `set(latest_psi.keys()) == set(PSI_FEATURE_NAMES)` per ADR 0009.
* SNS publish on threshold breach: seed readings that drive PSI > 0.25 OR force a score > 0.7, invoke, verify `_SNS.publish` was called with the expected payload (moto provides `@mock_aws` for SNS too — same context manager covers both).
* SNS no-publish on healthy: `alert_flag == False`, no SNS call.
* (If edge-trigger lands) SNS no-publish on still-breached: second consecutive breach invocation does NOT re-publish.

Component
`lambda_scorer` — Tier 2b parity-touching (imports `shared/features.py + shared/score.py + shared/drift.py`, including `compute_psi` for the first time in Lambda mode). See [[ml_obs_pipeline_shared_parity_boundary]] — the contract stays at `FEATURE_NAMES` (8) + `PSI_FEATURE_NAMES` (4). No parity-boundary edits this session unless plan-step surfaces a gap.

[[ml_obs_pipeline_parity_load_check]] applies — STOP if any Tier 2b load is missing from this brief.

Loads
* Tier 1: `context/_global.md`, `context/_interfaces.md`.
* Tier 2: `context/lambda_scorer.md`.
* Tier 2b parity: `shared/{features,score,drift}.py` (all three) + ADR 0005.
* Cross-component: `context/drift.md` (PSI cadence + thresholds), `local_runtime/service.py` (the structural cousin — `_feature_history` deque + every-Nth-tick PSI is the local-mode equivalent of what we're building in Lambda).
* ADRs: 0005 (parity boundary), 0007 (PSI cadence + load_reference contract), 0008 (operational reference — already loaded at cold-start), 0009 (PSI surface = 4 features), 0010 (DynamoDB schema; Item 2's STATE-row extension is pre-authorized in §Decision and §Item ordering), 0011 (review cascade — applies to this session's review packet).
* Memory: [[ml_obs_pipeline_shared_parity_boundary]], [[ml_obs_pipeline_parity_load_check]], [[ml_obs_pipeline_fuse_write_truncation]] (default to outputs/cp regardless of file size), [[ml_obs_pipeline_git_on_windows]].

Reference
* `HANDOFF.md §6 Q3` (model packaging — bundled default; affects cold-start size if SNS client adds meaningful weight), §6 Q4 (reference storage — bundled by default).
* `context/_interfaces.md §SNS alert payload` — the wire format the publish branch produces.
* `context/_interfaces.md §PSI parameters` — thresholds 0.10 / 0.25.
* `context/lambda_scorer.md` — §Open questions to close (cold-start latency measurement still deferred; PSI follow-on closes everything else).
* `shared/drift.py::compute_psi` — the function being called. Pure: no I/O, no module-level state. Reference is passed explicitly.
* `local_runtime/service.py` — mode-parity cousin. Same `compute_psi(list(feat_hist), reference=self._reference)` call shape; the only difference is where the window comes from (DynamoDB Query vs in-memory deque).
* `docs/sessions/2026-06-02-lambda_scorer-mvp.md` §"Open follow-ups" — the close-out list this session works through.

Constraints
* FUSE write truncation (per [[ml_obs_pipeline_fuse_write_truncation]] 2026-06-04 update). Default to outputs/cp regardless of file size.
* Parity boundary unchanged. `shared/{features,score,drift}.py` stays at the locked contract. No edits there this session.
* Bash 45 s cap. No long-running training; use `moto` for all AWS mocks, no real AWS calls. moto's `@mock_aws` covers SNS as well as DynamoDB — one context manager.
* Lambda 512 MB memory + 250 MB unzipped deploy zip. boto3.client("sns") is the same boto3 already in the deploy zip — no new heavyweight dep.
* No real AWS spend. All tests run against `moto`.
* PO does git on Windows per [[ml_obs_pipeline_git_on_windows]]. Commit drafts include the canonical PowerShell sequence per DEV_NORMS §7.
* **Reviewer-model loop (ADR 0011) applies to this session's review.** When the packet is ready, `.\scripts\gemini_review.ps1 -Slug <slug>` cycles through gemini → openrouter → groq → cerebras; weight the response against the provenance-footer's named provider per ADR 0011 §Consequences.
* Test count baseline: 361 passed + 1 skipped (post-2026-06-02). Expect net delta of +3 to +5 tests from Item 4.

Definition of done
* ✅ Item 1 query shape decision captured as a session-log note OR (if structurally novel — e.g. introducing a GetItem-then-Query pattern) ADR 0012.
* ✅ Item 2 STATE-row attributes land in code + `context/_interfaces.md` updated.
* ✅ Item 3 SNS publish wired with edge-trigger semantics (assuming PO greenlights) + `SNS_TOPIC_ARN` env var documented in `context/lambda_scorer.md` §Environment variables.
* ✅ Item 4 tests added; suite passes 361 + N where N is the Item 4 test count.
* ✅ Structural-parity tests stay green (`compute_psi` joins the trio of shared-imports guarded by structural-parity tests; add a fourth guard mirroring the existing three).
* ✅ Deploy-zip footprint verified ≤ ADR 0006 §Q4's ~124 MB unzipped baseline (boto3 already counted; SNS adds no new dep).
* ✅ Session log + review packet written. The reviewer-model cascade runs the review per ADR 0011; the response file's provenance footer should now render correctly (post-2026-06-02 footer escape fix).
* ⏳ Carry-forward: cold-start latency measurement post-deploy (`context/lambda_scorer.md` §Open questions); Terraform IaC for the DynamoDB table + SNS topic + IoT Rule + IAM (separate IaC session).

Open questions to raise with PO at plan-step

1. **Query shape (Item 1):** single Query Limit=1800 vs two Queries (150 + 1800). Claude's lean: single. PO call.
2. **PSI cadence (Item 1):** every-invocation compute vs `latest_ts`-gated. Claude's lean: every-invocation. PO call.
3. **Alert dedupe (Item 3):** edge-trigger vs always-publish. Claude's lean: edge-trigger to protect the SNS Always-Free 1000-email/month envelope. PO call.
4. **`alert_flag` semantics on the STATE row:** is it the CURRENT-invocation breach state (overwrites every invocation) or the LAST-PUBLISHED state (used by the edge-trigger)? Affects the dashboards adapter — the panel that lights up red wants current-state, while the edge-trigger wants last-published. Could be two separate attributes (`is_breach_now`, `last_alert_sent_at`).
5. **Session log / ADR shape:** if Item 1's query-shape decision is "single Query Limit=1800", it's a refinement within ADR 0010 (session-log note). If we end up with the GetItem-before-Query pattern for edge-trigger, that's structurally novel — ADR 0012 territory. Decide at plan-step based on which way Items 1 and 3 resolve.

Tone note for the session
The 2026-06-02 MVP session closed cleanly + the multi-provider review cascade landed structurally + the lambda_scorer code already mode-parity-imports `compute_psi` via the cold-start `REFERENCE` capture. This session is the smallest possible structural step — schema-additive, ~50 lines of new handler code, one new external service (SNS). The plan-step discipline matters because the PSI cadence + dedupe questions (Items 1.cadence, 3) shape the test surface, not the production code surface. Resolve those before scaffolding tests; the code lands quickly once the test shape is locked.

Reminder: outputs/cp pattern is the default. The Reviewer-model loop (ADR 0011) applies to this session's review packet — when the cascade runs it, weight findings against the response file's provenance footer.
