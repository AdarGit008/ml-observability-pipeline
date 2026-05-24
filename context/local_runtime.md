# local_runtime

## Purpose
Local-mode equivalent of `lambda_scorer`. Subscribes to Mosquitto, runs *the same* `drift.py` and scoring code, writes to local InfluxDB. Enables zero-cost continuous development.

## Current state
- [ ] Not started.

## Interfaces (in / out)
- **In:** MQTT subscribe to `factory/pumps/+/telemetry` on `localhost:1883`.
- **Out:** Writes to local InfluxDB (`localhost:8086`).
- **Shared logic:** Imports `drift.py` from the same module path as `lambda_scorer`. Divergence is a bug.

## Mode parity invariant
For the same input stream, local mode and AWS mode must produce the same scores and PSI values within floating-point tolerance. This is testable and should be tested.

## Open questions
- How do we structure the shared `drift.py` so it's importable by both Lambda (no installed deps allowed beyond what's bundled) and the local service? Likely: pure-Python, depend only on `numpy` + standard lib.

## Related ADRs
None yet. Likely: shared-logic packaging strategy.
