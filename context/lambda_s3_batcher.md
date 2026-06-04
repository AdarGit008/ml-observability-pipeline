# lambda_s3_batcher

## Purpose
Cold-path archiver. Replaces Kinesis Firehose. EventBridge wakes it
every 60 s; it drains reading rows written since its last run (per-pump
watermark + Query, ADR 0015) into one Parquet file per batch in S3,
partitioned `year=/month=/day=/hour=`. It moves rows; it computes
nothing — NO `shared/` import, outside the ADR 0005 parity set
(`test_batcher_does_not_import_shared` pins it, same posture as the
dashboards adapter / ADR 0014 §Decision 5).

## Current state
- ✅ Shipped 2026-06-04 (cold-path session, **ADR 0015**):
  `lambda_s3_batcher/handler.py` + 18 moto tests (watermark mechanics,
  Parquet round-trip read-back, empty-batch no-op, safety lag,
  at-least-once put-failure semantics, reserved-row exclusion,
  cold-start validation).
- Key mechanics: WATERMARK reserved SK row per pump (coexists with
  STATE per ADR 0010); cutoff trails the wall clock by
  `SAFETY_LAG_SECONDS` (default 5); exclusive lower bound via the
  `watermark + "0"` lexicographic suffix; watermarks advance for ALL
  pumps after a successful put, never regress; zero rows → true no-op.
- Failure semantics: at-least-once. Put-succeeded-watermark-failed
  re-archives the overlap (duplicates across files, never loss);
  dedupe key is `(pump_id, ts)`. Terraform pins
  `reserved_concurrent_executions = 1` so overlap needs a genuine
  failure, not scheduling jitter.

## Interfaces (in / out)
- **In:** EventBridge scheduled event (payload unused — the table
  holds the state). Cadence `rate(1 minute)` per ADR 0015 §Decision 3.
- **Out:** one Parquet file per non-empty batch
  (`_interfaces.md §S3 archive layout`); WATERMARK row writes
  (`_interfaces.md §DynamoDB schema`). Glue table
  (partition projection — no Crawler, no CreatePartition) reads the
  files.

## Environment variables
- `DDB_TABLE_NAME` (default `pump_hot_state`)
- `S3_BUCKET` — REQUIRED, fail-fast at cold start (ADR 0012 posture)
- `FLEET_SIZE` (default 15; 1..99 validated)
- `SAFETY_LAG_SECONDS` (default 5; ≥ 0 validated)
- `DDB_ENDPOINT_URL` / `S3_ENDPOINT_URL` — local-test affordances

## Resource sizing
256 MB / 30 s — the pyarrow import dominates the cold start; the warm
path is 1 BatchGetItem + 15 Queries + 1 PutObject over ~450 rows.

## Packaging
`scripts/build_batcher.{ps1,sh}` stages `.build/batcher_dist/`
(pyarrow manylinux2014_x86_64 from `scripts/batcher_requirements.txt`
+ handler, tests stripped) and enforces the ADR 0006 §Q4 footprint
check (~100 MB unzipped vs 250 MB ceiling). boto3 never bundled.
Run before `terraform plan`.

## Open questions
None. (Cadence + read pattern + engine resolved by ADR 0015.)

## Related ADRs
- ADR 0015 — watermark read pattern, pyarrow, 60 s cadence, partition
  projection, force_destroy (this component's charter).
- ADR 0010 — reading-row schema + reserved-SK coexistence rule.
- ADR 0013 — cost-math method (batcher reads ≈ $0.0001–0.0003/demo).
- ADR 0014 — the outside-parity-set posture the batcher inherits.
