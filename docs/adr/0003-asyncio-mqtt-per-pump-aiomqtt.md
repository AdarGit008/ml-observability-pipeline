# ADR 0003 — Asyncio + Aiomqtt, Per-Pump Connection, Retry-Forever, Schema-Only TLS Validation

- **Status:** Accepted
- **Date:** 2026-05-25
- **Deciders:** PO (Adar), Claude (architect), Gemini (reviewer)

## Context

PLAN.md §2.2 specifies that the simulator publishes telemetry "every 2 seconds" to MQTT, with `broker.target` switchable between local Mosquitto and AWS IoT Core (mTLS) on the same code path. Five interlocking design choices fall out of that brief, none of them obvious enough to leave implicit:

1. **MQTT client library.** `paho-mqtt` is the canonical Python MQTT client but is callback- and thread-driven, not asyncio-native. To use it from an asyncio runner we either hand-roll a thread-to-loop bridge or pull in `aiomqtt` (an asyncio-native wrapper by the same maintainer that uses paho underneath).
2. **Concurrency model.** Single asyncio loop with N tasks vs. process-per-pump. PLAN.md's 15-pump target and 0.5 Hz tick rate (7.5 msg/s aggregate) is trivial for asyncio; multiprocessing is overkill.
3. **MQTT connection topology.** One shared connection for the whole fleet, vs. one connection per pump. The trade-off is resource use (15 TCP sockets vs. 1) against mode-parity with AWS IoT Core (where one Thing = one client_id = one connection).
4. **Partial-failure policy.** If one pump's broker connection fails, the others should keep going. But for that one pump: abort, run-degraded, or retry-forever?
5. **AWS-IoT mTLS readiness.** The AWS account is not yet provisioned (per `ml-obs-pipeline-context`). The MQTT publisher contract must be ready for AWS IoT, but the implementation can't be written and tested today.

All five touch the simulator-runner shape directly. Documenting them in one ADR avoids three follow-up ADRs that all reference each other.

Anchors from `context/_global.md`: mode-parity (north star #6), single-PC dev (#2), AWS-specific differentiation (#3). The pump physical model already has degradation coupled to RPM (ADR 0002); the runner needs to publish that telemetry without distorting it.

## Decision

The simulator runner adopts the following five-part design:

1. **MQTT library: `aiomqtt`** (>=2.0) on top of `paho-mqtt` (>=2.0). aiomqtt is the asyncio-native wrapper by the same maintainer; we use its `async with Client(...)` / `await client.publish(...)` interface directly.
2. **Concurrency: single asyncio event loop, one task per pump.** Implemented in `simulator/runner.py::Fleet`. 15 pumps × 0.5 Hz is well under asyncio's comfort zone; process-per-pump is deferred to "if we ever need it" — which we won't at portfolio scale.
3. **Connection topology: one MQTT connection per pump, in BOTH local and AWS modes.** Each `Pump` is paired with its own `Publisher` (which owns one `aiomqtt.Client`, which owns one TCP socket). `client_id` matches the pump's id (e.g., `P-07`).
4. **Partial-failure policy: retry-forever, per-pump.** On `PublisherError` (connect refused, dropped, publish failed), the affected per-pump task waits with exponential backoff (1s → 30s ceiling) and reconnects independently. Other pumps are unaffected. **Backoff resets on each successful publish, not on each successful connect** — this closes the "publish-denied flapping" hole identified in Gemini Q3 of the 2026-05-25 mqtt-publishing review: a publisher with CONNECT permission but no PUBLISH permission (an AWS IoT policy that allows the former but not the latter) would otherwise loop at 1s forever, never engaging the 30s cap.
5. **AWS-IoT readiness: shape-only TLS schema validation.** The YAML schema accepts a `broker.tls` block (cert_path, key_path, ca_path) when `target: aws-iot`; `load_config` checks the paths are non-empty strings but does NOT touch disk. `AwsIotPublisher` exists, accepts a `TlsConfig` at construction, and raises `NotImplementedError` from `__aenter__` with a message pointing here. `Fleet.from_config` additionally rejects `target: aws-iot` up front so a misconfigured fleet fails before any pump tries to connect. File-existence and cert-content checks live in the (future) `AwsIotPublisher.__aenter__` body.

## Alternatives considered

### 1. MQTT library

**A. Hand-roll a paho ↔ asyncio bridge.** ~50 lines wrapping paho's threaded `loop_start()` with `asyncio.Event` and `loop.call_soon_threadsafe()`. Rejected: aiomqtt is by the same maintainer, ships with the bridge already tested, and pulls in zero protocol-stack difference. The portfolio signal value of "I wrote the bridge myself" is low compared to "the MQTT code is boring and correct."

**B. `gmqtt`** (pure-Python asyncio MQTT, no paho dep). Rejected: smaller maintenance footprint than paho/aiomqtt, fewer downstream users, and breaks the "paho is the canonical Python MQTT stack" intuition that a portfolio reviewer would expect.

### 2. Concurrency

**A. Process-per-pump (multiprocessing).** Rejected: 15 OS processes for a 7.5 msg/s aggregate workload is wildly over-provisioned. Each process would idle at multi-MB RSS for sub-percent CPU.

**B. Threading instead of asyncio.** Rejected: aiomqtt is asyncio-native, and the rest of the project (lambda_scorer, drift) leans on async-friendly idioms anyway. Mixing threads with future async code is a footgun this project shouldn't pay for.

### 3. Connection topology

**A. Single shared MQTT connection in local mode** (one client publishes for all 15 pumps), per-pump in AWS mode. The local optimization saves 14 TCP sockets. Rejected by the PO at session-brief time (Q2): it creates a behavioral diff between local and AWS that violates north star #6 (mode parity). With 15 connections to Mosquitto being well within its capacity, the optimization buys nothing useful.

**B. Single shared connection in BOTH modes.** Rejected because AWS IoT Core's threat model is "one Thing per pump" — sharing a `client_id` across pumps would conflate identities, and using a generic `client_id` would lose per-pump observability in CloudWatch.

### 4. Partial-failure policy

**A. Abort the whole fleet on any per-pump failure.** Simplest, but turns one flaky pump into a fleet outage. Rejected: a real pump fleet has pumps going offline all the time; the simulator should reflect that.

**B. Run-degraded (skip the failed pump, log, continue forever).** Rejected: a pump that "permanently" failed at startup might have a fixable problem upstream (broker just restarting, network glitch). Permanently giving up on it is too aggressive.

**C. Retry-forever (the decision).** A pump's per-task connect loop keeps trying with exponential backoff; the rest of the fleet is unaffected. PO picked this at session-brief time (Q4). Matches paho's own auto-reconnect philosophy.

**D. Reset backoff on successful CONNECT (rejected during Gemini review).** Initial implementation. Gemini Q3 identified the flapping hole: a publisher that connects but is denied publish would never engage the 30s cap. Replaced by "reset on successful publish" — requires getting one message through before trusting the connection.

### 5. AWS-IoT TLS validation

**A. Shape-only schema validation, file checks in the publisher (the decision).** Loader is pure schema validation; file existence is checked where the file is about to be opened.

**B. File-existence checks in `load_config`.** PO considered, rejected at session-brief time (Q3): couples the loader to the filesystem state, makes every test exercising the aws-iot path need three fake cert files on disk, and duplicates work that has to happen in the publisher anyway when the file is read.

**C. Full mTLS validation in `load_config`** (parse the cert chain, verify it's internally consistent). Rejected: this is the AWS-IoT session's actual implementation work, not load-time work. It also requires `cryptography` as a dep, which is overkill for "the path looks right."

## Consequences

**Positive:**

- **Mode parity preserved.** Same `Publisher` ABC, same per-pump connection topology, same `Fleet` runner across local and AWS targets. The implementation gap is exactly one class (`AwsIotPublisher.__aenter__`).
- **Per-pump observability.** Mosquitto and CloudWatch both see distinct `client_id`s — log lines for "pump P-07 disconnected" map cleanly to the right physical pump.
- **Failure isolation.** A flaky pump can't drag the rest of the fleet down. Recruiter-facing demo behavior matches what "production" fleet code should do.
- **Backoff cap engages correctly under publish-denied scenarios** (per the Gemini-Q3 reset-on-publish refinement).
- **Loader stays pure.** `load_config` is now back to "validate the YAML shape and return a typed dataclass" — no filesystem, no warnings, no runtime feasibility checks. Tests are simpler and the contract is clearer (`Fleet.from_config` is where runtime-feasibility lives).
- **Zero AWS spend during the wait.** The mTLS publisher doesn't actually run; we can keep iterating on the rest of the pipeline without provisioning AWS today.

**Negative:**

- **15 TCP connections to Mosquitto, not 1.** Trivial at 15 pumps but worth knowing if `pump_count` ever stretches toward the schema cap of 100.
- **aiomqtt is one more dependency** beyond the brief's stated paho-mqtt. We get the protocol stack we'd have written ourselves but with a thicker import-time footprint.
- **`AwsIotPublisher` is dead code in the main flow** (`Fleet.from_config` rejects `target: aws-iot` up front). The class still exists for direct callers and as a contract anchor, but real coverage of the connect path waits for the AWS-IoT session.
- **No real-broker tests in `pytest`.** The unit tests monkeypatch `aiomqtt.Client`; manual smoke step with Mosquitto in Docker is the wire-format check. This is a deliberate trade against pulling Docker into the test suite (the FUSE / cache situation in the sandbox makes that worse than usual).

**Follow-ups:**

- AWS account provisioning (still ⬜ in `ml-obs-pipeline-context`).
- The AWS-IoT session: implement `AwsIotPublisher.__aenter__` / `publish`, including TLS file-existence + cert parsing, IoT endpoint validation, and per-Thing policy attachment. Remove the `Fleet.from_config` reject; let the publisher itself be the gate.
- Scenario runner (seasonal_drift, fleet_expansion, real_failure) — `Fleet.from_config` raises `NotImplementedError` today; replace with a `Scenario` interface that mutates the per-pump state machines according to a schedule.
- Consider exposing `tick_seconds` via the YAML schema if a session ever needs to lower it for a fast-replay demo.

## References

- PLAN.md §2.2 — telemetry cadence and broker target.
- Session brief and resolution exchange (2026-05-25 mqtt-publishing).
- Session log: `docs/sessions/2026-05-25-simulator-mqtt-publishing.md`.
- Implementation: `simulator/publisher.py`, `simulator/runner.py`, `simulator/__main__.py`.
- Schema delta: `simulator/config.py::TlsConfig` + `_validate_broker`, `simulator/config.example.yaml`.
- Tests: `simulator/tests/test_publisher.py` (21 cases), `simulator/tests/test_runner.py` (25 cases incl. backoff sequence + reset-on-publish), tls block additions in `simulator/tests/test_config.py`.
- Review packet: `review_packets/2026-05-25-simulator-mqtt-publishing.md` — Q3 (reset-on-publish) and Q8 (Windows signal handling) drove changes from Proposed to Accepted.
- Review response: `review_responses/2026-05-25-simulator-mqtt-publishing.md`.
- Related ADRs: ADR 0002 (RPM coupling — feeds the telemetry this runner publishes).
- aiomqtt: https://aiomqtt.readthedocs.io/
