# Session 2026-05-29 — local_runtime — subscribe + window + influx

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (CLI)
- **Context loaded:** `_global`, `local_runtime`, `_interfaces`
- **Duration:** ~one session

## Intent
Build the first downstream consumer of the simulator: subscribe to local Mosquitto on the wildcard topic, maintain a 5-minute rolling feature window per pump, write scored rows to local InfluxDB. Score and PSI are stubbed; the goal is the integration shell + the storage write, not the ML.

## What changed
- New package `shared/` — peer to `local_runtime`, `simulator`. Holds the mode-parity-shared modules:
  - `shared/features.py` — pure `extract_features(window) -> dict[str, float]` returning the 8 features from PLAN.md §2.3 (4 raw + rolling mean/std of vibration_amp and bearing_temp). Population std (ddof=0) documented.
  - `shared/score.py` — stub `score(features) -> float` returning `clip(vibration_amp_mean_5m / 3.0, 0, 1)`. Interface locked for the model session.
  - `shared/drift.py` — stub `compute_psi(window_features, reference) -> dict[str, float]` returning sentinel values that span the warning + stable bands. Interface locked for the drift session.
- New package `local_runtime/`:
  - `subscriber.py` — `TelemetrySubscriber` + `retry_forever` driver. ONE aiomqtt connection, ONE wildcard subscription on `factory/pumps/+/telemetry`; strict regex extracts pump_id per message.
  - `window.py` — per-pump `deque(maxlen=N)` where `N = ceil(300 / tick_seconds)`. Local-only state (the Lambda mode reconstructs the window from DynamoDB per invocation).
  - `influx_writer.py` — `ScoredRow` dataclass + `build_point()` + `InfluxWriter` using `InfluxDBClientAsync` (aiohttp-backed native async client; switched from sync + `asyncio.to_thread` per Gemini Q4). Schema pinned in ADR 0005.
  - `service.py` — `ScorerService.handle(pump_id, telemetry)` glues subscriber → window → features → score → drift → writer.
  - `config.py` + `config.example.yaml` — YAML schema mirroring `simulator/config.py` patterns (strict, unknown-key rejection, `${ENV_VAR}` resolution for the Influx token).
  - `__main__.py` — `python -m local_runtime` entrypoint with the same `_loop_factory` + signal-handler patterns as the simulator.
- 85 new tests across 7 files (`tests/test_{features,window,config,subscriber,influx_writer,service,shared_stubs}.py`) — 80 from the initial implementation + 5 added during Gemini-review processing. All pass; full suite is 308 (223 simulator + 85 local_runtime), 1 pre-existing skip.
- `docker-compose.yml` — Mosquitto (uses existing `.local/mosquitto.conf`) + InfluxDB v2.7 with auto-setup. Onboarding step in the file's header comment.
- `requirements.txt` — added `numpy>=1.26` and `influxdb-client>=1.40`, both justified inline.
- `context/local_runtime.md` — `[ ]` ticked, ADR linked, mode-parity boundary documented.
- ADR 0005 — Shared mode-parity package + subscriber topology + InfluxDB schema.

## Decisions
- **Shared logic goes in a top-level `shared/` package**, not under either consumer. Reconciles PLAN.md §1's `lambda_scorer/drift.py` sketch with §2.1's "same drift.py module" statement: source-of-truth lives in `shared/`; Lambda's deployment .zip still ends up with a `drift.py` because the build copies it in. ADR 0005 §1.
- **Subscriber uses one connection and one wildcard subscription.** ADR 0003's per-pump topology applies to publishers (AWS IoT's Thing-per-pump model); subscribers have no such constraint. ADR 0005 §2.
- **InfluxDB schema:** measurement `pump_telemetry`, tag `pump_id` (low-cardinality, indexed), 17 flat numeric fields (8 features + score + 8 `psi_<feature>`). No nested structures (line protocol doesn't support them; Grafana plays nicer with flat fields). ADR 0005 §3.
- **The mode-parity boundary is the pure `shared.features.extract_features` function.** Local mode and Lambda both call it with a list of telemetry dicts; the only difference is where the list came from (deque vs. DynamoDB).

## Trade-offs surfaced
- Two top-level packages (`shared/` + each consumer) to keep in sync at Lambda packaging time. Mitigation: a CI-time check that the built zip imports `shared.*` cleanly. Deferred to the lambda_scorer session.
- `numpy` as a runtime dep. Pulled in early because the AWS Lambda mode will need it anyway; deferring would just push the same decision into a later session.
- Influx token committed in plaintext in `docker-compose.yml`. It's a LOCAL token for a LOCAL InfluxDB on localhost only; threat model is "developer's machine compromised" at which point this is the least of their problems. Config supports `${ENV_VAR}` substitution for developers who want it elsewhere.
- We don't batch InfluxDB writes. At 7.5 msg/s aggregate the per-write overhead is sub-ms; batching adds partial-write semantics complexity for no real throughput need.

## Gemini review highlights
Six points; two drove code changes (Q4 + Q6), three drove doc/follow-up amendments (Q1, Q2, Q3), one was a YAGNI confirmation (Q5). Full dispositions in ADR 0005 §"Addendum 2026-05-29 — Gemini review dispositions". The two highest-impact:

- **Q4 — `asyncio.to_thread` is the wrong tool when the library ships a native async API.** Gemini was right: `influxdb-client` has `InfluxDBClientAsync` (aiohttp-backed) since 1.36. Refactored `local_runtime/influx_writer.py` to use it; the writer is now `await self._write_api.write(...)` directly with no thread-pool hop. The fake test client became an async context manager with an async `write_api().write`. **Addressed.**
- **Q6 — `sys.modules` identity is not a strong enough parity test.** A vendored copy that both sides agree to import would still pass the `is` check. Gemini's fix (use `inspect.getfile` to verify the function physically loads from `/shared/`) is small and clearly better. Added three new tests in `test_service.py`. **Addressed.**

Other dispositions: Q1 (Terraform packaging) accepted but deferred to the lambda_scorer session — captured as a follow-up. Q2 (concurrency parity) partially accepted — invariant clarified in `context/local_runtime.md` to be about *output correctness*, not concurrency model; ProcessPoolExecutor rejected as disproportionate to portfolio scale. Q3 (PSI on every tick) partially accepted — schema unchanged, write-cadence question logged for the drift session. Q5 (PSI sentinel configurability) confirmed YAGNI; no change.

Links: `review_packets/2026-05-29-local_runtime-subscribe-window-influx.md` (with completed Resolution table) / `review_responses/2026-05-29-local_runtime-subscribe-window-influx.md`.

## State at end of session
- **Tests:** 308 passing, 1 skipped (the pre-existing Python 3.10 skip from `simulator/tests/test_main.py`). 85 are new (`local_runtime/tests/*` — 80 original + 5 added during Gemini-review processing: 3 structural parity + 2 reshaped Influx writer tests for the async context). Suite runs in ~1.3s.
- **Open follow-ups:**
  - Model session: implement `shared.score.score` with the real classifier; preserve interface.
  - Drift session: implement `shared.drift.compute_psi` with binned percentages + Laplace smoothing; preserve interface.
  - Lambda session: import from `shared/`; verify parity tests still pass; resolve HANDOFF.md §6 Q5 (DynamoDB schema) first.
  - Dashboards session: Grafana panels against the InfluxDB schema pinned by ADR 0005.
- **`context/local_runtime.md` updated?** yes (see commit).

## Note for next session
Subscriber/window/writer shell is wired. To pick up the model thread: `shared/score.py::score` is a 1-line stub (`clip(features["vibration_amp_mean_5m"] / 3.0, 0, 1)`) — the model session replaces the body with a real `predict_proba` call after loading `model.pkl` at import time. Don't touch the call sites in `local_runtime/service.py` or (when it lands) `lambda_scorer/handler.py`. Same posture for `shared/drift.py::compute_psi`.

To smoke-test locally:
```
docker compose up -d influxdb mosquitto
export INFLUX_TOKEN=ml-obs-local-token  # PowerShell: $env:INFLUX_TOKEN = "ml-obs-local-token"
cp local_runtime/config.example.yaml local_runtime/config.yaml
python -m simulator &
python -m local_runtime
# In another shell:
influx query 'from(bucket: "pump_telemetry") |> range(start: -1m) |> count()' --token ml-obs-local-token --org ml-obs
```
