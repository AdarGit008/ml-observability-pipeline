Here is your adversarial-but-fair code review, directly addressing your questions and calibrated against your portfolio constraints.

### 1. Asyncio bridge correctness (aiomqtt failures and `__aexit__`)
**Your call to silence `MqttError` in `__aexit__` is correct and standard practice.** When tearing down an MQTT client, if the broker has already severed the TCP connection (or if the network dropped), `client.disconnect()` within aiomqtt's context manager will raise an `MqttError`. If you let this bubble up out of `__aexit__`, you risk masking the *original* exception that caused the teardown (e.g., a `CancelledError` or a `PumpError`), which makes debugging a nightmare. 

*Risk surface:* Ensure your `publish()` call handles `MqttError` correctly. Aiomqtt's `publish()` queues the message. If the connection drops *after* queueing but *before* transmission, you will get an `MqttError`. Translating this to `PublisherError` is exactly the right boundary behavior. 

### 2. Per-pump connection topology at scale
**At 15 pumps, you are perfectly fine.** 
The theoretical concern with `paho-mqtt` (which `aiomqtt` wraps) is that historically, paho relied on background threads (`loop_start`). However, `aiomqtt` uses `paho`'s non-blocking `loop_read`/`loop_write`/`loop_misc` tied directly into the `asyncio` event loop. You do not have 15 blocked threads; you just have 15 standard asyncio socket readers.

*Fairness pitfall:* The only risk here is if your `Pump.step()` drift math suddenly became a blocking, CPU-bound nightmare. Because it's fast math, the asyncio scheduler will round-robin through your 15 tasks without starvation. This design perfectly fulfills your Mode Parity (Constraint #4) without violating Single-PC development (Constraint #2).

### 3. Retry-forever backoff cap (Reset-on-connect)
**Your suspicion is spot on. Reset-on-connect is a flaw and creates a "flapping" vulnerability.**
If a pump successfully connects but lacks the correct IAM/IoT policy to publish to its specific topic, AWS IoT Core will drop the connection *immediately* upon the first publish attempt. Under your current logic:
1. Connects successfully (Backoff resets to 1s).
2. Publishes.
3. Connection severed by broker due to auth/policy.
4. Reconnects after 1s.
5. Backoff resets to 1s.

You have just built a tight, infinite 1-second retry loop that bypasses your 30s cap entirely. 
*Fix:* Do not reset the backoff on connection. Reset the backoff on a **successful publish** (or if the pump tracks state, only reset if the last publish was > `MAX_BACKOFF_SECONDS` ago). 

### 4. Shape-only TLS validation in the loader
**Keep it as shape-only. Your separation of concerns here is excellent.**
The config loader (`config.py`) should validate *syntax and schema*. The factory or runtime component (`AwsIotPublisher`) should validate *environment* (disk/network). If you put file-existence checks in the loader, you entangle deployment environment state with configuration parsing. This ruins the testability of your configuration logic and forces disk-I/O into unit tests unnecessarily. 

### 5. `Fleet.from_config` rejecting aws-iot up front
**Keep the double-rejection.** It is not confusing; it is classic Defense in Depth.
*   `Fleet.from_config` throws the `NotImplementedError` to protect the **UX**. (Fails instantly, once, with a clear message).
*   `AwsIotPublisher.__aenter__` throws the `NotImplementedError` to protect the **Contract**. (In case someone instantiates the publisher directly in a future test or script, bypassing the Fleet factory).

Removing the fleet-level gate would result in 15 separate pump tasks dumping stack traces and retry warnings to stdout simultaneously. That is an awful UX for someone evaluating a portfolio project. 

### 6. Scenario stub placement (`UserWarning` vs `NotImplementedError`)
**This satisfies the original concern completely, and it is a superior solution.**
My previous feedback highlighted that silent failures (loading an incomplete config and doing nothing) are footguns. Throwing a loud `NotImplementedError` at fleet instantiation is a hard, fast failure. It leaves zero ambiguity for the portfolio reviewer evaluating the code. The specific timing (load vs init) doesn't matter as long as it happens synchronously at startup before the event loop runs wild.

### 7. Test coverage of the retry loop
**You are missing a critical testing pattern: mocking time.**
Asserting retry loop iteration counts verifies the *while loop*, but it completely ignores the *exponential math* and the *30s cap*. You do not need to rely on flaky `asyncio.sleep` behavior in your tests.
*Fix:* Use `unittest.mock.patch` on `asyncio.sleep`. 
```python
@patch("asyncio.sleep", new_callable=AsyncMock)
async def test_fleet_backoff_cap(mock_sleep, ...):
    # run fleet with failing publisher
    # ...
    # Assert the sleeps were exactly: 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0
    calls = mock_sleep.call_args_list
    assert calls[0][0][0] == 1.0
    assert calls[5][0][0] == 30.0
```
This gives you deterministic, fast, exact coverage of the backoff timing logic without any test jitter.

### 8. `__main__.py` Windows signal handling
**Your `KeyboardInterrupt` bubbling path works, but watch out for `CancelledError` teardown traps.**
On Windows, relying on `asyncio.run()` to catch `KeyboardInterrupt` and hurl `CancelledError` into all pending tasks is standard. However:
1. When `asyncio.run` receives `KeyboardInterrupt`, it cancels everything and aggressively tries to close the loop.
2. If `Fleet.run` intercepts `CancelledError`, it must do its teardown (e.g., closing MQTT connections cleanly) *quickly*. 
3. If your `LocalPublisher.__aexit__` requires the event loop to send a clean `DISCONNECT` packet, and the loop is already shutting down, you might trigger `Task was destroyed but it is pending!` errors.

*Alternative approach for bulletproof Windows/Linux parity:*
Instead of relying on `KeyboardInterrupt` bubbling, use Python's `signal` module to set an asyncio Event thread-safely. This works on the Windows ProactorEventLoop where `add_signal_handler` fails.
```python
import signal
shutdown_event = asyncio.Event()

def handle_sigint(signum, frame):
    loop.call_soon_threadsafe(shutdown_event.set)

signal.signal(signal.SIGINT, handle_sigint)
```
Pass `shutdown_event` into your `Fleet`. Have your pump tasks loop on `while not shutdown_event.is_set()`. This avoids the chaotic, aggressive `CancelledError` propagation and allows a perfectly graceful, deterministic teardown across all OSs.
