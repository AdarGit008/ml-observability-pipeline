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
5. **AWS-IoT readiness: shape-only TLS schema validation.** The YAML schema accepts a `broker.tls` block (cert_path, key_path, ca_path) when `target: aws-iot`; `load_config` checks the paths are non-empty strings but does NOT touch disk. `AwsIotPublisher` exists and accepts a `TlsConfig` at construction. **Updated 2026-05-27 (see §"Addendum 2026-05-27 — AwsIotPublisher wired"):** `AwsIotPublisher.__aenter__` is now implemented — it performs file-existence checks (`Path.is_file()` per field), builds an `ssl.SSLContext` (`create_default_context(SERVER_AUTH, cafile=ca_path)` + `load_cert_chain(certfile, keyfile)`), and hands the context to `aiomqtt.Client(tls_context=...)` on port 8883. File-existence and SSL build errors surface as `PublisherError`, so the runner's retry-forever loop catches them the same way as transient connect errors. `Fleet.from_config` no longer rejects `target: aws-iot`; the publisher itself is the gate. Pre-2026-05-27 the publisher raised `NotImplementedError` from `__aenter__` and the runner double-guarded by rejecting at config time.

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

- **Mode parity preserved.** Same `Publisher` ABC, same per-pump connection topology, same `Fleet` runner across local and AWS targets. With AwsIotPublisher wired (Addendum 2026-05-27), the implementation gap closed — both publishers share the same wire shape (QoS 0, retain=False, JSON payload, JSON-encoded telemetry dict).
- **Per-pump observability.** Mosquitto and CloudWatch both see distinct `client_id`s — log lines for "pump P-07 disconnected" map cleanly to the right physical pump.
- **Failure isolation.** A flaky pump can't drag the rest of the fleet down. Recruiter-facing demo behavior matches what "production" fleet code should do.
- **Backoff cap engages correctly under publish-denied scenarios** (per the Gemini-Q3 reset-on-publish refinement). With AwsIotPublisher now wired, this is no longer a hypothetical — a misconfigured IoT policy (CONNECT-but-not-PUBLISH) really would loop on the cap, not at 1s.
- **Loader stays pure.** `load_config` is now back to "validate the YAML shape and return a typed dataclass" — no filesystem, no warnings, no runtime feasibility checks. Tests are simpler and the contract is clearer (`Fleet.from_config` is where runtime-feasibility lives).
- **Zero AWS spend during the wait.** The mTLS publisher doesn't actually run until the PO points a config at it; we can keep iterating on the rest of the pipeline.

**Negative:**

- **15 TCP connections to Mosquitto, not 1.** Trivial at 15 pumps but worth knowing if `pump_count` ever stretches toward the schema cap of 100.
- **aiomqtt is one more dependency** beyond the brief's stated paho-mqtt. We get the protocol stack we'd have written ourselves but with a thicker import-time footprint.
- ~~**`AwsIotPublisher` is dead code in the main flow** (`Fleet.from_config` rejects `target: aws-iot` up front).~~ **Resolved 2026-05-27** — `AwsIotPublisher` is now the gate (missing certs → `PublisherError`, retry-forever applies). `Fleet.from_config` no longer rejects.
- **No real-broker tests in `pytest`.** The unit tests monkeypatch `aiomqtt.Client` (and `ssl.create_default_context` for the aws-iot path); manual smoke step with Mosquitto in Docker — and now also with AWS IoT Core via the MQTT test client — is the wire-format check. This is a deliberate trade against pulling Docker into the test suite (the FUSE / cache situation in the sandbox makes that worse than usual). The cost of this trade became visible during the 2026-05-27 smoke test (see §"Addendum 2026-05-27 — Windows event-loop policy") — both Windows-quirk bugs caught there would have been caught by an integration test that opens a real socket.

## Addendum 2026-05-27 — Windows event-loop policy

The first end-to-end smoke test of the simulator on Windows (Python 3.14) exposed a second Windows asyncio quirk beyond the SIGINT handler covered by §"Decision 5" / Gemini-Q8:

**Symptom:** Every per-pump connect timed out with `Operation timed out`; the broker log showed no incoming connections; the paho client emitted `Caught exception in on_socket_unregister_write`.

**Root cause:** Windows defaults to `ProactorEventLoop` (since Python 3.8). `ProactorEventLoop` does NOT implement `loop.add_reader()`/`add_writer()` — they raise `NotImplementedError`. paho-mqtt uses exactly those methods to register its socket with asyncio. Result: the socket never gets registered, paho can't notice the SYN-ACK reply, and the connect hangs until the OS-level TCP timeout fires.

**Fix:** `simulator/__main__.py` now passes `loop_factory=asyncio.SelectorEventLoop` to `asyncio.run()` on Windows. `SelectorEventLoop` supports `add_reader`/`add_writer` and is what paho/aiomqtt were designed against. We don't use the subprocess-async features that `ProactorEventLoop` is better at, so the swap is pure win. On Unix, `loop_factory=None` falls through to the platform default (which is `SelectorEventLoop` already).

**Why `loop_factory` over `set_event_loop_policy`:** the older `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` pattern works but emits `DeprecationWarning` on Python 3.12+; both APIs are slated for removal in 3.16. `loop_factory` (added to `asyncio.Runner` and `asyncio.run` in 3.12) is the modern non-deprecated equivalent.

**Why the unit tests didn't catch this:** the monkeypatched `aiomqtt.Client` in `simulator/tests/test_publisher.py` and `test_runner.py` never opens a real socket and never invokes `loop.add_reader`. The only path that exercises the failing code is a live broker connect, which is what the smoke test covers.

**Lessons logged in the session notes:** a future "integration smoke" suite running against a real `eclipse-mosquitto` container would catch issues in this class (Windows-asyncio, paho-aiomqtt version mismatches, mosquitto config quirks like `allow_anonymous` placement). Pulling Docker into `pytest` is still rejected for the reasons in the "No real-broker tests in pytest" bullet above; the smoke test in `docs/sessions/2026-05-25-simulator-mqtt-publishing.md` is the manual stand-in.

## Addendum 2026-05-27 — AwsIotPublisher wired

`AwsIotPublisher` went from stub to live implementation on 2026-05-27. Full session log: [`docs/sessions/2026-05-27-simulator-aws-iot-publisher.md`](../sessions/2026-05-27-simulator-aws-iot-publisher.md).

**Status change:** `AwsIotPublisher` was previously a stub — `__aenter__` raised `NotImplementedError` and `Fleet.from_config` double-guarded by rejecting `target: aws-iot` up front. Both gates were removed in favour of a single gate inside the publisher itself.

**What runs now in `AwsIotPublisher.__aenter__`:**

1. **File-existence checks** — `Path(p).is_file()` on each of `tls.cert_path`, `tls.key_path`, `tls.ca_path`. Missing files raise `PublisherError` naming the offending field (e.g., `"AWS IoT mTLS cert_path not found: '/Users/.../P-00.cert.pem' (...)"`). This step has no monkeypatch dependency, so the tests cover it with `tmp_path`-created placeholder files and a non-existent path.
2. **SSLContext build** — `ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_path)` followed by `ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)`. Malformed PEM, key/cert mismatch, expired CA, or a mid-rotation rotation race all surface as `ssl.SSLError` / `OSError`; we wrap both as `PublisherError` with the three file paths named.
3. **aiomqtt connect** — `aiomqtt.Client(hostname, port=8883, identifier=client_id, tls_context=ctx)` is instantiated and entered; `aiomqtt.MqttError` on connect is wrapped as `PublisherError`.

**Wire shape is identical to LocalPublisher.** QoS 0, `retain=False`, JSON-encoded payload. AWS IoT Core supports QoS 0 and 1 (not 2); we chose 0 for mode parity (north star #6) — the cost meter is per-message regardless of QoS, so QoS 1 is a viable future toggle if downstream consumers need at-least-once.

**Why `Fleet.from_config` no longer rejects.** The previous belt-and-braces double-guard was justified while the publisher was a stub (better error than `NotImplementedError` from inside an asyncio task). With the publisher really connecting, the publisher's own `PublisherError`s feed the retry-forever loop the same way local transport errors do, so the runner UX is now uniform across targets. Direct callers that bypass `Fleet.from_config` (constructing `AwsIotPublisher` themselves) still get the same gate inside `__aenter__`.

**Test coverage added** (`simulator/tests/test_publisher.py`): 13 new tests bringing the suite from 21 publisher tests / 139 total to 34 publisher tests / 152 total. The new tests cover existence checks (`missing_cert` / `missing_key` / `missing_ca`), happy path (`builds_ssl_context_and_passes_to_aiomqtt`), error wrapping (`wraps_ssl_error`, `wraps_oserror_on_ca_read`, `wraps_mqtt_error`), wire shape (`publish_emits_qos0_retain_false_json`), and the boring contract bits (idempotent exit, swallow on disconnect, publish-before-aenter, default port 8883). One runner test was swapped in place: `test_from_config_rejects_aws_iot_target` → `test_from_config_uses_aws_iot_publisher_for_aws_iot_target`.

**Smoke test:** documented in the session log. Phase A (Console-provisioned Thing "P-00" + cert + per-Thing policy, ~10 min), Phase B (`mosquitto_pub` verification with the downloaded cert), Phase C (`python -m simulator --config simulator/config.aws-iot.yaml --log-level INFO`), Phase D (observe in IoT Core MQTT test client subscribed to `factory/pumps/P-00/telemetry`). Per the brief's cost math, the smoke is scoped to single-pump P-00 only (~0 spend even ignoring credits).

**Follow-ups:**

- AWS IoT account-level provisioning is still PO-side / Console-only this session. Terraform-managed Things/policies/certs is the natural next infra session (deferred — too much surface for this session, per the brief).
- A future "integration smoke" suite running against either eclipse-mosquitto or a one-shot IoT-Core round trip would catch the issues that the existing monkeypatch-based unit tests can't (the Windows-loop quirk was the canary; SSL-version mismatches and CA chain drift would be the next class).
- Scenario runner (`seasonal_drift`, `fleet_expansion`, `real_failure`) — `Fleet.from_config` still raises `NotImplementedError` for non-healthy scenarios. Separate session.

## References

- PLAN.md §2.2 — telemetry cadence and broker target.
- Session brief and resolution exchange (2026-05-25 mqtt-publishing).
- Session log: `docs/sessions/2026-05-25-simulator-mqtt-publishing.md`.
- Session log: `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` (AwsIotPublisher wired).
- Implementation: `simulator/publisher.py`, `simulator/runner.py`, `simulator/__main__.py`.
- Schema delta: `simulator/config.py::TlsConfig` + `_validate_broker`, `simulator/config.example.yaml`.
- Tests: `simulator/tests/test_publisher.py` (34 cases), `simulator/tests/test_runner.py` (25 cases incl. backoff sequence + reset-on-publish), tls block additions in `simulator/tests/test_config.py`.
- Review packet: `review_packets/2026-05-25-simulator-mqtt-publishing.md` — Q3 (reset-on-publish) and Q8 (Windows signal handling) drove changes from Proposed to Accepted.
- Review packet: `review_packets/2026-05-27-simulator-aws-iot-publisher.md` — AwsIotPublisher wiring review.
- Review response: `review_responses/2026-05-25-simulator-mqtt-publishing.md`.
- Related ADRs: ADR 0002 (RPM coupling — feeds the telemetry this runner publishes).
- aiomqtt: https://aiomqtt.readthedocs.io/
- AWS IoT Core mTLS: https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html

## Addendum 2026-05-28 — Static config errors halt the fleet

**Origin:** Gemini Q3 of the 2026-05-27 aws-iot-publisher review
([review_responses/2026-05-27-simulator-aws-iot-publisher.md](../../review_responses/2026-05-27-simulator-aws-iot-publisher.md)).

**Carve-out from §Decision 4** (retry-forever, per-pump). Decision 4
remains the right policy for *transient* transport failures — broker
flaky, network blip, AWS IoT policy briefly misconfigured. But Gemini
flagged that a *static* configuration error (missing cert file,
malformed PEM, key/cert mismatch, unparseable URL) is structurally
different: looping forever at the 30 s cap with "cert_path not found"
logged every 30 s is the wrong UX on a single-PC dev machine (north
star #2). The developer wants an immediate crash so they can fix their
YAML / file paths, not a polling loop that buries the error.

**New exception class:** `PublisherConfigError` is a subclass of
`PublisherError`. Subclass relationship matters because generic
`except PublisherError` sites still catch it (load-bearing for callers
that don't know about the new type). Runner-side, the catch ordering
in `_run_pump` puts `except PublisherConfigError` BEFORE
`except PublisherError` — Python evaluates handlers in source order,
so the subclass-specific branch must come first.

**Raises that surface as `PublisherConfigError`:**

- File-existence check on cert/key/ca paths (`Path.is_file()` returns
  False in `AwsIotPublisher.__aenter__`).
- SSL context build failures: `ssl.SSLError` (malformed PEM, expired
  CA), `OSError` (cert rotated mid-rotation), and `ValueError`
  (encrypted PKCS#8 key without password, unsupported key type — per
  Gemini Q2 of the same review; `ValueError` was missing from the
  initial 2026-05-27 exception tuple).
- `_parse_mqtt_url` failure (unparseable URL). This one surfaces at
  publisher *construction* (in `__init__`), so it propagates up from
  `Fleet.from_config` rather than from inside a per-pump task — caught
  by `main()` on the from_config path.

**Raises that stay as transient `PublisherError`:**

- `aiomqtt.MqttError` on connect (`CONNREFUSED`, network down, policy
  mismatch — these CAN recover).
- `aiomqtt.MqttError` on publish.
- `aiomqtt.MqttError` on disconnect (swallowed in `__aexit__`).

**Runner behavior:**

`Fleet._run_pump` re-raises `PublisherConfigError` (logged at ERROR,
not WARNING). `Fleet.run`'s `asyncio.gather` wrapper catches it
alongside `asyncio.CancelledError` — both paths set the shutdown
event, drain the other tasks via `gather(return_exceptions=True)`, and
re-raise. The 2026-05-28 disconnect-bound wrapper from the prior
addendum is load-bearing here: without it, a pump mid-publish when the
fleet halts could stall the drain indefinitely. The two 2026-05-28
fixes (bounded disconnect + halt-on-static-error) interact correctly
because the bound is INSIDE `__aexit__`, which runs during the drain.

**`main()` behavior:**

New exit code `4` (`PUBLISHER_CONFIG_ERROR_CODE`), distinct from `2`
(YAML/schema error from `load_config`) and `3` (runner
`NotImplementedError` from a non-healthy scenario). `main()` catches
`PublisherConfigError` in two places: the `Fleet.from_config` block
(URL parse failure at construction) and the `asyncio.run` block
(cert-related failure from inside a per-pump task, re-raised by
`Fleet.run`). Both return code 4. CI can now distinguish "your YAML
is malformed" from "your YAML is valid but the cert is missing on
disk."

**Why not a fleet-level pre-flight (Gemini's Q5 compromise):** Gemini
suggested adding a one-line `Path(tls.ca_path).parent.exists()` check
in `Fleet.from_config` to short-circuit before spawning 15 tasks that
would each log the same error. We considered and skipped this — with
`PublisherConfigError` halting the fleet from inside the first
per-pump task that hits a missing cert, the same UX is achieved with
less code. There's a theoretical sub-second window where multiple
pumps could all hit the bad config before the runner halts (they
enter their publishers concurrently in `asyncio.gather`), but in
practice the halt is fast and the per-pump tasks see shutdown before
they get far. Belt-and-braces would add code for a vanishing case.
Documented in this addendum + `_run_pump`'s docstring for any future
session that wants to revisit.

**Test coverage:** 14 new tests (`test_publisher_config_error.py`: 11,
`test_runner_config_error.py`: 3) plus 5 new in
`test_main.py` for the exit-code wiring (one skipped on Python < 3.12
because `asyncio.run(loop_factory=...)` is 3.12+). Total suite is now
179 (160 pre-Q3 + 19 new). 178 pass on the 3.10 sandbox, 1 skipped.
All 179 should pass on the project's target 3.12+.

**Status:** Accepted. ADR 0003 (the parent) stays Accepted; this
addendum refines §Decision 4 rather than superseding it.
