# Session 2026-05-27 — simulator — aws-iot-publisher

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`)
- **Context loaded:** `_global`, `simulator` (Tier 2 only)
- **Reference loaded:** ADR 0003 (full doc + Addendum 2026-05-27 on Windows loop-factory), `simulator/publisher.py::AwsIotPublisher` stub, `docs/sessions/2026-05-25-simulator-mqtt-publishing.md`
- **Duration:** ~2h

## Intent

Flesh out `AwsIotPublisher.__aenter__` / `publish` / `__aexit__` so the
mTLS path against AWS IoT Core works end-to-end. Drop the
`target=aws-iot` reject in `Fleet.from_config` (the publisher itself
becomes the gate). Prove the round trip with a single pump P-00
publishing to IoT Core, observed via the Console MQTT test client.

## What changed

**Modified:**

- `simulator/publisher.py` — `AwsIotPublisher` went from stub
  (`NotImplementedError` from `__aenter__`) to live mTLS implementation:
  file-existence checks (`Path.is_file()` per cert/key/ca path) →
  `ssl.create_default_context(purpose=SERVER_AUTH, cafile=ca_path)` +
  `ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)` →
  `aiomqtt.Client(hostname, port=8883, identifier=client_id,
  tls_context=ctx)`. `_parse_mqtt_url` now used for both publishers
  (default port 8883 for aws-iot). All transport, SSL, and OS errors
  surface as `PublisherError` so the runner's retry-forever loop catches
  them. Wire shape identical to `LocalPublisher` (QoS 0, retain=False,
  JSON payload — mode parity, per ADR 0003 / north star #6).
- `simulator/runner.py` — dropped the `BrokerTarget.AWS_IOT` reject in
  `Fleet.from_config` and the unused `BrokerTarget` import. Module
  docstring + `from_config` docstring updated to reflect the
  single-guard (non-healthy scenario only).
- `simulator/tests/test_publisher.py` — 13 new tests for the aws-iot
  path (existence checks for each of cert/key/ca, happy path with
  `tmp_path` placeholder files + monkeypatched `ssl.create_default_context`
  + `aiomqtt.Client`, SSL/OS error wrapping, MqttError wrapping on connect,
  QoS-0 publish, publish-before-aenter, idempotent exit, swallow on
  disconnect, default port 8883, explicit-port override). Existing
  `FakeAiomqttClient` extended to record an optional `tls_context`
  kwarg.
- `simulator/tests/test_runner.py` — `test_from_config_rejects_aws_iot_target`
  replaced in place with `test_from_config_uses_aws_iot_publisher_for_aws_iot_target`
  (asserts the right subclass + TlsConfig threads through, no certs
  opened at construction). Import widened to include `AwsIotPublisher`.
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — Decision 5 rewritten
  to mark AwsIotPublisher implemented. Negative consequence "dead code
  in main flow" struck through with a 2026-05-27 resolution note. New
  Addendum 2026-05-27 §"AwsIotPublisher wired" capturing what runs in
  `__aenter__`, why the from_config reject was removed, the test
  coverage delta, and the smoke test plan. References section gained
  the new session log + review packet links.
- `context/simulator.md` — AWS IoT box ticked. Publisher ABC bullet
  rewritten (LocalPublisher port + AwsIotPublisher wire shape +
  port 8883 + Amazon Root CA validation). Open questions section
  marks the mTLS implementation resolved.
- `simulator/config.example.yaml` — the commented-out `tls:` example
  now points at the `simulator/.secrets/<pump_id>/` layout (matches the
  brief's Phase A) and references the new session log for the Console
  walkthrough.

**New:**

- `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` (this file).
- `review_packets/2026-05-27-simulator-aws-iot-publisher.md`.

**Out of scope (deferred):**

- Terraform-managed IoT Things/policies/certs — Console-only for now,
  per the session brief's explicit non-goal. The Phase A/B Console
  walkthrough below is what the PO runs; turning that into IaC is the
  natural next infra session.
- Switching to fleet-wide cert. ADR 0003's per-pump decision stands.
- Subscriber that actually consumes the IoT-Core-side telemetry —
  that's the lambda_scorer / local_runtime session.

PR: TBD — Adar opens after the commits below.

## Decisions

**No new ADR.** This session is the implementation of ADR 0003 Decision 5
+ executes on the follow-up explicitly named in the 2026-05-25
mqtt-publishing session log. ADR 0003 was updated in place with an
Addendum (immutable history preserved; the previous "stub" wording was
not deleted — Decision 5 now has both the original and the 2026-05-27
status note).

**Single gate inside the publisher.** Previously `Fleet.from_config`
double-guarded by rejecting `target: aws-iot` before any pump tried to
connect (belt-and-braces while the publisher was a stub).
`AwsIotPublisher.__aenter__` now raises `PublisherError` (not
`NotImplementedError`) on missing certs / bad SSL / connect refusal,
which feeds the runner's retry-forever loop the same way local transport
errors do. Removing the double-guard makes the runner UX uniform across
targets. Direct callers that bypass `Fleet.from_config` still get the
same gate inside `__aenter__`.

**QoS 0 in both modes.** Confirmed at session-brief time. AWS IoT Core
supports QoS 0 and 1 (not 2); we chose 0 for mode parity (north star #6).
The cost meter is per-message regardless of QoS, so this is a viable
future toggle if downstream consumers ever need at-least-once.

**File-existence check runs before SSL build.** `Path(p).is_file()` is
~100× cheaper than `ssl.create_default_context(cafile=...)`, and a
clean `PublisherError: AWS IoT mTLS cert_path not found: '/...'` is much
more actionable than `ssl.SSLError: [SSL] PEM lib (_ssl.c:4123)` for the
same root cause. The test
`test_aws_iot_aenter_missing_cert_does_not_touch_ssl_or_aiomqtt` pins
this ordering.

**No LocalPublisher refactor.** A small inheritance hierarchy (extract
`_AiomqttPublisher` with shared `publish` / `__aexit__`) was considered.
Rejected: the session brief explicitly says "the implementation gap is
exactly one class"; refactoring the LocalPublisher would have blown the
blast radius up unnecessarily and risked breaking the existing 21
LocalPublisher tests with no functional payoff. ~15 lines of duplicated
publish/exit code is the cost we pay; surfacing it explicitly for Gemini
in the review packet.

## Trade-offs surfaced

- **Duplicated publish/exit code between LocalPublisher and AwsIotPublisher.**
  ~15 lines. Mitigation if it grows: extract `_AiomqttPublisher` base.
  Flagged for Gemini.
- **SSLContext built fresh per connect (not cached).** Each retry rebuilds
  the context from disk. At the retry-forever cadence (1s→30s backoff),
  this is invisible; if reconnect rate ever climbs, caching is trivial.
  Trade against keeping retried cert paths consistent with disk state
  (e.g., a cert rotation should be picked up automatically without a
  process restart).
- **No ALPN.** AWS IoT Core mTLS on port 8883 doesn't need ALPN;
  `x-amzn-mqtt-ca` is the alternative for the :443 fallback path that
  exists for networks blocking 8883. We default to 8883 and trust the
  YAML to override (e.g., `mqtts://endpoint:443`) if a future deploy
  ever needs it.
- **No real-broker tests in pytest.** Unchanged from ADR 0003 — unit
  tests monkeypatch `ssl.create_default_context` + `aiomqtt.Client`.
  The smoke test below is the manual integration check.
- **Cert files live under `simulator/.secrets/<pump_id>/`.** Convention
  from `_interfaces.md §2.4`. Matches the brief's Phase A and is
  gitignored via `simulator/.secrets/` + the catch-all `*.pem` / `*.key`
  / `*.crt` rules already in `.gitignore`.

## Smoke test runbook (PO-side, 2026-05-27)

**Phase A — Console-provision the Thing "P-00" (~10 min).**

1. AWS Console → IoT Core (region: eu-central-1) → **Manage → Things →
   Create things → Create a single thing**.
2. Thing name: `P-00`. No Thing type, no Thing group, no shadow.
3. Device certificate: **Auto-generate a new certificate (recommended)**.
4. Skip the policy attachment on this screen — we'll create + attach it
   in Phase B. Click **Create thing**.
5. On the **Download certificates and keys** screen, download:
   - The device certificate (rename to `P-00.cert.pem` — strip the
     trailing `.crt` if the Console added it; observed 2026-05-28).
   - The public key (not needed, but download it for completeness).
   - The private key (rename to `P-00.private.key`).
   - **Amazon Root CA 1** — under "Root CA certificates", download
     RSA 2048 bit key, rename to `AmazonRootCA1.pem`.
   - **Fallback if the Console links to docs instead of serving the CA
     directly** (UI churn flagged by Gemini Q8 of the 2026-05-27
     review): `curl` it directly into the secrets folder:
     ```powershell
     curl.exe -o simulator/.secrets/P-00/AmazonRootCA1.pem `
       https://www.amazontrust.com/repository/AmazonRootCA1.pem
     ```
6. Save all three under `simulator/.secrets/P-00/`:
   ```
   simulator/.secrets/P-00/P-00.cert.pem
   simulator/.secrets/P-00/P-00.private.key
   simulator/.secrets/P-00/AmazonRootCA1.pem
   ```
7. Click **Activate** (the cert state changes from INACTIVE to ACTIVE)
   then **Done**.

**Phase B — Per-Thing policy (~5 min).**

1. IoT Core → **Manage → Security → Policies → Create policy**.
2. Policy name: `pump-P-00-policy`. Policy effect: Allow. JSON:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "iot:Connect",
         "Resource": "arn:aws:iot:eu-central-1:<account-id>:client/P-00"
       },
       {
         "Effect": "Allow",
         "Action": "iot:Publish",
         "Resource": "arn:aws:iot:eu-central-1:<account-id>:topic/factory/pumps/P-00/telemetry"
       }
     ]
   }
   ```
   (Replace `<account-id>` with the pdm-portfolio account id: `485215543435`.)
3. Click **Create**.
4. IoT Core → **Manage → Security → Certificates** → find the cert from
   Phase A (its ID will match the prefix of the downloaded files).
   Actions → **Attach policy** → select `pump-P-00-policy` → Attach.

**Phase C — Eliminate "cert vs. code" ambiguity (~2 min, optional but
recommended).**

1. Grab the ATS endpoint: IoT Core → **Settings** → "Endpoint" panel →
   copy the `Device data endpoint` (format:
   `<account-id>-ats.iot.eu-central-1.amazonaws.com`).
2. Verify the cert + policy work without the Python publisher in the
   loop, using `mosquitto_pub` on Windows (install via `winget install
   EclipseFoundation.Mosquitto` if needed):
   ```powershell
   mosquitto_pub `
     --cafile  simulator/.secrets/P-00/AmazonRootCA1.pem `
     --cert    simulator/.secrets/P-00/P-00.cert.pem `
     --key     simulator/.secrets/P-00/P-00.private.key `
     --host    <account-id>-ats.iot.eu-central-1.amazonaws.com `
     --port    8883 `
     --id      P-00 `
     --topic   "factory/pumps/P-00/telemetry" `
     --message '{"hello":"P-00","ts":"smoke-test"}' `
     --debug
   ```
   In a second window, IoT Core → **MQTT test client** → Subscribe to
   `factory/pumps/P-00/telemetry`. The `{"hello":"P-00","ts":"smoke-test"}`
   payload should appear.

**Phase D — End-to-end with the Python publisher.**

1. Create `simulator/config.aws-iot.yaml` (gitignored — it's a
   per-developer config copy):
   ```yaml
   fleet:
     pump_count: 1                  # single-pump smoke per the brief
     setpoint_rpm: 1800.0
     ambient_celsius: 22.0
     base_seed: 0
   scenario: healthy
   broker:
     target: aws-iot
     url: "<account-id>-ats.iot.eu-central-1.amazonaws.com"
     tls:
       cert_path: "simulator/.secrets/P-00/P-00.cert.pem"
       key_path:  "simulator/.secrets/P-00/P-00.private.key"
       ca_path:   "simulator/.secrets/P-00/AmazonRootCA1.pem"
   demo_mode: false
   ```
2. Run from the repo root:
   ```powershell
   python -m simulator --config simulator/config.aws-iot.yaml --log-level INFO
   ```
3. In the IoT Core MQTT test client, with the
   `factory/pumps/P-00/telemetry` subscription still active, you should
   see a JSON message arrive every ~2 seconds with the expected pump
   telemetry keys (`pump_id, ts, vibration_amp, bearing_temp,
   motor_current, rpm`).
4. Ctrl+C to stop. The simulator should log "pump P-00 connected" then
   exit cleanly within one tick.

**Phase E — Cost sanity check.**

- Single pump × 0.5 Hz × 5 min smoke = ~150 messages. AWS IoT Core in
  eu-central-1 is ~$1/million → essentially free even ignoring credits.
- Check Billing → Cost Explorer the next morning (per the brief's watch
  item). If it shows IoT Core spend > $0.01, something is wrong.

## Gemini review highlights

- Packet drafted at `review_packets/2026-05-27-simulator-aws-iot-publisher.md`.
- Run with: `.\scripts\gemini_review.ps1 -Slug simulator-aws-iot-publisher`
- Resolution table will be filled in this same session log after the
  response lands (matches the 2026-05-25 mqtt-publishing pattern).

## State at end of session

- **Tests:** 152 passing (30 pump + 63 config + 34 publisher + 25 runner)
  in 0.41s on the sandbox via `cp -r simulator /tmp/run/simulator && cd
  /tmp/run && python3 -m pytest simulator/tests/ -p no:cacheprovider`
  (the FUSE-mount pytest workaround documented in
  `[[ml-obs-pipeline-git-on-windows]]`). Was 139 pre-session; +13 new
  AwsIotPublisher tests, 1 runner test swapped in place (rejection →
  positive assertion).
- **Python:** sandbox runs 3.10.12; project target is 3.12. New code
  uses `from __future__ import annotations` and `ssl` stdlib only —
  nothing 3.12-only. Adar to re-run Windows-side on 3.12 before merge.
- **Manual smoke (AWS IoT):** runbook above. Pending PO execution.
- **Open follow-ups:**
  - Terraform-managed IoT Things/policies/certs (deferred — separate
    infra session; the brief says explicitly).
  - The other pumps' Things (P-01..P-14) — only P-00 is provisioned
    this session. Future demos will need either 14 more Things or a
    decision to switch to a fleet-wide cert (which ADR 0003 explicitly
    rejected; revisit only if there's a real reason).
  - Scenario runner (`seasonal_drift`, `fleet_expansion`,
    `real_failure`) — `Fleet.from_config` still raises
    `NotImplementedError` for non-healthy scenarios. Separate session.
- **`context/simulator.md`:** updated (AWS IoT box ticked, AwsIotPublisher
  description fleshed out, open questions section marks mTLS resolved,
  ADR 0003 addendum referenced).
- **`.gitignore`:** already covers `simulator/.secrets/` and
  `*.pem`/`*.key`/`*.crt`. Verified during the session — no change
  needed.
- **FUSE bugs encountered:** the now-familiar pattern. Three
  growth-related truncations during this session (publisher.py grew
  ~80 lines, ADR 0003 grew ~20 lines, test_runner.py grew during the
  Edit re-import, context/simulator.md grew during the in-place
  Edits). All fixed by rewriting via bash heredoc, matching
  `[[ml-obs-pipeline-git-on-windows]]`. Verified each rewrite with
  `tr -cd '\000' < file | wc -c` (must be 0) + `python3 -c "import ast;
  ast.parse(...)" `.

## Commits to run (PO-side, per `[[ml-obs-pipeline-git-on-windows]]`)

```powershell
cd "D:\Claude\ML Observability Pipeline"
```

**Commit 1 — AwsIotPublisher implementation + tests + runner drop:**

```powershell
git add simulator/publisher.py simulator/runner.py simulator/tests/test_publisher.py simulator/tests/test_runner.py simulator/config.example.yaml
git commit -m "simulator: implement AwsIotPublisher (mTLS via aiomqtt)

AwsIotPublisher.__aenter__ now performs file-existence checks on
cert/key/ca paths, builds an ssl.SSLContext (Amazon Root CA validates
the server cert, per-Thing cert+key authenticate the client) via
create_default_context + load_cert_chain, and connects to the AWS IoT
ATS endpoint on port 8883 via aiomqtt.Client(tls_context=...). All
transport, SSL, and OS errors surface as PublisherError so the
runner's retry-forever loop catches them the same way as local
transport errors. Wire shape identical to LocalPublisher (QoS 0,
retain=False, JSON payload — mode parity per ADR 0003).

Fleet.from_config no longer rejects target=aws-iot; the publisher
itself is the gate (missing certs raise PublisherError, retry-forever
applies).

Tests: 152 passing (was 139; +13 publisher tests, 1 runner test
replaced in place). Cert files in tests are tmp_path placeholders;
ssl.create_default_context is monkeypatched — no real x.509 in the
test tree per the session brief's DoD #4.

See docs/sessions/2026-05-27-simulator-aws-iot-publisher.md and
ADR 0003 §Addendum 2026-05-27 'AwsIotPublisher wired'."
```

**Commit 2 — ADR 0003 update + context update:**

```powershell
git add docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md context/simulator.md
git commit -m "docs: ADR 0003 addendum + simulator context for AwsIotPublisher

Decision 5 of ADR 0003 reworded to mark AwsIotPublisher implemented
(the prior 'stub' status is preserved historically alongside the
2026-05-27 update — no immutable-ADR violation; the addendum is
additive). New Addendum 2026-05-27 'AwsIotPublisher wired' captures
what runs in __aenter__, why the Fleet.from_config double-guard was
removed, the test coverage delta, and the smoke test plan.

context/simulator.md ticks the AWS IoT box, expands the Publisher
ABC description with port + wire shape + Amazon Root CA mention,
and marks the mTLS implementation question resolved."
```

**Commit 3 — review packet + session log:**

```powershell
git add review_packets/2026-05-27-simulator-aws-iot-publisher.md docs/sessions/2026-05-27-simulator-aws-iot-publisher.md
git commit -m "review: simulator aws-iot-publisher packet + session log"
```

Then run Gemini:

```powershell
.\scripts\gemini_review.ps1 -Slug simulator-aws-iot-publisher
```

**Commit 4 (post-Gemini) — Resolution table + any addressed feedback.**

## Note for next session

The AWS IoT path is now live for P-00. Two natural next-simulator-session
candidates, in priority order:

1. **Scenario runner** — implement `seasonal_drift` /
   `fleet_expansion` / `real_failure`. `Fleet.from_config` raises
   `NotImplementedError` for these today. A `Scenario` interface that
   mutates per-pump state machines on a schedule. Publisher layer
   doesn't change.

2. **First downstream consumer** — `lambda_scorer` or `local_runtime`
   subscribes to `factory/pumps/+/telemetry` and starts feeding the
   scoring pipeline. The AWS-IoT smoke proved a publisher; the next
   piece is something on the other end.

Watch items either way:

- **`simulator/.secrets/P-00/` exists only on the PO's machine.** A
  fresh clone will not run the aws-iot path. The local-target default
  in `config.example.yaml` is unchanged so onboarding stays trivial.
- **AWS Console UI churn.** The Console step-by-step in Phase A/B above
  was accurate as of 2026-05-27. If the UI shifted, drop into
  Claude-in-Chrome for an interactive walkthrough rather than guessing.
- **Per-Thing policy attachment is ARN-specific.** When P-01..P-14
  eventually need provisioning, each needs its own policy with its
  own client_id + topic ARNs. The Terraform follow-up will template
  this; until then, the per-pump policy creation is hand-work.
- **Cost.** Single-pump smoke is essentially free; a 15-pump
  24-hour-on demo is ~$0.65/day at IoT Core's $1/million rate. Always
  stop the simulator between sessions.

## Update 2026-05-28 — Ctrl+C shutdown bug + fix

**Symptom (during the 2026-05-27 Phase D smoke):** After confirming
telemetry was arriving in the IoT Core MQTT test client, Ctrl+C in the
terminal running `python -m simulator --config simulator/config.aws-iot.yaml`
did not stop the process. No "Task was destroyed" warning, no
KeyboardInterrupt traceback — the publisher just kept publishing.
Closing the terminal window terminated the process (Windows reaps
children on console session breakage). `Get-Process python` post-close
came back clean — no orphan running up the IoT Core meter.

**Diagnosis:** Two-tier signal handling (per ADR 0003 Gemini Q8) fires
correctly on Windows ProactorEventLoop via the
`signal.signal` + `loop.call_soon_threadsafe` bridge.
`fleet.request_shutdown()` sets the shutdown event. Per-pump tasks exit
their inner publish loops. But the publisher's `__aexit__` — the
disconnect path — was not bounded. The aws-iot variant adds TLS
close_notify handshake to the disconnect; paho-mqtt's keepalive flush
can also block. The Mosquitto smoke test on 2026-05-27 hadn't surfaced
this because Mosquitto's disconnect is sub-millisecond on a local
socket; AWS IoT over the public internet at 8883 has measurable
round-trip and any number of ways for the close_notify to stall.

The 2026-05-27 session-log "Trade-offs surfaced" bullet "SSLContext
built fresh per connect (not cached)" hinted at this; the actual lock-in
came from the missing exit-path bound, not the entry path.

**Fix:**

1. **`simulator/publisher.py`** — both `LocalPublisher.__aexit__` and
   `AwsIotPublisher.__aexit__` now wrap the inner
   `aiomqtt.Client.__aexit__` in `asyncio.wait_for(..., timeout=
   DISCONNECT_TIMEOUT_SECONDS)`. Default 3.0 s, module-level constant,
   monkeypatchable for tests. On `asyncio.TimeoutError`, the publisher
   logs a `WARNING` naming the client_id + timeout value and returns
   (the underlying socket is reaped by the OS when the process exits;
   AWS IoT clears the half-closed connection on keepalive timeout
   anyway).

2. **`simulator/__main__.py`** — extracted a `_ShutdownState` class with
   first-vs-second-call escalation. First Ctrl+C: log a heads-up + call
   `fleet.request_shutdown()` (graceful). Second Ctrl+C: log a forcing
   warning + call `os._exit(130)` (POSIX convention: SIGINT = 128+2).
   `force_exit` is a constructor parameter defaulting to `os._exit`, so
   tests can monkeypatch it without exiting pytest. `FORCE_EXIT_CODE`
   constant exposed for symmetric test assertions.

**Test coverage added (8 new tests, total 152 → 160):**

- `simulator/tests/test_publisher_shutdown.py` — 3 tests. A
  `HangingAiomqttClient` whose `__aexit__` blocks forever proves the
  `wait_for` ceiling fires for both `LocalPublisher` and
  `AwsIotPublisher`. `caplog` asserts the forced-disconnect warning is
  emitted with the right client_id. Third test pins the
  `DISCONNECT_TIMEOUT_SECONDS` module-level surface.
- `simulator/tests/test_main.py` — 5 tests on `_ShutdownState`. First
  call → fleet.request_shutdown(); second call → force_exit(130); third
  call → force_exit again (held-down Ctrl+C); FORCE_EXIT_CODE pinned at
  130; fleet's request_shutdown called exactly once across N signals.

All 160 tests pass in 0.43 s on the sandbox (`cp -r simulator
/tmp/run/simulator && cd /tmp/run && python3 -m pytest simulator/tests/
-p no:cacheprovider`).

**Trade-offs:**

- **Cancellation cascade on timeout.** When `wait_for` fires, the inner
  `aiomqtt.Client.__aexit__` task is cancelled. If the cancellation
  arrives mid-TLS-shutdown, the socket may be left in a half-closed
  state from the broker's perspective. AWS IoT cleans this up on
  keepalive timeout (default 60 s); Mosquitto closes immediately. Both
  cost nothing in our scale; flagged for the lambda_scorer session to
  be aware that "stale connections lingering for 60 s" is normal.
- **3 s default timeout is a guess.** Could be too tight on a flaky
  network (legitimate disconnect takes > 3 s) or too loose for a tight
  CI loop. Monkeypatchable, so tests dial it down to 10 ms. Live
  smoke-test data over the next few sessions will tell us if 3 s is
  the right ceiling.
- **`os._exit(130)` skips Python finalisation.** No `atexit` hooks fire,
  no flushing of stdio buffers beyond the OS-level pipe state, no
  Python `__del__` for any object. This is intentional — the whole
  point is "we tried to be polite, the broker isn't cooperating, get
  out NOW." If we ever add file-based state that needs flushing (the
  simulator doesn't today), this needs revisiting.
- **Mode parity preserved.** Both publishers get the same wrapper,
  same constant, same warning message format. The fix is symmetric per
  ADR 0003 / north star #6, even though only the aws-iot path actually
  triggered the bug.

**Files changed:**

- `simulator/publisher.py` — added `import asyncio`, `import logging`,
  module-level `log` + `DISCONNECT_TIMEOUT_SECONDS`; wrapped both
  `__aexit__`s in `wait_for`.
- `simulator/__main__.py` — added `import os`, module-level `log` +
  `FORCE_EXIT_CODE`; extracted `_ShutdownState` class; updated
  `_install_shutdown_handlers` to instantiate and return it; updated
  module docstring.
- `simulator/tests/test_publisher_shutdown.py` — NEW (126 lines, 3
  tests).
- `simulator/tests/test_main.py` — NEW (102 lines, 5 tests).
- `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` — this
  Update section.

**What's NOT in this follow-up:**

- ADR 0003 amendment. The disconnect-timeout pattern doesn't rise to
  the bar of a Decision change — it's an implementation refinement
  inside the existing "Publisher ABC owns its own connect/disconnect"
  decision. The fix lives in the session log + module docstrings + the
  new tests, where the next person debugging shutdown will look.
- Real-broker integration test. Still rejected for the same reasons as
  before (FUSE/Docker friction). The `HangingAiomqttClient` test
  exercises the wait_for code path; a future integration-smoke suite
  would catch real-world stalls.
- Investigation of *why* the AWS-IoT disconnect stalls in the first
  place. Could be paho's keepalive flush, could be TLS close_notify
  not ack'd by the broker — we have a workaround that bounds the
  symptom; root-causing is its own session if it ever matters.

**Commit to run (PO-side, appended to the 2026-05-27 batch):**

```powershell
cd "D:\Claude\ML Observability Pipeline"

git add simulator/publisher.py simulator/__main__.py simulator/tests/test_publisher_shutdown.py simulator/tests/test_main.py docs/sessions/2026-05-27-simulator-aws-iot-publisher.md

$msg = @"
simulator: bound publisher disconnect + add 2nd-Ctrl+C escalation

Two follow-up fixes from the 2026-05-27 AWS-IoT smoke test, where
Ctrl+C on Windows did not stop the simulator. The signal handler
fired and the per-pump tasks exited their inner publish loops, but
the publisher's __aexit__ blocked indefinitely waiting on the TLS
close_notify handshake (or paho's keepalive flush).

1. Bound publisher disconnect. simulator/publisher.py now wraps the
   inner aiomqtt.Client.__aexit__ in asyncio.wait_for with a 3-second
   ceiling (DISCONNECT_TIMEOUT_SECONDS, module-level constant,
   monkeypatchable). On TimeoutError, the publisher logs a forced-
   disconnect warning naming the client_id and returns. The OS reaps
   the half-closed socket on process exit; AWS IoT clears the half-
   closed connection on keepalive timeout. Symmetric across
   LocalPublisher and AwsIotPublisher per ADR 0003 mode parity.

2. Second-Ctrl+C escalates to os._exit(130). simulator/__main__.py
   extracts a _ShutdownState class (instance is invoked by both the
   asyncio-native and signal.signal+call_soon_threadsafe bridge
   paths). First call: graceful Fleet shutdown. Second call:
   force_exit(130) - POSIX SIGINT convention. Operator UX matches
   uvicorn/aiohttp.

Tests: +8 (160 total, was 152). test_publisher_shutdown.py: 3 tests
with a HangingAiomqttClient. test_main.py: 5 tests on _ShutdownState
state machine with monkeypatched os._exit.

Session log: docs/sessions/2026-05-27-simulator-aws-iot-publisher.md
section 'Update 2026-05-28'.
"@
git commit -m $msg
```
ceful Fleet shutdown. Second call:
   force_exit(130) - POSIX SIGINT convention. Operator UX matches
   uvicorn/aiohttp.

Tests: +8 (160 total, was 152). test_publisher_shutdown.py: 3 tests
with a HangingAiomqttClient. test_main.py: 5 tests on _ShutdownState
state machine with monkeypatched os._exit.

Session log: docs/sessions/2026-05-27-simulator-aws-iot-publisher.md
section 'Update 2026-05-28'.
"@
git commit -m $msg
```

## Update 2026-05-28 #2 — Gemini review resolutions

Gemini ran on the consolidated review packet
(`review_packets/2026-05-27-simulator-aws-iot-publisher.md`) covering
both the (A) AwsIotPublisher wiring and the (B) disconnect-bound +
Ctrl+C escalation. Full response in
`review_responses/2026-05-27-simulator-aws-iot-publisher.md`. Of the
13 questions, 9 came back as "you got it right" confirmations and 4
recommended changes. Three of the four were accepted as-is; one (Q5)
was declined with reasoning. Resolution table at the bottom of the
review packet was filled in alongside this update.

**Accepted changes (Q2, Q3, Q8):**

- **(Q2) Added `ValueError` to the SSL exception tuple in
  `AwsIotPublisher.__aenter__`.** Encrypted PKCS#8 keys without
  passwords (and a couple of other malformed-PEM cases) bubble up as
  `ValueError`, not `ssl.SSLError`. Without catching it, the runner
  would crash with an unhandled exception instead of producing a clean
  `PublisherConfigError` (see Q3). One-line fix to the existing
  `except (ssl.SSLError, OSError)` tuple. Test in
  `test_publisher_config_error.py::test_aws_iot_value_error_raises_config_error`.

- **(Q3) Introduced `PublisherConfigError(PublisherError)` and the
  halt-the-fleet behavior.** The meat of this update. Subclass of
  `PublisherError` so generic `except PublisherError` sites still work;
  the runner inspects the specific type to decide between retry-forever
  (transient) and halt-the-fleet (static config). Promoted three raise
  sites: missing-file checks in `AwsIotPublisher.__aenter__`, SSL/OS/
  ValueError on cert load, `_parse_mqtt_url` on bad URL. `MqttError`
  on connect/publish stays as plain `PublisherError` (truly transient).
  Runner: `_run_pump` catches `PublisherConfigError` before
  `PublisherError` (subclass-first ordering), logs at ERROR, re-raises.
  `Fleet.run` catches it alongside `CancelledError` in the
  `asyncio.gather` wrapper, sets shutdown, drains via
  `return_exceptions=True`, re-raises. `main()` catches at two layers
  (Fleet.from_config for URL-parse failure at construction; asyncio.run
  for cert failure from per-pump task), returns new exit code 4
  (`PUBLISHER_CONFIG_ERROR_CODE`). New ADR 0003 §Addendum 2026-05-28
  "Static config errors halt the fleet" carving out the exception
  from §Decision 4 retry-forever. Test coverage: 11 new in
  `test_publisher_config_error.py`, 3 new in `test_runner_config_error.py`,
  5 new in `test_main.py` (one skipped on Python < 3.12 because
  `asyncio.run(loop_factory=...)` is 3.12+).

- **(Q8) Added `curl` fallback to Phase A of the smoke runbook.** The
  AWS Console sometimes links to docs instead of serving the AmazonRootCA1
  cert directly. Inline `curl.exe -o simulator/.secrets/P-00/AmazonRootCA1.pem
  https://www.amazontrust.com/repository/AmazonRootCA1.pem` added so the
  next PO running this won't get stuck.

**Declined (Q5):** Gemini suggested a "pre-flight check" in
`Fleet.from_config` that the base secrets directory exists, to avoid
15 parallel stack traces for one missing folder. Declined because
`PublisherConfigError` halting from the first per-pump task that
hits the missing cert covers the same UX with less code — there's a
theoretical sub-second window where multiple pumps could log the
same error before the halt propagates, but in practice the halt is
fast. Documented in the `Fleet.from_config` docstring + ADR 0003
§Addendum so a future session reconsidering this has the context.

**Confirmations (no code change):** Q1 (SSLContext defaults — don't
pin TLS 1.2), Q4 (monkeypatched cert tests are fine — adding
`cryptography` would be testing the stdlib), Q6 (defer QoS knob,
proposed shape is right), Q7 (Rule of Three on LocalPublisher
refactor — wait for 3rd publisher), Q9 (3.0s timeout is fine, no YAML
knob), Q10 (accept half-open socket window; AWS IoT keepalive sweeps
in 60s), Q11 (`os._exit(130)` is the right choice; `sys.exit` /
re-raised `KeyboardInterrupt` both have known footguns in asyncio
signal handlers), Q12 (`_ShutdownState` class is pragmatic, not over-OO),
Q13 (mode parity for the disconnect bound is correct — prevents
future "only hangs on AWS" bug class).

**Test count:** 160 → **179** (+19). One skipped on Python 3.10 (the
`asyncio.run(loop_factory=...)` end-to-end main() test). On Adar's
3.12+ Windows, all 179 should pass.

**Files changed in this update:**

- `simulator/publisher.py` — `PublisherConfigError` class, promoted
  raises, added `ValueError` to SSL exception tuple, updated module
  docstring with the two exception classes' contract.
- `simulator/runner.py` — split `_run_pump`'s except into
  config-error-first + transient-second, `Fleet.run` catches
  `PublisherConfigError` alongside `CancelledError`, updated module +
  `Fleet.run` + `Fleet.from_config` docstrings.
- `simulator/__main__.py` — imported `PublisherConfigError`, added
  `PUBLISHER_CONFIG_ERROR_CODE = 4` constant, two new except clauses
  in `main()`, updated module docstring with the exit-code table.
- `simulator/__init__.py` — re-exported `PublisherConfigError` and
  `DISCONNECT_TIMEOUT_SECONDS`.
- `simulator/tests/test_publisher_config_error.py` — NEW (11 tests
  for the subclass relationship, missing-file raises, SSL/OS/ValueError
  wrapping, MqttError-stays-transient regression, bad-URL at
  construction).
- `simulator/tests/test_runner_config_error.py` — NEW (3 tests:
  fleet halts on config error from single pump, fleet drains other
  pumps when one halts with config error proving the (B) disconnect
  bound is load-bearing during drain, plain `PublisherError` still
  retries regression test).
- `simulator/tests/test_main.py` — added `sys` import, added
  `_REQUIRES_LOOP_FACTORY` skipif marker, added 5 new exit-code tests
  (one marked skip on 3.10).
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — new Addendum
  2026-05-28 "Static config errors halt the fleet".
- `docs/sessions/2026-05-27-simulator-aws-iot-publisher.md` — this
  Update #2 section + Phase A curl-fallback inline edit.
- `review_packets/2026-05-27-simulator-aws-iot-publisher.md` —
  Resolution table filled in for all 13 questions.

**Trade-offs:**

- **`PublisherConfigError` is a runtime carve-out from a Decision that
  ADR 0003 explicitly documented as "retry forever".** Worth being
  honest about: the original retry-forever decision was made for
  *transport* failures, not *configuration* failures. The carve-out
  is consistent with that intent, but the original ADR text said
  "retry forever" without that qualification. Addendum 2026-05-28
  makes the distinction explicit so a future reader doesn't read
  Decision 4 alone and infer "everything is retried".

- **`Fleet.run` re-raises out of `asyncio.gather`.** This is the first
  exception that's expected to propagate out of the runner in the
  project's history (CancelledError doesn't count — it's a control
  flow signal). `main()` had to grow two new except clauses to catch
  it at both possible surface points. Surface area expansion is small
  but documented in `__main__.py`'s module docstring exit-code table.

- **Test count grew 12%** (160 → 179). Mostly mechanical (one per
  raise site + one per exit-code path), but bears watching: at some
  point the per-error-class test multiplication starts costing more
  than it pays. We're not there yet.

- **Q5 redundancy debate.** Reasonable people could disagree on
  whether the pre-flight check is worth ~5 lines for the cleaner
  "one error log instead of N" UX. Document the reasoning so it's a
  one-step revisit if the multi-error log spam ever becomes a real
  pain point.

**Commit to run (PO-side, separate from the 2026-05-28 batch):**

```powershell
cd "D:\Claude\ML Observability Pipeline"

git add simulator/publisher.py simulator/runner.py simulator/__main__.py simulator/__init__.py `
        simulator/tests/test_publisher_config_error.py `
        simulator/tests/test_runner_config_error.py `
        simulator/tests/test_main.py `
        docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md `
        docs/sessions/2026-05-27-simulator-aws-iot-publisher.md `
        review_packets/2026-05-27-simulator-aws-iot-publisher.md `
        review_responses/2026-05-27-simulator-aws-iot-publisher.md

$msg = @"
simulator: address Gemini review (PublisherConfigError halts fleet, ValueError catch)

Three accepted changes from the Gemini review of the 2026-05-27 +
2026-05-28 aws-iot-publisher work:

- Q2: added ValueError to AwsIotPublisher's SSL exception tuple.
  Encrypted PKCS#8 keys without passwords (and a few other malformed-
  PEM cases) raise ValueError, not ssl.SSLError. Without the catch
  the runner would crash with an unhandled exception.

- Q3: introduced PublisherConfigError(PublisherError) for static
  config errors (missing cert, malformed PEM, bad URL). Runner halts
  the fleet on the subclass (vs retry-forever on the parent). main()
  catches it and returns new exit code 4 (PUBLISHER_CONFIG_ERROR_CODE)
  distinct from 2 (load_config ConfigError) and 3 (runner
  NotImplementedError). New ADR 0003 Addendum 2026-05-28 'Static
  config errors halt the fleet' carves this out from Decision 4
  retry-forever. The 2026-05-28 disconnect-bound from the prior
  commit is load-bearing here: it bounds the drain when the halting
  pump's exception cancels the others mid-publish.

- Q8: added a curl fallback to Phase A of the smoke runbook for the
  AmazonRootCA1.pem download; the Console sometimes links to docs
  instead of serving the cert directly.

Q5 (Gemini's fleet-level pre-flight check) declined as redundant with
Q3's halt-on-first-failure. Documented in Fleet.from_config docstring
and in ADR 0003 Addendum so a future session reconsidering this has
the context.

Tests: 160 -> 179 (+19, +14 from Q3 work and +5 from main() exit-code
tests). One skipped on Python 3.10 (the asyncio.run(loop_factory=...)
test; Adar runs 3.12+ on Windows where it passes).

Resolution table filled in:
review_packets/2026-05-27-simulator-aws-iot-publisher.md
"@
git commit -m $msg
```
