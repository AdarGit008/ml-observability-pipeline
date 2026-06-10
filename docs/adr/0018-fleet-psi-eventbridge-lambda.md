# ADR 0018 — Fleet-PSI EventBridge Lambda: Pooled Plant-Wide Drift Detection

- **Status:** Accepted (PO sign-off 2026-06-10; DeepSeek review folded 2026-06-10 — see §Addendum)
- **Date:** 2026-06-10
- **Deciders:** PO (Adar), Claude (architect), DeepSeek (reviewer — ADR 0011 §Addendum 2026-06-10)

## Principle (plain English)

**Some drift only shows up when you look at the whole plant at once.**
The per-pump scorer asks "is *this* pump drifting?" — but a fleet-wide
shift (the `seasonal_drift` scenario, ADR 0004: ambient temperature
rises across the plant and every pump's bearing temp creeps up a
little) can be too small on any single pump to cross the band while
being unmistakable across all of them together. So a scheduled Lambda
**pools the last five minutes of readings from every pump into one
window** and computes a single fleet PSI against the operational
reference. It is the one drift view no per-pump computation provides.
It alerts through the same shared decision and the same edge-trigger
the per-pump scorer uses — only the scope (`pump_id="FLEET"`) differs.

## Context

PLAN.md §2.7 always specified a fleet-level PSI on "an aggregated
5-minute fleet window" (referenced in ADR 0004, 0005, 0007). It was
the last unbuilt piece of the pipeline; ADR 0007 §Follow-ups parked it
("the fleet aggregation logic is the EventBridge Lambda's territory").
Two recent decisions made now the right time:

- **ADR 0017** put the PSI alert decision behind a shared composite
  `psi_alert_should_fire(window, psi)` (warmup gate + 0.25 threshold).
  That composite was built *specifically* so a second alert site — this
  one — could not diverge from the per-pump scorer (north star #6). The
  fleet Lambda is its first consumer.
- The hot path, schema, edge-trigger, and SNS topic all exist (ADR
  0010, 0012); the batcher (ADR 0015) is a working EventBridge-Lambda
  sibling that reads the fleet via a per-pump Query loop. The fleet-PSI
  Lambda is assembled almost entirely from established parts.

This session builds the **handler + tests + this ADR + context only**
(PO scope call 2026-06-10). The Terraform module (EventBridge rule,
Lambda, scoped IAM), the drift-only build script, and the teardown
sweep are deferred to a follow-on infra session — mirroring how the
batcher's logic and its wiring were staged.

## Decision

1. **New component `lambda_fleet_psi`** (compute-Lambda naming, sibling
   of `lambda_scorer` / `lambda_s3_batcher`). EventBridge-scheduled;
   the event payload is unused (the table holds the state).

2. **Pool, don't per-pump-aggregate.** For each pump `P-01..P-NN`,
   read the trailing `FLEET_WINDOW_SAMPLES` (150 = 5 min at the 2 s
   tick) reading rows via the scorer's `Query(PK=pump_id, sk
   begins_with "2", ScanIndexForward=False)` (ADR 0010), then
   **concatenate** every pump's readings into ONE window and call
   `shared.drift.compute_psi` once → a single 4-key fleet PSI. PSI is
   distributional, so pool order is irrelevant. Per-pump-PSI-then-max
   was rejected (see Alternatives) — it largely duplicates the
   `latest_psi` each STATE row already carries.
   The reference is the **single 15-pump pooled** operational reference
   (ADR 0008, `model.train.OPERATIONAL_REFERENCE_PUMPS = 15`);
   `load_reference` takes no `pump_id`, so pooling the live fleet window
   against it is apples-to-apples — the live window is the runtime
   analogue of how the reference was built (DeepSeek review §1).

3. **Read the hot table, not the cold archive.** Per-pump Query loop
   against `pump_hot_state` — the $0 sibling pattern (batcher, ADR
   0015). The 5-minute window is always resident in the hot table; the
   S3/Athena archive would add read cost and Parquet-scan complexity
   for no benefit.

4. **Write a FLEET STATE row + edge-triggered SNS.** A `pump_id="FLEET",
   sk="STATE"` row holds `latest_ts`, `latest_psi` (4-key Map),
   `alert_flag`, `pumps_reporting`, and (on/after first publish)
   `last_alert_sent_at` — the per-pump STATE shape of ADR 0012 with a
   `pumps_reporting` add. The alert is **edge-triggered exactly like
   the per-pump path**: `GetItem` the previous FLEET row, publish to
   SNS only on the False → True `alert_flag` flip, publish AFTER the
   write (at-most-once per edge). `FLEET` is a separate partition, so it
   is invisible to the scorer/batcher per-pump iteration and the
   score-path query (ADR 0010 §Reserved SK) — a plant-level view that
   disturbs no existing access pattern.

5. **Arm via the shared composite; no score path.** `alert_flag =
   psi_alert_should_fire(pooled, psi)` (ADR 0017) — same warmup gate
   and same 0.25 threshold as the scorer. There is no model here and no
   `score > 0.7` branch: fleet alerts are PSI-only (`alert_type =
   "psi_breach"`, no `score` field; `pump_id="FLEET"` marks the scope
   on the shared topic).

6. **Drift-only deployment.** The deploy zip ships
   `shared/{features,drift}.py` + numpy + the operational reference
   JSON — NOT `model.pkl`, NOT sklearn. `load_reference` skips the
   model/reference version check when `model.pkl` is absent (ADR 0007
   §4), so cold start needs only the reference. This is the
   "drift without sklearn" layer the `shared/drift.py` docstring
   anticipated; it keeps the fleet zip small.

7. **Empty fleet is a true no-op.** No reading rows on any pump → no
   STATE write, no alert (mirrors the batcher's empty-batch no-op, ADR
   0015).

## Alternatives considered

### 1. Aggregation semantic

**A. Pool all readings → one fleet PSI (the decision).** A single
plant-wide gauge; catches a fleet-wide distributional shift no per-pump
window reveals. Non-redundant.

**B. Per-pump PSI → max/mean across the fleet.** Compute PSI per pump,
then aggregate the scalars. Rejected: each pump's STATE row *already*
carries its `latest_psi` (ADR 0009/0012); a max-over-pumps is something
the dashboards adapter could derive client-side, and it adds nothing
that catches the pooled-only `seasonal_drift` signal. Pooling is the
value-add.

### 2. Data source

**A. Hot DynamoDB table, per-pump Query loop (the decision).** The
5-minute window is resident; the read pattern + cost are the batcher's,
already proven Always-Free-comfortable.

**B. S3/Athena cold archive.** The archive exists (ADR 0015), but it
lags by the batcher cadence + safety lag, stores Parquet (a scan/parse
step), and Athena queries carry cost. Wrong tool for a 5-minute hot
window. Rejected on cost + latency + complexity.

### 3. Output sink

**A. FLEET STATE row + edge-triggered SNS (the decision).** Reuses the
ADR 0012 two-attribute alert state and the existing topic; gives the
dashboard a row to surface and the edge-trigger a dedupe state.

**B. SNS alert only, no persistent row.** Simpler, but no dashboard/
audit trail and no clean edge-trigger state — it would re-page every 5
minutes while a breach persists, burning the Always-Free email envelope
(the exact failure ADR 0012 exists to prevent).

**C. CloudWatch custom metric + alarm.** AWS-idiomatic, but introduces
an observability surface the project doesn't otherwise use, and custom
metrics/alarms have cost-tier implications against north star #1.
Rejected as scope creep.

### 4. Where the alert decision lives

**A. The shared `psi_alert_should_fire` (the decision).** The whole
point of ADR 0017 §1: one definition, two sites, no divergence. A
structural-parity guard pins the import.

**B. A fleet-local threshold.** Re-introduces exactly the divergence
risk ADR 0017 closed. Rejected.

## Consequences

**Positive:**

- **The last pipeline component lands** — and the demo gains a
  plant-wide drift story (`seasonal_drift`) the per-pump panels can't
  tell on their own.
- **Zero new parity risk.** Arming goes through the shared composite;
  `compute_psi` is untouched; three structural-parity guards pin the
  `shared/` imports. The fleet site cannot drift from the scorer.
- **No disturbance to existing access patterns.** `FLEET` is its own
  partition; scorer, batcher, and the current adapter never read it.
- **Cheap.** One invocation / 5 min; per run ≈ 15 Queries (≤150 rows
  each) + 1 GetItem + 1 PutItem — far inside the envelope the batcher
  already validated (ADR 0013/0015). SNS bounded by incident count
  (edge-trigger).
- **Small cold start.** Drift-only zip (numpy + `shared/` + reference),
  no sklearn/model.pkl.

**Negative:**

- **Pooling hides *which* pump moved.** The fleet PSI says "the plant
  is drifting," not "P-07 is drifting" — by design; the per-pump
  `latest_psi` rows answer the latter. The two views are complementary.
  It is also a **late indicator** for a single drifting pump — one pump
  is ~1/N of the pooled mass, so its per-pump alert fires first; fleet
  PSI is for *systemic* shifts (`seasonal_drift`) (DeepSeek review §1).
- **The FLEET SNS payload deviates from the per-pump shape.** It carries
  `scope:"fleet"` + `score:null` (no model on the fleet path) where the
  per-pump payload carries a real `score` (DeepSeek review §2). A small,
  explicit contract extension; consumers filter on `scope`/`pump_id`.
- **The dashboard doesn't surface FLEET yet.** The adapter's
  `BatchGetItem` reads the 15 pump STATE keys; a FLEET panel is a small
  follow-on adapter change (Follow-ups).
- **Sample-count, not wall-clock, window.** Inherits ADR 0017's
  semantics; consistent with how `compute_psi` already thinks about a
  window.

**Follow-ups:**

- **Infra session:** `infra/modules/fleet_psi` (EventBridge
  `rate(5 minutes)` + Lambda + invoke permission; IAM = Query + GetItem +
  PutItem on the table ARN + `sns:Publish` on the topic ARN + scoped logs — the
  no-extra-access tripwire), `scripts/build_fleet_psi.{ps1,sh}`
  (drift-only staging: numpy + `shared/` + reference JSON; footprint
  check per ADR 0006 §Q4), `SNS_TOPIC_ARN`/`FLEET_SIZE` env wiring,
  and `aws_teardown.sh` additions (function + log group + EventBridge
  rule + the FLEET STATE row, though `force_destroy`/table teardown
  already sweeps the row).
- **Dashboards session:** a FLEET panel — adapter reads the FLEET STATE
  key alongside the 15 pump keys; a small, additive change to ADR 0014's
  contract.
- **Live verify:** confirm a `seasonal_drift` scenario arms the FLEET
  alert post-warm-up (rolls into the deferred demo-day rehearsal).

## References

- PLAN.md §2.7 — fleet-level PSI on the aggregated 5-minute window
  (what this ADR implements).
- ADR 0004 — `seasonal_drift` scenario ("fleet-level PSI catches it"),
  the motivating case.
- ADR 0017 — the shared `psi_alert_should_fire` composite this Lambda
  consumes; warmup gate + threshold.
- ADR 0012 — edge-triggered SNS + two-attribute alert state (the alert
  pattern mirrored on the FLEET row).
- ADR 0010 — DynamoDB schema; `FLEET` is a clean separate partition;
  the per-pump Query access pattern reused here.
- ADR 0015 — the EventBridge-Lambda sibling (per-pump fan-out read,
  empty no-op, FLEET_SIZE→P-NN expansion).
- ADR 0007 §4 — `load_reference` skips the version check when
  `model.pkl` is absent → the drift-only deployment.
- ADR 0005 — parity boundary; the structural-parity guards.
- Implementation: `lambda_fleet_psi/handler.py`,
  `lambda_fleet_psi/tests/test_handler.py`.
- Session log: `docs/sessions/2026-06-10-fleet-psi-lambda.md`.


## Addendum 2026-06-10 — DeepSeek review dispositions

Source: `review_responses/2026-06-10-fleet-psi-lambda.md` (deepseek-reasoner).
The one "blocking" point rested on an incorrect premise and is resolved
by clarification; the rest were accept-with-note. Net: small doc +
payload changes, no algorithm change.

### §1 — Pooling statistics / what `REFERENCE` is (Resolved — premise corrected + caveat added)

DeepSeek flagged this "blocking," suspecting `REFERENCE` is a *per-pump*
baseline (e.g. `references/FLEET.json`), which would make pooling 15
pumps against one pump's reference unsound.

**Verified against the code:** there is exactly one reference file,
`model/artifacts/operational_reference_distribution.json`;
`shared.drift.load_reference()` takes **no `pump_id`**; and `model.train`
builds it by pooling **15 pumps × 1800 HEALTHY ticks**
(`OPERATIONAL_REFERENCE_PUMPS = 15`, ADR 0008 — "matches the demo fleet's
pump count … fifteen pumps average out per-pump noise"). The reference is
already a 15-pump pooled distribution, so pooling the fleet's 15 live
windows against it is apples-to-apples — the live window is the runtime
analogue of how the reference was built. The algorithm is **sound as
written**; no refactor.

Where DeepSeek is right is the *framing*: added the caveat that fleet PSI
is a **systemic-shift detector and a late indicator** for any single
pump (≈1/N of pooled mass; per-pump alert fires first) — to the handler
docstring, §Decision §2, §Negative, and `context/lambda_fleet_psi.md`.
Heterogeneity (§1b) is moot here: the simulator fleet is homogeneous
(all pumps share `DEFAULT_PROFILES`, ADR 0002/0008) and the reference
averages across 15 of them; a healthy pooled window reads STABLE, pinned
by `test_pool_across_pumps_writes_fleet_row`.

### §2 — Score-less FLEET payload (Accepted)

Added `"scope":"fleet"` (generic subscribers filter without parsing
`pump_id`) AND `"score":null` (schema consistency) to the fleet payload;
`_interfaces.md` §SNS note + the test updated.

### §3 — Shared SNS topic (Accept, no change)

Right call for $0; `scope`/`pump_id` differentiate; per-consumer routing
is an SNS filter-policy concern.

### §4 — Hot-table read / pagination (Accept with guard)

Added a `log.warning` if `_read_pump_window`'s Query returns a
`LastEvaluatedKey` + a comment on the 1 MB / ~150-rows-at-0.5 Hz
guarantee. Warn-not-fail matches the scorer's single-Query posture.

### §5 — Empty vs always-write (Accept, no change)

Empty no-op is correct (missing row = "no data", not "no drift"); follows
the batcher precedent.

### §6 — Warmup gate vestigial at fleet scale (Accept with comment)

True — the pooled window almost always clears `PSI_MIN_SAMPLES`. Kept for
parity (arm through the same shared decision) and annotated in the handler.

### Minor

- `FLEET_PUMP_IDS` from `FLEET_SIZE`: the established project convention
  (identical to the batcher); `FLEET_SIZE` is the single fleet-size knob.
  Not a flaw.
- "`load_reference` for FLEET / `references/FLEET.json`": no such concept
  (see §1).

### Test count after dispositions

Sandbox full suite (excl. `lambda_s3_batcher`, pyarrow-gated): **425
passed, 1 skipped** — unchanged (the fold touched a payload assertion +
docs, added no tests). 9 fleet-PSI tests + 3 structural-parity guards green.


## Addendum 2026-06-10 (infra) — fleet-PSI deployable; DeepSeek review folded

Source: `review_responses/2026-06-10-fleet-psi-infra.md` (deepseek-reasoner).
Covers the deferred infra half: `infra/modules/fleet_psi`,
`scripts/build_fleet_psi.{ps1,sh}`, root wiring, teardown sweep. Build +
`terraform validate`/`plan` only — NO apply (stack stays down, $0). Net:
one design change (S3 upload), one doc fix (IAM wording); no algorithm or
handler change.

- **§2 S3 deploy path (Accepted — design change).** The module originally
  used a direct `filename` upload (the adapter pattern). Switched to the
  S3 `deploy/` path used by the scorer and batcher: it keeps the three
  wheel-shipping Lambdas on one upload mechanism (north star #5),
  eliminates the 50 MB direct-upload ceiling on any future numpy bump, and
  would already be required if sklearn were ever added. `code_bucket` is
  the archive bucket; `deploy/` is outside the Glue `year=*` projection
  and is swept by the bucket's `force_destroy`.
- **§1 IAM wording (Accepted — doc fix).** §Follow-ups above abbreviated
  the table grant as "Query"; the handler's edge-trigger read + STATE
  write also need `GetItem` + `PutItem`. The grant is exactly those three,
  scoped to the single table ARN (no `Scan`/`BatchGetItem`/`UpdateItem`/
  `DeleteItem`). Known breadth: IAM `LeadingKeys` can't restrict to the
  `FLEET` partition / `STATE` sort key (same caveat as the batcher); held
  by code review + single call sites + tests.
- **§3 `reserved_concurrent_executions = -1` (Accept, comment present).**
  New-account concurrency floor (min-10-unreserved) blocks a reservation,
  same as the batcher. Safe: the FLEET STATE row is idempotent-overwrite
  and the edge-trigger GetItem→PutItem is at-most-once-per-edge. Annotated
  in `main.tf`. Restore to 1 after a quota bump.
- **§4 log-group race / §5 env known-after-apply (Accept, no change).** The
  `depends_on` creation order is correct; the env block reads opaque in
  `plan` only because `SNS_TOPIC_ARN` is known-after-apply (same as the
  scorer) — not masking the static `DDB_TABLE_NAME`/`FLEET_SIZE`.
- **§6 teardown (Accept, no change — by design).** The sweep keeps its
  *verify-don't-delete* charter: `terraform destroy` deletes the rule +
  target + permission (the target cascades with the rule; the permission
  is removed with the function), and the sweep FAILS loudly on any
  residue. Manual `remove-targets`/`delete-rule` is declined — it would
  diverge from the batcher rule's identical assert-absence handling.
- **Extras (verified, no change).** numpy pinned to `2.4.6` (matches the
  scorer's `lambda_requirements.txt` — mode parity); `memory_mb=256` /
  `timeout_s=30` defaults present and plan-confirmed; `module.sns`
  dependency is tracked via the `topic_arn` reference; `retention_in_days
  = 7` set.
