# Session 2026-05-25 — simulator — mqtt-publishing

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`) — review pending
- **Context loaded:** `_global`, `simulator` (Tier 2 only — `_interfaces` not loaded; the MQTT topic + payload contract there was already locked and didn't need touching)
- **Duration:** ~1.5h

## Intent

Wire the MQTT publishing layer the previous simulator sessions deferred. `Pump.step()` already returns the telemetry dict; this session takes those dicts and publishes them to a per-pump MQTT topic on a 2-second cadence (PLAN.md §2.2) under a single asyncio event loop. The AWS-IoT side of the same code path is stubbed (the AWS account isn't provisioned yet — `ml-obs-pipeline-context`).

## What changed

**New files:**

- `simulator/publisher.py` — `Publisher` ABC (`__aenter__` / `__aexit__` / `publish`), `LocalPublisher` (aiomqtt-backed, QoS 0, retain=False, JSON-encoded payload), `AwsIotPublisher` stub (accepts `TlsConfig`, raises `NotImplementedError` from `__aenter__`), `PublisherError` (transport-error translation so the runner doesn't import aiomqtt), `make_publisher` factory, `topic_for` helper.
- `simulator/runner.py` — `Fleet` class. `from_config()` rejects non-healthy scenarios and `target: aws-iot` up front with `NotImplementedError`. `run()` spawns one asyncio task per pump; each task: enters its `Publisher`, ticks every `tick_seconds`, publishes, sleeps. On `PublisherError`: wait with exponential backoff (1s → 30s ceiling, reset on successful reconnect) and retry forever. Shutdown via `asyncio.Event`; clean teardown on `CancelledError` for callers that can't use `add_signal_handler`.
- `simulator/__main__.py` — `python -m simulator [--config PATH] [--log-level INFO]`. argparse + `asyncio.run`. Wires SIGINT/SIGTERM via `loop.add_signal_handler` on Unix; falls through to `KeyboardInterrupt` → `CancelledError` on Windows ProactorEventLoop.
- `simulator/tests/test_publisher.py` — 21 tests. Topic format, URL parsing (with/without scheme, default port), LocalPublisher with monkeypatched `aiomqtt.Client` (instance args, JSON-payload + QoS-0 + retain-false), MqttError-wrapping on connect and publish, idempotent disconnect, disconnect-time MqttError swallowed, AwsIotPublisher stub (config stored, `__aenter__` raises with ADR-0003 message, publish raises), `make_publisher` dispatch (all four combinations).
- `simulator/tests/test_runner.py` — 24 tests. `pump_id_for` bounds, `Fleet.__init__` validation, `from_config` building pumps with seeded ids + `LocalPublisher` per pump + demo_mode profiles applied, rejection of non-healthy scenarios and aws-iot target, end-to-end `run()` with `FakePublisher` (right topics, right telemetry keys, per-pump independence), `run()` shutdown-before-publish edge case, `run()` retry loop with `FlakyPublisher` + backoff constants monkeypatched to ms, per-pump failure isolation, backoff-reset-on-successful-connect.
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — single ADR bundling all five interlocking choices (library, concurrency, connection topology, partial-failure policy, TLS validation depth). Five mini-decisions in one doc because they're tightly coupled; splitting them would have produced three ADRs that all reference each other.
- `review_packets/2026-05-25-simulator-mqtt-publishing.md` — 8 specific questions for Gemini.

**Modified:**

- `simulator/config.py` — added `TlsConfig` frozen dataclass; `BrokerConfig.tls: Optional[TlsConfig] = None`; conditional validation in `_validate_broker` (required iff `target == aws-iot`, forbidden iff `target == local`); removed the `UserWarning` for non-healthy scenarios (moved to `Fleet.from_config`); removed the `warnings` import. Loader is now pure schema validation.
- `simulator/config.example.yaml` — `tls:` block documented with the aws-iot path (commented out under the default `local` target). Updated comments on the conditional rule and the new entry-point command.
- `simulator/__init__.py` — re-exports `TlsConfig`, `Publisher`, `LocalPublisher`, `AwsIotPublisher`, `PublisherError`, `make_publisher`, `topic_for`, `Fleet`, `pump_id_for`, the three backoff/tick constants.
- `simulator/tests/test_config.py` — added `AWS_IOT_YAML` snippet for tls-block tests; replaced `test_non_healthy_scenarios_parse_with_warning` with `test_non_healthy_scenarios_parse_without_warning` (asserts NO warning emitted — guards against re-introducing one in the loader); added `test_aws_iot_load_emits_no_warning`; added 9 tls-block tests (required-with-aws-iot, forbidden-with-local, must-be-mapping, missing each of cert/key/ca, unknown subkey, empty-string parametrized over all 3 fields, wrong-type); added `test_tls_config_is_frozen`; added `test_broker_missing_target`, `test_broker_missing_url`, `test_broker_unknown_key` (previously not exercised because `_assert_exact_keys` covered them — now broker uses its own validator). `test_aws_iot_broker_target_parses` was replaced by `test_aws_iot_with_tls_block_parses` (the old test would now fail because tls is required for aws-iot).
- `requirements.txt` — added `aiomqtt>=2.0` and `paho-mqtt>=2.0`. Two lines, both justified inline with pointers to ADR 0003 and the session that added them (per DEV_NORMS §6.2).
- `context/simulator.md` — MQTT box ticked, interfaces section expanded (Publisher ABC, per-pump topology, retry-forever policy), concurrency open-question resolved with a pointer to ADR 0003.

PR: TBD — Adar opens after commits 1-3.

## Decisions

**ADR 0003 — Asyncio + Aiomqtt, Per-Pump Connection, Retry-Forever, Schema-Only TLS Validation.** Five tightly-coupled choices bundled into one ADR:

1. **Library: aiomqtt** (>=2.0) on paho-mqtt (>=2.0). aiomqtt is the asyncio-native wrapper by the same maintainer; we use `async with Client(...)` / `await client.publish(...)` directly.
2. **Concurrency: single asyncio loop, one task per pump.** 15 pumps × 0.5 Hz is well under asyncio's comfort zone.
3. **Connection topology: one MQTT connection per pump, both modes.** PO picked this (Q2) for mode parity — AWS IoT's one-Thing-per-pump = one-client_id model lifts cleanly into local.
4. **Partial-failure policy: retry-forever, per-pump.** PO picked this (Q4). Exponential backoff 1s → 30s, reset on each successful reconnect.
5. **TLS validation: shape-only in loader, file checks deferred to `AwsIotPublisher.__aenter__`.** PO picked this (Q3). Loader stays pure schema validation.

**Scenario warning moved out of `load_config`.** The 2026-05-25 config-yaml session added a `UserWarning` per Gemini Q1; this session moves it to `Fleet.from_config` as a `NotImplementedError`. Loader is now pure schema validation. The original Gemini concern (silent-accept is a UX footgun) is still addressed — failure happens one stack-frame later, with a clearer error class. Flagged for Gemini in this session's packet (Q6) in case the original intent was "signal at config-load specifically."

**`Fleet.from_config` rejects aws-iot up front.** Belt-and-braces with `AwsIotPublisher.__aenter__` also raising. The runner-side reject means a user with `target: aws-iot` sees one clear error at construction; the publisher-side raise stays as a backstop for direct callers (tests, future library users). Flagged for Gemini (Q5).

## Trade-offs surfaced

- **aiomqtt is one more dep beyond paho-mqtt.** Paid for in exchange for ~50 lines of asyncio-bridge code we'd have written ourselves. Same author, same paho stack underneath — the wrapper is purely about idiom (async/await vs. callbacks), not protocol.
- **15 TCP connections to Mosquitto.** Trivial at fleet size 15. The schema cap is 100; at that scale we'd want to revisit, but PLAN.md targets 15.
- **`AwsIotPublisher` is dead code in `Fleet.from_config`'s flow** (rejected up front). Kept as a contract anchor and direct-caller backstop; real coverage waits for the AWS-IoT session.
- **No real-broker tests in pytest.** Unit tests monkeypatch `aiomqtt.Client`. The wire format is what `aiomqtt` itself owns; we exercise it through `mosquitto_sub -t 'factory/pumps/+/telemetry'` manually. Pulling Docker into pytest would compound the FUSE/cache issues the sandbox already has.
- **Backoff reset semantics.** "Successful connect = reset to 1s" rather than "N successful publishes = reset." The simpler rule is easier to test and reason about; the cost is that a pump in a connect-publish-drop loop stays in the fast retry tier. Flagged for Gemini (Q3).
- **`tick_seconds` not exposed in YAML.** Single source of truth in `runner.py` as a module constant. If a future session needs fast-replay demos, add it then. Defensible until proven otherwise.

## Gemini review highlights

Review pending. The 8 questions in the packet are deliberately specific:

1. aiomqtt error-class coverage (am I catching everything?).
2. Per-pump connection topology cost at 15 pumps under asyncio.
3. Backoff reset semantics (connect-counts-as-success).
4. Shape-only TLS validation defensibility.
5. Double-rejection (runner + publisher) for aws-iot.
6. `NotImplementedError` at fleet-construction vs. `UserWarning` at config-load (revisiting the previous session's Q1).
7. Better way to test the backoff reset behavior than connect-attempt count.
8. Windows signal-handling path correctness.

Resolution table to be filled after `gemini_review.ps1` runs.

## State at end of session

- **Tests:** 138 passing (30 pump + 63 config + 21 publisher + 24 runner), 0.36s in sandbox (`cp -r simulator /tmp/run/simulator && cd /tmp/run && python3 -m pytest simulator/tests/`).
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
  - Gemini review of this session's packet (8 questions).
  - AWS account provisioning (still ⬜ in `ml-obs-pipeline-context`) — unblocks the AWS-IoT session that wires `AwsIotPublisher.__aenter__`.
  - Scenario runner (seasonal_drift, fleet_expansion, real_failure) — `Fleet.from_config` raises today; replace with a real `Scenario` interface in its own session.
  - Onboarding UX is still deferred (PO 2026-05-25): no auto-default config, no `cp config.example.yaml config.yaml` README step yet. Reconfirmed this session — same call as the config-yaml session.
- **`context/simulator.md`:** updated (MQTT box ticked, interfaces section expanded, concurrency open-question resolved, this session log linked).
- **No FUSE bug recurrence** beyond what `[[ml-obs-pipeline-git-on-windows]]` already documents. Every existing-file edit went through bash heredoc as per the memory; verified with `wc -l` after each.

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
(1s -> 30s, reset on successful reconnect). python -m simulator
entry point with SIGINT/SIGTERM handling.

Config schema gains a TlsConfig dataclass and a broker.tls sub-block
required iff broker.target is 'aws-iot' and forbidden iff 'local'.
Shape-only validation (non-empty strings); file-existence checks live
in AwsIotPublisher when wired.

The UserWarning load_config used to emit for non-healthy scenarios
(config-yaml session, Gemini Q1) moves to Fleet.from_config as a
NotImplementedError so the loader is pure schema validation.

Tests: 138 passing (30 pump + 63 config + 21 publisher + 24 runner).
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
retry-forever per-pump backoff, shape-only TLS schema validation.

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

Then run Gemini:

```powershell
.\scripts\gemini_review.ps1 -Slug simulator-mqtt-publishing
```

Commit 4 (post-review) will follow the same shape as the config-yaml session: address each Gemini point, fill the Resolution table, commit with a PowerShell here-string for the multi-line message.

## Note for next session

Two natural next-simulator-session candidates, in priority order:

1. **AWS-IoT publisher** — implement `AwsIotPublisher.__aenter__` / `publish`. Blocked on AWS account provisioning (still ⬜ in `ml-obs-pipeline-context`). When it lands: drop the `Fleet.from_config` reject for `target: aws-iot`; let the publisher itself be the gate. The shape-only TLS schema validation done this session is the foundation — the publisher does the file-existence check, parses the cert chain, attaches the per-Thing policy.
2. **Scenario runner** — implement seasonal_drift / fleet_expansion / real_failure. `Fleet.from_config` raises `NotImplementedError` for these today. Implementation: a `Scenario` interface that mutates the per-pump state machines on a schedule. The publisher layer doesn't change.

Watch items either way:

- **aiomqtt API stability.** aiomqtt v2 is what we pinned. The migration from v1 was significant (sync `Client` → async context manager); a future v3 could move again. Pin tight if a fresh `pip install` ever brings in something newer than 2.x.
- **Onboarding UX is still on the deferred list.** No README setup step, no auto-default config, no entry-point shorthand. The `cp config.example.yaml config.yaml` step is documented in `__main__.py` and in the example file's header but not in README. Deliberate — its own session.
- **Subscribers don't exist yet.** The published telemetry currently has no consumer. `lambda_scorer` / `local_runtime` sessions will subscribe. Until then the manual `mosquitto_sub` is the only consumer.
