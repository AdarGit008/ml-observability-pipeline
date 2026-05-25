# Session 2026-05-24 — simulator — pump-model-and-state-machine

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (via `scripts/gemini_review.ps1`)
- **Context loaded:** `_global`, `simulator`, `_interfaces` (Tier 3 — telemetry dict is a cross-component contract)
- **Duration:** ~1h day 1 (2026-05-24) + ~1h day 2 (2026-05-25 — Gemini loop + resolution + workflow v2 build-out)

## Intent
Implement the physical model + four-state lifecycle for `simulator/pump.py` per PLAN.md §2.2. Definition of done: `Pump` class with `.step()` returning a telemetry dict; unit tests cover all four states. No MQTT.

## What changed
- `.gitignore` — Python + Terraform + project secrets, plus `.envrc` / `.python-version` / `*.bak-*` (added on day 2 from Gemini review). First commit of the session per `context/dev_workflow.md` deferred-decisions.
- `simulator/__init__.py` — exports `Pump`, `PumpState`.
- `simulator/pump.py` — `Pump` class, `PumpState` enum, `StateProfile` dataclass, `DEFAULT_PROFILES`, ISO-8601-ms timestamp helper. RPM equation per ADR 0002 (day 2).
- `simulator/tests/test_pump.py` — 30 tests, all passing in ~0.06 s.
- `review_packets/2026-05-24-simulator-pump.md` — 7 questions for Gemini; Resolution table filled in on day 2.
- `review_responses/2026-05-24-simulator-pump.md` — Gemini's response (day 2).
- `scripts/gemini_review.ps1` + `.sh` — direct-API review wrappers (workflow v2, day 1 mid-session add).
- `docs/adr/0001-direct-gemini-api-for-reviews.md` — records the Gemini CLI → API switch.
- `docs/adr/0002-rpm-coupled-to-degradation.md` — records the RPM-coupling deviation (day 2).
- `PLAN.md.docx §2.2` — RPM equation updated in-place to match ADR 0002 (day 2).
- `DEV_NORMS.md §4` — rewritten to call the script (day 1).
- `context/dev_workflow.md` — v2-changes log + `.gitignore` deferred-decision retired (day 1).
- `context/simulator.md` — current-state checkboxes updated through day 2.

PR: TBD — Adar opens after commit 4.

## Decisions
- **ADR 0001 (day 1):** Replace Gemini CLI with direct REST API script.
- **ADR 0002 (day 2):** Couple RPM to degradation. Deviates from PLAN.md §2.2; PLAN updated to match. PO approved bundling the PLAN edit into this session.

Six day-1 design choices that survived Gemini review (now confirmed, not just my judgment): linear-ramp degradation, FAILED-still-emits, auto + manual transitions, seeded RNG, tick-order (advance → sample → transition-check), `.terraform.lock.hcl` left tracked.

## Trade-offs surfaced
- **Linear ramp vs P-F curve.** Kept linear; added a docstring paragraph acknowledging the simplification per Gemini #1.
- **RPM independent of degradation.** Day 1: stayed literal to PLAN.md. Day 2 (Gemini #3): coupled to degradation. Cascade: bearing temp is no longer monotonic in degradation (correct physics — failed pumps are stationary and so run cooler at the bearings even with accumulated wear). Vibration is now the clean "wear" signal.
- **24h HEALTHY dwell.** Recruiter-hostile out of the box. Day 2 (Gemini #5): deferred to the config-yaml session via a TODO in `DEFAULT_PROFILES`; the planned `demo_mode` shortcut will compress it to ~60 ticks.
- **Tier-3 context load.** Brief declared `_global` + `simulator`. Added `_interfaces.md` because `.step()`'s return shape *is* the simulator → scorer contract. Asked PO before loading.

## Mid-session scope addition — Gemini workflow v2 (day 1)

Mid-session the Gemini CLI hit four friction points in a row (install, PowerShell arg passing, trust folders, `update_topic` internal tool-call bug). PO agreed v2 trigger had fired (per `context/dev_workflow.md`). Added:
- `scripts/gemini_review.ps1` + `.sh` — direct-API wrappers, no CLI.
- `docs/adr/0001-direct-gemini-api-for-reviews.md`.
- `DEV_NORMS.md §4` rewrite + `context/dev_workflow.md` v2-changes log.

The script then took four debugging passes before it ran clean end-to-end. Captured here so the next session doesn't re-discover them — each is now defended in the script with a `# Why this matters` comment:
1. **`ConvertTo-Json -Depth 10` introspects nested `System.String`** as an object → Gemini 400 "Starting an object on a scalar field." Fix: `HttpUtility.JavaScriptStringEncode` the prompt, bypass `ConvertTo-Json`.
2. **`[System.IO.File]::WriteAllText` ignores `$PWD`** — uses `[Environment]::CurrentDirectory`. Fix: `Join-Path $PWD ...`.
3. **`ReadAllText` on PS 5.1 defaults to ANSI** (Windows-1252). Fix: pass `[System.Text.Encoding]::UTF8` explicitly.
4. **`Invoke-RestMethod -Body <string>` on PS 5.1 can re-encode as ASCII.** Fix: convert body to UTF-8 bytes before passing to `-Body`.

## Gemini review highlights (day 2)

Gemini engaged substantively on all 7 questions (no rubber-stamp). Dispositions in the packet's Resolution table; summary:
- **Pushed back on RPM** (#3) — said deviate from spec. Agreed and wrote ADR 0002. Highest-impact change of the session.
- **Confirmed tick ordering** (#2) — Moore-machine semantic was the right call.
- **Spotted a real test bug** (#4) — at-ceiling comparison can't catch a zeroed rate. Added a from-zero derivative test.
- **Spotted the dead instantiation** (#7) — confirmed it was a leftover, deleted.
- **Recommended a docstring tweak** (#1) — added a P-F-curve acknowledgment paragraph.
- **Deferred dwell-times** (#5) to the config-yaml session via TODO.
- **Confirmed no AWS-specificity** (#6) and surfaced a future payload-size cost note (carried forward to `_interfaces.md`).

## State at end of session
- Tests: **30 passing** locally (sandbox: `cp simulator /tmp && pytest` per [[ml-obs-pipeline-git-on-windows]]; Windows-side: `pytest simulator/tests/` from project root).
- Open follow-ups:
  - Bearing-temp non-monotonicity in degradation — carry into `context/model.md` for the model session (rolling-window std will be a better wear feature than raw bearing temp).
  - Payload-size warning (AWS IoT bills per 5KB) — carry into `context/_interfaces.md` and surface again in lambda_s3_batcher.
  - `PLAN.md.docx.bak-2026-05-25` exists in the repo root from the .docx edit; FUSE prevented sandbox-side deletion. Now ignored via `*.bak-*` in .gitignore, but Adar should delete it on Windows: `Remove-Item "PLAN.md.docx.bak-2026-05-25"`.
- `context/simulator.md` updated: yes.

## Commits to run (PO-side, per [[ml-obs-pipeline-git-on-windows]])

```powershell
cd "D:\Claude\ML Observability Pipeline"
```

**Commit 1 — `.gitignore` first (dev_workflow.md deferred-decision):**

```powershell
git add .gitignore
git commit -m "chore: add Python + Terraform .gitignore

First commit of the simulator session. Per context/dev_workflow.md
deferred-decisions, .gitignore lands before any code. Includes:
- Python + Terraform (.terraform.lock.hcl tracked per HashiCorp guidance)
- Project secrets (simulator/.secrets/)
- Dev QoL (.envrc, .python-version - added on day 2 from Gemini review)
- Local .docx backup pattern (*.bak-*)"
```

**Commit 2 — simulator code, tests, review packet (with Resolution filled), session log, simulator context:**

```powershell
git add simulator/ review_packets/2026-05-24-simulator-pump.md review_responses/2026-05-24-simulator-pump.md docs/sessions/2026-05-24-simulator-pump-model.md context/simulator.md
git commit -m "simulator: add Pump physical model + 4-state machine

Implements PLAN.md section 2.2 (with ADR 0002 RPM deviation) and the
HEALTHY -> DEGRADING -> FAILING -> FAILED lifecycle. Pump.step() returns
a telemetry dict matching context/_interfaces.md; 30 pytest tests cover
all four states, transitions, RNG reproducibility, derivative fairness,
and ISO-8601-ms timestamps. No MQTT, no asyncio, no YAML config yet -
separate sessions. Gemini review (see review_packets/) drove 7 changes:
RPM coupled to degradation (ADR 0002), P-F-curve docstring, from-zero
derivative test, dead-instantiation cleanup, dwell-time TODO, and two
gitignore additions."
```

**Commit 3 — Gemini workflow v2 (scripts + ADR 0001 + DEV_NORMS + dev_workflow context):**

```powershell
git add scripts/ docs/adr/0001-direct-gemini-api-for-reviews.md DEV_NORMS.md context/dev_workflow.md
git commit -m "dev_workflow: replace Gemini CLI with direct-API script (ADR 0001)

Four friction points in the simulator session's first review attempt
(install, PowerShell arg passing, trust folders, CLI internal tool-call
bug) tripped the v1->v2 trigger in context/dev_workflow.md. Replaces
'gemini -p ...' with scripts/gemini_review.{ps1,sh} that POST the
review packet to the REST API directly. The script took four further
debugging passes (ConvertTo-Json depth, .NET cwd vs PWD, UTF-8 read on
PS 5.1, IRM body-encoding on PS 5.1) - all documented inline in the
script and the ADR. DEV_NORMS section 4 rewritten; ADR 0001 records
the decision and alternatives."
```

**Commit 4 — ADR 0002 + PLAN.md update:**

```powershell
git add docs/adr/0002-rpm-coupled-to-degradation.md PLAN.md.docx
git commit -m "spec: couple RPM to degradation in pump model (ADR 0002)

Per Gemini review #3: PLAN.md section 2.2 had RPM independent of
degradation, leaving FAILED pumps emitting healthy 1800 RPM - physically
implausible. New equation: RPM = setpoint*(1-degradation) +
N(0, 5+15*degradation). At d=0 reduces to original spec; at d=1 the
pump is near-stationary with high stutter (sigma=20). PLAN.md section
2.2 updated in-place; ADR 0002 is the authoritative justification.
Consequence: bearing temp no longer monotonic in degradation (RPM
drops faster than the +15*d direct term rises) - this is physically
correct. Vibration becomes the clean wear signal."
git push
```

After push, no further Gemini round needed for this packet (Resolution table is closed).

## Note for next session
Pump model + Gemini loop fully closed. Three things to watch when picking up the next simulator session (config YAML loading is the natural next):
1. The `demo_mode` shortcut promised in the `DEFAULT_PROFILES` TODO — wire it through the YAML loader.
2. The `*.bak-*` gitignore entry was a workaround for the FUSE-blocked deletion of `PLAN.md.docx.bak-2026-05-25`. Confirm Adar deleted that file Windows-side before the next session opens.
3. ADR 0002's "follow-ups" mention carrying the bearing-temp non-monotonicity note into `context/model.md` when the model session opens. Don't forget — feature engineering shouldn't pre-suppose monotonicity.
