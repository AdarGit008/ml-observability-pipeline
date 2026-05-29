Here is your adversarial-but-fair review. You’ve done an excellent job isolating the mode-parity boundary, but there are a few implementation details—specifically around the event loop, Terraform packaging, and the InfluxDB client—where the attempt to keep things simple has actually introduced structural friction. 

Here are the direct answers to your questions, highlighting risks and trade-offs.

### 1. `shared/` package layout and Terraform packaging
**The Risk:** Terraform's `archive_file` data source handles single directories beautifully via `source_dir`, but it **does not natively support combining multiple arbitrary directories** (like `shared/` and `lambda_scorer/`) into the root of a single ZIP file without bringing the entire repo context along.
**The Trade-off:** If you point `archive_file` at the repo root, your Lambda deployment package will include `local_runtime/`, `simulator/`, the `docs/`, etc. This bloats cold start times (violating the spirit of "polished AWS differentiation") and leaks dev-only code into production artifacts. 
**The Fix:** You will need a build step. Don't fight Terraform on this. A simple `scripts/build_lambda.ps1` that copies `shared/` and `lambda_scorer/` into a `.build/lambda_dist/` staging directory before Terraform runs will keep your ZIP pristine. Accept this DevOps friction now; it's the standard cost of doing a monorepo in AWS without heavy frameworks like SAM or Serverless.

### 2. Wildcard subscriptions vs. single connection
**The Risk:** The network topology (one connection, wildcard sub) is fine. The **concurrency model** is where your mode-parity argument falls apart. 
You stated: *"the handler is sync-per-message just like a Lambda hot-path invocation would be, so this is mode-correct."* 
This is false equivalence. In AWS, 15 simultaneous MQTT messages trigger 15 *concurrently executing* Lambda environments. In your `local_runtime`, a single asyncio event loop running a synchronous CPU-bound handler (numpy feature extraction) will serialize the workload. At 75 msg/s, doing numpy math and synchronous I/O blocks the event loop, potentially starving the MQTT client's background thread, leading to PINGREQ timeouts and dropped connections.
**The Fix:** The single wildcard connection is the right choice, but the handler *must* immediately offload the sync workload. You should dispatch the `extract_features` + `score` pipeline to a `ProcessPoolExecutor` or `ThreadPoolExecutor` to truly mirror Lambda's concurrent isolation.

### 3. InfluxDB Schema (Cardinality & Tagging)
**The Risk:** Your tag cardinality is practically zero. InfluxDB doesn't break a sweat until you hit ~100,000 unique tag values. 15 to 100 `pump_id`s is rounding error. Storage for 240M points/year is easily handled by any modern SSD.
**The real design weakness:** You are calculating and writing flat `psi_<feature>` fields on *every single telemetry reading*. If mode parity holds, this implies your AWS Lambda will also calculate Drift/PSI on every 2-second telemetry tick. Computing PSI across two arrays (window vs reference) is computationally heavy. Doing it 7.5 times a second in Lambda will actively drive up your AWS compute bill (violating constraint #1), and writing redundant PSI values to InfluxDB inflates storage.
**The Fix:** The schema is safe, but decouple the *telemetry/score* loop from the *drift/PSI* loop. PSI should be evaluated on a tumbling window (e.g., once every 5 minutes), not on every tick.

### 4. `asyncio.to_thread` for InfluxDB writes
**The Risk:** Wrapping a sync network call in `to_thread` at 7.5 to 75 ops/sec is unnecessary thread-pool thrashing and GIL contention. 
**The Fix:** You don't need `to_thread`. The official `influxdb-client` package already ships with an asynchronous API powered by `aiohttp`. 
Instead of:
`write_api = client.write_api(...)`
Use:
`from influxdb_client.client.write_api_async import WriteApiAsync`
`write_api = client.write_api_async()`
Use `await write_api.write(...)`. Don't fight the library by threading its synchronous wrapper when a native async implementation is provided out-of-the-box.

### 5. PSI Stub's Sentinel Values
**The Risk:** Making this configurable now is a textbook YAGNI (You Aren't Gonna Need It) violation. 
The static fixtures (`vibration_amp` at 0.15, rest < 0.10) perfectly fulfill the contract required for the downstream alerting session to have a deterministic trigger. Plumb the stubs exactly as they are. If a future test needs to simulate a massive >0.25 shift, use `unittest.mock.patch` inside that specific test module rather than polluting the shared stub's signature with environment variables or kwargs.

### 6. Mode-Parity Tests (Catching Vendored Copies)
**The Risk:** `local_runtime.service.extract_features is shared.features.extract_features` only proves that both namespaces point to the same cached object in `sys.modules`. If someone copies `features.py` into `local_runtime/` and imports it, `is` will fail (which is good), but it doesn't assert *where* the true source of logic lives.
**The Fix:** Use the `inspect` module to physically verify the filepath.
```python
import inspect
import local_runtime.service
from pathlib import Path

def test_structural_parity_no_vendoring():
    # 1. Assert it's the exact same function in memory
    import shared.features
    assert local_runtime.service.extract_features is shared.features.extract_features
    
    # 2. Assert the physical file executed is inside the repo's /shared/ directory
    func_file = Path(inspect.getfile(local_runtime.service.extract_features)).resolve()
    shared_dir = Path(__file__).parent.parent.parent / "shared"
    
    assert shared_dir in func_file.parents, f"extract_features is not being loaded from /shared/! Loaded from: {func_file}"
```
This physically locks the boundary. Even if module paths get weirdly mangled during a local test run, this ensures the runtime is executing the centralized file.
