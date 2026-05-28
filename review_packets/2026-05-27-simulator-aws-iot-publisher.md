# Review Packet 2026-05-27 — simulator — aws-iot-publisher

(Updated 2026-05-28 — extended with the disconnect-bound + Ctrl+C
escalation follow-up. Both pieces of work are in scope for this single
review since the 2026-05-28 fix was discovered while running the
2026-05-27 smoke test.)

> Paste this entire file into Gemini via:
> `.\scripts\gemini_review.ps1 -Slug simulator-aws-iot-publisher`

## Role for Gemini

You are an adversarial-but-fair code reviewer for a portfolio project.
Your job is not to rubber-stamp. Surface risks, design weaknesses, and
trade-offs that the author may have rationalized past. Cite specific
files and lines when possible.

## Project north stars (constraint anchors)

1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`. ADR
0003 (this session's parent decision) is the most directly relevant
prior document.

## Summary of the change

**Two related pieces of work, reviewed together:**

**(A) 2026-05-27 — AwsIotPublisher wired.** `AwsIotPublisher` went from
stub to live mTLS implementation. The previous `Fleet.from_config`
reject for `target: aws-iot` was dropped; the publisher itself is now
the gate (missing certs raise `PublisherError`, the runner's
retry-forever loop catches them). Implementation lives in
`simulator/publisher.py::AwsIotPublisher.__aenter__`: file-existence
checks via `Path.is_file()`, SSL context built via
`ssl.create_default_context(SERVER_AUTH, cafile=ca_path) +
load_cert_chain(certfile, keyfile)`, then `aiomqtt.Client(hostname,
port=8883, identifier=client_id, tls_context=ctx)`. Wire shape identical
to LocalPublisher (QoS 0, retain=False, JSON payload — mode parity per
ADR 0003 / north star #4). Smoke test against the real broker passed
on 2026-05-28: P-00 telemetry observed in the IoT Core MQTT test client.

**(B) 2026-05-28 — disconnect bound + 2nd-Ctrl+C escalation.** During
the Phase D smoke, Ctrl+C did not stop `python -m simulator`. Diagnosis:
the signal handler fired and per-pump tasks exited their inner publish
loops, but the publisher's `__aexit__` blocked indefinitely on the TLS
close_notify handshake (or paho's keepalive flush). Two-part fix:
(1) `simulator/publisher.py` wraps both publishers' inner
`aiomqtt.Client.__aexit__` in `asyncio.wait_for(timeout=
DISCONNECT_TIMEOUT_SECONDS)` (default 3.0s, monkeypatchable);
(2) `simulator/__main__.py` extracted a `_ShutdownState` class —
first Ctrl+C requests graceful shutdown, second Ctrl+C calls
`os._exit(130)` (POSIX SIGINT convention). `force_exit` is a constructor
parameter so tests substitute a spy.

**Test coverage:** 21 new tests across the two pieces, total 139 → 160:

- 13 new in `simulator/tests/test_publisher.py` for the aws-iot path
  (existence checks, happy path with monkeypatched ssl + aiomqtt,
  SSL/OS error wrapping, MqttError wrapping, QoS-0 publish, idempotent
  exit, port defaults).
- 3 new in `simulator/tests/test_publisher_shutdown.py` for the
  disconnect-timeout bound (HangingAiomqttClient for both publishers +
  constant-pin).
- 5 new in `simulator/tests/test_main.py` for the `_ShutdownState`
  state machine (first call requests, second forces, code 130, fleet
  request_shutdown called exactly once across many signals).

ADR 0003 updated in place with an Addendum 2026-05-27 "AwsIotPublisher
wired" (the original "stub" status preserved in Decision 5 alongside
the update). The 2026-05-28 disconnect fix is documented in the
session log Update section + publisher / __main__ module docstrings —
deliberately NOT amended into ADR 0003 because it's an implementation
refinement, not a decision change.

## Diff

Files changed across both pieces of work:

**(A) 2026-05-27 files:**

- `simulator/publisher.py` — `AwsIotPublisher` body replaced; URL parsing
  now used by both publishers. ~90 lines added (LocalPublisher
  untouched).
- `simulator/runner.py` — `BrokerTarget.AWS_IOT` reject block removed
  (~8 lines), docstrings updated, `BrokerTarget` import dropped.
- `simulator/tests/test_publisher.py` — 13 new tests, 3 stub-status
  tests removed, FakeAiomqttClient extended to record `tls_context`.
  Net +~370 lines (21 → 34 tests).
- `simulator/tests/test_runner.py` — `test_from_config_rejects_aws_iot_target`
  replaced in place with `test_from_config_uses_aws_iot_publisher_for_aws_iot_target`.
  Net ~0 lines.
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — Decision 5 reworded;
  Negative consequence struck through; new Addendum 2026-05-27
  "AwsIotPublisher wired" added; References list extended.
- `context/simulator.md` — AWS IoT box ticked, Publisher ABC bullet
  rewritten, open questions section marks mTLS implementation resolved.
- `simulator/config.example.yaml` — commented-out `tls:` example
  refreshed (points to `simulator/.secrets/<pump_id>/` and the new
  session log).

**(B) 2026-05-28 files:**

- `simulator/publisher.py` — added `import asyncio`, `import logging`,
  module-level `log` + `DISCONNECT_TIMEOUT_SECONDS = 3.0`; wrapped
  both `__aexit__`s in `wait_for`. ~30 lines net.
- `simulator/__main__.py` — added `import os`, module-level `log` +
  `FORCE_EXIT_CODE = 130`; extracted `_ShutdownState` class
  (~30 lines); `_install_shutdown_handlers` now instantiates and
  returns it; module docstring updated with the escalation
  explanation. ~60 lines net.
- `simulator/tests/test_publisher_shutdown.py` — NEW (126 lines, 3
  tests using a `HangingAiomqttClient` that blocks forever in aexit).
- `simulator/tests/test_main.py` — NEW (102 lines, 5 tests on the
  `_ShutdownState` state machine with monkeypatched `os._exit`).
- `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` — Update
  2026-05-28 section appended (~110 lines with diagnosis, fix
  rationale, trade-offs, commit command).

Full diff: `git diff main..HEAD -- simulator/ docs/ context/
review_packets/` after Adar commits both batches.

The interesting hunks to read inline:

**(A) `simulator/publisher.py::AwsIotPublisher.__aenter__`:**

```python
async def __aenter__(self) -> "AwsIotPublisher":
    # Existence checks first — clearer error than "FileNotFoundError
    # inside ssl.SSLContext.load_*" with no field name attached.
    for field, path_str in (
        ("cert_path", self._tls.cert_path),
        ("key_path", self._tls.key_path),
        ("ca_path", self._tls.ca_path),
    ):
        if not Path(path_str).is_file():
            raise PublisherError(
                f"AWS IoT mTLS {field} not found: {path_str!r} ..."
            )

    try:
        tls_context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=self._tls.ca_path,
        )
        tls_context.load_cert_chain(
            certfile=self._tls.cert_path,
            keyfile=self._tls.key_path,
        )
    except (ssl.SSLError, OSError) as e:
        raise PublisherError(...) from e

    self._client = aiomqtt.Client(
        hostname=self._host,
        port=self._port,
        identifier=self._client_id,
        tls_context=tls_context,
    )
    try:
        await self._client.__aenter__()
    except aiomqtt.MqttError as e:
        self._client = None
        raise PublisherError(...) from e
    return self
```

**(B) Disconnect-bound `__aexit__` (same shape in both publishers):**

```python
async def __aexit__(self, exc_type, exc, tb) -> None:
    if self._client is None:
        return
    client = self._client
    self._client = None
    try:
        await asyncio.wait_for(
            client.__aexit__(exc_type, exc, tb),
            timeout=DISCONNECT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning(
            "AWS IoT disconnect for %s timed out after %.1fs; forcing",
            self._client_id, DISCONNECT_TIMEOUT_SECONDS,
        )
    except aiomqtt.MqttError:
        pass
```

**(B) `simulator/__main__.py::_ShutdownState`:**

```python
class _ShutdownState:
    def __init__(self, fleet: Fleet, force_exit=os._exit) -> None:
        self._fleet = fleet
        self._requested = False
        self._force_exit = force_exit

    def __call__(self) -> None:
        if self._requested:
            log.warning(
                "second shutdown signal received; forcing exit (%d). ...",
                FORCE_EXIT_CODE,
            )
            self._force_exit(FORCE_EXIT_CODE)
            return  # only reached if force_exit was mocked
        self._requested = True
        log.info(
            "shutdown requested; pumps will finish their current tick "
            "and disconnect. Ctrl+C again to force immediate exit."
        )
        self._fleet.request_shutdown()
```

**(A) `simulator/runner.py::Fleet.from_config` after the drop:**

```python
if config.scenario is not ScenarioKind.HEALTHY:
    raise NotImplementedError(
        f"scenario {config.scenario.value!r} is parsed but the "
        "scenario runner is not yet implemented; only "
        f"{ScenarioKind.HEALTHY.value!r} produces behavior today. ..."
    )
profiles = profiles_for(config)
# ... rest unchanged
```

(The previous `if config.broker.target is BrokerTarget.AWS_IOT: raise
NotImplementedError(...)` block — 8 lines — was removed verbatim. The
`BrokerTarget` import was dropped at the same time since it became
unused.)

## Specific questions for Gemini

Be explicit. Vague packets get vague reviews. **Questions 1-8 are about
the (A) AwsIotPublisher work; questions 9-13 are about the (B)
disconnect-bound + Ctrl+C escalation follow-up.**

### (A) AwsIotPublisher wiring questions

1. **SSLContext build choice — `ssl.create_default_context(SERVER_AUTH,
   cafile=...)` vs. `ssl.SSLContext(PROTOCOL_TLS_CLIENT)` constructed
   from scratch.** `create_default_context` enables `check_hostname` and
   `verify_mode=CERT_REQUIRED` plus a curated cipher list — exactly what
   we want for AWS IoT. Is there any production-grade footgun in this
   defaults-driven path I should be aware of (e.g., session ticket
   handling, OCSP stapling expectations)? Specifically I am NOT setting
   anything about TLS version floor — `create_default_context` sets
   `TLS 1.2` as the minimum, which matches AWS IoT's documented support.
   Is that explicit pin worth adding anyway as defensive doc?

2. **`(ssl.SSLError, OSError)` exception tuple on the SSL build path.**
   I catch both because a race condition (cert rotated between the
   `is_file()` check and `create_default_context` opening the cafile)
   would raise `OSError`, while malformed PEM / key-cert mismatch raises
   `ssl.SSLError`. Are there other exception classes I should be
   catching from `load_cert_chain` / `create_default_context` —
   specifically `ValueError` (for unsupported key types) or
   `NotImplementedError` (if a future Python deprecates a cipher we
   relied on)? Letting those bubble as `PublisherError` would keep the
   runner's retry loop alive when a deeper config bug should probably
   crash instead.

3. **Backoff behavior for "cert is malformed" vs. "broker is briefly
   unavailable".** Both surface as `PublisherError` and feed the same
   retry-forever loop. A malformed cert will loop forever at the
   30s cap with the same `PublisherError: failed to build TLS context`
   logged every 30s — which is loud enough in dev but maybe too
   self-healing for a real production-style demo. Should there be a
   distinct exception class (e.g. `PublisherConfigError` ⊂ `PublisherError`)
   that the runner treats as fatal? Or is "loud-loop-forever" actually
   the right answer because in a real fleet, a cert that's malformed at
   startup might be valid after the next rotation deploy?

4. **Test approach for cert files — `tmp_path` placeholder PEMs + monkeypatched
   `ssl.create_default_context` vs. real test fixtures.** The brief
   forbids real x.509 material in the test tree (DoD #4). I do
   `tmp_path` placeholder files so `is_file()` returns true, then
   monkeypatch `ssl.create_default_context` to return a `FakeSSLContext`
   that records `load_cert_chain` args. This doesn't actually verify
   that `load_cert_chain` would succeed on a real PEM — but it does
   verify we wire the right paths to the right kwargs. Is the
   monkeypatch-based assertion sufficient, or is there a stronger
   property-based test (e.g., generating a self-signed cert at test
   setup time with the `cryptography` lib) that would be worth the new
   dep? I think no — but flagging in case Gemini disagrees.

5. **`Fleet.from_config` no longer rejects aws-iot — is the publisher-as-gate
   really enough?** Previously: belt-and-braces double-guard (config
   rejects + publisher rejects). Now: publisher only. A
   misconfigured fleet (e.g., 15 pumps all pointed at aws-iot with
   missing certs) will now log 15 "cert_path not found" PublisherErrors
   per retry cycle. That's noisier than a single up-front fail. Worth
   it for runner-UX uniformity (local and aws-iot fail the same way),
   or should there be a single-line "any pump's publisher rejected
   construction with PublisherError → abort fleet" inside
   `Fleet.from_config`?

6. **QoS 0 in both modes — confirmed at session-brief time. Should
   future drift / lambda_scorer need QoS 1, what's the right shape of
   the change?** I imagine: a `qos: int = 0` field on `BrokerConfig`,
   validated in `[0, 1]` (AWS IoT Core doesn't support 2), defaulting
   to 0. Or an `--qos` CLI flag on `python -m simulator` for
   experimentation without YAML churn. Premature today, but worth
   thinking about the surface area.

7. **No LocalPublisher refactor — accepting ~15 lines of duplicated
   publish/exit code rather than extracting a `_AiomqttPublisher`
   base.** Decision rationale in the session log §Decisions. Is this
   the right call, or is the duplication code-smell worse than the
   blast-radius of touching LocalPublisher? My concern with the
   refactor was breaking the existing 21 LocalPublisher tests; the
   counter-concern is that future Publisher subclasses (a tls-but-
   not-aws Mosquitto-2-with-mTLS, say) will fan the duplication out.
   The 2026-05-28 disconnect-bound work made this slightly worse —
   the wait_for wrapper now exists in two places, identical except
   for one log-message string ("MQTT" vs "AWS IoT"). Is *that* the
   inflection point where the base class becomes worth it?

8. **The smoke test runbook in the session log was drafted from
   memory of the AWS Console UI — the brief flagged that the IoT Core
   UI has changed twice in the last year.** I'd appreciate Gemini
   sanity-checking the Phase A/B steps against the current Console
   shape if it has knowledge of the post-2025 UI; specifically the
   "Auto-generate certificate" wording and the "Root CA certificates"
   download panel location. If Gemini doesn't know, that's fine — flag
   it as a watch item for the PO to verify when running the smoke.
   **Update 2026-05-28:** Phase A/B/C/D all executed cleanly modulo
   the `.cert.pem` vs `.cert.pem.crt` filename quirk noted in the
   session log; the runbook was largely accurate.

### (B) Disconnect-bound + Ctrl+C escalation questions

9. **`DISCONNECT_TIMEOUT_SECONDS = 3.0` — too tight or too loose?** Pulled
   from the gut: Mosquitto disconnects in sub-millisecond on a local
   socket; AWS IoT over public internet is typically <500 ms but can
   stretch to a couple of seconds on a flaky connection. 3 s gives
   ~6× the typical p99 with margin. Live data over the next few sessions
   will calibrate this. Is 3 s defensible as a default? Should it be
   configurable via YAML (a `broker.disconnect_timeout_seconds: 3.0`
   field) so a flaky-network user can dial it up without code change,
   or is "monkeypatch in tests, leave the constant alone in prod"
   sufficient until someone actually hits the ceiling legitimately?

10. **Cancellation cascade when `wait_for` fires.** When the timeout
    hits, asyncio cancels the inner `client.__aexit__` task by injecting
    `CancelledError`. If the TLS shutdown is mid-write, the socket may
    be left half-closed from the broker's perspective. AWS IoT cleans
    this up on keepalive timeout (default 60 s); Mosquitto closes
    immediately. **Is the leaked-half-open-socket window for 60 s on
    AWS IoT acceptable?** It costs nothing in our usage but might
    surface as "stale connections" in CloudWatch IoT metrics for a
    minute after every Ctrl+C. Should we follow the wait_for with an
    explicit `asyncio.shield`-protected synchronous close on the
    underlying socket — and if so, how do we reach it through aiomqtt's
    abstraction without import-hopping into paho?

11. **`os._exit(130)` vs. `sys.exit(130)` vs. `raise KeyboardInterrupt`
    vs. `signal.default_int_handler()`.** I chose `os._exit` because
    (a) the whole point is "Python isn't cooperating, get out NOW —
    skip atexit hooks, skip stdio flush, skip __del__"; (b) `sys.exit`
    raises `SystemExit` which can be caught and swallowed by a stray
    `except Exception:` somewhere; (c) re-raising `KeyboardInterrupt`
    from a signal handler running on the loop thread is undefined
    behavior in asyncio land. **Are there scenarios where `os._exit`
    is the *wrong* answer here** — e.g., a future telemetry-batching
    consumer that buffers messages in a Python-level queue would lose
    the buffered messages on force-exit, which a `sys.exit`+
    `atexit.register(flush)` pattern would handle. Today the simulator
    has zero such state, so `os._exit` is fine; but this is the kind
    of thing that bites later.

12. **`_ShutdownState` as a stateful class with `__call__` instead of a
    closure.** Plain closure over a `nonlocal` flag would be ~10 lines
    shorter. I chose the class because: (a) it's easier to unit-test
    (the test injects a fake `force_exit` via constructor; testing a
    closure means inspecting `__closure__` cells); (b) it has a clear
    public surface (`.requested` property) for any future code that
    wants to peek. **Is the class justified, or is this over-OO?** If
    over-OO, what's the right closure shape that's still testable?

13. **Mode parity for the disconnect bound.** Both `LocalPublisher` and
    `AwsIotPublisher` got the same `wait_for` wrapper, even though only
    the aws-iot path actually triggered the bug in the wild. Justified
    by ADR 0003 north star #6 (mode parity) — the publishers are
    expected to behave symmetrically. But it adds three lines of
    "defensive against a bug we've never seen" to LocalPublisher. **Is
    the parity argument the right one here, or should LocalPublisher
    stay simple and only AwsIotPublisher carry the wrapper?** My take:
    parity wins because (a) a future Mosquitto-with-mTLS user would
    hit the same TLS-teardown stall, and (b) the cost is three lines
    + one constant lookup. Want Gemini's challenge if any.

## What I'm NOT looking for in this review

- Style / formatting — handled by the project's existing conventions.
- Test count exhaustiveness — I'd rather you flag a missing scenario
  than count my tests.
- LocalPublisher behavior — unchanged in (A); (B) adds the same
  disconnect wrapper in both, which is in scope via Q13.
- Scenario runner — separate future session.
- Terraform-for-IoT — explicit non-goal for the 2026-05-27 brief.
- Root-causing *why* the AWS-IoT disconnect stalls (paho keepalive flush
  vs TLS close_notify) — we have a workaround that bounds the symptom;
  root-causing is its own session if it ever matters.

## Resolution (filled in by Claude after Gemini responds)

| # | Disposition | Notes |
|---|---|---|
| 1 | **Confirmed** | Keep `create_default_context`. Don't pin TLS 1.2 (anti-pattern). ALPN-on-:443 noted for future fallback. No code change. |
| 2 | **Addressed (code)** | Added `ValueError` to AwsIotPublisher SSL exception tuple — catches encrypted PKCS#8 keys without password. New test: `test_aws_iot_value_error_raises_config_error`. |
| 3 | **Addressed (code)** | New `PublisherConfigError ⊂ PublisherError`. Promoted missing-file, SSL/OS/ValueError, bad-URL raises. Runner halts fleet on the subclass (vs retry-forever on parent). `main()` returns new exit code 4 (`PUBLISHER_CONFIG_ERROR_CODE`). ADR 0003 §Addendum 2026-05-28 "Static config errors halt the fleet" carves out from Decision 4. +14 tests across `test_publisher_config_error.py` and `test_runner_config_error.py`. |
| 4 | **Confirmed** | Keep monkeypatched `tmp_path` approach. Adding `cryptography` to generate real PEMs would be testing the stdlib. No code change. |
| 5 | **Declined with reasoning** | Skipped Gemini's pre-flight check as redundant with Q3's halt-on-first-failure. Documented in `Fleet.from_config` docstring + ADR 0003 §Addendum. Trade: tiny multi-error log window vs ~5 fewer lines. |
| 6 | **Confirmed (defer)** | YAGNI. Proposed shape (`qos` on `BrokerConfig`, validated `[0,1]`) right when needed. No code change. |
| 7 | **Confirmed (keep duplication)** | Rule of Three. ~18 duplicated lines now (incl. the disconnect-bound wrapper). Wait for a 3rd publisher to extract `_AiomqttPublisher`. No code change. |
| 8 | **Addressed (docs)** | Added `curl.exe -o ... AmazonRootCA1.pem` fallback to Phase A of the runbook, and captured the `.pem.crt` vs `.pem` filename quirk inline. |
| 9 | **Confirmed** | Keep 3.0s `DISCONNECT_TIMEOUT_SECONDS`. No YAML knob — teardown is an operator concern. Monkeypatchable for tests. No code change. |
| 10 | **Confirmed (accept risk)** | AWS IoT keepalive sweeps zombies in 60s; hacking paho would be tight coupling. Future-watch for `lambda_scorer` consumers. No code change. |
| 11 | **Confirmed** | Keep `os._exit(130)`. Gemini's reasoning matched ours (`sys.exit` swallowable, re-raised `KeyboardInterrupt` is asyncio undefined behavior). No code change. |
| 12 | **Confirmed** | Keep `_ShutdownState` class. Pragmatic for testability via injected `force_exit`. Not over-OO. No code change. |
| 13 | **Confirmed** | Keep parity. Prevents future "only hangs on AWS" bug class for a Mosquitto-with-mTLS deployment. No code change. |
