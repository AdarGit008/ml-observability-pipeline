# ADR 0012 — Edge-Triggered SNS Alerts and Two-Attribute Alert State

- **Status:** Accepted (PO sign-off 2026-06-02; reviewer-cascade review pending)
- **Date:** 2026-06-02
- **Deciders:** PO (Adar), Claude (architect), reviewer cascade (pending)

## Principle (plain English)

**Alert on the edge, not the level.** A pump that crosses an alert
threshold is news once — the moment it crosses. The same pump still
being over the threshold two seconds later is not news; it's the same
incident. The handler therefore publishes to SNS only when the alert
state *flips* from healthy to breached, and the STATE row carries two
distinct pieces of alert state because two different consumers need
two different answers: the dashboards adapter asks "is this pump
breaching *right now*?" (`alert_flag`, overwritten every invocation)
and the dedupe logic asks "did we already tell anyone?"
(`last_alert_sent_at`, written only when a publish happens).

Conflating those two into one attribute forces one consumer to
reconstruct its answer from the other's bookkeeping. Keeping them
separate costs one DynamoDB Map attribute and makes both reads
trivial.

The rest of this ADR is the context, the alternatives, and the
failure-ordering trade-off the edge-trigger accepts.

## Context

The PSI follow-on session (2026-06-02) added the alert path to
`lambda_scorer/handler.py`: `alert_flag = max(psi.values()) > 0.25 OR
score > 0.7` per `_interfaces.md §SNS alert payload`, publishing to
the topic named by the new required `SNS_TOPIC_ARN` env var.

The open question was dedupe. The fleet runs 15 pumps × ~30
messages/min. A pump that enters a degraded state and stays there
breaches on *every* invocation — ~30 alerts/min/pump if every breach
publishes. Two constraints make that untenable:

- **SNS Always-Free is 1,000 email deliveries/month** (north star #1:
  $0 lifetime AWS cost). One persistently-degraded pump in a single
  30-minute demo would burn ~900 of them.
- **Alert fatigue is the failure mode PSI exists to avoid.** ADR 0007
  Q5 (Gemini): a drift metric that cries constantly "becomes
  unactionable due to constant false positives." Same logic applies
  one layer up, at the alert channel.

The complication: the Lambda is stateless (ADR 0010), so "did the
previous invocation breach?" has to come from DynamoDB. The previous
state lives on the STATE row — which the score-path window query
deliberately excludes (`sk begins_with "2"`). Edge-triggering
therefore needs a read the MVP didn't have.

A second-order question rode along (plan-step Q4): when the
dashboards adapter lands, its "light the panel red" read wants
*current* breach state, while the edge-trigger wants *last-published*
state. One attribute can't serve both without one consumer doing
inference.

## Decision

1. **Edge-trigger.** The handler publishes to SNS only when
   `alert_flag` transitions False → True. The previous value is read
   via a `GetItem` on the STATE row immediately before the STATE
   overwrite — one extra eventually-consistent read (half an RCU) per
   invocation. A persisting breach publishes exactly once.

2. **Two-attribute alert state on the STATE row** (extension
   pre-authorized by ADR 0010 §Decision):
   - `alert_flag` (bool) — CURRENT-invocation breach state,
     overwritten every invocation. The dashboards adapter's
     "red now" read.
   - `last_alert_sent_at` (ISO-8601 ts) — set to the invocation `ts`
     when a publish fires; carried forward verbatim otherwise;
     ABSENT until the pump's first publish (no null sentinel — a
     missing attribute reads as "never alerted").

3. **Publish ordering: after the STATE write.** The publish decision
   is made before the write (it needs the previous flag), but the
   `_SNS.publish` call executes after the STATE row lands. See
   §Consequences for the at-most-once trade-off this picks.

4. **Reset semantics are implicit.** When a breach clears,
   `alert_flag` lands False and the next False → True flip publishes
   again. No cooldown window, no hysteresis band — a pump oscillating
   across the threshold publishes on each rising edge. Accepted at
   demo scale; §Consequences names the production-shaped alternative
   (hysteresis or a min-interval gate on `last_alert_sent_at`).

## Alternatives considered

### 1. Dedupe strategy

**A. Edge-trigger via previous STATE read (the decision).** One
GetItem per invocation. Bounded alert volume regardless of how long
a breach persists. The dedupe lives where the state lives.

**B. Always-publish, dedupe downstream.** Simplest handler (no
GetItem, no flip logic); push throttling to the SNS subscription or
the consumer. Rejected: SNS has no native per-topic dedupe for
standard topics; email subscribers get the raw firehose. The 1,000
deliveries/month Always-Free envelope dies in one demo. FIFO topics
have dedupe but a 5-minute dedupe window and no email protocol
support — wrong tool.

**C. Time-gate on `last_alert_sent_at` (publish at most every N
minutes).** Equivalent read cost to A but different semantics —
a persisting breach re-pages every N minutes. Operationally
defensible (PagerDuty-style re-page) but strictly more code than A
for behaviour the demo doesn't need. The attribute A lands is the
one C would need; C stays reachable without schema change.

### 2. Alert-state attribute shape

**A. Two attributes (the decision).** `alert_flag` = now;
`last_alert_sent_at` = last publish. Each consumer reads its own
attribute literally.

**B. Single `alert_flag` doing double duty.** The edge-trigger
compares against the previous `alert_flag` — workable (it's what
the flip logic reads anyway) — but leaves no record of *when* an
alert went out, which the dashboards adapter and any future
re-page logic both want. Rejected: saves one attribute, loses the
audit trail.

**C. Last-published state only (`alert_active`).** Inverts B's
problem: dashboards wanting "red now" would have to recompute
breach from `latest_psi` + `latest_score`, duplicating threshold
logic client-side. Rejected on the same duplication-of-contract
grounds as ADR 0009's "don't make consumers re-derive."

### 3. Where the previous-state read happens

**A. Dedicated `GetItem` before the STATE write (the decision).**
Explicit, single-purpose, half an RCU eventually-consistent.

**B. Fold the STATE row into the window Query.** Drop the
`begins_with "2")` predicate and fish the STATE row out of the
result. Rejected: re-opens the ADR 0010 reserved-SK filter
invariant that `test_handler_window_query_excludes_state_row` +
`test_state_sk_outside_year_range_filter` exist to pin, saves
half an RCU, and couples two unrelated access patterns.

**C. `ConditionExpression` on the STATE PutItem + exception-driven
publish.** Write-time conditional ("only set last_alert_sent_at if
alert_flag was False") avoids the read but turns control flow into
exception handling and still needs the read to build the carried-
forward attributes. Rejected as cleverness with no payoff.

## Consequences

**Positive:**

- **Alert volume is bounded by incident count, not invocation
  count.** A demo scenario that degrades two pumps produces two
  emails, not nine hundred. The Always-Free envelope survives any
  demo length.
- **Both alert consumers read literal attributes.** Dashboards:
  `alert_flag`. Dedupe/audit: `last_alert_sent_at`. No client-side
  threshold re-derivation.
- **The hot path stays four DynamoDB operations** (Query, PutItem
  reading, GetItem STATE, PutItem STATE) — one more than MVP, all
  single-digit milliseconds, well inside the 2 s tick budget and
  the Always-Free RCU/WCU envelope (~13.5 K invocations/demo).
- **Time-gated re-paging (alternative 1C) stays one small diff
  away** — the attribute it needs is already on the row.

**Negative:**

- **At-most-once per edge.** Publish-after-write means: if the
  publish raises after the STATE row landed (`alert_flag=True`,
  `last_alert_sent_at` set), the invocation errors loudly in
  CloudWatch, but the IoT-Rule retry re-runs against
  `prev alert_flag == True` and does NOT re-publish — the email is
  lost. The inverse ordering (publish-before-write) converts lost
  alerts into duplicate alerts on the write-failure path.
  Lost-on-publish-failure was chosen because the failure is loud
  (Lambda error metric + log) while a duplicate is silent noise,
  and because SNS publish failures at demo volume are rarer than
  the demo is long. A production deployment that prefers
  duplicates flips two statements and accepts idempotent
  consumers.
- **No hysteresis.** A pump oscillating across 0.25 publishes on
  every rising edge. At demo scale with scripted scenarios this
  doesn't occur; production would add a min-interval gate on
  `last_alert_sent_at` (alternative 1C) or a two-threshold
  hysteresis band.
- **The GetItem reads the row the handler is about to overwrite.**
  A reader at the wrong moment could see the row between read and
  write — but the only writer for a given pump's STATE row is this
  handler processing that pump's messages (~0.5/sec, serially
  triggered), so read-modify-write races are theoretical at this
  fleet size. ADR 0010 §Item ordering's TransactWriteItems upgrade
  path covers the future where that stops being true.

**Follow-ups:**

- Dashboards adapter consumes `alert_flag` + `last_alert_sent_at`
  via the existing BatchGetItem pattern (no new access pattern).
- IaC session: SNS topic Terraform module + `SNS_TOPIC_ARN` env
  wiring on the Lambda + `sns:Publish` scoped to the topic ARN in
  the execution-role policy + email subscription for the demo.
- Cold-start latency re-measurement still deferred
  (`context/lambda_scorer.md` §Open questions) — the SNS client
  adds one more boto3 client construction at init; expected
  negligible, measured post-deploy.

## References

- `_interfaces.md §SNS alert payload` — the wire format + threshold
  definition this ADR's publish branch produces.
- `_interfaces.md §DynamoDB schema` — STATE-row attributes landed
  this session.
- ADR 0007 — PSI thresholds + the alert-fatigue rationale (Gemini
  Q5) that motivates edge-triggering.
- ADR 0009 — `latest_psi` is the 4-key surface; the payload's `psi`
  map mirrors it.
- ADR 0010 — schema; the STATE-row extension this ADR's attributes
  land was pre-authorized in §Decision; §Item ordering carries the
  TransactWriteItems upgrade path.
- Implementation: `lambda_scorer/handler.py` (edge-trigger +
  publish ordering), `lambda_scorer/tests/test_handler.py`
  (`test_sns_publish_on_threshold_breach`,
  `test_sns_no_publish_when_healthy`,
  `test_sns_no_republish_when_still_breached`).
- Session log: `docs/sessions/2026-06-02-lambda_scorer-psi-followon.md`
  (also carries the single-Query window decision — an ADR 0010
  refinement, not part of this ADR).
