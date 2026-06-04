# local_runtime

## Purpose
Local-mode equivalent of `lambda_scorer`. Subscribes to Mosquitto, runs *the same* `shared/drift.py` and `shared/score.py` code, writes scored rows to local InfluxDB. Enables zero-cost continuous development on the scoring + drift logic without spending AWS credits.

## Current state
- [x] Subscriber + window + InfluxDB writer + score/PSI stubs in place (2026-05-29 session).
- [x] Gemini review processed; ADR 0005 promoted Proposed → Accepted (2026-05-29).
- [x] Real `score()` — model session (2026-06-01).
- [x] Real `compute_psi()` + every-Nth-tick cadence + per-pump feature-history deque (drift session 2026-06-01).
- [x] PSI surface shrunk to four raw features (2026-06-03, ADR 0009). InfluxDB schema goes 17 → 13 fields per point on compute ticks.
- [ ] `lambda_scorer` analogue importing the same `shared/` modules — lambda session.
- [ ] Dashboards pointing at the schema this session pinned — dashboards session (panel set wires against the four surviving PSI fields).

## Interfaces (in / out)
- **In:** MQTT subscribe to `factory/pumps/+/telemetry` on `localhost:1883` — ONE connection, wildcard subscription (see ADR 0005).
- **Out:** Writes to local InfluxDB v2 (`localhost:8086`) via the native async client (`InfluxDBClientAsync`). Measurement `pump_telemetry`, tag `pump_id`, **13 numeric fields per point on compute ticks (8 features + score + 4 PSI per ADR 0009; was 17 with 8 PSI pre-ADR-0009); 9 fields on non-compute ticks** (psi_* omitted so InfluxDB stores nulls — ADR 0007 §5). Schema pinned in ADR 0005 §3 with the ADR 0009 amendment.
- **Shared logic:** Imports `shared.features`, `shared.score`, `shared.drift` — same import path as `lambda_scorer`. Divergence is a bug.

## Mode parity invariant
For the same input stream, local mode and AWS mode must produce the same per-message scores and PSI values within floating-point tolerance.

**Important — what parity is and is not:** the invariant is about *output correctness* under the same input stream, not about *concurrency model*. The local subscriber is one asyncio loop dispatching sync handlers sequentially; the AWS Lambda path is N concurrent invocations. Both produce identical rows for identical inputs because the per-message computation (`extract_features` + `score` + `compute_psi`) is the same pure-function chain. Per Gemini Q2 of the 2026-05-29 review.

The parity boundary is `shared.features.extract_features(window: list[dict]) -> dict[str, float]` (plus `shared.score.score(features) -> float` and `shared.drift.compute_psi(window, reference) -> dict[str, float]`). Both local_runtime and Lambda call them; the only difference is the source of the window and reference (in-memory + load_reference() once at init vs. DynamoDB read + bundled artifact in the Lambda zip).

ADR 0009 (2026-06-03) introduced the second locked-contract feature list: `shared.features.PSI_FEATURE_NAMES`. The scorer input contract stays at `FEATURE_NAMES` (8); the drift surface contract is now `PSI_FEATURE_NAMES` (4). Both modes import the same two constants from `shared/features.py`; the structural test pins the strict-subset relation.

### State-management asymmetry between local and Lambda (Gemini Q7, 2026-06-01)

The local runtime and Lambda hot path hold the **same data structures** but build them from **different sources**, by design:

| State                        | Local runtime                          | Lambda hot path (when it lands)                |
|------------------------------|----------------------------------------|------------------------------------------------|
| Telemetry rolling window     | `FeatureWindow` deque in process memory| Reconstructed from DynamoDB per invocation     |
| PSI feature-history window   | `_feature_history` deque per pump      | Reconstructed from DynamoDB per invocation     |
| Reference distribution       | `load_reference()` once at service init| `load_reference()` once at Lambda cold-start, bundled in the deploy zip |
| Trained model                | `_load_classifier()` once at first score| `_load_classifier()` once at Lambda cold-start, bundled in the deploy zip |

This asymmetry is **not a code smell** — it's the right adaptation to two deployment models. The local subscriber holds a long-lived asyncio loop and can keep windows in memory cheaply; Lambda invocations are stateless between calls and must rebuild from a durable store (DynamoDB). The mode-parity invariant is preserved because both sides feed `compute_psi(window, reference)` the same per-message inputs and get the same outputs — the *source* of the window differs, but the *shape* doesn't.

The line that does need to stay symmetric: any logic that touches the *contents* of these structures (transformations, filters, aggregations) lives in `shared/`. The in-memory-vs-DynamoDB choice is structural plumbing; the logic over those structures is the parity boundary.

**Tests:**
- `test_mode_parity_uses_shared_features_module` — `sys.modules` identity check.
- `test_structural_parity_no_vendoring` (+ siblings for score and drift) — uses `inspect.getfile` to verify the functions physically execute from `shared/`, not a vendored fork. Per Gemini Q6 of the 2026-05-29 review.
- `test_psi_feature_names_is_subset_of_feature_names` (in `local_runtime/tests/test_features.py`) — ADR 0009 structural invariant: PSI surface is a strict subset of the scorer input set.

## Open questions
- Window source on the AWS side: HANDOFF.md §6 Q5 (DynamoDB schema) still gates the lambda_scorer's window read pattern. local_runtime sidesteps this via the in-memory `FeatureWindow` and `_feature_history`.
- ~~Reference rebuild from demo-paced healthy data~~ — closed by ADR 0008 (2026-06-02).
- ~~Autocorrelated rolling-feature PSI~~ — closed by ADR 0009 (2026-06-03, PSI surface shrink).

## Related ADRs
- ADR 0005 — Shared mode-parity package + subscriber topology + InfluxDB schema (Accepted 2026-05-29). §3 schema line carries an ADR 0009 amendment (17 → 13 fields per point).
- ADR 0006 — Trained model + reference distribution + training-data dwell stretch (Accepted 2026-06-01).
- ADR 0007 — PSI implementation + per-tick cadence + reference-load API (Accepted 2026-06-01).
- ADR 0008 — Operational PSI reference source-separated from training corpus (Accepted 2026-06-02).
- ADR 0009 — PSI surface ≠ scorer feature set (Accepted 2026-06-03). Shrinks InfluxDB schema to 13 fields on compute ticks.
- ADR 0003 — Per-pump topology applies to publishers; subscribers diverge per ADR 0005.

## Session log
- 2026-05-29 — `docs/sessions/2026-05-29-local_runtime-subscribe-window-influx.md` — first downstream consumer wired; 80 → 82 new tests after Gemini review; smoke step documented; full test suite 308 passing.
- 2026-06-01 — `docs/sessions/2026-06-01-drift-real-psi.md` — drift session; real PSI + every-Nth-tick cadence + Q2 refactor (load_reference is the explicit I/O entry); 322 → 340 passing.
- 2026-06-02 — `docs/sessions/2026-06-02-model-operational-reference.md` — operational reference (ADR 0008); 346 passing.
- 2026-06-03 — `docs/sessions/2026-06-03-drift-psi-surface-cleanup.md` — PSI surface shrink (ADR 0009); 350 passing.
