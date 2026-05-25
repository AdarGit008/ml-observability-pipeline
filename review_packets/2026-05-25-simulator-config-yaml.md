# Review Packet 2026-05-25 — simulator — config-yaml

> Run from the repo root with:
> `.\scripts\gemini_review.ps1 -Slug simulator-config-yaml`
> (or `./scripts/gemini_review.sh simulator-config-yaml` on bash).
> See ADR 0001 for why we don't use the Gemini CLI.

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

Retires the `DEFAULT_PROFILES` TODO from the 2026-05-24 pump session by adding a typed YAML config loader for the simulator. Three new module files (`simulator/config.py`, `simulator/config.example.yaml`, `simulator/tests/test_config.py`), 45 new tests (75 total passing), one new runtime dependency (`PyYAML>=6.0` — first entry in a new `requirements.txt`). Also exports the new symbols from `simulator/__init__.py`, ticks the relevant box in `context/simulator.md`, and carries the ADR-0002 bearing-temp non-monotonicity note into `context/model.md` so the (future) model session won't pre-suppose monotonicity.

No MQTT, no asyncio, no scenario runner — those are explicitly out of scope for this session per the PO brief. The `scenario` and `broker.target` fields are accepted at load time so demo manifests can be authored ahead of the runner; trying to actually *use* a non-`healthy` scenario today is a no-op (only HEALTHY-state behavior is implemented in `pump.py`).

## Diff (file-level)

New:
- `simulator/config.py` — `SimulatorConfig`/`FleetConfig`/`BrokerConfig` frozen dataclasses, `ScenarioKind`/`BrokerTarget` enums, `ConfigError`, `load_config(path)`, `profiles_for(config)`. ~280 lines incl. docstrings.
- `simulator/config.example.yaml` — annotated example with inline commentary on every field, plus the `demo_mode` trade-off explained.
- `simulator/tests/test_config.py` — 45 tests covering happy paths, schema-validation errors (missing/unknown keys at top level + fleet + broker), type errors (bool-as-int, float-as-int, string-not-string), range errors (pump_count, setpoint_rpm, ambient_celsius), enum errors (scenario + broker.target), I/O errors (missing file, empty file, malformed YAML, non-mapping top-level), and the `profiles_for` overlay (demo_mode on/off, non-HEALTHY states untouched, returned dict independent of `DEFAULT_PROFILES`, end-to-end smoke that a Pump driven by demo_mode profiles exits HEALTHY within ~60 ticks).
- `requirements.txt` — first runtime dep file in the repo, just `PyYAML>=6.0` with a comment about future dev-deps split.

Modified:
- `simulator/__init__.py` — re-exports the new symbols alongside `Pump` / `PumpState`.
- `context/simulator.md` — `config.yaml` checkbox ticked, interfaces section updated, link to today's session log.
- `context/model.md` — admonition block at the top carrying ADR-0002's bearing-temp non-monotonicity note.

## Specific questions for Gemini

1. **Validation severity for non-wired scenarios.** `config.py` accepts `scenario: seasonal_drift` (and the other two non-healthy values) at load time without warning. The reasoning: demo manifests can be authored ahead of the runner. The risk: someone runs the simulator today expecting drift, sees only HEALTHY-state behavior, and is silently confused. Should `load_config` log a warning (or raise) when `scenario` is anything other than `healthy` until the scenario runner lands? Or is the silent-accept the right call given the schema-stability argument?

2. **`DEMO_MODE_HEALTHY_DWELL_TICKS = 60` is a hardcoded module-level constant.** Trade-off: hardcoding keeps the YAML schema small (no `demo_mode_dwell_ticks: 60` field to bikeshed), but the only escape hatch for "I want a 30-tick demo mode" is editing source. Should `demo_mode` be a numeric override (`demo_mode_dwell_ticks: int | null`, where non-null implies enabled) instead of a boolean + module constant? My instinct is the bool-plus-constant is cleaner for the portfolio-demo audience — 60 ticks is the right answer for the only audience that runs this. But would like a sanity check.

3. **Range bounds for `pump_count`, `setpoint_rpm`, `ambient_celsius`.** The bounds in `_PUMP_COUNT_MIN`/`_MAX` etc. are intentionally conservative typo-catchers, not physical limits. Are any of them likely to surprise a future user (e.g. `pump_count = 51` failing in a fleet-expansion scenario test)? Should the upper bounds be raised, or annotated with "to relax this, edit `_PUMP_COUNT_MAX`"?

4. **`ConfigError(ValueError)` vs a fresh exception class.** Subclassing `ValueError` lets callers catch a coarser net but blurs the contract (was it a config problem or a downstream value problem?). Should `ConfigError` inherit directly from `Exception` instead?

5. **`profiles_for` returns a new dict but the `StateProfile` instances are shared references with `DEFAULT_PROFILES`** (only the HEALTHY entry gets a new `StateProfile` in demo mode). Since `StateProfile` is `@dataclass(frozen=True)`, the shared reference is read-only — but is there a future-foot-gun where someone unfreezes `StateProfile` and the sharing surprises them? Should `profiles_for` deep-copy?

6. **`requirements.txt` shape.** This is the project's first dep file. I went with a single runtime file and a comment that dev-deps (pytest, ruff, etc.) will split out later. Alternatives: `pyproject.toml` with PEP 621 metadata + `[project.optional-dependencies] dev = [...]`, or `requirements-dev.txt` alongside. Which would Gemini lean toward for a portfolio project that recruiters might `pip install -r`?

7. **Anything AWS-specific that's leaking into the config schema before the IoT session?** The `broker.target: aws-iot` field is the only AWS string today; everything else (URL, credentials, region) is intentionally absent. Is the schema future-flexible enough for the (planned) mTLS-with-cert-paths AWS IoT case without a v2 schema rewrite?

## What I'm NOT looking for in this review
- **Style / formatting.** No linter configured yet; will land in a dev_workflow session.
- **`scripts/`-side review.** No changes to `gemini_review.ps1` or `.sh` this session.
- **MQTT, scenarios, or asyncio.** Out of scope per the PO brief — separate sessions.
- **`PLAN.md` deviations.** None this session.

## Constraints reminder
- `simulator/config.py` must work on Python 3.12 (the project target). Tests are running on 3.10 in the sandbox per `[[ml-obs-pipeline-git-on-windows]]`; PO confirms 3.12 Windows-side before merge.
- `yaml.safe_load` only — `yaml.load` is forbidden (arbitrary-code-execution).
- No `from __future__ import` removals from `pump.py`; new module uses it too for forward-reference unions on 3.9+ compatibility (cheap insurance; harmless on 3.12).

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. Validation severity for non-wired scenarios | **Addressed** | Added `warnings.warn(UserWarning, ...)` in `load_config` when `scenario != HEALTHY`. Schema still parses; user sees an explicit "parsed but not implemented" message. New test `test_non_healthy_scenarios_parse_with_warning` asserts the warning fires for all three non-healthy values; new test `test_healthy_scenario_emits_no_warning` guards against the warning firing on the wired path. |
| 2. `DEMO_MODE_HEALTHY_DWELL_TICKS` constant vs YAML override | **Confirmed (no change)** | Gemini agreed with the original instinct: minimalism is polish, the only sensible value for portfolio demos is ~60 ticks, advanced use cases can edit the module constant. Recorded for the trade-offs section of the session log. |
| 3. Range bounds | **Addressed** | Raised `_PUMP_COUNT_MAX` 50 → 100 per Gemini's "single-PC asyncio still tolerable at ~100" suggestion. Inline comment cites this review. `setpoint_rpm` / `ambient_celsius` bounds left as-is (Gemini didn't flag them specifically). `test_pump_count_out_of_range` parametrization updated 51 → 101. Error message format ("must be in [min, max], got X") was already explicit enough — no change there. |
| 4. `ConfigError(ValueError)` vs `Exception` | **Addressed** | Changed `class ConfigError(ValueError)` → `class ConfigError(Exception)`. Docstring updated to cite the review and explain why subclassing `ValueError` is risky (accidental swallowing of stdlib `ValueError`s from `yaml.safe_load`). No callers were depending on the `ValueError` parent (only `pytest.raises(ConfigError, ...)` usage in tests). |
| 5. `profiles_for` shared `StateProfile` references | **Confirmed (no change)** | Gemini agreed: relying on `@dataclass(frozen=True)` is idiomatic; deep-copy would be defensive programming against deliberate type-definition sabotage. Existing test `test_profiles_for_returns_independent_dict` covers the dict-level isolation, which is the contract that matters. |
| 6. `requirements.txt` shape | **Confirmed (no change)** | Gemini agreed: single `requirements.txt` is what recruiters expect; `pyproject.toml` would imply a build backend we're not setting up. Plan to add `requirements-dev.txt` when pytest/ruff are formalized. Existing comment in `requirements.txt` already mentions this split. |
| 7. AWS-specific leakage | **Confirmed (no change)** | Gemini agreed: current schema is fine, the `_assert_exact_keys` strict-unknown-keys check (already in place) is what makes the future IoT-session additions (`broker.endpoint`, `broker.cert_path`, etc.) safe to introduce. Carry-forward noted in session log's "Note for next session". |
| Add'l obs A: `load_config(path=None)` → defaults | **Rejected (PO decision 2026-05-25)** | PO chose to keep the strict loader and address the broader load/install/run UX in a later session. Gemini's suggestion would have introduced a second source of truth for defaults (`config.example.yaml` and a `default_config()` helper) and blurred the loader contract; the README setup step (`cp simulator/config.example.yaml simulator/config.yaml`) is the deferred fix. `simulator/config.yaml` added to `.gitignore` so user-local tuning doesn't leak. |
| Add'l obs B: YAML safe-load attack test | **Addressed** | Added `test_yaml_safe_load_rejects_python_tag_attack` — confirms the classic `!!python/object/apply:os.system [...]` vector surfaces as a `ConfigError` (via the existing `yaml.YAMLError` handler), proving the safety property to a reviewer and guarding against an accidental `safe_load` → `load` swap in a future edit. |
