# ADR 0015 — Cold-Path Batcher: Watermark Read Pattern, pyarrow, 60 s Cadence

- **Status:** Accepted (PO sign-off 2026-06-04; reviewer-cascade review pending)
- **Date:** 2026-06-04
- **Deciders:** PO (Adar), Claude (architect), reviewer cascade (pending)

## Principle (plain English)

**The batcher drains; it never computes.** Every 60 seconds an
EventBridge rule wakes a small Lambda that asks each pump's partition
one question — "which reading rows landed since my last visit?" —
writes the answer to one Parquet file in S3, and records where it
stopped. The "where it stopped" mark is a **watermark row** living in
the same table under a second reserved sort key, `"WATERMARK"`,
sibling to ADR 0010's `"STATE"`. Rows move; nothing is scored,
extracted, or drifted — the batcher stays outside the ADR 0005 parity
set for the same reason the dashboards adapter does (ADR 0014
§Decision 5): it imports no `shared/` code, and a future change that
does joins the parity set in the same PR.

This ADR locks the session's three open knobs: how the batcher finds
new rows (watermark + per-pump Query), what writes the Parquet
(pyarrow, no pandas), and how often it runs (60 s).

## Context

The cold path is the project's Firehose replacement (anti-pattern
list; no Always-Free tier). `PLAN.md §2.6` names the shape — Lambda +
EventBridge schedule, Parquet in S3 under
`year=/month=/day=/hour=/`, Glue Catalog table in Terraform with no
Crawler — but left three knobs open, carried in
`context/lambda_s3_batcher.md` §Open questions and HANDOFF §6 Q6:

1. **Batch read pattern.** "Rows since last batch" is not a native
   DynamoDB query; something must remember the boundary.
2. **Parquet engine.** Parquet writers are heavy; the scorer's zip
   already taught us to measure before bundling (ADR 0006 §Q4:
   ~124 MB unzipped of a 250 MB quota).
3. **Cadence.** 60 s vs 5 min, with both free at demo scale.

Free-tier posture verified in-session (2026-06-04): S3's 5 GB
Standard storage is Always-Free; Glue Catalog's first 1 M objects
stored + 1 M requests/month are Always-Free; EventBridge rule
evaluation and scheduled invocations are free at this scale (the
Scheduler flavor's free tier alone is 14 M invocations/month; a demo
uses ~30). The one residue: S3 PUT requests beyond the monthly
allowance bill at $0.005/1K — 30 PUTs/demo ≈ **$0.0002/demo**, noise
recorded here rather than in a new ADR-0013-style exception (it is
three orders of magnitude under ADR 0013's accepted dimes).

## Decision

### 1. Read pattern: per-pump watermark row + Query

A second reserved sort key joins `"STATE"` (ADR 0010):

```
PK = pump_id
SK = "WATERMARK"
last_cutoff = <ISO-8601 ts>   # hi bound of the last successful batch
updated_at  = <ISO-8601 ts>   # batcher invocation time
```

Coexistence is free under ADR 0010's convention: the hot path's
`SK begins_with "2"` predicate excludes any SK starting with a
letter, and the batcher's own BETWEEN range (timestamps on both
ends) excludes both `"STATE"` and `"WATERMARK"`, which sort above
any `"2026-…"` string.

Per invocation the batcher:

1. Computes `cutoff = now − SAFETY_LAG_SECONDS` (default 5 s). The
   scorer keys reading rows by *telemetry* timestamp; a row whose ts
   precedes the wall clock can still be in flight through IoT Rule →
   Lambda → PutItem. The lag keeps the query's upper bound behind
   that pipeline's worst case, so a row is never younger than the
   batch that should have carried it.
2. `BatchGetItem`s the fleet's WATERMARK rows (one call, same shape
   as the adapter's STATE read). A missing row means "never
   archived" → lower bound is the epoch.
3. Per pump, `Query(PK = pump_id, SK BETWEEN last_cutoff⁺ AND
   cutoff)` — `last_cutoff⁺` is `last_cutoff + "0"`, the
   lexicographic strictly-greater suffix trick that makes the lower
   bound exclusive (BETWEEN is inclusive; a row keyed exactly at the
   previous cutoff was already archived). Pagination is honored;
   at ~30 rows/pump/minute it never triggers.
4. Writes one Parquet file, then advances **every** pump's watermark
   to `cutoff` — also the pumps that contributed no rows this round
   (their next window simply starts later; the safety-lag contract
   is identical for all pumps).
5. **Zero rows fleet-wide → true no-op**: no S3 put, no watermark
   write, nothing to clean up.

Cost math (ADR 0013 method): a batch reads ~30 rows × ~150 B ≈
4.5 KB per pump ≈ 1–2 eventually-consistent RRUs, ~15–30 RRU/min
fleet-wide, **~450–900 RRU per 30-min demo ≈ $0.0001–0.0003** — three
orders of magnitude under the hot path's accepted dimes. Watermark
writes add 15 WCU-equivalents/min; same noise bucket.

Failure semantics: the S3 put and the 15 watermark PutItems are not
transactional. If the put succeeds and a watermark write fails, that
pump's next batch re-archives the overlap — **duplicate rows across
two Parquet files, never lost rows**. At-least-once is the right
posture for an archive; consumers that care can dedupe on
`(pump_id, ts)`, which is unique by construction (it was the
DynamoDB primary key).

### 2. Parquet engine: pyarrow, no pandas

The handler builds a `pyarrow.Table` directly from column lists —
pandas adds nothing at 450 rows. Footprint per the ADR 0006 §Q4
method: pyarrow ≈ 100 MB unzipped + handler + boto3-from-runtime
(never bundled — infra session #1) lands the batcher's **own** zip at
~100 MB against the 250 MB quota — more headroom than the scorer's
124 MB, and the zipped artifact (~40 MB) stays under the 50 MB
direct-upload limit. The build script (`scripts/build_batcher.{ps1,sh}`,
sibling of `build_lambda`/`build_adapter`) stages and enforces the
measurement, same as the scorer's.

### 3. Cadence: 60 s (EventBridge rule, `rate(1 minute)`)

HANDOFF §6 Q6's default, decided on demo-story grounds since both
options are free: 60 s yields ~450 rows per file and **30 files
visibly accumulating during a 30-minute demo** — `aws s3 ls` mid-demo
shows the cold path breathing. 5 min would yield six files and a
five-minute data-loss window on error. Invocation count (30/demo vs
6/demo) is irrelevant at EventBridge/Lambda free-tier scale.

### 4. Glue table: Terraform-declared schema + partition projection

The Glue table (sub-module `infra/modules/glue_catalog`, per the
`context/infra.md` recorded default) declares the Parquet columns
(`pump_id string, ts string, vibration_amp double, bearing_temp
double, motor_current double, rpm double, score double`) and
`year/month/day/hour` partition keys with **partition projection**
table parameters. No Crawler (anti-pattern list) — and no
`glue:CreatePartition` calls from the batcher either: projection
computes partition locations from the query predicate, so the
batcher's IAM stays `dynamodb:Query`/`BatchGetItem`/`PutItem` on the
table + `s3:PutObject` on the bucket prefix, with no Glue permissions
at all. Catalog object count stays at ~2 (database + table), inside
the 1 M Always-Free forever.

### 5. Bucket teardown: `force_destroy = true`

`terraform destroy` refuses non-empty buckets. PO call (2026-06-04):
the archive bucket sets `force_destroy = true` — the archive is
demo-ephemeral by design (apply → demo → teardown lifecycle, ADR
0013's framing), and one less imperative sweep step that can fail
mid-teardown. `aws_teardown.sh` still verifies bucket absence after
destroy.

## Alternatives considered

### 1. Read pattern

**A. Watermark + per-pump Query (the decision).** Idiomatic
incremental export; pennies-invisible cost; one new reserved SK
under an established coexistence rule.

**B. DynamoDB Streams → batcher.** No re-read at all — the stream
hands the batcher exactly the new writes. Rejected on moving parts:
an event source mapping with its own batching windows and error
semantics replaces the *scheduled* batcher PLAN names, moto coverage
of stream-triggered Lambdas is markedly weaker than of Query, and
the demo story changes from "a cron drains the table" to "a stream
pushes" — more impressive-sounding, harder to demo deterministically.
Recorded as the production-scale upgrade path.

**C. Recent-window Scan.** Computed per the ADR 0013 method: the
table holds ~13.5 K reading rows (~2 MB) by demo end, so each Scan
reads on average ~1 MB ≈ 125 EC-RRUs → ~3,750 RRU/demo, **~5–8× the
Query pattern and O(table history)** — and a `FilterExpression`
doesn't reduce billed capacity (charged on items *accessed*, ADR
0013 §Alternatives D). Affordable at demo scale but the weakest
portfolio signal: it advertises not knowing the key schema you
designed. Rejected.

### 2. Parquet engine

**A. pyarrow alone (the decision).** One dependency, native Arrow →
Parquet, no pandas.

**B. awswrangler.** Convenient `wr.s3.to_parquet`, but bundles
pandas + pyarrow + boto3 — the heaviest option, and bundling boto3
violates the infra-session lock (runtime-provided).

**C. fastparquet.** Smaller binary than pyarrow but requires pandas
anyway and is the less-trodden path in AWS tooling. No win.

**D. CSV instead.** Zero heavy dependencies, but Parquet is named in
PLAN, Glue/Athena over columnar Parquet is the portfolio story, and
CSV forfeits types + compression. Rejected.

### 3. Cadence

**60 s (the decision)** vs **5 min**: both free; decided on the
visible-accumulation demo story and the smaller data-loss window
(§Decision 3). A `schedule_expression` Terraform variable keeps the
knob one-line revisable.

### 4. Partition registration

**A. Partition projection (the decision).** Zero per-partition API
calls, zero Glue IAM on the batcher, zero drift between S3 layout
and catalog.

**B. Batcher calls `glue:CreatePartition` after each put.** Couples
the data plane to catalog mutation, widens IAM, and a failed call
strands an unqueryable file. Rejected.

**C. Glue Crawler.** Anti-pattern list (standing cost + the exact
"schema in Terraform" rule it would bypass). Never.

## Consequences

**Positive:**

- Incremental, at-least-once archival with per-pump checkpoints; no
  re-reads, no lost rows, cost noise under ADR 0013's accepted dimes.
- The batcher's IAM is four scoped actions on two resources; the
  scoped policy doubles as the no-compute tripwire, same trick as
  ADR 0014.
- Athena-queryable from the first file, no Crawler, no partition
  bookkeeping anywhere.
- All three knobs are single-variable revisable (cadence, lag,
  fleet size) without touching the ADR's structure.

**Negative:**

- **Two reserved SKs now coexist** (`"STATE"`, `"WATERMARK"`). Any
  future SK convention must check against both; the coexistence rule
  lives in ADR 0010 §Consequences and `_interfaces.md §DynamoDB
  schema` (updated this session).
- **Duplicates on partial failure** (S3 put lands, watermark write
  doesn't). Accepted: archive consumers dedupe on `(pump_id, ts)`;
  the alternative (TransactWriteItems spanning a non-DynamoDB S3
  put) doesn't exist.
- **Late rows beyond the safety lag are skipped, permanently.** A
  reading whose PutItem completes more than `SAFETY_LAG_SECONDS`
  after its telemetry ts misses its batch and every later one. At
  demo scale (sub-second pipeline latency vs 5 s lag) this is
  theoretical; a production posture would widen the lag or move to
  Streams (Alternatives 1B).
- **pyarrow is the project's largest single dependency** (~100 MB
  unzipped) for writing ~450-row files. Accepted for the portfolio
  signal; CSV remains the recorded fallback if the footprint check
  ever fails.

**Follow-ups:**

- `aws_teardown.sh` gains: bucket absence (post-`force_destroy`),
  Glue database + table absence, batcher Lambda + log group + role +
  EventBridge rule absence (this session).
- README cost table: cite this ADR for the ~$0.0002/demo S3 PUT
  residue alongside ADR 0013's line.
- Athena workgroup + example queries are a future-session nicety,
  not infra this session creates.

## References

- `HANDOFF.md §6 Q6` — the cadence question this ADR resolves.
- `context/lambda_s3_batcher.md` §Open questions — read pattern +
  cadence, resolved here.
- `context/_interfaces.md` — §S3 archive layout (the locked path
  shape); §DynamoDB schema gains the WATERMARK row this session.
- ADR 0005 / ADR 0014 §Decision 5 — the outside-parity-set posture
  the batcher inherits; the inverse import test pins it.
- ADR 0006 §Q4 — zip footprint method; ~124 MB scorer baseline the
  batcher's ~100 MB sits beside.
- ADR 0010 — reading-row schema + reserved-SK coexistence rule the
  WATERMARK row extends.
- ADR 0013 — cost-math method; the accepted-dimes yardstick all
  numbers above are measured against.
- Implementation: `lambda_s3_batcher/handler.py`,
  `lambda_s3_batcher/tests/test_handler.py`,
  `infra/modules/{s3_archive,glue_catalog,lambda_s3_batcher}`,
  `scripts/build_batcher.{ps1,sh}`.
- Session log: `docs/sessions/2026-06-04-infra-cold-path.md`.
- External (verified 2026-06-04): AWS S3 Always-Free 5 GB; Glue
  Catalog 1 M objects + 1 M requests/month Always-Free; EventBridge
  scheduler free tier 14 M invocations/month.
