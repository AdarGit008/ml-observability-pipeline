# Session 2026-05-25 — simulator — config-yaml

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`)
- **Context loaded:** `_global`, `simulator` (Tier 2 only — no cross-component contracts touched today, so `_interfaces` not loaded)
- **Duration:** ~1h

## Intent

Retire the `DEFAULT_PROFILES` TODO from the 2026-05-24 pump session by adding a typed YAML config loader for the simulator, with a `demo_mode` flag that collapses HEALTHY dwell to ~60 ticks so a fresh local clone exercises the full HEALTHY → FAILED arc in under five minutes.

Constraint: no MQTT publishing this session (per the brief). The `broker` and `scenario` fields are accepted in the schema so demo manifests can be authored ahead of the runner, but no client and no scenario logic are wired.

## What changed

**New files:**
- `simulator/config.py` — `SimulatorConfig` / `FleetConfig` / `BrokerConfig` frozen dataclasses, `ScenarioKind` / `BrokerTarget` enums, `ConfigError`, `load_config(path)`, `profiles_for(config)`. Strict validation: required keys, unknown-key reject, type checks (booleans excluded from `int` / `float`), range bounds on `pump_count` / `setpoint_rpm` / `ambient_celsius`, non-empty `broker.url`. `DEMO_MODE_HEALTHY_DWELL_TICKS = 60`.
- `simulator/config.example.yaml` — annotated example with inline commentary on every field, including the `demo_mode` trade-off.
- `simulator/tests/test_config.py` — 45 tests covering happy paths, every schema-error branch, the bool-as-int gotcha, the example-yaml round-trip, the `profiles_for` overlay (demo on/off, non-HEALTHY untouched, returned-dict-is-independent), and an end-to-end Pump smoke that confirms a demo-mode pump exits HEALTHY within `DEMO_MODE_HEALTHY_DWELL_TICKS` steps.
- `requirements.txt` — project's first runtime-dep file, `PyYAML>=6.0` as the only entry, with a comment about a future dev-deps split.
- `review_packets/2026-05-25-simulator-config-yaml.md` — 7 specific questions for Gemini.

**Modified:**
- `simulator/__init__.py` — re-exports the new symbols alongside `Pump` / `PumpState`.
- `context/simulator.md` — config.yaml checkbox ticked, interfaces section updated to describe the loader, link to this session log.
- `context/model.md` — admonition block at the top carrying ADR-0002's bearing-temp non-monotonicity note (so the model session sees it before designing features).

PR: TBD — Adar opens after commit 2.

## Decisions

No new ADRs this session — all choices were inside existing constraints. Decisions that *could* have become ADRs but didn't, recorded here:

- **No pydantic.** PyYAML + a hand-rolled `_validate` is enough for a 4-field schema. Pydantic would add ~20 MB of deps and an import-time cost for a problem this small. If validation surface area grows (Lambda configs, IaC inputs, scoring service configs), reconsider then with a proper ADR.
- **Bool `demo_mode` + module constant, not a numeric `demo_mode_dwell_ticks`.** Keeps the YAML schema small; the only sensible value for portfolio demos is "~2 minutes." Surfaced as Gemini question #2 in case I'm wrong.
- **Non-`healthy` scenarios silently accepted at load time.** Schema stability lets demo manifests be authored ahead of the runner. Surfaced as Gemini question #1 — could go either way; PO will pick from Gemini's response.
- **Frozen dataclasses everywhere.** Cheap immutability guarantee; matches `Pump`-side `StateProfile` choice. Test enforces it.

## Trade-offs surfaced

- **Range bounds are typo-catchers, not physics.** `pump_count ∈ [1, 50]`, `setpoint_rpm ∈ [1.0, 10000.0]`, `ambient_celsius ∈ [-50.0, 80.0]`. The intent is to catch `pump_cont: 1500` style typos cheaply; nothing physical is being enforced. Documented in `config.py` comments. Surfaced as Gemini question #3.
- **Bool-as-int rejection.** `pump_count: true` would silently parse as `1` without the explicit `isinstance(value, bool)` guard. This costs one line and one test but prevents a class of YAML-author confusion. Kept.
- **Module-level state.** `DEFAULT_PROFILES` (from `pump.py`) and `DEMO_MODE_HEALTHY_DWELL_TICKS` (from `config.py`) are both module-level constants. `profiles_for` returns a *new dict* but the inner `StateProfile` instances are shared references — safe because `StateProfile` is `@dataclass(frozen=True)`. Tested via the "mutating returned dict doesn't corrupt `DEFAULT_PROFILES`" case. Surfaced as Gemini question #5.

## Sandbox / FUSE discovery (mid-session)

The Edit and Write tools silently truncate writes that would grow an existing file beyond its original byte length on the project's FUSE mount. Discovered when `simulator/__init__.py` (originally 259 bytes) was supposed to grow to ~880 bytes — the file tools both reported success, but `wc -l` showed the file still at 8 lines (the cut point exactly where the original ended). Mtime was unchanged. New-file Writes are unaffected.

Worked around in-session by writing via `cat > file <<'EOF' ... EOF` from bash. To be carried into the `[[ml-obs-pipeline-git-on-windows]]` memory entry — same FUSE layer, new failure mode, same `/tmp` / bash workaround.

## Gemini review highlights

Gemini engaged substantively on all 7 questions and added 2 design observations (no rubber-stamp). Full dispositions in the packet's Resolution table; summary:

- **Q1 (validation severity) — Addressed.** Gemini argued silent-accept is a UX footgun for the portfolio audience: a recruiter who tries `scenario: seasonal_drift`, sees no drift, and assumes the project is broken. Added `warnings.warn(UserWarning)` in `load_config` for non-HEALTHY scenarios, plus two new tests (`test_non_healthy_scenarios_parse_with_warning`, `test_healthy_scenario_emits_no_warning`).
- **Q3 (range bounds) — Addressed.** Bumped `_PUMP_COUNT_MAX` 50 → 100 on Gemini's call that single-PC asyncio can tolerate ~100 pumps. `test_pump_count_out_of_range` parametrization updated 51 → 101. Error message form ("must be in [min, max], got X") was already explicit; no change there.
- **Q4 (`ConfigError` parent) — Addressed.** Changed `ConfigError(ValueError)` → `ConfigError(Exception)` after Gemini pointed out that a caller's `except ValueError` could accidentally swallow unrelated value errors from inside `yaml.safe_load` or type-conversion utilities. No callers were depending on the `ValueError` parent.
- **Q2, Q5, Q6, Q7 — Confirmed (no code change).** Gemini agreed with the original instinct on all four: minimalism for `demo_mode` (Q2), no deep-copy in `profiles_for` (Q5), single `requirements.txt` for now (Q6), strict-unknown-keys is what guards future schema additions (Q7).
- **Additional observation A (no-config-file defaults) — Rejected (PO 2026-05-25).** Gemini suggested `load_config(path=None)` could return a `SimulatorConfig` with sensible defaults so `python -m simulator.pump` works without copying the example. PO chose to keep the strict loader and address the broader load/install/run UX in a later session. Concrete step taken this session: `simulator/config.yaml` added to `.gitignore` so user-local tuning won't leak; the README setup step is deferred.
- **Additional observation B (YAML safe-load attack test) — Addressed.** Added `test_yaml_safe_load_rejects_python_tag_attack` to prove `!!python/object/apply:os.system [...]` is rejected via the existing `yaml.YAMLError` handler.

The three accepted changes shipped in commit 4 (below). Tests now total **77 passing** (+2 from the original 75).

## State at end of session

- **Tests: 77 passing** (30 pre-existing pump tests + 47 config tests — original 45 plus the 2 added from Gemini's review), 0.19 s in sandbox (`cp simulator /tmp && pytest`).
- **Python:** sandbox runs 3.10.12, project target is 3.12. New code uses `from __future__ import annotations`; nothing 3.12-only used. Adar to re-run Windows-side on 3.12 before merge.
- **Open follow-ups:**
  - File-tool / FUSE write-truncation bug — memory `[[ml-obs-pipeline-git-on-windows]]` updated mid-session; carry the bash-heredoc workaround forward.
  - **Onboarding UX (deferred to a later session, PO 2026-05-25):** The "fresh-clone to first-run" path needs work — at minimum a README setup step (`cp simulator/config.example.yaml simulator/config.yaml`), maybe a `python -m simulator` entry point. PO explicitly chose to keep `load_config` strict (no auto-defaults) and tackle load/install/run UX as its own session. `simulator/config.yaml` is now in `.gitignore` so user-local edits don't leak.
  - MQTT publishing (paho-mqtt, asyncio) — next natural simulator session.
  - Scenario runner (seasonal_drift, fleet_expansion, real_failure) — currently emits a warning but produces HEALTHY behavior; when the runner lands, replace the warning with a `NotImplementedError` for unwired scenarios.
- **`context/simulator.md`:** updated. `config.yaml` checkbox ticked, interfaces section expanded, this session log linked.
- **`context/model.md`:** updated with the ADR-0002 carry-in note (cross-component touch, called out in the brief).

## Commits to run (PO-side, per `[[ml-obs-pipeline-git-on-windows]]`)

```powershell
cd "D:\Claude\ML Observability Pipeline"
```

**Commit 1 — simulator config code, tests, example, requirements.txt:**

```powershell
git add simulator/config.py simulator/config.example.yaml simulator/tests/test_config.py simulator/__init__.py requirements.txt
git commit -m "simulator: add YAML config loader + demo_mode dwell shortcut

Retires the DEFAULT_PROFILES TODO from the 2026-05-24 pump session.

New schema deserializes to a frozen SimulatorConfig with strict validation:
required keys, unknown-key reject, type checks (booleans excluded from int/
float), range bounds on pump_count/setpoint_rpm/ambient_celsius, non-empty
broker.url. ScenarioKind and BrokerTarget enums accept all planned values
so demo manifests are stable from day one; only the healthy scenario is
wired this session. profiles_for(config) returns a per-state StateProfile
dict that overlays HEALTHY dwell -> 60 ticks (~2 min) when demo_mode is
true, leaving DEGRADING/FAILING/FAILED untouched.

45 new tests covering every schema-error branch, the example.yaml round
trip, and an end-to-end Pump smoke; 75 tests total passing.

Adds PyYAML>=6.0 as the project's first runtime dep (requirements.txt)."
```

**Commit 2 — context updates (simulator + model carry-in):**

```powershell
git add context/simulator.md context/model.md
git commit -m "context: tick simulator config.yaml box; carry ADR-0002 note into model

Tick the config.yaml checkbox in context/simulator.md and expand the
interfaces section to describe the loader, the demo_mode flag, and the
profiles_for() overlay. Link the 2026-05-25 session log.

Carry the ADR-0002 bearing-temp non-monotonicity note into context/model.md
so the (future) model session knows not to pre-suppose monotonicity in
feature engineering. Cross-component touch was called out in the session
brief; the note is inert until the model session opens."
```

**Commit 3 — review packet + session log (pre-review snapshot):**

```powershell
git add review_packets/2026-05-25-simulator-config-yaml.md docs/sessions/2026-05-25-simulator-config-yaml.md
git commit -m "review: simulator config-yaml packet + session log"
```

(Skip the push for now; commit 4 follows immediately.)

Then run Gemini (already done — `review_responses/2026-05-25-simulator-config-yaml.md` is on disk):

```powershell
.\scripts\gemini_review.ps1 -Slug simulator-config-yaml
```

**Commit 4 — Gemini-review changes + Resolution + response file:**

```powershell
git add simulator/config.py simulator/tests/test_config.py .gitignore review_packets/2026-05-25-simulator-config-yaml.md review_responses/2026-05-25-simulator-config-yaml.md docs/sessions/2026-05-25-simulator-config-yaml.md

# Use a PowerShell here-string for the multi-line message so embedded
# double quotes don't terminate the -m argument early. The closing "@
# must sit at column 0 with no leading whitespace.
$msg = @"
simulator: address Gemini review (warn on non-healthy scenario, raise pump cap, ConfigError parent)

Three accepted changes from review_packets/2026-05-25-simulator-config-yaml.md:

- Q1: load_config now emits warnings.warn(UserWarning) when scenario is
  non-healthy. Schema still parses; user sees an explicit "parsed but not
  implemented" message. Two new tests guard the path.
- Q3: _PUMP_COUNT_MAX 50 -> 100. Single-PC asyncio still tolerable per
  Gemini; test_pump_count_out_of_range parametrization updated 51 -> 101.
- Q4: ConfigError now inherits from Exception, not ValueError, to keep
  callers' except ValueError from accidentally swallowing config errors.

Plus a new test_yaml_safe_load_rejects_python_tag_attack from Gemini's
additional observation B, proving safe_load rejects the classic RCE
vector via the existing YAMLError handler.

Add'l obs A (load_config(path=None) auto-defaults) was rejected per PO
decision: keep the strict loader; the broader fresh-clone-to-first-run
UX is deferred to a later session. Concrete change here: add
simulator/config.yaml to .gitignore so user-local tuning doesn't leak.

Tests: 77 passing (30 pump + 47 config). Two of Gemini's points were
confirmations of the original design (Q2 demo_mode constant, Q5 no
deep-copy); two more (Q6 requirements.txt shape, Q7 AWS-leakage) were
no-change-needed. Full disposition table in the review packet.
"@
git commit -m $msg
git push
```

## Note for next session

Config-yaml loop is closed (Gemini review + Resolution committed in commit 4). The natural next simulator session is **MQTT publishing** (paho-mqtt, asyncio). Items to watch:

1. **`broker.target` is already in the schema** — wire `local` (Mosquitto) and `aws-iot` (mTLS with cert paths) behind a single `Publisher` interface so `Pump.step()` callers don't branch. mTLS cert paths likely need a new sub-block under `broker:` (e.g. `broker.tls.cert_path`, `.key_path`, `.ca_path`); add via additive schema, no breaking changes.
2. **`scenario`** is still parsed-but-unused. When MQTT lands, also stub a `Scenario` interface and have `load_config` route to it; non-`healthy` scenarios should `raise NotImplementedError` at runner-construction time, not at load time. (This may be revisited by Gemini's answer to question #1 in the current packet.)
3. **Concurrency model** open question in `context/simulator.md` resolves in the MQTT session — default is single asyncio loop with 15 tasks + paho-mqtt's asyncio integration.
4. **File-tool / FUSE write-truncation bug** — update memory `[[ml-obs-pipeline-git-on-windows]]` before opening the MQTT session so the next Claude doesn't re-discover it. Workaround: write new files via Write tool, but use bash heredocs for any file that's already on disk.
