# local_runtime

## Purpose
Local-mode equivalent of `lambda_scorer`. Subscribes to Mosquitto, runs *the same* `shared/drift.py` and `shared/score.py` code, writes scored rows to local InfluxDB. Enables zero-cost continuous development on the scoring + drift logic without spending AWS credits.

## Current state
- [x] Subscriber + window + InfluxDB writer + score/PSI stubs in place (2026-05-29 session).
- [x] Gemini review processed; ADR 0005 promoted Proposed → Accepted (2026-05-29).
- [ ] Real `score()` — model session.
- [ ] Real `compute_psi()` — drift session.
- [ ] `lambda_scorer` analogue importing the same `shared/` modules — lambda session.
- [ ] Dashboards pointing at the schema this session pinned — dashboards session.

## Interfaces (in / out)
- **In:** MQTT subscribe to `factory/pumps/+/telemetry` on `localhost:1883` — ONE connection, wildcard subscription (see ADR 0005).
- **Out:** Writes to local InfluxDB v2 (`localhost:8086`) via the native async client (`InfluxDBClientAsync`). Measurement `pump_telemetry`, tag `pump_id`, 17 numeric fields per point (8 features + score + 8 PSI). Schema pinned in ADR 0005.
- **Shared logic:** Imports `shared.features`, `shared.score`, `shared.drift` — same import path as the future `lambda_scorer`. Divergence is a bug.

## Mode parity invariant
For the same input stream, local mode and AWS mode must produce the same per-message scores and PSI values within floating-point tolerance.

**Important — what parity is and is not:** the invariant is about *output correctness* under the same input stream, not about *concurrency model*. The local subscriber is one asyncio loop dispatching sync handlers sequentially; the AWS Lambda path is N concurrent invocations. Both produce identical rows for identical inputs because the per-message computation (`extract_features` + `score` + `compute_psi`) is the same pure-function chain. Per Gemini Q2 of the 2026-05-29 review.

The parity boundary is `shared.features.extract_features(window: list[dict]) -> dict[str, float]` (plus `shared.score.score` and `shared.drift.compute_psi`). Both local_runtime and Lambda call them; the only difference is the source of the window (in-memory deque vs. DynamoDB read).

**Tests:**
- `test_mode_parity_uses_shared_features_module` — `sys.modules` identity check.
- `test_structural_parity_no_vendoring` (+ siblings for score and drift) — uses `inspect.getfile` to verify the functions physically execute from `shared/`, not a vendored fork. Per Gemini Q6 of the 2026-05-29 review.

## Open questions
- **PSI write cadence** (Gemini Q3, 2026-05-29). PLAN.md §2.5 prescribes per-tick PSI computation in the hot path; PLAN.md §2.7's fleet-level PSI is on a 5-minute schedule. Per-tick PSI values barely change between ticks (window drifts by ~1/1800 per 2 s) and writing them to InfluxDB on every tick inflates storage. Open: should the drift session move per-pump PSI writes to a less-frequent cadence (every Nth tick? separate `pump_drift` measurement on the 5-min schedule?). Compatible with the ADR 0005 schema either way.
- Window source on the AWS side: HANDOFF.md §6 Q5 (DynamoDB schema) still gates the lambda_scorer's window read pattern. local_runtime sidesteps this via the in-memory `FeatureWindow`.
- Real model + PSI implementations land in the model + drift sessions; the call sites here don't change.

## Related ADRs
- ADR 0005 — Shared mode-parity package + subscriber topology + InfluxDB schema (Accepted 2026-05-29).
- ADR 0003 — Per-pump topology applies to publishers; subscribers diverge per ADR 0005.

## Session log
- 2026-05-29 — `docs/sessions/2026-05-29-local_runtime-subscribe-window-influx.md` — first downstream consumer wired; 80 → 82 new tests after Gemini review; smoke step documented; full test suite 308 passing.
