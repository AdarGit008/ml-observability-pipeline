# Session 2026-05-25 — simulator — mqtt-publishing

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`)
- **Context loaded:** `_global`, `simulator` (Tier 2 only — `_interfaces` not loaded; the MQTT topic + payload contract there was already locked and didn't need touching)
- **Duration:** ~2h

## Intent

Wire the MQTT publishing layer the previous simulator sessions deferred. `Pump.step()` already returns the telemetry dict; this session takes those dicts and publishes them to a per-pump MQTT topic on a 2-second cadence (PLAN.md §2.2) under a single asyncio event loop. The AWS-IoT side of the same code path is stubbed (the AWS account isn't provisioned yet — `ml-obs-pipeline-context`).

## What changed

**New files:**

- `simulator/publisher.py` — `Publisher` ABC (`__aenter__` / `__aexit__` / `publish`), `LocalPublisher` (aiomqtt-backed, QoS 0, retain=False, JSON-encoded payload), `AwsIotPublisher` stub (accepts `TlsConfig`, raises `NotImplementedError` from `__aenter__`), `PublisherError` (transport-error translation so the runner doesn't import aiomqtt), `make_publisher` factory, `topic_for` helper.
- `simulator/runner.py` — `Fleet` class. `from_config()` rejects non-healthy scenarios and `target: aws-iot` up front with `NotImplementedError`. `run()` spawns one asyncio task per pump; each task: enters its `Publisher`, ticks every `tick_seconds`, publishes, sleeps. On `PublisherError`: wait with exponential backoff (1s → 30s ceiling, **reset on each successful publish** — see Gemini Q3 below) and retry forever. Shutdown via `asyncio.Event`; clean teardown on `CancelledError` for callers that can't use `add_signal_handler`.
- `simulator/__main__.py` — `python -m simulator [--config PATH] [--log-level INFO]`. argparse + `asyncio.run`. Two-tier signal handling: `loop.add_signal_handler` on Unix; `signal.signal` + `loop.call_soon_threadsafe` fallback on Windows ProactorEventLoop (per Gemini Q8 — avoids the `KeyboardInterrupt`/`CancelledError` teardown trap).
- `simulator/tests/test_publisher.py` — 21 tests. Topic format, URL parsing, LocalPublisher with monkeypatched `aiomqtt.Client`, MqttError-wrapping, idempotent disconnect, disconnect-time MqttError swallowed, AwsIotPublisher stub, `make_publisher` dispatch.
- `simulator/tests/test_runner.py` — 25 tests. `pump_id_for` bounds, `Fleet.__init__` validation, `from_config` builds pumps+publishers correctly, rejection of non-healthy scenarios and aws-iot target, end-to-end `run()` with `FakePublisher`, shutdown-before-publish edge case, retry loop with `FlakyPublisher`, per-pump failure isolation, exact backoff sequence climb-to-cap (`[1, 2, 4, 8, 16, 30, 30]` — Gemini Q7), backoff reset on successful publish (Gemini Q3).
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — single ADR bundling all five interlocking choices. Promoted from Proposed to Accepted after Gemini review.
- `review_packets/2026-05-25-simulator-mqtt-publishing.md` — 8 specific questions for Gemini, Resolution table filled.

**Modified:**

- `simulator/config.py` — added `TlsConfig` frozen dataclass; `BrokerConfig.tls: Optional[TlsConfig] = None`; conditional validation in `_validate_broker` (required iff `target == aws-iot`, forbidden iff `target == local`); removed the `UserWarning` for non-healthy scenarios (moved to `Fleet.from_config`); removed the `warnings` import. Loader is now pure schema validation.
- `simulator/config.example.yaml` — `tls:` block documented with the aws-iot path (commented out under the default `local` target).
- `simulator/__init__.py` — re-exports `TlsConfig`, `Publisher`, `LocalPublisher`, `AwsIotPublisher`, `PublisherError`, `make_publisher`, `topic_for`, `Fleet`, `pump_id_for`, the three backoff/tick constants.
- `simulator/tests/test_config.py` — added `AWS_IOT_YAML` snippet; replaced `test_non_healthy_scenarios_parse_with_warning` with `test_non_healthy_scenarios_parse_without_warning`; added `test_aws_iot_load_emits_no_warning`; added 9 tls-block tests; added `test_tls_config_is_frozen`; added 3 broker-validator tests; replaced `test_aws_iot_broker_target_parses` with `test_aws_iot_with_tls_block_parses` (the old test would now fail since tls is required for aws-iot).
- `requirements.txt` — added `aiomqtt>=2.0` and `paho-mqtt>=2.0` with inline justification.
- `context/simulator.md` — MQTT box ticked, interfaces section expanded (Publisher ABC, per-pump topology, retry-forever policy), concurrency open-question resolved with a pointer to ADR 0003.

PR: TBD — Adar opens after commit 4.

## Decisions

**ADR 0003 — Asyncio + Aiomqtt, Per-Pump Connection, Retry-Forever, Schema-Only TLS Validation.** Five tightly-coupled choices bundled into one ADR:

1. **Library: aiomqtt** (>=2.0) on paho-mqtt (>=2.0). aiomqtt is the asyncio-native wrapper by the same maintainer.
2. **Concurrency: single asyncio loop, one task per pump.**
3. **Connection topology: one MQTT connection per pump, both modes.** PO Q2.
4. **Partial-failure policy: retry-forever, per-pump.** PO Q4. **Backoff resets on each successful PUBLISH, not on each successful CONNECT** — Gemini Q3 identified the flapping vulnerability with reset-on-connect.
5. **TLS validation: shape-only in loader, file checks deferred to `AwsIotPublisher.__aenter__`.** PO Q3.

**Scenario warning moved out of `load_config`.** From `UserWarning` (config-yaml session per Gemini Q1) to `NotImplementedError` in `Fleet.from_config`. Loader is now pure schema validation. Gemini confirmed in this session's Q6 — the specific stack frame (load vs. init) doesn't matter as long as it's a synchronous hard failure at startup.

**`Fleet.from_config` rejects aws-iot up front + `AwsIotPublisher.__aenter__` also raises.** Belt-and-braces, confirmed by Gemini Q5 as defense in depth.

## Trade-offs surfaced

- **aiomqtt is one more dep beyond paho-mqtt.** Paid for in exchange for ~50 lines of asyncio-bridge code we'd have written ourselves. Same author, same paho stack underneath.
- **15 TCP connections to Mosquitto.** Trivial at fleet size 15. Schema cap is 100; at that scale we'd want to revisit.
- **`AwsIotPublisher` is dead code in `Fleet.from_config`'s flow.** Kept as a contract anchor and direct-caller backstop; real coverage waits for the AWS-IoT session.
- **No real-broker tests in pytest.** Unit tests monkeypatch `aiomqtt.Client`. Mosquitto-in-Docker smoke is manual; Docker-in-pytest would compound the FUSE/cache issues.
- **Backoff reset on PUBLISH, not CONNECT.** Closes the AWS-IoT publish-denied flapping hole (Gemini Q3). Cost: a connect-only success doesn't restart the fast-retry tier — a publisher that connects but never publishes will see backoff climb to the 30s cap as it should.
- **`tick_seconds` not exposed in YAML.** Single source of truth in `runner.py`.

## Gemini review highlights

Full disposition table in `review_packets/2026-05-25-simulator-mqtt-publishing.md`. Eight questions, three of which drove code changes:

- **Q3 (reset-on-connect flapping vulnerability) — ADDRESSED (code change).** A publisher that connects successfully but is denied PUBLISH (e.g., an AWS IoT policy that allows the former but not the latter) would loop forever at the 1s initial backoff, never engaging the 30s cap. Fix: `simulator/runner.py::_run_pump` now resets `backoff` immediately after each `await publisher.publish(...)` succeeds, not after each `async with publisher:` enter. Module docstring + ADR 0003 §Decision + §Alternatives 4 updated.
- **Q7 (better testing pattern for backoff math) — ADDRESSED (test added).** Connect-attempt counts verify the loop runs; they don't verify the math. Added `test_fleet_backoff_climbs_to_cap_then_holds` which monkeypatches `Fleet._wait_or_shutdown` (our wait abstraction; Gemini suggested `asyncio.sleep` but we don't go through sleep — we go through `asyncio.wait_for(shutdown.wait(), ...)` so the shutdown event can interrupt). Asserts the exact sequence `[1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]`. Also added `test_fleet_backoff_resets_on_successful_publish` (proves the Q3 fix). Renamed the old `test_fleet_run_resets_backoff_on_successful_connect` to `test_fleet_run_continues_after_failed_connects` (more honest — it never actually verified the reset).
- **Q8 (Windows signal-handling teardown trap) — ADDRESSED (code change).** Relying on `KeyboardInterrupt` → `CancelledError` propagation on Windows can leave the MQTT DISCONNECT packet unsent ("Task was destroyed but it is pending!"). Fix: `simulator/__main__.py::_install_shutdown_handlers` now tries `loop.add_signal_handler` first (Unix), falls back to `signal.signal` + `loop.call_soon_threadsafe(fleet.request_shutdown)` on Windows. The C signal handler hops into the loop without going through the chaotic cancellation path. `KeyboardInterrupt` remains a paranoia backstop in `main()`.

- **Q1, Q2, Q4, Q5, Q6 — Confirmed (no code change).** Asyncio-bridge / `__aexit__`-silencing correctness (Q1), aiomqtt-uses-non-blocking-paho explanation captured in the ADR (Q2), shape-only TLS validation defensibility (Q4), defense-in-depth double-rejection for aws-iot (Q5), `NotImplementedError` at fleet-construction satisfies the original silent-failure concern (Q6).

ADR 0003 promoted from Proposed → Accepted after the three changes landed. Tests now **139 passing** (was 138 pre-review; +2 new, -1 renamed/replaced).

## State at end of session

- **Tests:** 139 passing (30 pump + 63 config + 21 publisher + 25 runner), 0.35s in sandbox (`cp -r simulator /tmp/run/simulator && cd /tmp/run && python3 -m pytest simulator/tests/`).
- **Python:** sandbox runs 3.10.12; project target is 3.12. New code uses `from __future__ import annotations`; nothing 3.12-only. Adar to re-run Windows-side on 3.12 before merge.
- **Manual smoke (Mosquitto):** documented but not yet run. From the project root:

  ```bash
  # Terminal 1 — broker
  docker run --rm -p 1883:1883 eclipse-mosquitto

  # Terminal 2 — subscriber (any topic under the fleet)
  mosquitto_sub -t 'factory/pumps/+/telemetry'

  # Terminal 3 — fleet (after `cp simulator/config.example.yaml simulator/config.yaml`)
  python -m simulator --config simulator/config.yaml --log-level INFO
  ```

  Expected: 15 lines of JSON every 2 seconds, one per pump, topics `factory/pumps/P-00/telemetry` … `factory/pumps/P-14/telemetry`.
- **Open follow-ups:**
  - AWS account provisioning (still ⬜ in `ml-obs-pipeline-context`) — unblocks the AWS-IoT session that wires `AwsIotPublisher.__aenter__`.
  - Scenario runner (seasonal_drift, fleet_expansion, real_failure) — `Fleet.from_config` raises today; replace with a real `Scenario` interface in its own session.
  - Onboarding UX is still deferred (PO 2026-05-25): no auto-default config, no `cp config.example.yaml config.yaml` README step yet. Reconfirmed this session.
- **`context/simulator.md`:** updated (MQTT box ticked, interfaces section expanded, concurrency open-question resolved, this session log linked).
- **No FUSE bug recurrence** beyond what `[[ml-obs-pipeline-git-on-windows]]` already documents. Every existing-file edit went through bash heredoc; verified with `wc -l` after each. One Edit attempt during the Gemini-review fix loop did silently truncate (test_runner.py: 453 → 450 lines), surfaced as a `SyntaxError: '(' was never closed` mid-import, fixed by rewriting via bash heredoc — exactly the pattern documented in memory.

## Commits to run (PO-side, per `[[ml-obs-pipeline-git-on-windows]]`)

```powershell
cd "D:\Claude\ML Observability Pipeline"
```

**Commit 1 — publisher + runner + config schema delta + tests + requirements:**

```powershell
git add simulator/publisher.py simulator/runner.py simulator/__main__.py simulator/config.py simulator/config.example.yaml simulator/__init__.py simulator/tests/test_publisher.py simulator/tests/test_runner.py simulator/tests/test_config.py requirements.txt
git commit -m "simulator: add MQTT publishing layer (aiomqtt, asyncio, per-pump)

Wires the MQTT publishing the prior simulator sessions deferred:
Publisher ABC + LocalPublisher (aiomqtt-backed, QoS 0, retain=False,
JSON payload) + AwsIotPublisher stub + Fleet runner with one asyncio
task per pump on a 2-second cadence. Retry-forever per-pump backoff
(1s -> 30s, reset on successful publish per ADR 0003 / Gemini Q3).
python -m simulator entry point with two-tier signal handling for
clean shutdown on Unix and Windows.

Config schema gains a TlsConfig dataclass and a broker.tls sub-block
required iff broker.target is 'aws-iot' and forbidden iff 'local'.
Shape-only validation (non-empty strings); file-existence checks live
in AwsIotPublisher when wired.

The UserWarning load_config used to emit for non-healthy scenarios
(config-yaml session, Gemini Q1) moves to Fleet.from_config as a
NotImplementedError so the loader is pure schema validation.

Tests: 139 passing (30 pump + 63 config + 21 publisher + 25 runner).
aiomqtt>=2.0 and paho-mqtt>=2.0 added to requirements.txt."
```

**Commit 2 — ADR 0003 + context updates:**

```powershell
git add docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md context/simulator.md
git commit -m "docs: ADR 0003 (MQTT publishing design) + simulator context update

Bundles five interlocking choices into one ADR rather than three that
all reference each other: aiomqtt over hand-rolled paho bridging,
single asyncio loop + one task per pump, one MQTT connection per
pump in BOTH local and AWS modes (mode parity per north star #6),
retry-forever per-pump backoff (reset on successful publish per
Gemini Q3), shape-only TLS schema validation. Status: Accepted.

context/simulator.md ticks the MQTT checkbox, expands the interfaces
section with the Publisher ABC + per-pump topology, resolves the
concurrency open question against ADR 0003, and links this session
log."
```

**Commit 3 — review packet + session log (pre-review snapshot):**

```powershell
git add review_packets/2026-05-25-simulator-mqtt-publishing.md docs/sessions/2026-05-25-simulator-mqtt-publishing.md
git commit -m "review: simulator mqtt-publishing packet + session log"
```

Then run Gemini (already done — `review_responses/2026-05-25-simulator-mqtt-publishing.md` is on disk):

```powershell
.\scripts\gemini_review.ps1 -Slug simulator-mqtt-publishing
```

**Commit 4 — Gemini-review changes + Resolution + response file:**

The Q3 and Q8 code changes and the Q7 test additions are already in commits 1 and the ADR update in commit 2 (the implementation arrived after Gemini's response). Commit 4 captures the response file, the filled Resolution table in the packet, and the updated session log:

```powershell
git add review_packets/2026-05-25-simulator-mqtt-publishing.md review_responses/2026-05-25-simulator-mqtt-publishing.md docs/sessions/2026-05-25-simulator-mqtt-publishing.md

# Use a PowerShell here-string for the multi-line message so embedded
# double quotes don't terminate the -m argument early. The closing "@
# must sit at column 0 with no leading whitespace.
$msg = @"
simulator: address Gemini review (reset backoff on publish, Win signal handler, exact-sequence test)

Three accepted changes from review_packets/2026-05-25-simulator-mqtt-publishing.md:

- Q3: backoff now resets on each successful PUBLISH (not on each
  successful CONNECT). Closes the flapping vulnerability where a
  publisher with CONNECT but not PUBLISH permission would loop at
  the 1s initial backoff forever and never engage the 30s cap.
  Already in commit 1 (simulator/runner.py); ADR 0003 §Decision
  and §Alternatives 4 already reflect this in commit 2.

- Q7: added test_fleet_backoff_climbs_to_cap_then_holds asserting
  the exact backoff sequence [1, 2, 4, 8, 16, 30, 30] by mocking
  Fleet._wait_or_shutdown. Also test_fleet_backoff_resets_on_
  successful_publish for the Q3 fix. Renamed old reset test
  (which only verified the loop ran, not the math). Already in
  commit 1.

- Q8: __main__.py's _install_shutdown_handlers now falls back to
  signal.signal + loop.call_soon_threadsafe(fleet.request_shutdown)
  when add_signal_handler is unavailable (Windows ProactorEventLoop).
  Avoids the KeyboardInterrupt -> CancelledError -> aggressive loop
  teardown chain that could leave MQTT DISCONNECT packets unsent.
  Already in commit 1.

Q1, Q2, Q4, Q5, Q6 — confirmations, no code change. Full disposition
table in the review packet.

ADR 0003 status: Proposed -> Accepted.

Tests: 139 passing (was 138 pre-review; +2 new, -1 renamed/replaced).
"@
git commit -m $msg
git push
```

## Note for next session

Two natural next-simulator-session candidates, in priority order:

1. **AWS-IoT publisher** — implement `AwsIotPublisher.__aenter__` / `publish`. Blocked on AWS account provisioning. When it lands: drop the `Fleet.from_config` reject for `target: aws-iot`; let the publisher itself be the gate. The shape-only TLS schema validation done this session is the foundation — the publisher does the file-existence check, parses the cert chain, attaches the per-Thing policy.
2. **Scenario runner** — implement seasonal_drift / fleet_expansion / real_failure. `Fleet.from_config` raises `NotImplementedError` for these today. Implementation: a `Scenario` interface that mutates the per-pump state machines on a schedule. The publisher layer doesn't change.

Watch items either way:

- **aiomqtt API stability.** aiomqtt v2 is what we pinned. A future v3 could move again; pin tight if a fresh `pip install` ever brings in something newer than 2.x.
- **Onboarding UX is still on the deferred list.** Its own session.
- **Subscribers don't exist yet.** The published telemetry currently has no consumer. `lambda_scorer` / `local_runtime` sessions will subscribe. Until then the manual `mosquitto_sub` is the only consumer.
