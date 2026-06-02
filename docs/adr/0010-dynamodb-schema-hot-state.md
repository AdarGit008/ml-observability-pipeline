# ADR 0010 — DynamoDB Schema for Hot State

- **Status:** Accepted (PO sign-off 2026-06-02; Gemini review pending)
- **Date:** 2026-06-02
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer — pending)

## Principle (plain English)

**One row per reading, one row per pump's latest snapshot.** History
and "what's happening right now" are two different access shapes;
the table layout reflects that.

Each scoring invocation writes a **reading row** keyed
`(pump_id, timestamp)` that records the telemetry, the extracted
features, and the model's failure probability for that one message.
These rows accumulate, oldest-to-newest, into the per-pump history
the rolling feature window (and, later, the PSI window) reads from.
Each scoring invocation also overwrites a single **STATE row** keyed
`(pump_id, "STATE")` that holds the pump's latest snapshot — most
recent timestamp, most recent score, and (in the PSI follow-on) the
latest PSI dict + alert flag. Two items per invocation, two access
shapes from one table.

The asymmetry between "history-is-a-range-of-rows" and
"latest-is-one-row" is what makes the dashboards adapter trivial:
Grafana panel refresh becomes a single `BatchGetItem` across the 15
STATE rows instead of 15 separate `Query` calls hunting for "most
recent row per pump." The same separation gives the PSI follow-on a
clean place to land — the STATE row grows new attributes,
the reading-row schema doesn't change.

The reading row IS the source of truth. The STATE row is an
opportunistic snapshot, eventually consistent with the most recent
reading row. The hot path writes both; if a write fails partially,
the reading row wins because that's the row the next score path
reads from.

The rest of this ADR is the context that surfaced the decision, the
three options considered, and the mechanics that lock the schema
across access patterns.

## Context

`HANDOFF.md §6 Q5` framed three composite-key options for the
hot-state DynamoDB table; the decision was deliberately deferred
until the lambda_scorer session because every other access pattern
(IoT Rule → scorer → write, scorer → dashboards → read, future
PSI window) depends on knowing the key shape. That session is now;
this ADR resolves Q5 and locks the schema for the project's lifetime.

The three options carried forward from HANDOFF.md §6 Q5:

1. **A. `PK = pump_id, SK = timestamp`.** One row per reading; "all
   readings for pump P" is a single `Query` with a sort-key range;
   per-pump partition is wide but never hot enough to matter at
   demo fleet size.
2. **B. `PK = pump_id#bucket(1min), SK = timestamp`.** Same row shape
   as A but partitioned by minute-bucket to avoid hot per-pump
   partitions. Better dispersion, harder queries (multi-bucket
   coalesce for any window larger than one minute).
3. **C. `PK = pump_id, SK = state`.** One mutable row per pump; each
   write overwrites. Cheapest writes, no history.

Constraints driving the decision:

- **Fleet size and message rate.** 15 pumps × 30 messages/min =
  7.5 invocations/sec across the fleet, ~0.5/sec per pump. Per-pump
  partition write throughput is four orders of magnitude under
  DynamoDB's per-partition 1000 WCU floor.
- **The MVP hot path needs the last 150 readings per pump** for
  `shared.features.extract_features` (5-minute rolling window at
  2 s/tick).
- **The PSI follow-on needs the last 1800 readings per pump** for
  `shared.drift.compute_psi` (1-hour PSI window).
- **The dashboards session needs "latest score per pump"** for
  Grafana panel refresh.
- **The Lambda must be stateless across containers** per the AWS
  Lambda north star — per-pump rolling state can't live in
  container memory because at 7.5 inv/sec across 15 pumps no single
  container reliably sees a full per-pump history.
- **Always-Free DynamoDB caps** (25 GB, 25 WCU, 25 RCU). Demo
  volume is ~13.5 K invocations per 30-minute demo; storage and
  throughput both well inside the envelope.
- **Constraint #4 (mode parity)** — the scoring path is shared
  shape with `local_runtime`. Local mode holds the window in an
  in-memory deque per `local_runtime/window.py`; AWS mode
  reconstructs it from DynamoDB. The reading-row schema is what
  makes the AWS-mode reconstruction equivalent to the local-mode
  deque.

## Decision

**Adopt Option A with a STATE-row sibling.** Two item shapes share
one table:

### Reading row

```
PK = pump_id            # e.g., "P-07"
SK = <ISO-8601 ts>      # e.g., "2026-06-02T14:32:01.123Z"
vibration_amp = <float>
bearing_temp  = <float>
motor_current = <float>
rpm           = <float>
score         = <float>  # P(failure_48h) ∈ [0, 1]
```

One row per IoT Rule invocation. Sort-key strings are
lexicographically equivalent to chronological order (ISO-8601 UTC
with millisecond precision per `_interfaces.md §Telemetry payload`),
so range queries are natural. The four raw telemetry fields land in
the row (not just the score) so the score-path query can
reconstruct the rolling feature window from a single
`Query` — no need to fetch a second table or join across items.

### STATE row

```
PK = pump_id
SK = "STATE"
latest_ts    = <ISO-8601 ts>
latest_score = <float>
# PSI follow-on adds:
#   latest_psi  = { vibration_amp: float, bearing_temp: float, motor_current: float, rpm: float }
#   alert_flag  = bool
```

One row per pump, overwritten every invocation. The reserved sort
key literal `"STATE"` puts it in the same partition as the reading
rows but sorts outside any timestamp range (timestamps start with
digits; `"STATE"` starts with `S`). Dashboards read the 15 STATE
rows via a single `BatchGetItem` per panel refresh.

### Access patterns (locked)

| Pattern | Operation | Notes |
|---|---|---|
| Hot path: read rolling window | `Query(PK=pump_id, SK begins_with "2", Limit=150, ScanIndexForward=False)` then reverse to oldest-first | The `begins_with "2"` predicate filters out the STATE row (whose SK starts with `S`). Limit=150 = 5-minute window at 2 s/tick. |
| Hot path: append reading | `PutItem` on the reading row | One per invocation. |
| Hot path: overwrite STATE | `PutItem` on the STATE row | One per invocation. Idempotent. |
| PSI follow-on: read 1-hour window | Same `Query` as hot-path read, `Limit=1800` | Schema unchanged. Only the query parameter widens. |
| Dashboards: latest snapshot per pump | `BatchGetItem` across the 15 STATE keys | One DynamoDB call per panel refresh. |
| Retention sweep (future) | `Query(PK=pump_id, SK < <cutoff>)` + `BatchWriteItem(Delete)` | TTL is the production-clean alternative; demo doesn't need it. |

### Item ordering

The reading PutItem and the STATE PutItem are **not** issued via
`TransactWriteItems`. Two separate calls, no atomic ordering. This
is acceptable because:

- The STATE row is an opportunistic snapshot, not a source of
  truth. Brief inconsistency (STATE updated but reading row not
  yet visible, or vice versa) converges within milliseconds and is
  invisible to a Grafana panel refresh.
- The hot path always reads the reading rows (never the STATE row)
  to reconstruct the rolling window, so STATE drift doesn't
  corrupt the score.
- If a future feature needs atomic guarantees across the two rows
  (e.g., an alert-flag race), `TransactWriteItems` is a drop-in
  swap with a small additional cost.

### Capacity mode

On-demand (`PAY_PER_REQUEST`). The Always-Free 25 RCU/WCU baseline
applies to provisioned tables; on-demand is the simpler choice for
ephemeral demo workloads and stays inside the 25 RCU/WCU equivalent
for bursts of this size. The IaC session locks the actual capacity
mode in Terraform.

## Alternatives considered

### 1. Composite-key shape

**A. `PK = pump_id, SK = timestamp` (the decision).** Cleanest
mapping from "all readings for a pump in a window" → a single
`Query`. STATE row coexists via the reserved `"STATE"` sort key
in the same partition.

**B. `PK = pump_id#bucket(1min), SK = timestamp`.** Sharded by
minute-bucket to spread writes across more partitions. Theoretical
benefit: better dispersion under hot-pump load. Real cost at this
scale: zero benefit (per-pump partition is at ~0.5 WCU/sec, four
orders of magnitude under DynamoDB's hot-partition floor) plus
query complexity (a 5-minute window crosses five buckets; a 1-hour
window crosses sixty; coalescing 60 query results in the handler
is real code). Rejected: solves a problem we don't have, at the
cost of code we'd then have to maintain.

**C. `PK = pump_id, SK = state` (single mutable row).** Cheapest
writes — one `UpdateItem` per invocation — but loses every reading
that wasn't the most recent. The MVP hot path needs the last 150
readings for `extract_features`; the PSI follow-on needs the last
1800. Reconstructing this from "just the latest row" would require
either a second mechanism (Kinesis Streams, DynamoDB Streams →
buffer, etc.) or a fundamental schema change mid-project.
Rejected: drops the history both phases need.

### 2. Where the STATE row lives

**A. Same table, reserved SK literal `"STATE"` (the decision).**
One table, one IAM scope, one Terraform module. Reading rows and
STATE row share a partition per pump; query patterns separate them
via SK prefix. Dashboards adapter `BatchGetItem`s the 15 STATE
items by exact key — no `Scan`, no `Query` per pump.

**B. Separate `pump_state` table.** Cleaner separation of concerns
(history table vs. snapshot table) but doubles the IaC surface and
adds a second IAM policy for the lambda_scorer execution role.
Future PSI follow-on would write to both tables per invocation —
same two-PutItem cost, more moving parts. Rejected: the SK-prefix
discriminator inside one table is simpler and gives the same
operational properties.

**C. No STATE row — dashboards adapter queries "latest row per
pump."** Forces the dashboards session to invent a per-pump
`Query(Limit=1, ScanIndexForward=False)` pattern that runs 15
times per panel refresh, then rewrites it when PSI lands and the
adapter needs to surface `latest_psi`. The STATE row is five extra
lines of handler code that unblocks the dashboards adapter
cleanly. Rejected on integration cost: deferring saves nothing,
costs downstream rework.

### 3. ADR shape

**A. New ADR 0010 (the decision).** Q5 is the project's first
DynamoDB schema decision; the choice sticks for the project's
lifetime (undoing it means data migration, not code rewrite).
Structurally novel — distinct from ADR 0005's parity boundary,
ADR 0006's model packaging, ADR 0007's PSI cadence. Deserves its
own ADR per the same logic that produced ADR 0008 vs ADR 0009 last
session.

**B. Session-log note inside the lambda_scorer MVP session log.**
Smaller doc surface but conflates "we shipped the MVP" with "we
locked the table schema." A future engineer hunting for "why is
the SK an ISO string?" reads the session log only if they already
know the right session. ADRs are the discoverable layer. Rejected:
the visibility argument wins.

## Consequences

**Positive:**

- **One table, two access shapes, three access patterns.** Reading
  history is a `Query` with a SK range; latest snapshot is a
  `GetItem` (single-pump) or `BatchGetItem` (fleet). The hot path
  uses two of these per invocation.
- **PSI follow-on is additive only.** The reading-row schema stays
  fixed (only the query `Limit` widens from 150 to 1800); the
  STATE row gets two new attributes (`latest_psi`, `alert_flag`).
  No schema migration, no breaking change.
- **Dashboards adapter is trivial.** Single `BatchGetItem` over 15
  STATE keys per panel refresh, vs. 15 separate queries hunting
  for "latest row per pump." Adapter code is ~10 lines of boto3
  instead of an internal coalesce loop.
- **Lambda statelessness preserved.** Per-pump rolling state lives
  in DynamoDB (the reading rows); the Lambda is a pure function
  from `(input event, table state)` → `(table writes)`. Cold
  containers and warm containers produce the same answer.
- **Mode parity intact.** `shared/features.py` and
  `shared/score.py` are unchanged. The AWS-mode "reconstruct
  window from DynamoDB" path produces the same feature dict as
  the local-mode "window from in-memory deque" path because both
  feed `extract_features` an ordered list of telemetry dicts.
- **Always-Free envelope comfortable.** Demo write volume:
  13.5 K invocations × 2 PutItems = 27 K writes per demo. Read
  volume: 13.5 K queries (150 rows ÷ 1 KB each ≈ 150 KB/query) +
  STATE GetItems for dashboards. All well inside 25 RCU/WCU
  equivalent on-demand.

**Negative:**

- **Two PutItems per invocation instead of one.** ~0.5 ms extra
  hot-path latency, ~negligible on a 2 s tick budget. Adds a tiny
  amount of WCU consumption that doesn't materially affect the
  Always-Free margin.
- **STATE-row eventual consistency.** Brief windows where the
  STATE row reflects an earlier invocation while a later reading
  row is in flight. Acceptable because: dashboards are
  eventual-consistency tolerant, and the next invocation will
  overwrite the STATE row within ~33 ms (the per-pump tick
  cadence). If a future feature requires atomic ordering,
  `TransactWriteItems` is the upgrade path.
- **Per-pump partition concentration.** Option A doesn't disperse
  writes across partitions the way Option B would. At 0.5 WCU/sec
  per pump this is invisible — but a future scale-up (e.g., a
  1000-pump demo) would force a re-evaluation. ADR 0010 is the
  re-opening point if that happens.
- **Reserved SK literal `"STATE"`.** Adds a small piece of
  schema convention that the score-path read must filter out
  (`SK begins_with "2"` excludes it, since ISO-8601 timestamps
  start with year digits). Any future SK convention (e.g., a
  `"META"` row) has to coexist with this rule. Tolerable; the
  alternative (a separate STATE table) costs more.

**Follow-ups:**

- **PSI compute + STATE-row extension.** The next lambda_scorer
  session adds the `compute_psi` call on the 1-hour window
  (same query, `Limit=1800`), writes `latest_psi` + `alert_flag`
  to the STATE row, and publishes to SNS on threshold breach.
  Schema-additive only.
- **Terraform module for the table.** The IaC session creates
  the `pump_hot_state` table in `infra/modules/dynamodb/` with
  the schema above. Capacity mode locked then; on-demand is
  the recommendation per §Decision.
- **Retention strategy.** Demo doesn't need it; production
  clean-up would set TTL on reading rows (e.g., 24 hours) and
  let DynamoDB sweep. Out of scope here.

## References

- `HANDOFF.md §6 Q5` — the open question this ADR resolves.
- `context/_interfaces.md §DynamoDB schema` — updated from TBD to
  the resolution in this session.
- `context/lambda_scorer.md` — DynamoDB schema removed from
  §Open questions; ADR 0010 added to §Related ADRs.
- ADR 0005 — parity boundary. `lambda_scorer/handler.py` imports
  `shared.features.extract_features` + `shared.score.score` +
  `shared.drift.load_reference` as peers; the structural-parity
  test enforces this.
- ADR 0006 §Q4 — deploy-zip footprint baseline (~124 MB
  unzipped). DynamoDB schema doesn't affect zip size; included
  for cross-reference with the lambda_scorer session.
- ADR 0007 — PSI cadence + `load_reference` contract. The
  forward-looking PSI window storage commitment (DynamoDB-backed,
  not in-process) lives in this ADR; the cadence + formula stay
  in 0007.
- ADR 0008 — operational reference source. Read by cold-start via
  `load_reference`; unaffected by table schema.
- ADR 0009 — PSI surface ≠ scorer feature set. STATE row's future
  `latest_psi` attribute holds the 4-element PSI dict per ADR 0009.
- Implementation: `lambda_scorer/handler.py` (cold-start +
  hot-path against the schema above), `lambda_scorer/tests/test_handler.py`
  (moto-backed table mock + the three access patterns under test).
- Session log: `docs/sessions/2026-06-02-lambda_scorer-mvp.md`.
- Review packet: `review_packets/2026-06-02-lambda_scorer-mvp.md`.
