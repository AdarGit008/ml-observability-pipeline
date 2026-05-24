# lambda_s3_batcher

## Purpose
Cold-path archiver. Replaces Kinesis Firehose. EventBridge triggers it every 60s; reads recent DynamoDB writes (via stream or TTL-indexed query) and writes one Parquet file per minute to S3, partitioned `year=/month=/day=/hour=`.

## Current state
- [ ] Not started.
- Spec defined in `PLAN.md §2.6`.

## Interfaces (in / out)
- **In:** EventBridge scheduled event (every 60s, configurable).
- **Out:** Parquet files in S3 under partitioned prefix. Glue Catalog table reads from this.

## Open questions
- Schedule granularity: 60s (default, ~7.5K records per file) vs 5 min (fewer invocations, larger blast radius on error). (HANDOFF.md §6 Q6 — default: 60s.)
- DynamoDB streams vs query: streams are cleaner but more moving parts; query is simpler but needs a written-at index.

## Related ADRs
None yet. Likely: batching strategy rationale.
