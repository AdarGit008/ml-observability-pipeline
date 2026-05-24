# lambda_scorer

## Purpose
Hot-path Lambda. One invocation per MQTT message via IoT Rule. Reads recent feature window from DynamoDB, computes rolling features, scores, updates per-pump PSI, writes back, fires SNS on threshold breach.

## Current state
- [ ] Not started.
- Spec defined in `PLAN.md §2.5`.

## Interfaces (in / out)
- **In:** IoT Rule event envelope wrapping the telemetry JSON (`_interfaces.md`).
- **Out:** DynamoDB writes (feature window append + state record), SNS publish on alert.
- **Shared logic:** `drift.py` is imported by both this handler and `local_runtime/scorer_service.py`. Keep import path stable across modes.

## Resource sizing
- 512 MB memory.
- Bundled model pickle in deployment package.
- Volume: 15 pumps × 30 msg/min × 30-min demo ≈ 13.5K invocations per demo. Well inside Always-Free 1M/mo.

## Open questions
- DynamoDB schema (HANDOFF.md §6 Q5) — blocking. Must resolve before implementation.
- Cold-start latency with bundled pickle — needs measurement; if >2s, switch to S3 cold-load.

## Related ADRs
None yet. Likely: model packaging choice, DynamoDB access pattern.
