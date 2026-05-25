This is a solid, focused session. Introducing configuration via `yaml.safe_load` with frozen dataclasses establishes a strong foundation before the complexity of asyncio and MQTT arrive. 

Here is my adversarial-but-fair review of the simulator config layer, organized by your specific questions, followed by a few overarching design notes.

### Answers to Your Specific Questions

**1. Validation severity for non-wired scenarios (Silent accept vs. Warn/Raise)**
*Recommendation:* **Log a warning.**
*Reasoning:* A silent accept is a UX footgun for the portfolio audience. If a recruiter or reviewer sees `scenario: seasonal_drift` in the example YAML, changes their local config to try it, and sees zero drift, their immediate assumption will be "this project is broken," not "the schema is future-proofed."
Use Python's `logging.warning()` or `warnings.warn()` in `load_config` when `scenario != "healthy"`. It maintains the schema stability you want (it doesn't crash) but explicitly tells the user: *"Scenario '{scenario}' parsed but not yet implemented. Running in HEALTHY mode."*

**2. `DEMO_MODE_HEALTHY_DWELL_TICKS = 60` (Hardcoded vs. override)**
*Recommendation:* **Keep the boolean + hardcoded constant.**
*Reasoning:* Constraint #5 dictates a polished repo, and for demo purposes, minimalism is polish. Leaking `demo_mode_dwell_ticks` into the YAML bikesheds the schema and blurs the line between a "quick demo" toggle and full scenario authoring. 60 ticks (~1 minute at 1Hz) is the perfect amount of time to watch a terminal or a Grafana dashboard populate before a simulated failure occurs. If someone needs different behavior, they are in advanced territory and should edit the source (or write a custom Scenario runner later).

**3. Range bounds for `pump_count` and others**
*Recommendation:* **Keep them as constants, but ensure the error messages are highly explicit.**
*Reasoning:* As long as `ConfigError` says exactly `"pump_count 51 exceeds maximum of 50"`, it's not a surprise. I would suggest setting `_PUMP_COUNT_MAX` to something reasonably high for a single-PC load test (e.g., `100` or `1000` depending on what your future asyncio loop can handle without pegging the CPU). You don't need to over-engineer an escape hatch in the YAML; Python developers know how to change a module-level `_MAX` variable.

**4. `ConfigError(ValueError)` vs a fresh exception class**
*Recommendation:* **Inherit directly from `Exception`.**
*Reasoning:* Config loading is an infrastructural boundary. If you subclass `ValueError`, a caller might write `except ValueError:` hoping to catch a config issue, but accidentally swallow a standard library `ValueError` thrown from deep inside `yaml.safe_load` or a type-conversion utility. Inheriting from `Exception` enforces a strict, explicit contract for domain errors.

**5. `profiles_for` shared references with `DEFAULT_PROFILES`**
*Recommendation:* **Do not deep-copy; the current design is correct.**
*Reasoning:* `StateProfile` is a `@dataclass(frozen=True)`. In Python, relying on immutability for shared references is idiomatic and performant. If a future developer explicitly removes `frozen=True` from `StateProfile` and mutates it, they are deliberately breaking the contract and own the resulting bugs. Don't add deep-copy boilerplate to protect against active sabotage of your type definitions.

**6. `requirements.txt` shape**
*Recommendation:* **Stick with a single `requirements.txt` for now, but plan for `requirements-dev.txt`.**
*Reasoning:* You asked what recruiters will `pip install -r`. The answer is `requirements.txt`. While `pyproject.toml` is the modern standard, converting to it right now violates your "one polished repo, not half-finished" constraint if you aren't also setting up a build backend (like `hatchling` or `flit`). Keep it dead simple. When you add `pytest` and `ruff`, put them in `requirements-dev.txt` and add a comment in the README: *"To run the simulator: pip install -r requirements.txt. To run tests: pip install -r requirements-dev.txt"*.

**7. AWS-specific leakage in the schema**
*Recommendation:* **The current schema is fine, but strictly reject unknown keys to prep for the IoT session.**
*Reasoning:* `broker.target: aws-iot` is a clean enum. When you get to the IoT session, you will need to add fields like `broker.endpoint`, `broker.cert_path`, `broker.key_path`, and `broker.ca_path`. As long as your current loader rejects unknown keys (e.g., throwing a `ConfigError` if it sees a key it doesn't recognize), you protect against typos today and cleanly introduce those new fields tomorrow. It is much easier to loosen strict validation later than to tighten loose validation. 

### Additional Design Observations

*   **Absence of a config file:** What happens if `config.yaml` doesn't exist? Does `load_config()` crash? For a polished single-PC dev experience, consider allowing `load_config(path: str | None = None)` where passing `None` (or finding no file at the default path) returns a `SimulatorConfig` instantiated with sensible defaults (e.g., 1 pump, local broker). This allows someone to just run `python -m simulator.pump` without needing to copy the example YAML first.
*   **YAML loading security:** Since you are enforcing `yaml.safe_load` (excellent), ensure your `test_config.py` doesn't just test valid/invalid schemas, but explicitly tests that passing a string with a YAML tag that requires `yaml.load` (like `!!python/object/apply:os.system ['echo hacked']`) is rejected safely by the parser. (PyYAML's `safe_load` handles this, but a test proves it to a reviewer).
