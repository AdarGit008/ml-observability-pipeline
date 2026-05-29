# Review Packet 2026-05-29 — local_runtime — subscribe + window + influx

> Paste this entire file into Gemini via:
> `.\scripts\gemini_review.ps1 -Slug 2026-05-29-local_runtime-subscribe-window-influx`

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. **Mode parity between local and AWS demo paths.** Especially load-bearing for this review — the session's main goal was to set up the parity boundary.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`. ADR 0005 (the new one for this session) carries the main design trade-offs.

## Summary of the change
First downstream consumer of the simulator is wired. New top-level `shared/` package (peer to `simulator/` and the future `lambda_scorer/`) holds three mode-parity modules: a pure 8-feature extractor (`shared/features.py`), a scoring stub (`shared/score.py`), and a PSI stub (`shared/drift.py`). New `local_runtime/` package subscribes to local Mosquitto on the wildcard `factory/pumps/+/telemetry`, maintains a per-pump 5-minute rolling deque, runs the pure extractor + the stubs, and writes one row per scored reading to local InfluxDB v2. `docker-compose.yml` brings up Mosquitto + InfluxDB for the smoke step. 80 new tests; full suite 303 passing.

The session was deliberately scoped to the integration shell — score and PSI are stubs because the model and drift sessions own the real implementations. The shared interfaces are locked so those sessions can drop in real bodies without touching call sites here or in the future Lambda.

## Files changed
- `shared/__init__.py` (new) — package docstring documenting the mode-parity contract.
- `shared/features.py` (new) — `extract_features(window: Iterable[Mapping]) -> dict[str, float]`, returns the 8 features in `FEATURE_NAMES`. Pure (numpy + stdlib only).
- `shared/score.py` (new) — stub `score(features) -> float`.
- `shared/drift.py` (new) — stub `compute_psi(window_features, reference) -> dict[str, float]` returning sentinel values that span the warning + stable PSI bands.
- `local_runtime/__init__.py`, `__main__.py`, `config.py`, `config.example.yaml`, `subscriber.py`, `window.py`, `influx_writer.py`, `service.py` (all new).
- `local_runtime/tests/test_{features,window,config,subscriber,influx_writer,service,shared_stubs}.py` — 80 new tests.
- `docker-compose.yml` (new) — Mosquitto (reuses the existing `.local/mosquitto.conf`) + InfluxDB v2.7 with auto-setup.
- `requirements.txt` — added `numpy>=1.26` and `influxdb-client>=1.40`.
- `context/local_runtime.md` — ticked the first DoD checkbox, documented the mode-parity boundary, linked ADR 0005.
- `docs/adr/0005-shared-mode-parity-package-and-subscriber-topology.md` (new) — combined ADR for the three design choices.

## Specific questions for Gemini

1. **Is the `shared/` package layout actually the right reconciliation of PLAN.md §1's `lambda_scorer/drift.py` sketch and §2.1's "same drift.py module" statement?** ADR 0005 §1 walks through the alternatives I considered (under-local_runtime, under-lambda_scorer, copy-paste). What I want to know: is there a hidden cost I'm not seeing, like a packaging or imports complication that bites in the lambda_scorer session? I'm especially worried about the deployment .zip — the Terraform `archive_file` data source will need to copy both `shared/` and `lambda_scorer/` into the zip; is that a clean idiom or is it the kind of thing that gets fragile?

2. **Single MQTT connection with wildcard vs. per-pump subscriptions** — I argued (ADR 0005 §2) that ADR 0003's per-pump topology applies to the publish side because of AWS IoT's Thing-per-pump model, and that subscribers have no such constraint. I picked one wildcard connection. Is there a *latency* or *fairness* argument I missed? E.g., does a single asyncio task fanning out 7.5 msg/s through one handler risk one slow pump blocking the others? My read is that the handler is sync-per-message just like a Lambda hot-path invocation would be, so this is mode-correct. Push on this if you see a real cost.

3. **InfluxDB schema — pump_id-as-tag, flat `psi_<feature>` fields** (ADR 0005 §3). Specifically: at 15 pumps × 30 readings/min × eventually a year of retention = ~240M points, is the tag-per-pump_id cardinality survivable, or do I need to think about retention policies now? The local mode is meant to be a continuous dev environment — `docker compose down -v` is the reset hatch — but if the schema baked in here is what the AWS dashboard mode will mirror, the cardinality story matters.

4. **`asyncio.to_thread` for InfluxDB writes** — the official Python client is sync, so `influx_writer.py` wraps `write_api.write` in `asyncio.to_thread`. At 7.5 msg/s this is fine; at 75 msg/s (PLAN.md's worst-case 100-pump cap) is the GIL contention worth worrying about? Should I be reaching for `aiohttp` + the v2 HTTP API directly? My current view: no — at portfolio scale `to_thread` is correct, and the real risk is making the writer aware of batching, which is a future-session decision.

5. **PSI stub's sentinel values** — `shared/drift.py::_STUB_PSI` puts `vibration_amp` at 0.15 (warning band) and the rest under 0.10 (stable). This is a deliberate fixture so downstream alert wiring has a known input. Is there a case for making the stub configurable (env var? config kwarg?) so a future test that exercises the "significant shift" band has a fixture to point at, or is that premature?

6. **Mode-parity tests** — `test_mode_parity_uses_shared_features_module` asserts via `importlib` that `local_runtime.service.extract_features is shared.features.extract_features`. This catches a `from shared.features import extract_features` getting accidentally renamed locally, but it doesn't catch a *vendored copy*. Is there a stronger structural test I should add now, before the lambda_scorer session lands, to make the parity boundary truly enforceable?

## What I'm NOT looking for in this review
- Style/formatting — handled by linter (not yet running but the existing simulator code sets the tone).
- The score/drift stubs themselves — they're stubs by design; review the *interfaces*, not the placeholder bodies.
- DynamoDB schema — HANDOFF.md §6 Q5 is still open and is the lambda_scorer session's blocker. This session sidesteps it via the in-memory window.

## Resolution (filled in by Claude after Gemini responds)

Full dispositions in ADR 0005 §"Addendum 2026-05-29 — Gemini review dispositions". Summary below.

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. Terraform packaging of `shared/` + `lambda_scorer/` | Accepted, deferred | The lambda_scorer session will add `scripts/build_lambda.{ps1,sh}` to stage both dirs into `.build/lambda_dist/` before `archive_file` zips it. Standard monorepo-in-AWS practice; not in scope for local_runtime. |
| 2. Concurrency parity false equivalence | Partially accepted | Mode-parity invariant clarified as *output correctness*, not concurrency model. ProcessPoolExecutor rejected as disproportionate to portfolio scale (7.5 msg/s; 50 msg/s at 100-pump cap). Doc change in `context/local_runtime.md` + ADR addendum. |
| 3. PSI on every tick wastes storage/Lambda CPU | Partially accepted | Schema unchanged (per-tick PSI fields stay); write-cadence question logged as drift-session open question. PLAN.md §2.5 prescribes per-tick PSI computation; storage-write frequency is a separate axis the drift session can tune. |
| 4. `asyncio.to_thread` instead of native async client | **Accepted — code change.** | Refactored `InfluxWriter` to use `InfluxDBClientAsync`. `FakeClient` reshaped as async context manager. New test `test_writer_write_is_awaited_not_to_thread` pins the shape. |
| 5. PSI sentinel configurability | Accepted as-stated (YAGNI) | No code change; `_STUB_PSI` stays a module constant. Future significant-shift fixtures can `unittest.mock.patch` it inline. |
| 6. Structural parity test via `inspect.getfile` | **Accepted — code change.** | Added 3 tests (`test_structural_parity_no_vendoring` + siblings for score/drift) that resolve the function file via `inspect.getfile` and assert it sits under `shared/`. Catches vendored-fork drift that pure `is` checks would miss. |

**Aggregate impact:** 303 → 308 tests passing, 1 pre-existing skip. ADR 0005 promoted Proposed → Accepted. No constraint-anchor violations (mode parity strengthened; $0 unchanged; single-PC unchanged).
