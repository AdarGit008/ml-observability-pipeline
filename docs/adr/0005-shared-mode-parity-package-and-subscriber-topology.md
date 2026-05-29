# ADR 0005 — Shared Mode-Parity Package + Single-Connection Subscriber Topology + InfluxDB Schema

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

The 2026-05-29 local_runtime session set up the first downstream
consumer of the simulator's telemetry: subscribe to local Mosquitto,
maintain a 5-minute rolling window per pump, score, compute PSI, write
to InfluxDB. Three interlocking design choices fell out of that brief
that are each non-obvious enough to deserve a record, and recording
them together avoids three follow-up ADRs that cross-reference each
other:

1. **Where shared mode-parity logic lives.** PLAN.md §1's repo layout
   sketches `lambda_scorer/drift.py` (a per-Lambda module). PLAN.md
   §2.1 says "the Lambda handler imports the same `drift.py` module
   as `local_runtime/scorer_service.py`" — i.e., it's shared between
   the two. The sketch and the §2.1 statement are not consistent.
   Mode parity is north star #4 (`context/_global.md`), so the
   reconciliation has to favour sharing; the question is *where*.
2. **Subscriber connection topology.** ADR 0003 §Decision 3 mandates
   one MQTT connection per pump on the *publish* side, because AWS
   IoT's threat model is "one Thing = one client_id = one connection."
   Subscribers have no such constraint. The choice is one wildcard
   subscription (`factory/pumps/+/telemetry`) on a single connection
   vs. 15 per-pump subscriptions.
3. **InfluxDB schema.** PLAN.md §2.8 says "storage" but the section
   is empty in the PLAN doc. The schema for the `pump_telemetry`
   measurement (tags vs. fields, naming, PSI shape) is this session's
   territory.

All three touch the local_runtime shape directly. Anchors from
`context/_global.md`: mode parity (#4 — the dominant constraint
here), single-PC dev (#2 — argues against per-pump subscriber
connections that buy nothing locally), AWS-specific differentiation
(#3 — the schema decisions here also need to survive the AWS hot-path
that will write the same conceptual rows to DynamoDB).

## Decision

The local_runtime session adopts the following three-part design:

1. **Top-level `shared/` package** (peer to `local_runtime`,
   `lambda_scorer`, `simulator`) holds the mode-parity logic.
   `shared/features.py` (the 8-feature pure extractor),
   `shared/score.py` (scoring interface — stub today, real model
   later), `shared/drift.py` (PSI interface — stub today, real
   implementation later). Dependency ceiling: standard library +
   `numpy`. Both `local_runtime` and a future `lambda_scorer` import
   the same modules as peers. The Lambda packaging step copies
   `shared/` into the deployment zip alongside `lambda_scorer/`.
2. **Single MQTT connection, single wildcard subscription** for the
   local subscriber. `factory/pumps/+/telemetry` on one
   `aiomqtt.Client`. The `pump_id` is parsed from the topic on each
   incoming message via a strict regex (`^factory/pumps/(P-\d{2})/telemetry$`).
   ADR 0003's per-pump connection rule applies to the publish side
   only and remains the correct call there.
3. **InfluxDB schema for the `pump_telemetry` measurement:**
   - Tags: `pump_id` (one tag, low cardinality 1–100).
   - Fields: 8 feature fields (named per `shared.features.FEATURE_NAMES`)
     + `score` + 8 PSI fields prefixed `psi_<feature>`. Total: 17
     numeric fields per point.
   - Timestamp: the telemetry payload's `ts`, normalised to UTC.

## Alternatives considered

### 1. Where shared mode-parity logic lives

**A. Top-level `shared/` package (the decision).** Neither component
owns the logic; both import as peers. Clean separation of "what's
shared" from "what's local-only" or "what's Lambda-only." Mirrors the
convention in projects with similar deploy-time-bundling needs (e.g.,
AWS SAM examples). Cost: one extra top-level directory, and the
Lambda packaging step has to copy two roots into the .zip instead of
one.

**B. `local_runtime/shared/` — shared logic lives under
local_runtime, Lambda imports `from local_runtime.shared import ...`.**
Rejected because it implies local_runtime owns the parity logic, and
Lambda is a "client" of local_runtime. The reality is the inverse if
anything — Lambda is the production target — and either way the
asymmetry is misleading.

**C. `lambda_scorer/drift.py` (PLAN.md §1's sketch).** Rejected for
the symmetric reason: it puts the parity logic under one of the two
consumers. Also conflicts with PLAN.md §2.1, which is the
authoritative mode-parity statement. The sketch in §1 was an
expedient: this ADR codifies the resolution. The hot path Lambda
package will still contain a `drift.py` at packaging time — copied
from `shared/drift.py` during the deployment build — so the §1
sketch remains accurate as a description of the deployed zip layout,
but the source-of-truth lives in `shared/`.

**D. Vendor-copy the shared logic into both consumers.** Rejected on
the mode-parity north star: any drift between the two copies is by
definition a parity violation, and humans copying-and-pasting files
across two directories is the textbook way to introduce drift.

### 2. Subscriber connection topology

**A. One MQTT connection, wildcard subscription (the decision).**
The subscriber owns one TCP socket to Mosquitto and one MQTT
subscription. The `pump_id` is parsed from the topic per message.
Cleanest local-mode shape; no behavioural surprise.

**B. 15 MQTT connections, per-pump topic subscriptions** (mirroring
publisher topology). Considered for the symmetry argument: if the
publisher is per-pump for AWS reasons, the subscriber being per-pump
makes the topology diagram cleaner. Rejected because the constraint
that drove publisher per-pump topology (AWS IoT's Thing-per-pump
model) doesn't apply to the subscriber side at all. The Lambda hot
path doesn't subscribe — it's invoked per-message by an IoT Rule
trigger — so the subscriber topology doesn't replicate to AWS mode
and "looking symmetric on the diagram" buys nothing real. Cost would
be 15 TCP sockets and 15 paho client-id rows in Mosquitto's log for
no functional benefit.

**C. One connection per pump_id-prefix subgroup** (e.g., 3 connections
× 5 pumps each). Rejected as ceremony with no win; the wildcard
already does what subgrouping would achieve and at lower cost.

### 3. InfluxDB schema

**A. `pump_id` as a tag, flat `psi_<feature>` fields (the decision).**
Tags are indexed in InfluxDB v2's TSI; queries that filter by pump_id
(every per-pump panel in Grafana) hit the index directly.
`psi_<feature>` flat fields play nicely with Grafana's standard
field-selector queries (one field = one series).

**B. `pump_id` as a field.** Rejected: InfluxDB doesn't index
fields, so `WHERE field == 'P-07'` is a full scan over the
measurement. At 15 pumps × 30 readings/min × 1 hour = 27K points/hour
this is cheap, but at scale (or with year-long retention) it's the
wrong shape.

**C. Nested PSI map field (JSON-encoded).** Rejected: InfluxDB line
protocol doesn't support nested structures, so the writer would have
to JSON-encode the dict into a single string field. Grafana would
then need a transform to plot per-feature PSI — extra moving parts
for no win.

**D. Separate `pump_drift` measurement for PSI.** Considered:
separating scores from drift would let the two have different
retention policies. Rejected because they're per-reading siblings;
splitting them doubles the per-message InfluxDB load and requires
joining at query time. Retention policy split, if needed, can be
done via continuous queries later without re-shaping the measurements.

## Consequences

**Positive:**

- **Mode parity is enforceable, not just aspirational.** The
  `shared/features.py` import path is testable
  (`test_mode_parity_uses_shared_features_module`); a future Lambda
  that forks the module gets caught by the same test once it's added
  to the lambda_scorer's tests.
- **One MQTT connection on the subscriber side simplifies the
  Mosquitto log, the per-message latency story, and the asyncio
  topology** — no Task-per-pump fan-out, just one message loop.
- **InfluxDB schema is Grafana-ready.** Per-pump filter via the
  `pump_id` tag, per-feature PSI panel via the `psi_<feature>`
  fields, no transforms needed.
- **PLAN.md §1's `lambda_scorer/drift.py` sketch is reconciled
  cleanly.** The shared module is the source of truth; the deployed
  Lambda zip still contains a `drift.py` because the build step
  copies it in.

**Negative:**

- **Two top-level packages to keep in sync at packaging time.** The
  Lambda deployment step now has to bundle both `shared/` and
  `lambda_scorer/` into the zip. This is a 1-line `cp -r` in the
  Terraform `archive_file` data source, but it's a step that has to
  exist, and a future session may forget it. Mitigation: a
  CI-or-deploy-time check that `lambda_scorer.handler` actually
  imports `shared.*` cleanly from the built zip.
- **InfluxDB token in the docker-compose env block is committed in
  plaintext.** It's a *local* token for a *local* InfluxDB exposed on
  `localhost:8086` only, so the threat model is "developer's machine
  is compromised" — at which point the InfluxDB token is the least
  of their problems. The local_runtime config uses `${INFLUX_TOKEN}`
  resolution so a developer who wants secrecy can swap it.
- **Wildcard subscription means malformed topics that match the
  prefix (e.g., `factory/pumps/X/telemetry`) hit the subscriber.**
  Mitigated by the strict pump_id regex that drops non-matching
  messages with a log line; tested in
  `test_subscriber_skips_non_matching_topics`.
- **`numpy` as a runtime dep** (1.26+) in addition to the existing
  PyYAML/aiomqtt/paho-mqtt. `numpy` is the universal "linear algebra
  but not scipy" choice; the rolling mean/std could be implemented
  with `statistics` stdlib but the AWS Lambda mode will eventually
  need `numpy` for the scorer's feature vector handling anyway, so
  pulling it in now keeps the dep set stable across modes.

**Follow-ups:**

- The model session: implements `shared.score.score` for real
  (`HistGradientBoostingClassifier.predict_proba`), bundles
  `model.pkl`.
- The drift session: implements `shared.drift.compute_psi` for real
  (binned percentages vs. reference distribution, Laplace smoothing).
- The lambda_scorer session: imports from `shared/` and verifies the
  parity tests still pass after Lambda lands.
- The dashboards session: Grafana panels that query the schema
  pinned in this ADR.
- DynamoDB schema resolution (HANDOFF.md §6 Q5) is still open and is
  the lambda_scorer session's blocker. local_runtime doesn't gate on
  it because the InfluxDB schema decided here is local-only.

## References

- PLAN.md §1 (repo layout sketch — the source of the
  `lambda_scorer/drift.py` ambiguity).
- PLAN.md §2.1 (mode parity statement — the resolution direction).
- PLAN.md §2.3 (8-feature spec — the feature schema this ADR pins).
- PLAN.md §2.7 (PSI thresholds — drive the stub's sentinel values).
- PLAN.md §2.9 (Grafana + InfluxDB datasource — drives the schema
  decisions).
- `context/local_runtime.md` (mode-parity invariant text).
- `context/_interfaces.md` (telemetry payload + PSI parameters).
- ADR 0003 (publisher topology — referenced for the per-pump-vs.-wildcard
  asymmetry rationale).
- Session log: `docs/sessions/2026-05-29-local_runtime-subscribe-window-influx.md`.
- Review packet: `review_packets/2026-05-29-local_runtime-subscribe-window-influx.md`.
- Implementation: `shared/features.py`, `shared/score.py`,
  `shared/drift.py`, `local_runtime/subscriber.py`,
  `local_runtime/window.py`, `local_runtime/influx_writer.py`,
  `local_runtime/service.py`.
- InfluxDB v2 line protocol: https://docs.influxdata.com/influxdb/v2/reference/syntax/line-protocol/
- influxdb-client Python: https://github.com/influxdata/influxdb-client-python

## Addendum 2026-05-29 — Gemini review dispositions

Source: `review_responses/2026-05-29-local_runtime-subscribe-window-influx.md`.
Six points raised; dispositions below. Two drove code changes (Q4 +
Q6); three drove doc/follow-up amendments (Q1, Q2, Q3); one was a
YAGNI confirmation (Q5).

### Q1 — Terraform packaging of `shared/` + `lambda_scorer/`

**Disposition:** Accepted, deferred to lambda_scorer session.

**Gemini's point:** Terraform's `archive_file.source_dir` doesn't
combine multiple arbitrary directories cleanly. Pointing it at the
repo root drags `local_runtime/`, `simulator/`, `docs/` etc. into the
deployment zip, bloating Lambda cold-start and leaking dev code into
production artifacts.

**Decision:** The lambda_scorer session will introduce
`scripts/build_lambda.ps1` (and a `.sh` companion) that stages
`shared/` and `lambda_scorer/` into `.build/lambda_dist/` before
Terraform's `archive_file` zips it. This is standard monorepo-in-AWS
practice without SAM/Serverless; we're not going to fight Terraform
on it. Folded into the lambda_scorer session's brief — local_runtime
itself doesn't deploy anything so the friction is captured but
deferred.

### Q2 — Concurrency parity (false equivalence flagged)

**Disposition:** Partially accepted — clarification, not code change.

**Gemini's point:** The local subscriber's
"sync-handler-per-message" model on a single asyncio loop is *not*
the same as Lambda's "N concurrent invocation environments." At
high message rates the local handler would serialise CPU work that
Lambda parallelises. Suggested `ProcessPoolExecutor` fan-out.

**Decision:** The mode-parity invariant is about *output
correctness* under the same input stream, not *concurrency model*.
Two handlers that compute the same `extract_features` + `score` +
`compute_psi` on the same window produce the same row regardless of
how many run in parallel. ProcessPoolExecutor adds real complexity
(window-snapshot pickling per dispatch, IPC, process lifecycle
under signal handlers) for no measurable win at PLAN.md's 7.5 msg/s
target. At the 100-pump cap (50 msg/s) and ~10 μs per
`extract_features` call, the loop spends 0.5 ms/s on CPU work —
nowhere near the GIL contention or PINGREQ-starvation territory
Gemini flagged. If the project ever stretches toward 1000-pump
fleet sizes (it won't, per scope) the fan-out is a reachable
refactor without breaking parity.

**Doc change:** `context/local_runtime.md`'s "Mode parity invariant"
section now states explicitly that the invariant is about
per-message output, not throughput. This Addendum is the long-form
record.

### Q3 — Writing PSI on every tick is wasteful

**Disposition:** Partially accepted — schema unchanged, write
cadence flagged as open question for the drift session.

**Gemini's point:** Computing and writing PSI on every 2-second
telemetry tick burns Lambda CPU (constraint #1) and inflates Influx
storage with values that barely change. PSI should be on a tumbling
window (~5 min), not per-tick.

**Decision:** PLAN.md §2.5 prescribes per-tick PSI computation in
the hot path ("update PSI accumulator vs. reference distribution");
that's not in scope to change here. PLAN.md §2.7's fleet-level PSI
is already on a 5-minute EventBridge schedule, so the prescription
isn't blind. But Gemini's storage concern is real: the rolling
1-hour window's PSI value drifts by ~1/1800 per tick, so writing it
on every tick is mostly redundant rows.

**The schema** (per-tick PSI fields in `pump_telemetry`) **stays**.
Changing the measurement post-ADR for a stub session adds churn.

**The write cadence** is a documented open question for the drift
session: write PSI on every Nth tick (e.g., every 30 ticks ~ once
per minute) and let intermediate rows have NULL psi_* fields, OR
write to a separate `pump_drift` measurement on the existing
tumbling schedule. Either is compatible with this ADR's schema.
Tracked in `context/local_runtime.md` open questions.

### Q4 — `asyncio.to_thread` on a sync client when an async API exists

**Disposition:** Accepted — code change landed this session.

**Gemini's point:** `influxdb-client` ships an aiohttp-backed async
API via `influxdb_client.client.influxdb_client_async.InfluxDBClientAsync`
(since 1.36). Wrapping the sync client's `write_api.write` in
`asyncio.to_thread` is unnecessary thread-pool thrash.

**Decision:** Implementation refined. `local_runtime/influx_writer.py`
now uses `InfluxDBClientAsync` directly; `__aenter__`/`__aexit__`
are awaited (the client is itself an async context manager); the
per-message path is `await write_api.write(...)`. The factory
signature is unchanged so test injection still works; the test
`FakeClient` was updated to be an async context manager with an
async `write_api().write(...)`.

**Test:** `test_writer_write_is_awaited_not_to_thread` pins the
shape — the fake's `write` is `async def`; if the writer ever
regresses to a sync wrap, awaiting a non-coroutine would fail
loudly. `requirements.txt` already pinned `influxdb-client[async]`;
no dep changes.

### Q5 — PSI sentinel configurability

**Disposition:** Accepted as-stated (i.e., not configurable). No
code change.

**Gemini's point:** YAGNI. The static fixtures perfectly fulfil
the downstream alerting contract; a future test needing a
significant-shift band can `unittest.mock.patch` it inline.

**Decision:** Concur. `shared/drift.py::_STUB_PSI` stays a module
constant.

### Q6 — Stronger structural parity test

**Disposition:** Accepted — code change landed this session.

**Gemini's point:** `local_runtime.service.extract_features is
shared.features.extract_features` only proves the same `sys.modules`
cached object is referenced from both sides. A vendored copy that
both sides agree to import would still pass `is`. Use
`inspect.getfile` to verify the function physically loads from
`/shared/`.

**Decision:** Three new tests in
`local_runtime/tests/test_service.py`:
`test_structural_parity_no_vendoring` (for `extract_features`),
`test_structural_parity_score_loads_from_shared`, and
`test_structural_parity_compute_psi_loads_from_shared`. Each
resolves the function's source file via `inspect.getfile` and
asserts it sits inside the repo's `shared/` directory. A vendored
copy inside `local_runtime/` would now fail loudly.

### Test count after dispositions

303 → 308 passing, 1 pre-existing skip. New: 3 structural parity
tests + 2 reshaped Influx writer tests covering the async context.
