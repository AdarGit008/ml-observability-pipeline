# Review Packet 2026-05-25 — simulator — mqtt-publishing

> Run via: `.\scripts\gemini_review.ps1 -Slug simulator-mqtt-publishing`
> (writes `review_responses/2026-05-25-simulator-mqtt-publishing.md`).

## Role for Gemini

You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)

1. **$0 lifetime AWS cost.** No always-running AWS resources.
2. **Single-PC development.** No spare hardware assumed.
3. **AWS-specific differentiation.** Choices with clean GCP/Azure analogues are weaker portfolio signals.
4. **Mode parity** between local and AWS demo paths — same scoring/drift logic, same Publisher contract.
5. **One polished repo, not five half-finished ones.**

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`. Locked architecture decisions: `docs/adr/`.

## Summary of the change

This session wires the MQTT publishing layer that previous simulator sessions deferred. `Pump.step()` already returns a telemetry dict; the new code publishes that dict to a per-pump MQTT topic on a 2-second cadence under a single asyncio event loop. The brief picked four design points up front (aiomqtt over hand-rolled paho bridging; per-pump MQTT connection in both local and AWS modes; shape-only TLS schema validation; retry-forever on per-pump connect failure); all four are now implemented and documented in **ADR 0003** as one bundled decision.

The aws-iot publisher exists as a stub (`AwsIotPublisher.__aenter__` raises `NotImplementedError`) because the AWS account isn't provisioned yet. `Fleet.from_config` rejects `target: aws-iot` up front so a misconfigured fleet fails clearly before any pump tries to connect; the stub stays as a contract anchor and a backstop for direct callers.

Bonus cleanup: the `UserWarning` that `load_config` used to emit for non-healthy scenarios (added during the 2026-05-25 config-yaml session per your Q1) has been moved to `Fleet.from_config` as a `NotImplementedError`. The loader is now pure schema validation, which I think is cleaner — but if you disagree I'm interested in the argument.

## Diff

Changed files (full diffs available in the commits in the session log):

```
simulator/config.py            +TlsConfig dataclass, conditional tls validation, removed UserWarning
simulator/config.example.yaml  +annotated tls block (commented out under local target)
simulator/publisher.py         +Publisher ABC, LocalPublisher (aiomqtt), AwsIotPublisher stub, make_publisher
simulator/runner.py            +Fleet + per-pump asyncio task, retry-forever, shutdown event
simulator/__main__.py          +argparse entry point, signal handlers
simulator/__init__.py          +re-exports for new symbols
simulator/tests/test_config.py +16 tls/warning tests (62 → 78 cases — wait, 47 → 63)
simulator/tests/test_publisher.py +21 cases (new file)
simulator/tests/test_runner.py +24 cases (new file)
requirements.txt               +aiomqtt>=2.0, paho-mqtt>=2.0 (explicit transitive pin)
docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md  +new ADR bundling all five interlocking choices
context/simulator.md           +MQTT box ticked, interfaces section expanded, concurrency open-question resolved
```

Test count: **30 pump + 63 config + 21 publisher + 24 runner = 138 passing** (was 77).

## Specific questions for Gemini

Be explicit. Vague packets get vague reviews. Lines below cite `simulator/...` paths.

1. **Asyncio bridge correctness.** `simulator/publisher.py::LocalPublisher` uses aiomqtt's `async with Client(...)` directly and translates `aiomqtt.MqttError` to `PublisherError`. Is there a failure mode in aiomqtt 2.x that I'm not catching here — e.g., disconnects mid-`publish()` that surface as something other than `MqttError`? In particular, is silencing the disconnect-time `MqttError` in `__aexit__` (lines ~120-130) the right call, or am I masking a class of bug the runner should know about?

2. **Per-pump connection topology at scale.** ADR 0003 §4 picks per-pump-in-both-modes for mode parity. At 15 pumps × one TCP connection × one aiomqtt-managed background task each, are there asyncio fairness or socket-state pitfalls you'd flag? My calibration: 15 connections to Mosquitto is well within its capacity, and AWS IoT Core's per-Thing model is the actual reason. But I'd like a sanity check that I'm not missing a hidden cost (e.g., paho's background-thread-per-client adds up?).

3. **Retry-forever backoff cap.** `INITIAL_BACKOFF_SECONDS = 1.0`, `MAX_BACKOFF_SECONDS = 30.0`, doubled on each failure, reset on each successful connect. Reset-on-connect is the bit I'd most expect to argue about: a pump that connects, publishes once, then immediately drops — does that count as "successful" for backoff purposes? My current implementation says yes (the next failure starts at 1s again). The alternative is "successful = published N readings without failure," which adds state.

4. **Shape-only TLS validation in the loader.** ADR 0003 §5 — `load_config` checks `cert_path`/`key_path`/`ca_path` are non-empty strings but doesn't touch disk. The file-existence and cert-parse checks live in `AwsIotPublisher` (when it's wired). Is this the right separation, or would you push for file-existence in the loader despite the test-fixture cost (every aws-iot config test would need three fake cert files on disk)?

5. **`Fleet.from_config` rejecting aws-iot up front.** Currently both `Fleet.from_config` AND `AwsIotPublisher.__aenter__` reject the aws-iot path with `NotImplementedError`. Belt-and-braces, but is the double-rejection confusing? An alternative is "let the publisher be the only gate" — but then a 15-pump fleet logs 15 identical retry-loop warnings before the user understands "AWS isn't wired."

6. **Scenario stub placement.** The 2026-05-25 config-yaml session put a `UserWarning` in `load_config` for non-healthy scenarios per your previous review (Q1). This session moves it to `Fleet.from_config` as `NotImplementedError`, on the grounds that the loader should be pure schema validation. Your previous argument was that silent-accept was a UX footgun. `NotImplementedError` at fleet-construction time is still a loud failure (just one stack-frame later) — does that satisfy the original concern, or did you intend the signal to come *at config-load time* specifically?

7. **Test coverage of the retry loop.** `test_runner.py::test_fleet_run_retries_on_publisher_error` and `test_fleet_run_resets_backoff_on_successful_connect` exercise the connect-with-backoff path with a `FlakyPublisher`. The "backoff reset" test is asserting connect-attempt count, not the actual backoff timing, because the timing is hard to assert reliably under asyncio scheduler jitter. Is there a more direct way to verify the reset behavior that I'm missing?

8. **`__main__.py` Windows signal handling.** On Windows, `loop.add_signal_handler` raises `NotImplementedError` on the ProactorEventLoop. I catch that and fall through to letting `KeyboardInterrupt` bubble out of `asyncio.run`, which cancels the tasks, which propagates `CancelledError` into `Fleet.run`'s `except` block, which sets shutdown and drains. Have I correctly traced the cancellation path, or is there a Windows-specific scenario (e.g., SIGBREAK) where this misbehaves?

## What I'm NOT looking for in this review

- **Style / formatting.** Not yet linted (ruff/black land in a dev_workflow session).
- **Real-broker integration tests.** Deliberately deferred — manual mosquitto smoke step is documented in the session log. Pulling Docker into pytest is more cost than benefit at this stage.
- **YAML schema additions** beyond the tls block. The brief was explicit about staying minimal.
- **AwsIotPublisher implementation depth.** It's a stub; the real implementation is its own session.
- **Type checker** runs (no mypy in CI yet).

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. <summarize> | Addressed / Deferred / Rejected | <where, why> |
| 2. ... | ... | ... |
| 3. ... | ... | ... |
| 4. ... | ... | ... |
| 5. ... | ... | ... |
| 6. ... | ... | ... |
| 7. ... | ... | ... |
| 8. ... | ... | ... |
